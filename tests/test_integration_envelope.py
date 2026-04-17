"""Integration tests for the channel architecture refactor.

Verifies end-to-end: message envelope propagation, session keying by
channel_id:sender, task_type auto-close, and Prometheus metrics wiring.

Uses the smoke-local provider (no external API calls).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import Config
from lucyd import LucydDaemon


def _make_config(tmp_path: Path) -> Config:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("# Soul\nI am IntegrationBot.")

    data = {
        "agent": {
            "name": "IntegrationAgent",
            "workspace": str(workspace),
            "context": {"stable": ["SOUL.md"], "semi_stable": []},
            "skills": {"dir": "skills", "always_on": []},
        },
        "user": {"name": "testuser"},
        "http": {
            "enabled": False, "host": "127.0.0.1", "port": 0, "token_env": "",
            "download_dir": str(tmp_path / "downloads"),
            "max_body_bytes": 10485760, "max_attachment_bytes": 52428800,
            "rate_limit": 30, "rate_window": 60,
            "status_rate_limit": 60, "rate_limit_cleanup_threshold": 1000,
        },
        "models": {
            "primary": {
                "provider": "smoke-local",
                "model": "smoke-integration",
                "max_tokens": 64,
                "reply_text": "Acknowledged.",
                "cost_per_mtok": [1.0, 5.0, 0.1],
            },
        },
        "memory": {
            "db": "", "search_top_k": 10, "vector_search_limit": 10000,
            "embedding_timeout": 15,
            "consolidation": {"enabled": False, "confidence_threshold": 0.6},
            "recall": {
                "decay_rate": 0.03, "max_facts_in_context": 20,
                "max_dynamic_tokens": 1500, "max_episodes_at_start": 3,
                "archive_messages": 20,
                "personality": {
                    "priority_vector": 35, "priority_episodes": 25,
                    "priority_facts": 15, "priority_commitments": 40,
                    "fact_format": "natural", "show_emotional_tone": True,
                    "episode_section_header": "Recent conversations",
                },
            },
            "maintenance": {"stale_threshold_days": 90},
            "indexer": {"include_patterns": [], "exclude_dirs": [],
                        "chunk_size_chars": 1600, "chunk_overlap_chars": 320,
                        "embed_batch_limit": 100},
        },
        "tools": {
            "enabled": [], "plugins_dir": "plugins.d",
            "output_truncation": 30000,
            "subagent_deny": [], "subagent_max_turns": 0, "subagent_timeout": 0,
            "exec_timeout": 120, "exec_max_timeout": 600,
            "filesystem": {"allowed_paths": [], "default_read_limit": 2000},
            "web_search": {"provider": "", "api_key_env": "", "timeout": 15},
            "web_fetch": {"timeout": 15},
        },
        "documents": {"enabled": False, "max_chars": 30000, "max_file_bytes": 10485760,
                       "text_extensions": []},
        "logging": {"suppress": []},
        "vision": {"max_image_bytes": 5242880, "max_dimension": 1568,
                    "jpeg_quality_steps": [85, 60, 40]},
        "behavior": {
            "silent_tokens": [], "typing_indicators": False,
            "debounce_ms": 0,
            "api_retries": 0, "api_retry_base_delay": 0,
            "message_retries": 0, "message_retry_base_delay": 0,
            "agent_timeout_seconds": 30,
            "max_turns_per_message": 5, "max_cost_per_message": 0.0,
            "notify_target": "",
            "compaction": {
                "threshold_tokens": 150000, "max_tokens": 2048,
                "prompt": "Summarize.", "keep_recent_pct": 0.33,
                "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
                "diary_prompt": "Write a log.",
            },
        },
        "paths": {
            "state_dir": str(tmp_path / "state"),
            "sessions_dir": str(tmp_path / "sessions"),
            "metering_db": str(tmp_path / "metering.db"),
            "log_file": str(tmp_path / "logs" / "lucyd.log"),
        },
    }
    return Config(data, config_dir=tmp_path)


async def _boot_daemon(tmp_path: Path, pool: object) -> LucydDaemon:
    config = _make_config(tmp_path)
    daemon = LucydDaemon(config)
    Path(config.state_dir).mkdir(parents=True, exist_ok=True)
    daemon.pool = pool
    daemon._init_provider()
    daemon._init_sessions()
    daemon._init_skills()
    daemon._init_context()
    daemon._init_metering()
    daemon._init_tools()
    return daemon


async def _send_and_process(daemon: LucydDaemon, item: dict) -> dict:
    """Enqueue a message, run the loop, return the result."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    item["response_future"] = future
    await daemon.queue.put(item)
    await daemon.queue.put(None)
    await asyncio.wait_for(daemon._message_loop(), timeout=15.0)
    daemon.running = True  # Reset for next call
    return future.result()


# ─── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_envelope_channel_id_in_session_key(tmp_path, pool):
    """channel_id propagates to session key as channel_id:sender."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "ping",
        "sender": "test-user",
        "source": "http",
        "channel_id": "test",
        "task_type": "conversational",
    })

    assert "Acknowledged" in result.get("reply", "")

    # Session should be keyed as "test:test-user"
    contacts = await daemon.session_mgr.list_contacts()
    assert "test:test-user" in contacts


@pytest.mark.asyncio
async def test_task_type_auto_close(tmp_path, pool):
    """task_type 'task' auto-closes the session after response."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "one-shot request",
        "sender": "ephemeral-user",
        "source": "http",
        "channel_id": "test",
        "task_type": "task",
    })

    assert "Acknowledged" in result.get("reply", "")

    # Session should have been auto-closed
    contacts = await daemon.session_mgr.list_contacts()
    assert "test:ephemeral-user" not in contacts


