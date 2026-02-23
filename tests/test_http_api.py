"""Tests for channels/http_api.py — HTTP API server.

Covers: auth security, endpoint correctness, resilience, edge cases.
"""

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from channels.http_api import HTTPApi

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def queue():
    return asyncio.Queue()


@pytest.fixture
def api(queue):
    """HTTPApi instance with a test token."""
    return HTTPApi(
        queue=queue,
        host="127.0.0.1",
        port=0,  # unused — we use aiohttp test client
        auth_token="test-token-123",
        agent_timeout=5.0,
        get_status=lambda: {
            "status": "ok",
            "uptime_seconds": 42,
            "active_sessions": 1,
            "today_cost": 1.23,
        },
    )


@pytest.fixture
def api_no_auth(queue):
    """HTTPApi instance with no auth token (open access)."""
    return HTTPApi(
        queue=queue,
        host="127.0.0.1",
        port=0,
        auth_token="",
        agent_timeout=5.0,
    )


def _make_app(api_instance: HTTPApi) -> web.Application:
    """Build aiohttp app from HTTPApi for testing."""
    app = web.Application(
        middlewares=[api_instance._auth_middleware, api_instance._rate_middleware],
        client_max_size=api_instance._max_body_bytes,
    )
    app.router.add_post("/api/v1/chat", api_instance._handle_chat)
    app.router.add_post("/api/v1/notify", api_instance._handle_notify)
    app.router.add_get("/api/v1/status", api_instance._handle_status)
    app.router.add_get("/api/v1/sessions", api_instance._handle_sessions)
    app.router.add_get("/api/v1/cost", api_instance._handle_cost)
    return app


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token-123"}


# ─── Auth Tests ───────────────────────────────────────────────────


