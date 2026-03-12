"""HTTP API server for Lucyd daemon.

Provides REST endpoints for external integrations (n8n, scripts, monitoring).
Runs alongside Telegram/CLI channel — not a replacement, a parallel input source.

Endpoints:
    POST /api/v1/chat    — Synchronous: send message, await response
    POST /api/v1/notify  — Fire-and-forget: queue event, return immediately
    GET  /api/v1/status   — Health check + daemon stats
    POST /api/v1/evolve  — Trigger memory evolution (rewrite understanding files)
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
    def __init__(self, max_requests: int, window_seconds: int,
                 cleanup_threshold: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self.cleanup_threshold = cleanup_threshold
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        # Periodic sweep: evict stale keys when dict grows large
        if len(self._hits) > self.cleanup_threshold:
            stale = [k for k, v in self._hits.items()
                     if not v or now - v[-1] >= self.window]
            for k in stale:
                del self._hits[k]
        hits = self._hits[key]
        self._hits[key] = [t for t in hits if now - t < self.window]
        if len(self._hits[key]) >= self.max_requests:
            return False
        self._hits[key].append(now)
        return True


class HTTPApi:
    """HTTP API server that feeds messages into the daemon's queue."""

    _AUTH_EXEMPT_PATHS = frozenset({"/api/v1/status"})
    _READ_ONLY_PATHS = frozenset({
        "/api/v1/status",
        "/api/v1/sessions",
        "/api/v1/cost",
        "/api/v1/monitor",
    })

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
        get_monitor: Any = None,
        handle_reset: Any = None,
        get_history: Any = None,
        handle_evolve: Any = None,
        *,
        download_dir: str,
        max_body_bytes: int,
        rate_limit: int,
        rate_window: int,
        status_rate_limit: int,
        rate_cleanup_threshold: int,
        agent_name: str = "",
        control_queue: asyncio.Queue | None = None,
    ):
        self.queue = queue
        self._control_queue = control_queue or queue
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.agent_timeout = agent_timeout
        self.agent_name = agent_name
        self._get_status = get_status
        self._get_sessions = get_sessions
        self._get_cost = get_cost
        self._get_monitor = get_monitor
        self._handle_reset_cb = handle_reset
        self._get_history = get_history
        self._handle_evolve_cb = handle_evolve
        self._download_dir = download_dir
        self._max_body_bytes = max_body_bytes
        self._runner: web.AppRunner | None = None
        self._rate_limiter = _RateLimiter(max_requests=rate_limit, window_seconds=rate_window,
                                          cleanup_threshold=rate_cleanup_threshold)
        self._status_rate_limiter = _RateLimiter(max_requests=status_rate_limit, window_seconds=rate_window,
                                                  cleanup_threshold=rate_cleanup_threshold)

    # ─── Response Helper ─────────────────────────────────────────

    def _json_response(self, data: dict, status: int = 200) -> web.Response:
        """Wrap web.json_response with agent identity injection."""
        if self.agent_name:
            data["agent"] = self.agent_name
        resp = web.json_response(data, status=status)
        if self.agent_name:
            resp.headers["X-Lucyd-Agent"] = self.agent_name
        return resp

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
        app.router.add_get("/api/v1/monitor", self._handle_monitor)
        app.router.add_post("/api/v1/sessions/reset", self._handle_reset)
        app.router.add_get(
            "/api/v1/sessions/{session_id}/history", self._handle_history,
        )
        app.router.add_post("/api/v1/evolve", self._handle_evolve)
        app.router.add_post("/api/v1/compact", self._handle_compact)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("HTTP API listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._runner:
            await self._runner.cleanup()
        # Clean transient download files
        dl_dir = Path(self._download_dir)
        if dl_dir.exists():
            for f in dl_dir.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                except OSError:
                    pass
        log.info("HTTP API stopped")

    # ─── Auth Middleware ──────────────────────────────────────────

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Health check endpoints are always open
        if request.path in self._AUTH_EXEMPT_PATHS:
            return await handler(request)

        # No token configured = service misconfigured, deny all protected endpoints
        if not self.auth_token:
            return self._json_response(
                {"error": "No auth token configured"}, status=503,
            )

        # Validate bearer token
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], self.auth_token):
            log.warning("HTTP API: auth failed from %s %s",
                        request.remote, request.path)
            return self._json_response(
                {"error": "unauthorized"}, status=401,
            )
        return await handler(request)

    # ─── Rate Limit Middleware ────────────────────────────────────

    @web.middleware
    async def _rate_middleware(self, request: web.Request, handler):
        client_ip = request.remote or "unknown"
        if request.path in self._READ_ONLY_PATHS or (
            request.path.startswith("/api/v1/sessions/")
            and request.method == "GET"
        ):
            limiter = self._status_rate_limiter
        else:
            limiter = self._rate_limiter
        if not limiter.check(client_ip):
            return self._json_response(
                {"error": "rate limit exceeded"}, status=429,
            )
        return await handler(request)

    # ─── Attachment Decoding ─────────────────────────────────────

    def _extract_attachments(self, body: dict) -> list[Attachment] | None:
        """Extract and decode attachments from an HTTP request body."""
        raw = body.get("attachments")
        if raw and isinstance(raw, list):
            return self._decode_attachments(raw) or None
        return None

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
            safe_name = f"{ts}_{Path(filename).name}"
            local_path = dl_dir / safe_name
            local_path.write_bytes(data)

            attachments.append(Attachment(
                content_type=content_type,
                local_path=str(local_path),
                filename=Path(filename).name,
                size=len(data),
                is_voice=bool(item.get("is_voice", False)),
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
        except (json.JSONDecodeError, ValueError):
            return self._json_response(
                {"error": "invalid JSON body"}, status=400,
            )

        message = body.get("message", "").strip()
        if not message:
            return self._json_response(
                {"error": "\"message\" field is required"}, status=400,
            )

        sender = f"http-{body.get('sender', 'default')}"
        context = body.get("context", "")

        # Prepend context label if provided
        text = f"[{context}] {message}" if context else message

        # Create Future for response capture
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        attachments = self._extract_attachments(body)

        queue_item = {
            "sender": sender,
            "type": "http",
            "text": text,
            "response_future": future,
        }
        if attachments:
            queue_item["attachments"] = attachments

        await self.queue.put(queue_item)

        log.info("HTTP /chat queued: sender=%s context=%s attachments=%d",
                 sender, context, len(attachments) if attachments else 0)

        try:
            result = await asyncio.wait_for(future, timeout=self.agent_timeout)
            return self._json_response(result, status=200)
        except TimeoutError:
            log.error("HTTP /chat timeout for sender=%s", sender)
            return self._json_response(
                {"error": "processing timeout"}, status=408,
            )

    async def _handle_notify(self, request: web.Request) -> web.Response:
        """POST /api/v1/notify — fire-and-forget notification.

        Body: message (required), source/ref/data (optional metadata).
        All /notify uses system type.
        """
        try:
            body = await request.json()
        except web.HTTPException:
            raise
        except (json.JSONDecodeError, ValueError):
            return self._json_response(
                {"error": "invalid JSON body"}, status=400,
            )

        message = body.get("message", "").strip()
        if not message:
            return self._json_response(
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

        attachments = self._extract_attachments(body)

        queue_item = {
            "sender": sender,
            "type": "system",
            "text": f"[AUTOMATED SYSTEM MESSAGE] {text}",
            "notify_meta": notify_meta or None,
            "notify": True,
        }
        if attachments:
            queue_item["attachments"] = attachments

        await self.queue.put(queue_item)

        log.info("HTTP /notify queued: sender=%s source=%s ref=%s attachments=%d",
                 sender, source_label, ref, len(attachments) if attachments else 0)

        return self._json_response(
            {"accepted": True, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            status=202,
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /api/v1/status — health check + stats."""
        status = self._get_status() if self._get_status else {"status": "ok"}

        return self._json_response(status, status=200)

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions — list active sessions."""
        sessions = self._get_sessions() if self._get_sessions else []

        return self._json_response({"sessions": sessions}, status=200)

    async def _handle_cost(self, request: web.Request) -> web.Response:
        """GET /api/v1/cost — query cost by period."""
        period = request.query.get("period", "today")
        if period not in ("today", "week", "all"):
            return self._json_response(
                {"error": "period must be 'today', 'week', or 'all'"}, status=400,
            )

        cost_data = self._get_cost(period) if self._get_cost else {"period": period, "total_cost": 0.0, "models": []}

        return self._json_response(cost_data, status=200)

    async def _handle_monitor(self, request: web.Request) -> web.Response:
        """GET /api/v1/monitor — live agentic loop state."""
        monitor_data = self._get_monitor() if self._get_monitor else {"state": "unknown"}

        return self._json_response(monitor_data, status=200)

    async def _handle_reset(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/reset — reset sessions.

        Routes the reset through the message queue so it serializes
        with message processing (no race with _process_message).
        """
        try:
            body = await request.json()
        except web.HTTPException:
            raise
        except (json.JSONDecodeError, ValueError):
            return self._json_response(
                {"error": "invalid JSON body"}, status=400,
            )

        target = body.get("target", "all")
        if not target or not isinstance(target, str):
            return self._json_response(
                {"error": "\"target\" must be a non-empty string"}, status=400,
            )

        # Route through control queue (priority over messages)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        reset_msg: dict[str, Any] = {
            "type": "reset",
            "sender": target,
            "response_future": future,
        }
        if target == "all":
            reset_msg["all"] = True
        await self._control_queue.put(reset_msg)

        try:
            result = await asyncio.wait_for(future, timeout=self.agent_timeout)
        except TimeoutError:
            return self._json_response(
                {"error": "reset timed out"}, status=408,
            )

        return self._json_response(result, status=200)

    async def _handle_history(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}/history — session transcript."""
        session_id = request.match_info["session_id"]
        full = request.query.get("full", "").lower() in ("true", "1", "yes")

        if self._get_history:
            history_data = self._get_history(session_id, full)
        else:
            history_data = {"session_id": session_id, "events": []}

        return self._json_response(history_data, status=200)

    async def _handle_evolve(self, request: web.Request) -> web.Response:
        """POST /api/v1/evolve — queue self-driven evolution."""
        if not self._handle_evolve_cb:
            return self._json_response(
                {"error": "evolution not available"}, status=503,
            )

        try:
            result = await self._handle_evolve_cb()
        except Exception:
            log.exception("Evolution endpoint failed")
            return self._json_response(
                {"error": "internal error"}, status=500,
            )

        return self._json_response(result, status=202)

    async def _handle_compact(self, request: web.Request) -> web.Response:
        """POST /api/v1/compact — force diary write + compaction.

        Routes through the message queue to serialize with message processing
        (same path as FIFO compact).
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        await self.queue.put({
            "type": "compact",
            "response_future": future,
        })

        try:
            result = await asyncio.wait_for(future, timeout=self.agent_timeout)
        except TimeoutError:
            return self._json_response(
                {"error": "compact timed out"}, status=408,
            )

        status = 200 if result.get("status") == "completed" else 202
        return self._json_response(result, status=status)
