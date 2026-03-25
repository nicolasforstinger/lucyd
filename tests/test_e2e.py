"""End-to-end integration test — boots the real framework stack.

Uses smoke-local provider (no API calls) and no channel (HTTP-only mode).
Sends a message through the queue, verifies response, session state, and metering.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import Config
from lucyd import LucydDaemon


def _make_e2e_config(tmp_path: Path) -> Config:
    """Build a minimal Config for a full daemon boot with no external deps."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("# Soul\nI am TestBot.")

    data = {
        "agent": {
            "name": "E2EAgent",
            "workspace": str(workspace),
            "context": {"stable": ["SOUL.md"], "semi_stable": []},
            "skills": {"dir": "skills", "always_on": []},
        },
        "channel": {"type": "", "debounce_ms": 0},
        "http": {
            "enabled": False, "host": "127.0.0.1", "port": 0, "token_env": "",
            "download_dir": str(tmp_path / "downloads"),
            "max_body_bytes": 10485760,
            "callback_url": "", "callback_token_env": "",
            "callback_timeout": 10, "callback_max_failures": 10,
            "max_attachment_bytes": 52428800,
            "rate_limit": 30, "rate_window": 60,
            "status_rate_limit": 60, "rate_limit_cleanup_threshold": 1000,
        },
        "models": {
            "primary": {
                "provider": "smoke-local",
                "model": "smoke-e2e",
                "max_tokens": 64,
                "reply_text": "Hello from smoke test!",
                "cost_per_mtok": [1.0, 5.0, 0.1],
            },
        },
        "memory": {
            "db": "", "search_top_k": 10, "vector_search_limit": 10000,
            "fts_min_results": 3, "embedding_timeout": 15,
            "consolidation": {"enabled": False, "min_messages": 4,
                              "confidence_threshold": 0.6, "max_extraction_chars": 50000},
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
            "tts": {"provider": "", "api_key_env": "", "timeout": 60, "api_url": ""},
        },
        "stt": {"backend": ""},
        "documents": {"enabled": False, "max_chars": 30000, "max_file_bytes": 10485760,
                       "text_extensions": []},
        "logging": {"max_bytes": 0, "backup_count": 0, "suppress": []},
        "vision": {"max_image_bytes": 5242880, "max_dimension": 1568,
                    "jpeg_quality_steps": [85, 60, 40]},
        "behavior": {
            "silent_tokens": [], "typing_indicators": False,
            "error_message": "error", "sqlite_timeout": 5,
            "api_retries": 0, "api_retry_base_delay": 0,
            "message_retries": 0, "message_retry_base_delay": 0,
            "audit_truncation_limit": 500, "agent_timeout_seconds": 30,
            "max_turns_per_message": 5, "max_cost_per_message": 0.0,
            "queue_capacity": 100, "queue_poll_interval": 0.1,
            "quote_max_chars": 200, "notify_target": "",
            "compaction": {
                "threshold_tokens": 150000, "max_tokens": 2048,
                "prompt": "Summarize.", "keep_recent_pct": 0.33,
                "keep_recent_pct_min": 0.05, "keep_recent_pct_max": 0.9,
                "min_messages": 4, "tool_result_max_chars": 2000,
                "warning_pct": 0.8, "diary_prompt": "Write a log.",
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


@pytest.mark.asyncio
async def test_e2e_message_cycle(tmp_path):
    """Boot daemon, send a message, verify response + session + metering."""
    config = _make_e2e_config(tmp_path)
    daemon = LucydDaemon(config)

    # Ensure state dir exists (monitor.json writes there)
    Path(config.state_dir).mkdir(parents=True, exist_ok=True)

    # Run daemon startup (without PID file / signals)
    daemon._init_provider()
    daemon._init_channel()
    daemon._init_sessions()
    daemon._init_skills()
    daemon._init_context()
    daemon._init_metering()
    daemon._init_tools()
    if daemon.channel is not None:
        await daemon.channel.connect()

    # Enqueue a message with a response future to capture the reply
    response_future = asyncio.get_event_loop().create_future()
    await daemon.queue.put({
        "text": "Hello, agent!",
        "sender": "e2e_user",
        "source": "http",
        "response_future": response_future,
    })
    # Signal the loop to stop after processing
    await daemon.queue.put(None)

    # Run the message loop with a timeout
    await asyncio.wait_for(daemon._message_loop(), timeout=15.0)

    # Verify response received (HTTP source returns a dict with 'reply' key)
    result = response_future.result()
    assert result is not None
    reply = result if isinstance(result, str) else result.get("reply", "")
    assert "Hello from smoke test!" in reply

    # Verify session state persisted
    sessions_dir = Path(config.sessions_dir)
    session_files = list(sessions_dir.glob("*.json"))
    assert len(session_files) >= 1, "Session state file should be persisted"

    # Verify metering record created
    import sqlite3
    meter_conn = sqlite3.connect(str(config.metering_db))
    rows = meter_conn.execute("SELECT COUNT(*) FROM costs").fetchone()
    meter_conn.close()
    assert rows[0] >= 1, "At least one metering record should exist"

    # Cleanup
    if daemon.channel is not None:
        await daemon.channel.disconnect()