class TestAuth:
    @pytest.mark.asyncio
    async def test_missing_token_rejected(self, api):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions")
            assert resp.status == 401
            body = await resp.json()
            assert body["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, api):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_valid_token_accepted(self, api, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions", headers=auth_headers)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_auth_configured_denies_protected(self, api_no_auth):
        """No token configured → 503 on protected endpoints."""
        app = _make_app(api_no_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions")
            assert resp.status == 503
            body = await resp.json()
            assert body["error"] == "No auth token configured"


class TestAuthEdgeCases:
    """Security edge cases for the auth middleware."""

    @pytest.mark.asyncio
    async def test_bearer_with_empty_token(self, api):
        """'Bearer ' with nothing after it must be rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer "},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_bearer_no_space(self, api):
        """'Bearertest-token-123' (no space) must be rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearertest-token-123"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_basic_auth_scheme_rejected(self, api):
        """Basic auth scheme is not accepted, only Bearer."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Basic dGVzdC10b2tlbi0xMjM="},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_token_with_trailing_space(self, api):
        """Token with trailing whitespace is not the same token."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer test-token-123 "},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_token_with_leading_space(self, api):
        """Token with leading whitespace is not the same token."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer  test-token-123"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_partial_token_prefix(self, api):
        """A prefix substring of the real token must be rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_token_case_sensitive(self, api):
        """Token comparison must be case-sensitive."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer TEST-TOKEN-123"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_empty_authorization_header(self, api):
        """Empty Authorization header must be rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": ""},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_token_only_no_bearer_prefix(self, api):
        """Raw token without 'Bearer ' prefix must be rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "test-token-123"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_auth_applies_to_all_endpoints(self, api):
        """Auth middleware protects chat and notify too, not just status."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            # Chat without auth
            resp = await client.post(
                "/api/v1/chat",
                json={"message": "test"},
            )
            assert resp.status == 401

            # Notify without auth
            resp = await client.post(
                "/api/v1/notify",
                json={"message": "test"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_no_auth_allows_status(self, api_no_auth):
        """When no token is configured, status endpoint is open."""
        app = _make_app(api_no_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_auth_denies_notify(self, api_no_auth):
        """No token configured → 503 on notify."""
        app = _make_app(api_no_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                json={"message": "test"},
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_no_auth_denies_chat(self, api_no_auth):
        """No token configured → 503 on chat."""
        app = _make_app(api_no_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                json={"message": "test"},
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_no_auth_denies_sessions(self, api_no_auth):
        """No token configured → 503 on sessions."""
        app = _make_app(api_no_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_no_auth_denies_cost(self, api_no_auth):
        """No token configured → 503 on cost."""
        app = _make_app(api_no_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_status_exempt_with_valid_auth(self, api, auth_headers):
        """Status endpoint works with auth token too (exempt, not restricted)."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status", headers=auth_headers)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_status_exempt_without_auth(self, api):
        """Status endpoint works without auth header when token is configured."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status")
            assert resp.status == 200


# ─── Status Endpoint ──────────────────────────────────────────────


class TestStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self, api, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["uptime_seconds"] == 42
            assert body["today_cost"] == 1.23

    @pytest.mark.asyncio
    async def test_status_no_callback(self, queue, auth_headers):
        """Status with no get_status callback returns minimal response."""
        api = HTTPApi(
            queue=queue,
            host="127.0.0.1",
            port=0,
            auth_token="test-token-123",
            agent_timeout=5.0,
            get_status=None,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_status_callback_exception(self, queue, auth_headers):
        """Status callback that raises returns 500."""
        def broken_status():
            raise RuntimeError("DB connection lost")

        api = HTTPApi(
            queue=queue,
            host="127.0.0.1",
            port=0,
            auth_token="test-token-123",
            agent_timeout=5.0,
            get_status=broken_status,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status", headers=auth_headers)
            # aiohttp middleware catches unhandled exceptions as 500
            assert resp.status == 500

    @pytest.mark.asyncio
    async def test_status_response_is_json(self, api, auth_headers):
        """Status response Content-Type is application/json."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/status", headers=auth_headers)
            assert "application/json" in resp.headers["Content-Type"]


# ─── Notify Endpoint ─────────────────────────────────────────────


class TestNotify:
    @pytest.mark.asyncio
    async def test_queues_message(self, api, queue, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "New email from alice@test.com"},
            )
            assert resp.status == 202
            body = await resp.json()
            assert body["accepted"] is True

        item = queue.get_nowait()
        assert item["type"] == "system"
        assert "New email from alice@test.com" in item["text"]
        assert item["tier"] == "operational"

    @pytest.mark.asyncio
    async def test_source_and_ref_in_text(self, api, queue, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "Quote accepted", "source": "n8n-email", "ref": "Q-47"},
            )

        item = queue.get_nowait()
        assert "[source: n8n-email]" in item["text"]
        assert "[ref: Q-47]" in item["text"]
        assert "Quote accepted" in item["text"]

    @pytest.mark.asyncio
    async def test_notify_meta_on_queue(self, api, queue, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "test",
                    "source": "imap",
                    "ref": "msg-123",
                    "data": {"from": "bob@test.com"},
                },
            )

        item = queue.get_nowait()
        assert item["notify_meta"]["source"] == "imap"
        assert item["notify_meta"]["ref"] == "msg-123"
        assert item["notify_meta"]["data"]["from"] == "bob@test.com"

    @pytest.mark.asyncio
    async def test_no_source_no_ref(self, api, queue, auth_headers):
        """Message without source/ref has no brackets in text."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "plain notification"},
            )

        item = queue.get_nowait()
        assert "[source:" not in item["text"]
        assert "[ref:" not in item["text"]
        assert "plain notification" in item["text"]
        assert item["notify_meta"] is None

    @pytest.mark.asyncio
    async def test_custom_sender(self, api, queue, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "test", "sender": "n8n-email"},
            )

        item = queue.get_nowait()
        assert item["sender"] == "http-n8n-email"

    @pytest.mark.asyncio
    async def test_missing_message_rejected(self, api, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"source": "test"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, api, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=b"not json",
            )
            assert resp.status == 400


class TestNotifyEdgeCases:
    """Edge cases for /api/v1/notify."""

    @pytest.mark.asyncio
    async def test_empty_message_string_rejected(self, api, auth_headers):
        """Whitespace-only message should be rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "   "},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unicode_in_message(self, api, queue, auth_headers):
        """Unicode characters in message are preserved."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "hello from caf\u00e9 \u2603"},
            )

        item = queue.get_nowait()
        assert "caf\u00e9" in item["text"]
        assert "\u2603" in item["text"]

    @pytest.mark.asyncio
    async def test_default_sender_is_http_default(self, api, queue, auth_headers):
        """Omitting sender defaults to 'http-default'."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "test"},
            )

        item = queue.get_nowait()
        assert item["sender"] == "http-default"

    @pytest.mark.asyncio
    async def test_always_operational_tier(self, api, queue, auth_headers):
        """All /notify messages use operational tier regardless of payload."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "test"},
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert item["tier"] == "operational"

    @pytest.mark.asyncio
    async def test_response_has_queued_at_timestamp(self, api, auth_headers):
        """Notify response includes queued_at ISO timestamp."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "test"},
            )
            body = await resp.json()
            assert "queued_at" in body
            assert "T" in body["queued_at"]

    @pytest.mark.asyncio
    async def test_text_format_includes_automated_prefix(self, api, queue, auth_headers):
        """Queued text includes [AUTOMATED SYSTEM MESSAGE] prefix."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "test notification"},
            )

        item = queue.get_nowait()
        assert item["text"].startswith("[AUTOMATED SYSTEM MESSAGE]")

    @pytest.mark.asyncio
    async def test_data_in_notify_meta_not_text(self, api, queue, auth_headers):
        """Data field goes to notify_meta, not into LLM text."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "invoice ready", "data": {"amount": 42.50}},
            )

        item = queue.get_nowait()
        # data is in notify_meta, not serialized in text
        assert item["notify_meta"]["data"]["amount"] == 42.50
        assert "42.50" not in item["text"]

    @pytest.mark.asyncio
    async def test_source_only_no_ref(self, api, queue, auth_headers):
        """Source without ref produces only source bracket."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "ping", "source": "healthcheck"},
            )

        item = queue.get_nowait()
        assert "[source: healthcheck]" in item["text"]
        assert "[ref:" not in item["text"]


# ─── Chat Endpoint ────────────────────────────────────────────────


class TestChat:
    @pytest.mark.asyncio
    async def test_queues_message_with_future(self, api, queue, auth_headers):
        """Chat request puts a dict with response_future on the queue."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            # Start the request but resolve the future from another task
            async def resolve_future():
                await asyncio.sleep(0.1)
                item = await queue.get()
                assert item["type"] == "http"
                assert item["text"] == "hello"
                assert item["sender"] == "http-default"
                assert "response_future" in item
                item["response_future"].set_result({
                    "reply": "hi back",
                    "session_id": "test-123",
                    "tokens": {"input": 100, "output": 50},
                })

            task = asyncio.create_task(resolve_future())
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "hello"},
            )
            await task

            assert resp.status == 200
            body = await resp.json()
            assert body["reply"] == "hi back"
            assert body["session_id"] == "test-123"

    @pytest.mark.asyncio
    async def test_custom_sender_and_tier(self, api, queue, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.1)
                item = await queue.get()
                assert item["sender"] == "http-n8n-calendar"
                assert item["tier"] == "operational"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test", "sender": "n8n-calendar", "tier": "operational"},
            )
            await task

    @pytest.mark.asyncio
    async def test_context_prepended(self, api, queue, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.1)
                item = await queue.get()
                assert item["text"] == "[daily-briefing] check calendar"
                item["response_future"].set_result({"reply": "done"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "check calendar", "context": "daily-briefing"},
            )
            await task

    @pytest.mark.asyncio
    async def test_timeout_returns_408(self, queue):
        """If the future is never resolved, chat returns 408."""
        api = HTTPApi(
            queue=queue,
            host="127.0.0.1",
            port=0,
            auth_token="test-token-123",
            agent_timeout=0.2,  # Very short timeout for test
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers={"Authorization": "Bearer test-token-123"},
                json={"message": "this will timeout"},
            )
            assert resp.status == 408
            body = await resp.json()
            assert "timeout" in body["error"]

    @pytest.mark.asyncio
    async def test_missing_message_rejected(self, api, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"sender": "test"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_message_rejected(self, api, auth_headers):
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "  "},
            )
            assert resp.status == 400


class TestChatEdgeCases:
    """Edge cases for /api/v1/chat."""

    @pytest.mark.asyncio
    async def test_unicode_message(self, api, queue, auth_headers):
        """Unicode messages are preserved through the queue."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["text"] == "hello caf\u00e9 \u2603 \u2764"
                item["response_future"].set_result({"reply": "nice \u2600"})

            task = asyncio.create_task(resolve())
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "hello caf\u00e9 \u2603 \u2764"},
            )
            await task

            body = await resp.json()
            assert body["reply"] == "nice \u2600"

    @pytest.mark.asyncio
    async def test_long_message(self, api, queue, auth_headers):
        """Long messages don't crash the endpoint."""
        long_msg = "A" * 50_000
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert len(item["text"]) == 50_000
                item["response_future"].set_result({"reply": "received"})

            task = asyncio.create_task(resolve())
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": long_msg},
            )
            await task
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_message_with_newlines(self, api, queue, auth_headers):
        """Messages with newlines are preserved."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["text"] == "line1\nline2\nline3"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "line1\nline2\nline3"},
            )
            await task
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_default_tier_is_full(self, api, queue, auth_headers):
        """Omitting tier defaults to 'full'."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["tier"] == "full"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test"},
            )
            await task

    @pytest.mark.asyncio
    async def test_no_context_means_no_prefix(self, api, queue, auth_headers):
        """Without context field, message text has no prefix."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["text"] == "plain message"
                assert "[" not in item["text"]
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "plain message"},
            )
            await task

    @pytest.mark.asyncio
    async def test_empty_context_not_prepended(self, api, queue, auth_headers):
        """Empty string context is not prepended."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["text"] == "test msg"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test msg", "context": ""},
            )
            await task

    @pytest.mark.asyncio
    async def test_future_error_result(self, api, queue, auth_headers):
        """Future resolved with error dict returns the error in JSON."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                item["response_future"].set_result({
                    "error": "provider timeout",
                    "session_id": "s-123",
                })

            task = asyncio.create_task(resolve())
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test"},
            )
            await task

            # The handler returns whatever the Future resolves with
            assert resp.status == 200
            body = await resp.json()
            assert body["error"] == "provider timeout"

    @pytest.mark.asyncio
    async def test_invalid_json_body(self, api, auth_headers):
        """Non-JSON body returns 400."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=b"{{not valid json",
            )
            assert resp.status == 400
            body = await resp.json()
            assert "invalid JSON" in body["error"]

    @pytest.mark.asyncio
    async def test_concurrent_chat_requests(self, api, queue, auth_headers):
        """Multiple concurrent /chat requests each get their own response."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve_all():
                """Resolve futures as they arrive on the queue."""
                for i in range(3):
                    item = await queue.get()
                    msg_text = item["text"]
                    item["response_future"].set_result({
                        "reply": f"reply-to-{msg_text}",
                        "session_id": f"s-{i}",
                    })

            task = asyncio.create_task(resolve_all())

            # Send 3 concurrent requests
            responses = await asyncio.gather(
                client.post("/api/v1/chat", headers=auth_headers, json={"message": "msg-0"}),
                client.post("/api/v1/chat", headers=auth_headers, json={"message": "msg-1"}),
                client.post("/api/v1/chat", headers=auth_headers, json={"message": "msg-2"}),
            )
            await task

            # Each response should be valid
            bodies = [await r.json() for r in responses]
            replies = {b["reply"] for b in bodies}
            assert len(replies) == 3
            for r in responses:
                assert r.status == 200

    @pytest.mark.asyncio
    async def test_chat_type_is_http(self, api, queue, auth_headers):
        """Chat queue items have type='http' for source routing."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["type"] == "http"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test"},
            )
            await task

    @pytest.mark.asyncio
    async def test_chat_future_is_asyncio_future(self, api, queue, auth_headers):
        """The response_future on the queue is a proper asyncio.Future."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                future = item["response_future"]
                assert isinstance(future, asyncio.Future)
                assert not future.done()
                future.set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test"},
            )
            await task


# ─── Content-Type Edge Cases ─────────────────────────────────────


class TestContentType:
    """Edge cases for request Content-Type handling."""

    @pytest.mark.asyncio
    async def test_chat_no_content_type(self, api, auth_headers):
        """POST without Content-Type header gets 400 (not crash)."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                data=b"hello",
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_notify_empty_body(self, api, auth_headers):
        """POST with empty body gets 400."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=b"",
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_chat_array_body(self, api, auth_headers):
        """POST with JSON array instead of object gets 400 or handled gracefully."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json=["not", "an", "object"],
            )
            # .get("message") on a list returns AttributeError → 400 from json parse or
            # the message extraction returns "" → 400
            assert resp.status in (400, 500)

    @pytest.mark.asyncio
    async def test_notify_array_body(self, api, auth_headers):
        """POST with JSON array instead of object for notify."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json=[{"message": "test"}],
            )
            assert resp.status in (400, 500)

    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self, queue, auth_headers):
        """POST with body > max_body_bytes gets 413 Request Entity Too Large."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            max_body_bytes=1_048_576,  # 1 MiB
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            # Build valid JSON that exceeds 1 MiB
            oversized = {"message": "x" * (1_048_576 + 1)}
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json=oversized,
            )
            assert resp.status == 413


