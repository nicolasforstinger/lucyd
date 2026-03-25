"""HTTP API server — the daemon's single inbound/outbound interface.

All messages enter via HTTP. Channel bridges, lucydctl, n8n, and scripts
all talk to these endpoints. The API is always on.

Endpoints:
    POST /api/v1/chat         — Synchronous: send message, await response
    POST /api/v1/message      — Fire-and-forget: queue user message, return 202
    POST /api/v1/system       — Fire-and-forget: queue system event, return 202
    POST /api/v1/notify       — Fire-and-forget: queue notification event, return 202
    GET  /api/v1/status       — Health check + daemon stats
    POST /api/v1/evolve       — Trigger memory evolution (rewrite understanding files)
    POST /api/v1/index        — Run workspace indexing
    GET  /api/v1/index/status — Workspace index status
    POST /api/v1/consolidate  — Run memory consolidation (extract facts from workspace files)
    POST /api/v1/maintain     — Run memory maintenance
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

from models import Attachment
from log_utils import _log_safe

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
        "/api/v1/index/status",
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
        get_monitor: Any = None,
        get_history: Any = None,
        handle_evolve: Any = None,
        handle_index: Any = None,
        handle_index_status: Any = None,
        handle_consolidate: Any = None,
        handle_maintain: Any = None,
        metering_db: Any = None,
        *,
        download_dir: str,
        max_body_bytes: int,
        max_attachment_bytes: int = 0,
        rate_limit: int,
        rate_window: int,
        status_rate_limit: int,
        rate_cleanup_threshold: int,
        agent_name: str = "",
        control_queue: asyncio.Queue | None = None,
        webhook_secret: str = "",
    ):
        self.queue = queue
        self._control_queue = control_queue or queue
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.agent_timeout = agent_timeout
        self.agent_name = agent_name
        self._webhook_secret = webhook_secret
        self._get_status = get_status
        self._get_sessions = get_sessions
        self._get_monitor = get_monitor
        self._get_history = get_history
        self._handle_evolve_cb = handle_evolve
        self._handle_index_cb = handle_index
        self._handle_index_status_cb = handle_index_status
        self._handle_consolidate_cb = handle_consolidate
        self._handle_maintain_cb = handle_maintain
        self._metering_db = metering_db
        self._download_dir = download_dir
        self._max_body_bytes = max_body_bytes
        self._max_attachment_bytes = max_attachment_bytes
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
            middlewares=[self._auth_middleware, self._webhook_middleware, self._rate_middleware],
            client_max_size=self._max_body_bytes,
        )
        app.router.add_post("/api/v1/chat", self._handle_chat)
        app.router.add_post("/api/v1/chat/stream", self._handle_chat_stream)
        app.router.add_post("/api/v1/message", self._handle_message)
        app.router.add_post("/api/v1/system", self._handle_system)
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
        app.router.add_post("/api/v1/index", self._handle_index)
        app.router.add_get("/api/v1/index/status", self._handle_index_status)
        app.router.add_post("/api/v1/consolidate", self._handle_consolidate)
        app.router.add_post("/api/v1/maintain", self._handle_maintain)

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

        # Localhost is trusted (agent's own environment — at jobs, lucydctl, etc.)
        remote = request.remote or ""
        if remote in ("127.0.0.1", "::1"):
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

    # ─── Webhook Signature Middleware ─────────────────────────────

    _WEBHOOK_PATHS = frozenset({
        "/api/v1/message",
        "/api/v1/notify",
    })

    @web.middleware
    async def _webhook_middleware(self, request: web.Request, handler):
        """Verify HMAC-SHA256 webhook signature for inbound webhook endpoints."""
        if (
            self._webhook_secret
            and request.method == "POST"
            and request.path in self._WEBHOOK_PATHS
        ):
            sig_header = request.headers.get("X-Webhook-Signature", "")
            body = await request.read()
            expected = hmac.new(
                self._webhook_secret.encode(), body, "sha256",
            ).hexdigest()
            if not sig_header or not hmac.compare_digest(sig_header, expected):
                log.warning("Webhook signature mismatch from %s %s",
                            request.remote, request.path)
                return self._json_response(
                    {"error": "invalid webhook signature"}, status=401,
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
        Raises web.HTTPBadRequest on malformed base64.
        Skips attachments exceeding max_attachment_bytes.
        """
        dl_dir = Path(self._download_dir)
        dl_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        attachments = []
        for item in raw:
            content_type = item.get("content_type", "")
            data_b64 = item.get("data", "")
            if not content_type or not data_b64:
                continue

            try:
                data = base64.b64decode(data_b64)
            except Exception:
                raise web.HTTPBadRequest(
                    text='{"error": "invalid base64 in attachment"}',
                    content_type="application/json",
                ) from None  # noqa: B904 — intentional: hide decode internals from client

            if self._max_attachment_bytes and len(data) > self._max_attachment_bytes:
                log.warning("HTTP attachment rejected: %d bytes > limit %d",
                            len(data), self._max_attachment_bytes)
                continue

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

    # ─── Shared Parsing ────────────────────────────────────────────

    async def _parse_and_queue(
        self,
        request: web.Request,
        msg_type: str,
        sender_default: str = "default",
        text_prefix: str = "",
        extra_fields: dict | None = None,
    ) -> web.Response:
        """Shared parse body -> validate -> build queue item -> queue -> 202 response.

        Used by _handle_message, _handle_system, _handle_notify.
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

        sender = f"http-{body.get('sender', sender_default)}"
        text = f"{text_prefix}{message}" if text_prefix else message

        attachments = self._extract_attachments(body)

        queue_item: dict[str, Any] = {
            "sender": sender,
            "type": msg_type,
            "text": text,
        }
        if attachments:
            queue_item["attachments"] = attachments
        if extra_fields:
            queue_item.update(extra_fields)

        await self.queue.put(queue_item)

        log.info("HTTP /%s queued: sender=%s attachments=%d",
                 msg_type, _log_safe(sender),
                 len(attachments) if attachments else 0)

        return self._json_response(
            {"accepted": True, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            status=202,
        )

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
                 _log_safe(sender), _log_safe(context),
                 len(attachments) if attachments else 0)

        try:
            result = await asyncio.wait_for(future, timeout=self.agent_timeout)
            return self._json_response(result, status=200)
        except TimeoutError:
            log.error("HTTP /chat timeout for sender=%s", _log_safe(sender))
            return self._json_response(
                {"error": "processing timeout"}, status=408,
            )

    async def _handle_chat_stream(self, request: web.Request) -> web.StreamResponse:
        """POST /api/v1/chat/stream — SSE: send message, stream response tokens."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._json_response({"error": "invalid JSON body"}, status=400)

        message = body.get("message", "").strip()
        if not message:
            return self._json_response({"error": "\"message\" field is required"}, status=400)

        sender = f"http-{body.get('sender', 'default')}"
        context = body.get("context", "")
        text = f"[{context}] {message}" if context else message

        # SSE response
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        # Stream delta queue — daemon pushes deltas, we send as SSE
        delta_queue: asyncio.Queue = asyncio.Queue()

        attachments = self._extract_attachments(body)
        queue_item: dict[str, Any] = {
            "sender": sender,
            "type": "http",
            "text": text,
            "response_future": future,
            "stream_queue": delta_queue,
        }
        if attachments:
            queue_item["attachments"] = attachments

        await self.queue.put(queue_item)

        got_done = False
        try:
            # Stream deltas as SSE events until done
            while True:
                try:
                    event = await asyncio.wait_for(delta_queue.get(), timeout=self.agent_timeout)
                except TimeoutError:
                    await resp.write(b"event: error\ndata: {\"error\": \"timeout\"}\n\n")
                    got_done = True
                    break
                if event is None:
                    break  # sentinel
                # Route error events to SSE error channel
                if event.get("error"):
                    event_data = json.dumps(event, ensure_ascii=False)
                    await resp.write(f"event: error\ndata: {event_data}\n\n".encode())
                    got_done = True
                    break
                event_data = json.dumps(event, ensure_ascii=False)
                await resp.write(f"data: {event_data}\n\n".encode())
                if event.get("done"):
                    got_done = True
                    break
            # Guarantee a terminal event — covers sentinel-only (no deltas)
            if not got_done:
                await resp.write(b"data: {\"done\": true, \"stop_reason\": \"end_turn\"}\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            try:
                await resp.write_eof()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass  # client already disconnected

        return resp

    async def _handle_message(self, request: web.Request) -> web.Response:
        """POST /api/v1/message — fire-and-forget user message.

        Body: message (required), sender (optional, default "http-default").
        Queues a user message without waiting for the agent's response.
        CLI equivalent: lucydctl --message "text"
        """
        return await self._parse_and_queue(request, msg_type="user")

    async def _handle_system(self, request: web.Request) -> web.Response:
        """POST /api/v1/system — fire-and-forget system event.

        Body: message (required), sender (optional, default "http-system").
        Queues a system event without waiting for the agent's response.
        CLI equivalent: lucydctl --system "text"
        """
        return await self._parse_and_queue(
            request, msg_type="system",
            sender_default="system",
            text_prefix="[AUTOMATED SYSTEM MESSAGE] ",
        )

    async def _handle_notify(self, request: web.Request) -> web.Response:
        """POST /api/v1/notify — fire-and-forget notification.

        Body: message (required), source/ref/data (optional metadata).
        All /notify uses system type. Has extra metadata fields so it
        doesn't fully reduce to _parse_and_queue.
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
        notify_meta: dict = {}
        if source_label:
            notify_meta["source"] = source_label
        if ref:
            notify_meta["ref"] = ref
        if data is not None:
            notify_meta["data"] = data

        attachments = self._extract_attachments(body)

        queue_item: dict[str, Any] = {
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
                 _log_safe(sender), _log_safe(source_label), _log_safe(ref),
                 len(attachments) if attachments else 0)

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
        """GET /api/v1/cost — raw cost records for a billing period (YYYY-MM)."""
        import time as _time
        period = request.query.get("period", _time.strftime("%Y-%m"))
        if not self._metering_db:
            return self._json_response({"error": "metering not available"}, status=400)
        return self._json_response(self._metering_db.get_records(period))

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
        """POST /api/v1/evolve — queue self-driven evolution.

        Body (optional): {"force": true} to skip pre-check for new logs.
        """
        if not self._handle_evolve_cb:
            return self._json_response(
                {"error": "evolution not available"}, status=503,
            )

        force = False
        if request.body_exists:
            try:
                body = await request.json()
                force = bool(body.get("force", False))
            except (json.JSONDecodeError, ValueError):
                pass  # No body or invalid JSON — default force=False

        try:
            result = await self._handle_evolve_cb(force=force)
        except Exception:
            log.exception("Evolution endpoint failed")
            return self._json_response(
                {"error": "internal error"}, status=500,
            )

        status = 200 if result.get("status") == "skipped" else 202
        return self._json_response(result, status=status)

    async def _handle_compact(self, request: web.Request) -> web.Response:
        """POST /api/v1/compact — force diary write + compaction.

        Routes through the message queue to serialize with message processing.
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

    async def _handle_index(self, request: web.Request) -> web.Response:
        """POST /api/v1/index — run workspace indexing."""
        if not self._handle_index_cb:
            return self._json_response(
                {"error": "indexing not available"}, status=503,
            )

        full = False
        if request.body_exists:
            try:
                body = await request.json()
                full = bool(body.get("full", False))
            except (json.JSONDecodeError, ValueError):
                pass

        try:
            result = await self._handle_index_cb(full=full)
        except Exception:
            log.exception("Index endpoint failed")
            return self._json_response(
                {"error": "internal error"}, status=500,
            )

        return self._json_response(result, status=200)

    async def _handle_index_status(self, request: web.Request) -> web.Response:
        """GET /api/v1/index/status — workspace index status."""
        if not self._handle_index_status_cb:
            return self._json_response(
                {"error": "index status not available"}, status=503,
            )

        try:
            result = self._handle_index_status_cb()
        except Exception:
            log.exception("Index status endpoint failed")
            return self._json_response(
                {"error": "internal error"}, status=500,
            )

        return self._json_response(result, status=200)

    async def _handle_consolidate(self, request: web.Request) -> web.Response:
        """POST /api/v1/consolidate — run memory consolidation."""
        if not self._handle_consolidate_cb:
            return self._json_response(
                {"error": "consolidation not available"}, status=503,
            )

        try:
            result = await self._handle_consolidate_cb()
        except Exception:
            log.exception("Consolidate endpoint failed")
            return self._json_response(
                {"error": "internal error"}, status=500,
            )

        return self._json_response(result, status=200)

    async def _handle_maintain(self, request: web.Request) -> web.Response:
        """POST /api/v1/maintain — run memory maintenance."""
        if not self._handle_maintain_cb:
            return self._json_response(
                {"error": "maintenance not available"}, status=503,
            )

        try:
            result = await self._handle_maintain_cb()
        except Exception:
            log.exception("Maintain endpoint failed")
            return self._json_response(
                {"error": "internal error"}, status=500,
            )

        return self._json_response(result, status=200)