@pytest.mark.asyncio
async def test_conversational_session_stays_open(tmp_path, pool):
    """task_type 'conversational' keeps the session open."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "first message",
        "sender": "persistent-user",
        "source": "http",
        "channel_id": "telegram",
        "task_type": "conversational",
    })

    assert "Acknowledged" in result.get("reply", "")

    # Session should still be open
    contacts = await daemon.session_mgr.list_contacts()
    assert "telegram:persistent-user" in contacts


@pytest.mark.asyncio
async def test_default_envelope_values(tmp_path, pool):
    """Missing envelope fields default to channel_id='http', task_type='conversational'."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "bare message",
        "sender": "bare-user",
        "source": "http",
        # No channel_id or task_type
    })

    assert "Acknowledged" in result.get("reply", "")

    # Defaults: channel_id="http", so key is "http:bare-user"
    contacts = await daemon.session_mgr.list_contacts()
    assert "http:bare-user" in contacts


@pytest.mark.asyncio
async def test_metrics_incremented(tmp_path, pool):
    """Prometheus metrics are incremented after message processing."""
    import metrics

    if not metrics.ENABLED:
        pytest.skip("prometheus_client not installed")

    daemon = await _boot_daemon(tmp_path, pool)

    # Clear metric state for test isolation
    metrics.MESSAGES_TOTAL._metrics.clear()

    await _send_and_process(daemon, {
        "text": "metric test",
        "sender": "metric-user",
        "source": "http",
        "channel_id": "test",
        "task_type": "task",
    })

    # MESSAGES_TOTAL should have an observation for our labels
    samples = list(metrics.MESSAGES_TOTAL.collect())
    assert samples, "MESSAGES_TOTAL should have samples"
    total = sum(
        s.value for metric in samples for s in metric.samples
        if s.labels.get("channel_id") == "test"
        and s.labels.get("task_type") == "task"
    )
    assert total > 0, "MESSAGES_TOTAL{channel_id=test, task_type=task} should be > 0"

    # SESSION_CLOSE_TOTAL should fire for task_type auto-close
    close_samples = list(metrics.SESSION_CLOSE_TOTAL.collect())
    close_total = sum(
        s.value for metric in close_samples for s in metric.samples
        if s.labels.get("reason") == "auto_task"
    )
    assert close_total > 0, "SESSION_CLOSE_TOTAL{reason=auto_task} should be > 0"


# ─── reply_to routing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_to_default(tmp_path, pool):
    """No reply_to — normal HTTP response with reply text."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "normal request",
        "sender": "caller",
        "source": "http",
        "channel_id": "test",
    })

    assert "Acknowledged" in result.get("reply", "")
    assert "redirected_to" not in result
    assert result.get("silent") is not True


@pytest.mark.asyncio
async def test_reply_to_silent(tmp_path, pool):
    """reply_to='silent' — reply marked silent, not delivered."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "silent request",
        "sender": "caller",
        "source": "http",
        "channel_id": "test",
        "reply_to": "silent",
    })

    assert "Acknowledged" in result.get("reply", "")
    assert result.get("silent") is True


@pytest.mark.asyncio
async def test_reply_to_redirect(tmp_path, pool):
    """reply_to='<sender>' — reply enqueued as system message into target session."""
    daemon = await _boot_daemon(tmp_path, pool)

    # First, create the target's session so we can verify the redirect lands
    await _send_and_process(daemon, {
        "text": "setup target session",
        "sender": "target-user",
        "source": "http",
        "channel_id": "test",
        "task_type": "conversational",
    })
    assert "test:target-user" in await daemon.session_mgr.list_contacts()

    # Now send a message with reply_to pointing to the target
    result = await _send_and_process(daemon, {
        "text": "generate a reply for someone else",
        "sender": "caller",
        "source": "http",
        "channel_id": "test",
        "reply_to": "target-user",
    })

    # Caller gets the reply with redirect metadata
    assert "Acknowledged" in result.get("reply", "")
    assert result.get("redirected_to") == "target-user"

    # The redirect enqueued a system message — drain it
    # The queue should have the redirected message
    assert not daemon.queue.empty(), "Redirect should have enqueued a system message"
    redirected_item = daemon.queue.get_nowait()
    assert redirected_item["sender"] == "target-user"
    assert redirected_item["type"] == "system"
    assert redirected_item["task_type"] == "system"
    assert "Acknowledged" in redirected_item["text"]


# ─── system convention via /message ──────────────────────────────


@pytest.mark.asyncio
async def test_message_with_system_task_type(tmp_path, pool):
    """task_type 'system' via /message auto-closes like the old /system endpoint."""
    daemon = await _boot_daemon(tmp_path, pool)

    result = await _send_and_process(daemon, {
        "text": "system event via /message",
        "sender": "system-user",
        "source": "http",
        "channel_id": "test",
        "task_type": "system",
    })

    assert "Acknowledged" in result.get("reply", "")

    # Session should have been auto-closed (same as old /system behavior)
    contacts = await daemon.session_mgr.list_contacts()
    assert "test:system-user" not in contacts