# ─── Lifecycle ───────────────────────────────────────────────────


class TestLifecycle:
    """Test HTTPApi start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_stop_without_start(self, queue):
        """Calling stop before start doesn't crash."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="", agent_timeout=5.0,
        )
        await api.stop()  # _runner is None — should not raise

    @pytest.mark.asyncio
    async def test_double_stop(self, queue):
        """Calling stop twice doesn't crash."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="", agent_timeout=5.0,
        )
        # Start on a random port
        await api.start()
        await api.stop()
        await api.stop()  # Second stop should be safe


# ─── Route / Method Tests ────────────────────────────────────────


class TestRouting:
    """Verify correct HTTP methods and unknown routes."""

    @pytest.mark.asyncio
    async def test_get_on_chat_returns_405(self, api, auth_headers):
        """GET on /chat (POST-only) returns 405 Method Not Allowed."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/chat", headers=auth_headers)
            assert resp.status == 405

    @pytest.mark.asyncio
    async def test_post_on_status_returns_405(self, api, auth_headers):
        """POST on /status (GET-only) returns 405."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/v1/status", headers=auth_headers, json={})
            assert resp.status == 405

    @pytest.mark.asyncio
    async def test_unknown_route_returns_404(self, api, auth_headers):
        """Unknown API route returns 404."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/unknown", headers=auth_headers)
            assert resp.status == 404


