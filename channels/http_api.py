"""HTTP API server for Lucyd daemon.

Provides REST endpoints for external integrations (n8n, scripts, monitoring).
Runs alongside Telegram/CLI channel — not a replacement, a parallel input source.

Endpoints:
    POST /api/v1/chat    — Synchronous: send message, await response
    POST /api/v1/notify  — Fire-and-forget: queue event, return immediately
    GET  /api/v1/status   — Health check + daemon stats
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import defaultdict
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


class _RateLimiter:
    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        self._hits[key] = [t for t in hits if now - t < self.window]
        if len(self._hits[key]) >= self.max_requests:
            return False
        self._hits[key].append(now)
        return True


class HTTPApi:
    """HTTP API server that feeds messages into the daemon's queue."""

    def __init__(
        self,
        queue: asyncio.Queue,
        host: str,
        port: int,
        auth_token: str,
        agent_timeout: float,
        get_status: Any = None,
    ):
        self.queue = queue
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.agent_timeout = agent_timeout
        self._get_status = get_status
        self._runner: web.AppRunner | None = None
        self._rate_limiter = _RateLimiter(max_requests=30, window_seconds=60)
        self._status_rate_limiter = _RateLimiter(max_requests=60, window_seconds=60)

    # ─── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the HTTP server."""
        app = web.Application(middlewares=[self._auth_middleware, self._rate_middleware])
        app.router.add_post("/api/v1/chat", self._handle_chat)
        app.router.add_post("/api/v1/notify", self._handle_notify)
        app.router.add_get("/api/v1/status", self._handle_status)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("HTTP API listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._runner:
            await self._runner.cleanup()
            log.info("HTTP API stopped")

    # ─── Auth Middleware ──────────────────────────────────────────

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Status endpoint with no token configured = open (for health checks)
        if not self.auth_token:
            return await handler(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], self.auth_token):
            log.warning("HTTP API: auth failed from %s %s",
                        request.remote, request.path)
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
        return await handler(request)

    # ─── Rate Limit Middleware ────────────────────────────────────

    @web.middleware
    async def _rate_middleware(self, request: web.Request, handler):
        client_ip = request.remote or "unknown"
        if request.path == "/api/v1/status":
            limiter = self._status_rate_limiter
        else:
            limiter = self._rate_limiter
        if not limiter.check(client_ip):
            return web.json_response(
                {"error": "rate limit exceeded"}, status=429,
            )
        return await handler(request)

    # ─── Endpoints ────────────────────────────────────────────────

    async def _handle_chat(self, request: web.Request) -> web.Response:
        """POST /api/v1/chat — synchronous message + response."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": "invalid JSON body"}, status=400,
            )

        message = body.get("message", "").strip()
        if not message:
            return web.json_response(
                {"error": "\"message\" field is required"}, status=400,
            )

        sender = f"http-{body.get('sender', 'default')}"
        context = body.get("context", "")
        tier = body.get("tier", "full")

        # Prepend context label if provided
        text = f"[{context}] {message}" if context else message

        # Create Future for response capture
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        await self.queue.put({
            "sender": sender,
            "type": "http",
            "text": text,
            "tier": tier,
            "response_future": future,
        })

        log.info("HTTP /chat queued: sender=%s context=%s", sender, context)

        try:
            result = await asyncio.wait_for(future, timeout=self.agent_timeout)
            return web.json_response(result, status=200)
        except TimeoutError:
            log.error("HTTP /chat timeout for sender=%s", sender)
            return web.json_response(
                {"error": "processing timeout"}, status=408,
            )

    async def _handle_notify(self, request: web.Request) -> web.Response:
        """POST /api/v1/notify — fire-and-forget event."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": "invalid JSON body"}, status=400,
            )

        event = body.get("event", "").strip()
        if not event:
            return web.json_response(
                {"error": "\"event\" field is required"}, status=400,
            )

        data = body.get("data", {})
        sender = f"http-{body.get('sender', 'default')}"
        priority = body.get("priority", "normal")

        # Format as structured text for the LLM
        text = f"[{event}]: {json.dumps(data, ensure_ascii=False)}"

        # Priority determines tier: urgent = full (Opus via routing), normal = operational (Haiku)
        tier = "full" if priority == "urgent" else "operational"

        await self.queue.put({
            "sender": sender,
            "type": "system",
            "text": f"[AUTOMATED SYSTEM MESSAGE] {text}",
            "tier": tier,
        })

        log.info("HTTP /notify queued: event=%s sender=%s priority=%s",
                 event, sender, priority)

        return web.json_response(
            {"accepted": True, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            status=202,
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /api/v1/status — health check + stats."""
        if self._get_status:
            status = self._get_status()
        else:
            status = {"status": "ok"}

        return web.json_response(status, status=200)
