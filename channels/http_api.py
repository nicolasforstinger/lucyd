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
import base64
import hmac
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from aiohttp import web

from channels import Attachment

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

    _AUTH_EXEMPT_PATHS = frozenset({"/api/v1/status"})

    def __init__(
        self,
        queue: asyncio.Queue,
        host: str,
        port: int,
        auth_token: str,
        agent_timeout: float,
        get_status: Any = None,
        get_sessions: Any = None,
        get_cost: Any = None,
        download_dir: str = "/tmp/lucyd-http",  # noqa: S108 — default; overridden by config
        max_body_bytes: int = 10 * 1024 * 1024,
    ):
        self.queue = queue
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.agent_timeout = agent_timeout
        self._get_status = get_status
        self._get_sessions = get_sessions
        self._get_cost = get_cost
        self._download_dir = download_dir
        self._max_body_bytes = max_body_bytes
        self._runner: web.AppRunner | None = None
        self._rate_limiter = _RateLimiter(max_requests=30, window_seconds=60)
        self._status_rate_limiter = _RateLimiter(max_requests=60, window_seconds=60)

    # ─── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the HTTP server."""
        app = web.Application(
            middlewares=[self._auth_middleware, self._rate_middleware],
            client_max_size=self._max_body_bytes,
        )
        app.router.add_post("/api/v1/chat", self._handle_chat)
        app.router.add_post("/api/v1/notify", self._handle_notify)
        app.router.add_get("/api/v1/status", self._handle_status)
        app.router.add_get("/api/v1/sessions", self._handle_sessions)
        app.router.add_get("/api/v1/cost", self._handle_cost)

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
        # Health check endpoints are always open
        if request.path in self._AUTH_EXEMPT_PATHS:
            return await handler(request)

        # No token configured = service misconfigured, deny all protected endpoints
        if not self.auth_token:
            return web.json_response(
                {"error": "No auth token configured"}, status=503,
            )

        # Validate bearer token
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
        if request.path in ("/api/v1/status", "/api/v1/sessions", "/api/v1/cost"):
            limiter = self._status_rate_limiter
        else:
            limiter = self._rate_limiter
        if not limiter.check(client_ip):
            return web.json_response(
                {"error": "rate limit exceeded"}, status=429,
            )
        return await handler(request)

    # ─── Attachment Decoding ─────────────────────────────────────

    def _decode_attachments(self, raw: list[dict]) -> list[Attachment]:
        """Decode base64 attachments from HTTP body, save to disk.

        Each item must have 'content_type' and 'data' (base64-encoded).
        Optional 'filename' for the original name.
        Returns list of Attachment objects with local paths.
        """
        dl_dir = Path(self._download_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)

        attachments = []
        for item in raw:
            content_type = item.get("content_type", "")
            data_b64 = item.get("data", "")
            if not content_type or not data_b64:
                continue

            data = base64.b64decode(data_b64)
            filename = item.get("filename", "attachment")
            ts = int(time.time() * 1000)
            safe_name = f"{ts}_{filename}"
            local_path = dl_dir / safe_name
            local_path.write_bytes(data)

            attachments.append(Attachment(
                content_type=content_type,
                local_path=str(local_path),
                filename=filename,
                size=len(data),
            ))
            log.debug("HTTP attachment saved: %s (%d bytes)", local_path, len(data))

        return attachments

    # ─── Endpoints ────────────────────────────────────────────────

    async def _handle_chat(self, request: web.Request) -> web.Response:
        """POST /api/v1/chat — synchronous message + response."""
        try:
            body = await request.json()
        except web.HTTPException:
            raise
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

        # Decode attachments if present
        attachments = None
        raw_attachments = body.get("attachments")
        if raw_attachments and isinstance(raw_attachments, list):
            attachments = self._decode_attachments(raw_attachments) or None

        queue_item = {
            "sender": sender,
            "type": "http",
            "text": text,
            "tier": tier,
            "response_future": future,
        }
        if attachments:
            queue_item["attachments"] = attachments

        await self.queue.put(queue_item)

        log.info("HTTP /chat queued: sender=%s context=%s attachments=%d",
                 sender, context, len(attachments) if attachments else 0)

        try:
            result = await asyncio.wait_for(future, timeout=self.agent_timeout)
            return web.json_response(result, status=200)
        except TimeoutError:
            log.error("HTTP /chat timeout for sender=%s", sender)
            return web.json_response(
                {"error": "processing timeout"}, status=408,
            )

    async def _handle_notify(self, request: web.Request) -> web.Response:
        """POST /api/v1/notify — fire-and-forget notification.

        Body: message (required), source/ref/data (optional metadata).
        All /notify uses operational tier and system type.
        """
        try:
            body = await request.json()
        except web.HTTPException:
            raise
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
        source_label = body.get("source", "")
        ref = body.get("ref", "")
        data = body.get("data")

        # Build LLM text with optional prefix brackets
        parts = []
        if source_label:
            parts.append(f"[source: {source_label}]")
        if ref:
            parts.append(f"[ref: {ref}]")
        parts.append(message)
        text = " ".join(parts)

        # Metadata for webhook echo-back
        notify_meta = {}
        if source_label:
            notify_meta["source"] = source_label
        if ref:
            notify_meta["ref"] = ref
        if data is not None:
            notify_meta["data"] = data

        # Decode attachments if present
        attachments = None
        raw_attachments = body.get("attachments")
        if raw_attachments and isinstance(raw_attachments, list):
            attachments = self._decode_attachments(raw_attachments) or None

        queue_item = {
            "sender": sender,
            "type": "system",
            "text": f"[AUTOMATED SYSTEM MESSAGE] {text}",
            "tier": "operational",
            "notify_meta": notify_meta or None,
        }
        if attachments:
            queue_item["attachments"] = attachments

        await self.queue.put(queue_item)

        log.info("HTTP /notify queued: sender=%s source=%s ref=%s attachments=%d",
                 sender, source_label, ref, len(attachments) if attachments else 0)

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

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions — list active sessions."""
        if self._get_sessions:
            sessions = self._get_sessions()
        else:
            sessions = []

        return web.json_response({"sessions": sessions}, status=200)

    async def _handle_cost(self, request: web.Request) -> web.Response:
        """GET /api/v1/cost — query cost by period."""
        period = request.query.get("period", "today")
        if period not in ("today", "week", "all"):
            return web.json_response(
                {"error": "period must be 'today', 'week', or 'all'"}, status=400,
            )

        if self._get_cost:
            cost_data = self._get_cost(period)
        else:
            cost_data = {"period": period, "total_cost": 0.0, "models": []}

        return web.json_response(cost_data, status=200)