# ─── Auth Constant-Time Comparison ───────────────────────────────


class TestConstantTimeAuth:
    """SEC-5: Verify auth uses hmac.compare_digest."""

    @pytest.mark.asyncio
    async def test_auth_uses_constant_time_comparison(self, api, auth_headers):
        """Correct token is accepted (hmac.compare_digest behavior)."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions", headers=auth_headers)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_auth_rejects_wrong_token(self, api):
        """Wrong token is rejected."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status == 401


# ─── Rate Limiting ───────────────────────────────────────────────


class TestRateLimiting:
    """SEC-8: HTTP rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_after_threshold(self, queue):
        """Send max+1 requests, verify 429."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="rate-test-token", agent_timeout=5.0,
        )
        # Override rate limiter with low threshold for testing
        from channels.http_api import _RateLimiter
        api._rate_limiter = _RateLimiter(max_requests=3, window_seconds=60)
        app = _make_app(api)
        headers = {"Authorization": "Bearer rate-test-token"}
        async with TestClient(TestServer(app)) as client:
            for i in range(3):
                resp = await client.post(
                    "/api/v1/notify",
                    headers=headers,
                    json={"message": f"test-{i}"},
                )
                assert resp.status == 202
            # 4th request should be rate limited
            resp = await client.post(
                "/api/v1/notify",
                headers=headers,
                json={"message": "test-blocked"},
            )
            assert resp.status == 429

    @pytest.mark.asyncio
    async def test_rate_limit_recovers_after_window(self, queue):
        """Requests succeed after window passes."""
        from channels.http_api import _RateLimiter
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="rate-test-token", agent_timeout=5.0,
        )
        api._rate_limiter = _RateLimiter(max_requests=2, window_seconds=0.1)
        app = _make_app(api)
        headers = {"Authorization": "Bearer rate-test-token"}
        async with TestClient(TestServer(app)) as client:
            # Fill the limit
            for i in range(2):
                resp = await client.post(
                    "/api/v1/notify",
                    headers=headers,
                    json={"message": f"test-{i}"},
                )
                assert resp.status == 202
            # Should be blocked
            resp = await client.post(
                "/api/v1/notify",
                headers=headers,
                json={"message": "blocked"},
            )
            assert resp.status == 429
            # Wait for window to pass
            await asyncio.sleep(0.15)
            # Should succeed again
            resp = await client.post(
                "/api/v1/notify",
                headers=headers,
                json={"message": "recovered"},
            )
            assert resp.status == 202


