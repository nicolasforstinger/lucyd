"""SessionManager.append_outbound_to_user — cross-session append from agent:self."""
from __future__ import annotations

from typing import Any

import pytest

from session import SessionManager
from messages import AssistantMessage


@pytest.mark.asyncio
async def test_append_outbound_to_existing_session(pool: Any) -> None:
    """Appending to a session with prior messages adds an AssistantMessage at the end."""
    mgr = SessionManager(pool=pool, agent_name="A")

    # Pre-populate with one user + one assistant message
    session = await mgr.get_or_create("user:n")
    await session.add_user_message("hi", sender="n")
    msg: AssistantMessage = {"role": "agent", "text": "hello back"}
    await session.add_assistant_message(msg)
    initial_count = len(session.messages)

    # Append outbound
    await mgr.append_outbound_to_user(
        target_key="user:n",
        text="proactive ping",
        attachment_refs=[],
        source_metadata={"from": "agent_self"},
    )

    # Reload and verify
    mgr2 = SessionManager(pool=pool, agent_name="A")
    session2 = await mgr2.get_or_create("user:n")
    assert len(session2.messages) == initial_count + 1
    last = session2.messages[-1]
    assert last["role"] == "agent"
    assert last["text"] == "proactive ping"


@pytest.mark.asyncio
async def test_append_outbound_to_empty_session_prepends_anchor(pool: Any) -> None:
    """If the target session has no messages, a synthetic anchor user message is prepended."""
    mgr = SessionManager(pool=pool, agent_name="A")

    await mgr.append_outbound_to_user(
        target_key="user:n",
        text="hello world",
        attachment_refs=[],
        source_metadata={"from": "agent_self"},
    )

    mgr2 = SessionManager(pool=pool, agent_name="A")
    session = await mgr2.get_or_create("user:n")
    assert len(session.messages) == 2
    assert session.messages[0]["role"] == "user"
    assert "external trigger" in session.messages[0]["content"].lower()
    assert session.messages[1]["role"] == "agent"
    assert session.messages[1]["text"] == "hello world"


@pytest.mark.asyncio
async def test_append_outbound_carries_attachment_refs(pool: Any) -> None:
    """Attachment refs (paths/metadata, not raw bytes) are stored on the message."""
    mgr = SessionManager(pool=pool, agent_name="A")
    session = await mgr.get_or_create("user:n")
    await session.add_user_message("hi", sender="n")

    refs = [{"filename": "out.pdf", "size": 1234, "content_type": "application/pdf",
             "path": "/data/workspace/out.pdf"}]
    await mgr.append_outbound_to_user(
        target_key="user:n",
        text="see attached",
        attachment_refs=refs,
        source_metadata={"from": "agent_self"},
    )

    mgr2 = SessionManager(pool=pool, agent_name="A")
    session2 = await mgr2.get_or_create("user:n")
    last = session2.messages[-1]
    assert last["role"] == "agent"
    assert last["text"] == "see attached"
    assert last.get("attachments") == refs