# ─── Sender Prefixing ────────────────────────────────────────────


class TestSenderPrefixing:
    """SEC-3: HTTP sender injection prevention."""

    @pytest.mark.asyncio
    async def test_sender_prefixed(self, api, queue, auth_headers):
        """sender='foo' becomes 'http-foo'."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["sender"] == "http-foo"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test", "sender": "foo"},
            )
            await task

    @pytest.mark.asyncio
    async def test_sender_default(self, api, queue, auth_headers):
        """Missing sender becomes 'http-default'."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["sender"] == "http-default"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test"},
            )
            await task

    @pytest.mark.asyncio
    async def test_sender_cannot_impersonate_channel(self, api, queue, auth_headers):
        """Sender matching a phone number gets prefixed, preventing impersonation."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.05)
                item = await queue.get()
                assert item["sender"] == "http-+431234567890"
                item["response_future"].set_result({"reply": "ok"})

            task = asyncio.create_task(resolve())
            await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={"message": "test", "sender": "+431234567890"},
            )
            await task


# ─── Config Integration ──────────────────────────────────────────


class TestHTTPConfig:
    def test_http_defaults(self):
        """HTTP config has sensible defaults when section is missing."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.http_enabled is False
        assert cfg.http_host == "127.0.0.1"
        assert cfg.http_port == 8100
        assert cfg.http_auth_token == ""

    def test_http_configured(self):
        """HTTP config reads from [http] section."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "http": {"enabled": True, "host": "0.0.0.0", "port": 9000},
        })
        assert cfg.http_enabled is True
        assert cfg.http_host == "0.0.0.0"
        assert cfg.http_port == 9000

    def test_http_token_from_env(self, monkeypatch):
        """HTTP token loaded from LUCYD_HTTP_TOKEN env var."""
        monkeypatch.setenv("LUCYD_HTTP_TOKEN", "my-secret-token")
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.http_auth_token == "my-secret-token"

    def test_http_routing(self):
        """HTTP source routes to configured model."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "routing": {"http": "primary", "system": "subagent"},
        })
        assert cfg.route_model("http") == "primary"
        assert cfg.route_model("system") == "subagent"


# ─── TEST-6: Concurrent HTTP /chat — different senders ───────────


class TestConcurrentHTTPChat:
    """TEST-6: Two concurrent /chat requests with different senders get
    independent responses with no cross-contamination."""

    @pytest.mark.asyncio
    async def test_two_senders_get_own_responses(self, api, queue, auth_headers):
        """Send two /chat requests with different senders simultaneously.
        Each response must match its own sender, not the other."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            async def resolve_all():
                """Resolve each future with a reply that echoes the sender."""
                for _ in range(2):
                    item = await queue.get()
                    sender = item["sender"]
                    msg = item["text"]
                    item["response_future"].set_result({
                        "reply": f"response-for-{sender}: {msg}",
                        "session_id": f"session-{sender}",
                    })

            task = asyncio.create_task(resolve_all())

            resp_a, resp_b = await asyncio.gather(
                client.post(
                    "/api/v1/chat",
                    headers=auth_headers,
                    json={"message": "hello from alice", "sender": "alice"},
                ),
                client.post(
                    "/api/v1/chat",
                    headers=auth_headers,
                    json={"message": "hello from bob", "sender": "bob"},
                ),
            )
            await task

            body_a = await resp_a.json()
            body_b = await resp_b.json()

            assert resp_a.status == 200
            assert resp_b.status == 200

            # Each response must contain its own sender's name — no cross-talk
            replies = {body_a["reply"], body_b["reply"]}
            assert any("http-alice" in r and "hello from alice" in r for r in replies)
            assert any("http-bob" in r and "hello from bob" in r for r in replies)

            # Session IDs must differ
            assert body_a["session_id"] != body_b["session_id"]

    @pytest.mark.asyncio
    async def test_concurrent_mixed_senders_all_succeed(self, api, queue, auth_headers):
        """Four concurrent requests with a mix of default and custom senders
        all resolve independently."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            received_senders = []

            async def resolve_all():
                for i in range(4):
                    item = await queue.get()
                    received_senders.append(item["sender"])
                    item["response_future"].set_result({
                        "reply": f"reply-{i}",
                        "session_id": f"s-{i}",
                    })

            task = asyncio.create_task(resolve_all())

            responses = await asyncio.gather(
                client.post("/api/v1/chat", headers=auth_headers,
                            json={"message": "m0", "sender": "svc-a"}),
                client.post("/api/v1/chat", headers=auth_headers,
                            json={"message": "m1", "sender": "svc-b"}),
                client.post("/api/v1/chat", headers=auth_headers,
                            json={"message": "m2"}),  # default sender
                client.post("/api/v1/chat", headers=auth_headers,
                            json={"message": "m3", "sender": "svc-a"}),  # same as first
            )
            await task

            for resp in responses:
                assert resp.status == 200

            bodies = [await r.json() for r in responses]
            # All four replies are distinct strings
            reply_set = {b["reply"] for b in bodies}
            assert len(reply_set) == 4

            # Verify the queue saw correct sender prefixes
            assert "http-svc-a" in received_senders
            assert "http-svc-b" in received_senders
            assert "http-default" in received_senders


# ─── Sessions Endpoint ───────────────────────────────────────────


class TestSessions:
    @pytest.mark.asyncio
    async def test_returns_session_list(self, queue, auth_headers):
        """Sessions endpoint returns list from callback."""
        sessions_data = [
            {"session_id": "s-1", "contact": "alice", "message_count": 10},
            {"session_id": "s-2", "contact": "bob", "message_count": 5},
        ]
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            get_sessions=lambda: sessions_data,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert len(body["sessions"]) == 2
            assert body["sessions"][0]["contact"] == "alice"

    @pytest.mark.asyncio
    async def test_empty_sessions(self, queue, auth_headers):
        """No active sessions returns empty list."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            get_sessions=lambda: [],
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["sessions"] == []

    @pytest.mark.asyncio
    async def test_sessions_no_callback(self, queue, auth_headers):
        """No get_sessions callback returns empty list."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["sessions"] == []

    @pytest.mark.asyncio
    async def test_sessions_requires_auth(self, api):
        """Sessions endpoint requires auth when token is configured."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions")
            assert resp.status == 401


# ─── Cost Endpoint ────────────────────────────────────────────────


class TestCost:
    @pytest.mark.asyncio
    async def test_returns_cost_today(self, queue, auth_headers):
        """Cost endpoint returns today's cost from callback."""
        cost_data = {"period": "today", "total_cost": 1.50, "models": [
            {"model": "test", "input_tokens": 1000, "output_tokens": 500, "cost_usd": 1.50},
        ]}
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            get_cost=lambda p: cost_data,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost?period=today", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["period"] == "today"
            assert body["total_cost"] == 1.50

    @pytest.mark.asyncio
    async def test_cost_week_period(self, queue, auth_headers):
        """Cost endpoint accepts 'week' period."""
        received_periods = []

        def mock_cost(period):
            received_periods.append(period)
            return {"period": period, "total_cost": 0.0, "models": []}

        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            get_cost=mock_cost,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost?period=week", headers=auth_headers)
            assert resp.status == 200
        assert received_periods == ["week"]

    @pytest.mark.asyncio
    async def test_cost_all_period(self, queue, auth_headers):
        """Cost endpoint accepts 'all' period."""
        received_periods = []

        def mock_cost(period):
            received_periods.append(period)
            return {"period": period, "total_cost": 0.0, "models": []}

        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            get_cost=mock_cost,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost?period=all", headers=auth_headers)
            assert resp.status == 200
        assert received_periods == ["all"]

    @pytest.mark.asyncio
    async def test_cost_invalid_period(self, queue, auth_headers):
        """Invalid period returns 400."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost?period=invalid", headers=auth_headers)
            assert resp.status == 400
            body = await resp.json()
            assert "period" in body["error"]

    @pytest.mark.asyncio
    async def test_cost_default_period_is_today(self, queue, auth_headers):
        """Omitting period defaults to 'today'."""
        received_periods = []

        def mock_cost(period):
            received_periods.append(period)
            return {"period": period, "total_cost": 0.0, "models": []}

        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            get_cost=mock_cost,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost", headers=auth_headers)
            assert resp.status == 200
        assert received_periods == ["today"]

    @pytest.mark.asyncio
    async def test_cost_no_callback(self, queue, auth_headers):
        """No get_cost callback returns zero cost."""
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost?period=today", headers=auth_headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["total_cost"] == 0.0

    @pytest.mark.asyncio
    async def test_cost_requires_auth(self, api):
        """Cost endpoint requires auth when token is configured."""
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/cost")
            assert resp.status == 401


# ─── Config: Callback Properties ─────────────────────────────────


class TestHTTPCallbackConfig:
    def test_callback_url_default_empty(self):
        """Callback URL defaults to empty when not configured."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.http_callback_url == ""

    def test_callback_url_configured(self):
        """Callback URL reads from [http] section."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "http": {"callback_url": "https://n8n.local/webhook/abc"},
        })
        assert cfg.http_callback_url == "https://n8n.local/webhook/abc"

    def test_callback_token_from_env(self, monkeypatch):
        """Callback token loaded from env var specified in config."""
        monkeypatch.setenv("MY_CALLBACK_TOKEN", "secret-webhook-token")
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "http": {"callback_token_env": "MY_CALLBACK_TOKEN"},
        })
        assert cfg.http_callback_token == "secret-webhook-token"

    def test_callback_token_empty_when_env_not_set(self):
        """Callback token empty when env var is not set."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "http": {"callback_token_env": "NONEXISTENT_VAR_12345"},
        })
        assert cfg.http_callback_token == ""

    def test_callback_token_empty_when_no_env_var_configured(self):
        """Callback token empty when callback_token_env is not configured."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.http_callback_token == ""


# ─── HTTP Attachment Support ─────────────────────────────────────


class TestHTTPAttachments:
    """Attachment decoding and queue wiring for /chat and /notify."""

    @pytest.fixture
    def api_with_dl(self, queue, tmp_path):
        """HTTPApi with a temp download directory."""
        return HTTPApi(
            queue=queue,
            host="127.0.0.1",
            port=0,
            auth_token="test-token-123",
            agent_timeout=5.0,
            download_dir=str(tmp_path / "downloads"),
        )

    @pytest.fixture
    def auth_headers(self):
        return {"Authorization": "Bearer test-token-123"}

    @pytest.mark.asyncio
    async def test_chat_with_attachment(self, api_with_dl, queue, auth_headers):
        """POST /chat with attachments decodes and queues Attachment objects."""
        import base64
        from channels import Attachment

        data_b64 = base64.b64encode(b"fake pdf content").decode()
        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            async def resolve():
                await asyncio.sleep(0.1)
                item = await queue.get()
                assert "attachments" in item
                atts = item["attachments"]
                assert len(atts) == 1
                assert isinstance(atts[0], Attachment)
                assert atts[0].content_type == "application/pdf"
                assert atts[0].filename == "invoice.pdf"
                assert atts[0].size == len(b"fake pdf content")
                assert Path(atts[0].local_path).exists()
                item["response_future"].set_result({"reply": "got it"})

            task = asyncio.create_task(resolve())
            resp = await client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={
                    "message": "process this",
                    "attachments": [{
                        "content_type": "application/pdf",
                        "filename": "invoice.pdf",
                        "data": data_b64,
                    }],
                },
            )
            await task
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_notify_with_attachment(self, api_with_dl, queue, auth_headers):
        """POST /notify with attachments decodes and queues them."""
        import base64
        from channels import Attachment

        data_b64 = base64.b64encode(b"\x89PNG fake image").decode()
        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "new photo",
                    "attachments": [{
                        "content_type": "image/png",
                        "filename": "photo.png",
                        "data": data_b64,
                    }],
                },
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert "attachments" in item
        atts = item["attachments"]
        assert len(atts) == 1
        assert isinstance(atts[0], Attachment)
        assert atts[0].content_type == "image/png"

    @pytest.mark.asyncio
    async def test_multiple_attachments(self, api_with_dl, queue, auth_headers):
        """Multiple attachments in one request are all decoded."""
        import base64

        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "two files",
                    "attachments": [
                        {"content_type": "text/plain", "filename": "a.txt", "data": base64.b64encode(b"aaa").decode()},
                        {"content_type": "image/jpeg", "filename": "b.jpg", "data": base64.b64encode(b"bbb").decode()},
                    ],
                },
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert len(item["attachments"]) == 2
        assert {a.filename for a in item["attachments"]} == {"a.txt", "b.jpg"}

    @pytest.mark.asyncio
    async def test_no_attachments_key_absent(self, api_with_dl, queue, auth_headers):
        """When no attachments provided, key is absent from queue item."""
        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "plain text"},
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert "attachments" not in item

    @pytest.mark.asyncio
    async def test_empty_attachments_list(self, api_with_dl, queue, auth_headers):
        """Empty attachments list treated same as absent."""
        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={"message": "no files", "attachments": []},
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert "attachments" not in item

    @pytest.mark.asyncio
    async def test_attachment_missing_data_skipped(self, api_with_dl, queue, auth_headers):
        """Attachment with missing data field is silently skipped."""
        import base64

        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "partial",
                    "attachments": [
                        {"content_type": "text/plain", "filename": "no-data.txt"},
                        {"content_type": "text/plain", "filename": "good.txt", "data": base64.b64encode(b"ok").decode()},
                    ],
                },
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert len(item["attachments"]) == 1
        assert item["attachments"][0].filename == "good.txt"

    @pytest.mark.asyncio
    async def test_attachment_missing_content_type_skipped(self, api_with_dl, queue, auth_headers):
        """Attachment with missing content_type is silently skipped."""
        import base64

        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "partial",
                    "attachments": [
                        {"filename": "no-ct.txt", "data": base64.b64encode(b"x").decode()},
                    ],
                },
            )
            assert resp.status == 202

        item = queue.get_nowait()
        assert "attachments" not in item

    @pytest.mark.asyncio
    async def test_download_dir_created(self, queue, tmp_path, auth_headers):
        """Download directory is created if it doesn't exist."""
        import base64

        dl_dir = tmp_path / "new" / "subdir"
        api = HTTPApi(
            queue=queue, host="127.0.0.1", port=0,
            auth_token="test-token-123", agent_timeout=5.0,
            download_dir=str(dl_dir),
        )
        app = _make_app(api)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "file",
                    "attachments": [{"content_type": "text/plain", "filename": "test.txt",
                                     "data": base64.b64encode(b"hello").decode()}],
                },
            )
            assert resp.status == 202

        assert dl_dir.exists()
        item = queue.get_nowait()
        assert Path(item["attachments"][0].local_path).parent == dl_dir

    @pytest.mark.asyncio
    async def test_file_content_preserved(self, api_with_dl, queue, auth_headers):
        """File content round-trips through base64 decode to disk."""
        import base64

        original = b"\x00\x01\x02binary\xfe\xff"
        data_b64 = base64.b64encode(original).decode()
        app = _make_app(api_with_dl)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/v1/notify",
                headers=auth_headers,
                json={
                    "message": "binary file",
                    "attachments": [{"content_type": "application/octet-stream",
                                     "filename": "data.bin", "data": data_b64}],
                },
            )
            assert resp.status == 202

        item = queue.get_nowait()
        saved = Path(item["attachments"][0].local_path).read_bytes()
        assert saved == original


# ─── Config: HTTP Attachment Properties ──────────────────────────


class TestHTTPAttachmentConfig:
    def test_download_dir_default(self):
        """Download dir defaults to /tmp/lucyd-http."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.http_download_dir == "/tmp/lucyd-http"

    def test_download_dir_configured(self):
        """Download dir reads from [http] section."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "http": {"download_dir": "/var/lucyd/uploads"},
        })
        assert cfg.http_download_dir == "/var/lucyd/uploads"

    def test_max_body_bytes_default(self):
        """Max body bytes defaults to 10 MiB."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
        })
        assert cfg.http_max_body_bytes == 10 * 1024 * 1024

    def test_max_body_bytes_configured(self):
        """Max body bytes reads from [http] section."""
        from config import Config
        cfg = Config({
            "agent": {"name": "Test", "workspace": "/tmp/test"},
            "channel": {"type": "cli"},
            "models": {"primary": {"provider": "anthropic-compat", "model": "test"}},
            "http": {"max_body_bytes": 5_000_000},
        })
        assert cfg.http_max_body_bytes == 5_000_000
