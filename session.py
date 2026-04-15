"""Session manager — persistence, routing, and compaction.

Sessions stored in PostgreSQL: sessions.sessions, sessions.messages,
sessions.events. Messages loaded into RAM during processing and
persisted back to Postgres on state changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import metrics
from messages import AssistantMessage, Message, ToolResultsMessage, UserMessage

log = logging.getLogger(__name__)


AUDIT_TRUNCATION_LIMIT = 500


class ConsecutiveRoleError(RuntimeError):
    pass


def _text_from_content(content: Any) -> str:
    """Extract text from message content.

    Content should always be ``str`` in the internal session format.
    Non-string content (e.g. legacy content block lists) is detected,
    logged, and coerced — the operator should investigate via the
    ``session_invalid_content_format_total`` metric.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    # Non-string content: legacy format or upstream bug.
    log.warning("Non-string content detected (type=%s), coercing", type(content).__name__)
    if metrics.ENABLED:
        metrics.ERRORS_TOTAL.labels(error_type="invalid_content_format").inc()
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _validate_turn_structure(messages: list[Message]) -> None:
    """Detect corrupted turn structure in a message list.

    Checks strict adjacency: every agent message with tool_calls must
    be immediately followed by a tool_result message.  Violations are
    logged and counted via Prometheus — the session is NOT mutated.
    The API call will reject the invalid structure, and the operator
    investigates and fixes the DB manually.
    """
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg["role"] == "agent" and msg.get("tool_calls"):
            if i + 1 < len(messages) and messages[i + 1]["role"] == "tool_result":
                i += 2  # valid pair, skip both
                continue
            # Orphaned tool_calls: no adjacent tool_result
            tc_ids = [tc.get("id", "?") for tc in msg.get("tool_calls", [])]
            log.error(
                "Session corruption: orphaned tool_calls at index %d "
                "(tool_call_ids=%s, next_role=%s)",
                i, tc_ids,
                messages[i + 1]["role"] if i + 1 < len(messages) else "END",
            )
            if metrics.ENABLED:
                metrics.ERRORS_TOTAL.labels(error_type="session_corruption").inc()
        elif msg["role"] == "tool_result":
            # tool_result without preceding agent(tool_calls)
            log.error(
                "Session corruption: orphaned tool_result at index %d",
                i,
            )
            if metrics.ENABLED:
                metrics.ERRORS_TOTAL.labels(error_type="session_corruption").inc()
        i += 1


def _context_tokens_from_usage(usage: dict[str, Any]) -> int:
    """Extract context token count from a usage dict.

    Reads the ``context_tokens`` field directly.  Old messages missing
    this field are detected via the ``missing_context_tokens`` metric
    and fall through with estimated math until they age out via compaction.
    """
    if "context_tokens" in usage:
        return int(usage["context_tokens"])
    log.warning("Message missing context_tokens field, using input+cache estimate")
    if metrics.ENABLED:
        metrics.ERRORS_TOTAL.labels(error_type="missing_context_tokens").inc()
    return int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_tokens", 0))


class Session:
    """A single conversation session backed by PostgreSQL."""

    def __init__(
        self,
        session_id: str,
        pool: Any,
        client_id: str,
        agent_id: str,
        model: str = "",
        contact: str = "",
    ) -> None:
        self.id = session_id
        self._pool = pool
        self._client_id = client_id
        self._agent_id = agent_id
        self.messages: list[Message] = []
        self.model = model
        self.contact = contact
        self.created_at = time.time()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.compaction_count = 0
        self.warned_about_compaction = False
        self.consolidation_pending = False
        self.pending_system_warning = ""
        self.trace_id = ""  # Set per-message; included in events
        self._persisted_count = 0  # Messages already in DB; append-only beyond this

    async def load(self) -> bool:
        """Load session state and messages from Postgres. Return True if loaded."""
        row = await self._pool.fetchrow(
            "SELECT * FROM sessions.sessions WHERE id = $1",
            self.id,
        )
        if not row:
            return False

        self.model = row["model"]
        self.contact = row["contact"]
        self.created_at = row["created_at"].timestamp()
        self.total_input_tokens = row["total_input_tokens"]
        self.total_output_tokens = row["total_output_tokens"]
        self.compaction_count = row["compaction_count"]
        self.warned_about_compaction = row["warned_about_compaction"]
        self.pending_system_warning = row["pending_system_warning"]

        # Load messages ordered by ordinal
        msg_rows = await self._pool.fetch(
            "SELECT content FROM sessions.messages "
            "WHERE session_id = $1 ORDER BY ordinal",
            self.id,
        )
        self.messages = [json.loads(r["content"]) for r in msg_rows]
        self._persisted_count = len(self.messages)
        _validate_turn_structure(self.messages)

        log.info("Resumed session %s (%d messages)", self.id, len(self.messages))
        return True

    async def _save_session_meta(self, conn: Any) -> None:
        """Update session metadata row."""
        await conn.execute(
            """UPDATE sessions.sessions SET
               model = $2, updated_at = now(),
               total_input_tokens = $3, total_output_tokens = $4,
               compaction_count = $5, warned_about_compaction = $6,
               pending_system_warning = $7
               WHERE id = $1""",
            self.id, self.model,
            self.total_input_tokens, self.total_output_tokens,
            self.compaction_count, self.warned_about_compaction,
            self.pending_system_warning,
        )

    async def save_state(self) -> None:
        """Persist session state + append new messages to Postgres.

        Only inserts messages beyond ``_persisted_count`` — existing
        rows keep their original ``created_at`` timestamps.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._save_session_meta(conn)
                for i in range(self._persisted_count, len(self.messages)):
                    msg = self.messages[i]
                    content = {k: v for k, v in msg.items() if not k.startswith("_")}
                    await conn.execute(
                        """INSERT INTO sessions.messages
                           (client_id, agent_id, session_id, role, content, ordinal)
                           VALUES ($1, $2, $3, $4, $5::jsonb, $6)""",
                        self._client_id, self._agent_id, self.id,
                        msg["role"], json.dumps(content, ensure_ascii=False), i,
                    )
                self._persisted_count = len(self.messages)

    async def replace_all_messages(self) -> None:
        """Delete and re-insert all messages atomically.

        Used by compaction which rewrites the entire message list.
        Normal message flow uses ``save_state()`` (append-only).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._save_session_meta(conn)
                await conn.execute(
                    "DELETE FROM sessions.messages WHERE session_id = $1",
                    self.id,
                )
                for i, msg in enumerate(self.messages):
                    content = {k: v for k, v in msg.items() if not k.startswith("_")}
                    await conn.execute(
                        """INSERT INTO sessions.messages
                           (client_id, agent_id, session_id, role, content, ordinal)
                           VALUES ($1, $2, $3, $4, $5::jsonb, $6)""",
                        self._client_id, self._agent_id, self.id,
                        msg["role"], json.dumps(content, ensure_ascii=False), i,
                    )
                self._persisted_count = len(self.messages)

    async def append_event(self, event: dict[str, Any]) -> None:
        """Append event to sessions.events table."""
        event["timestamp"] = time.time()
        if self.trace_id:
            event["trace_id"] = self.trace_id
        await self._pool.execute(
            """INSERT INTO sessions.events
               (client_id, agent_id, session_id, event_type, payload, trace_id)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6)""",
            self._client_id, self._agent_id, self.id,
            event.get("type", "unknown"),
            json.dumps(event, ensure_ascii=False),
            event.get("trace_id"),
        )

    async def add_user_message(self, text: str, sender: str = "", source: str = "") -> None:
        """Add user message to session.

        Raises ``ConsecutiveRoleError`` if the last message is already
        a user message — the caller must ensure role alternation.
        """
        if self.messages and self.messages[-1]["role"] == "user":
            raise ConsecutiveRoleError(
                f"Cannot add user message: last message is already role=user "
                f"(session {self.id}, {len(self.messages)} messages)"
            )
        user_msg: UserMessage = {"role": "user", "content": text}
        self.messages.append(user_msg)
        await self.append_event({
            "type": "message", "role": "user", "content": text,
            "from": sender, "source": source,
        })
        await self.save_state()

    async def add_assistant_message(self, msg: AssistantMessage, persist_only: bool = False) -> None:
        """Add assistant response (from LLMResponse.to_internal_message()).

        When persist_only=True, skip appending to self.messages (use when the
        agentic loop already appended to session.messages in-place).
        """
        if not persist_only:
            self.messages.append(msg)
        usage = msg.get("usage", {})
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        await self.append_event({"type": "message", **msg})
        if not persist_only:
            await self.save_state()

    async def add_tool_results(self, results: list[dict[str, Any]], persist_only: bool = False) -> None:
        """Add tool results to session.

        When persist_only=True, skip appending to self.messages (use when the
        agentic loop already appended to session.messages in-place).
        """
        if not persist_only:
            tr_msg: ToolResultsMessage = {"role": "tool_result", "results": results}
            self.messages.append(tr_msg)
        for r in results:
            await self.append_event({
                "type": "tool_result",
                "tool_use_id": r.get("tool_call_id", ""),
                "content": _text_from_content(r.get("content", ""))[:AUDIT_TRUNCATION_LIMIT],
            })
        if not persist_only:
            await self.save_state()

    @property
    def last_input_tokens(self) -> int:
        """Total context tokens from most recent assistant message."""
        for msg in reversed(self.messages):
            if msg["role"] == "agent":
                return _context_tokens_from_usage(msg.get("usage", {}))
        return 0

    def needs_compaction(self, threshold: int) -> bool:
        """Check if session needs compaction based on token count."""
        return self.last_input_tokens > threshold


class SessionManager:
    """Manages session routing and lifecycle via PostgreSQL."""

    def __init__(
        self,
        pool: Any,
        client_id: str,
        agent_id: str,
        agent_name: str = "Assistant",
    ) -> None:
        self._pool = pool
        self._client_id = client_id
        self._agent_id = agent_id
        self.agent_name = agent_name
        self._sessions: dict[str, Session] = {}
        self._on_close_callbacks: list[Callable[..., Any]] = []

    # ── Public API ───────────────────────────────────────────────

    async def has_session(self, sender: str) -> bool:
        """Check if a sender has an active session."""
        if sender in self._sessions:
            return True
        row = await self._pool.fetchval(
            "SELECT 1 FROM sessions.sessions "
            "WHERE client_id = $1 AND agent_id = $2 AND contact = $3 "
            "AND closed_at IS NULL",
            self._client_id, self._agent_id, sender,
        )
        return row is not None

    async def list_contacts(self) -> list[str]:
        """Return list of contacts with active sessions."""
        rows = await self._pool.fetch(
            "SELECT DISTINCT contact FROM sessions.sessions "
            "WHERE client_id = $1 AND agent_id = $2 AND closed_at IS NULL",
            self._client_id, self._agent_id,
        )
        return [r["contact"] for r in rows]

    def list_sessions(self) -> list[Session]:
        """Return list of currently loaded sessions."""
        return list(self._sessions.values())

    async def session_count(self) -> int:
        """Number of active sessions."""
        val = await self._pool.fetchval(
            "SELECT COUNT(*) FROM sessions.sessions "
            "WHERE client_id = $1 AND agent_id = $2 AND closed_at IS NULL",
            self._client_id, self._agent_id,
        )
        return val or 0

    async def get_index(self) -> dict[str, dict[str, Any]]:
        """Return session index: contact → {session_id, created_at}."""
        rows = await self._pool.fetch(
            "SELECT contact, id, created_at FROM sessions.sessions "
            "WHERE client_id = $1 AND agent_id = $2 AND closed_at IS NULL",
            self._client_id, self._agent_id,
        )
        return {
            r["contact"]: {"session_id": r["id"], "created_at": r["created_at"].timestamp()}
            for r in rows
        }

    def get_loaded(self, contact: str) -> Session | None:
        """Return loaded session for contact, or None if not loaded."""
        return self._sessions.get(contact)

    async def save_state(self, session: Session) -> None:
        """Persist session state to Postgres."""
        await session.save_state()

    async def get_or_create(self, contact: str, model: str = "") -> Session:
        """Get existing session for contact, or create new one."""
        if contact in self._sessions:
            return self._sessions[contact]

        # Check for existing active session in DB
        row = await self._pool.fetchrow(
            "SELECT id FROM sessions.sessions "
            "WHERE client_id = $1 AND agent_id = $2 AND contact = $3 "
            "AND closed_at IS NULL",
            self._client_id, self._agent_id, contact,
        )

        if row:
            session = Session(
                row["id"], self._pool, self._client_id, self._agent_id,
                model=model, contact=contact,
            )
            if await session.load():
                self._sessions[contact] = session
                return session

        # Create new session
        session_id = str(uuid.uuid4())
        session = Session(
            session_id, self._pool, self._client_id, self._agent_id,
            model=model, contact=contact,
        )

        await self._pool.execute(
            """INSERT INTO sessions.sessions
               (id, client_id, agent_id, contact, model)
               VALUES ($1, $2, $3, $4, $5)""",
            session_id, self._client_id, self._agent_id, contact, model,
        )

        await session.append_event({
            "type": "session", "id": session_id, "model": model,
            "contact": contact,
        })

        self._sessions[contact] = session
        log.info("Created session %s for %s", session_id, contact)
        if metrics.ENABLED:
            metrics.SESSION_OPEN_TOTAL.inc()
        return session

    def on_close(self, callback: Callable[..., Any]) -> None:
        """Register a callback for session close.

        Callback signature: async def cb(session) or def cb(session).
        Callbacks fire before closing — messages still accessible.
        """
        self._on_close_callbacks.append(callback)

    async def close_session(self, contact: str) -> bool:
        """Close the session for a contact. Next message starts fresh."""
        session = self._sessions.pop(contact, None)

        # Find session in DB
        row = await self._pool.fetchrow(
            "SELECT id FROM sessions.sessions "
            "WHERE client_id = $1 AND agent_id = $2 AND contact = $3 "
            "AND closed_at IS NULL",
            self._client_id, self._agent_id, contact,
        )
        if not row:
            return False

        # Mark as closed in DB
        await self._pool.execute(
            "UPDATE sessions.sessions SET closed_at = now() WHERE id = $1",
            row["id"],
        )

        # Fire callbacks (consolidation). Session object still valid in memory.
        if session:
            for cb in self._on_close_callbacks:
                try:
                    result = cb(session)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("on_close callback failed")

        return True

    async def close_session_by_id(self, session_id: str) -> bool:
        """Close a session by its UUID."""
        row = await self._pool.fetchrow(
            "SELECT contact FROM sessions.sessions "
            "WHERE id = $1 AND client_id = $2 AND agent_id = $3 "
            "AND closed_at IS NULL",
            session_id, self._client_id, self._agent_id,
        )
        if not row:
            return False
        return await self.close_session(row["contact"])

    async def compact_session(
        self,
        session: Session,
        provider: Any,
        compaction_prompt: str,
        model_name: str = "",
        cost_rates: list[float] | None = None,
        trace_id: str = "",
        *,
        keep_recent_pct: float = 0.33,
        min_messages: int = 4,
        tool_result_max_chars: int = 2000,
        max_tokens: int = 0,
        system_blocks: list[dict[str, str]] | None = None,
        cost: Any = None,
    ) -> None:
        """Compact old messages using a summarization model."""
        metering = None
        converter = None
        provider_name = ""
        currency = "EUR"
        if cost is not None:
            metering = cost.metering
            model_name = cost.model_name
            cost_rates = cost.cost_rates
            provider_name = cost.provider_name
            converter = cost.converter
            currency = cost.currency
        if len(session.messages) < min_messages:
            return

        split_point = int(len(session.messages) * (1 - keep_recent_pct))

        while (split_point < len(session.messages) - 1
               and session.messages[split_point]["role"] == "tool_result"):
            split_point += 1

        old_messages = session.messages[:split_point]
        recent_messages = session.messages[split_point:]

        conversation_text = ""
        for msg in old_messages:
            if msg["role"] == "user":
                text = msg["content"]
                if text:
                    conversation_text += f"user: {text}\n\n"
            elif msg["role"] == "agent":
                text = msg.get("text", "")
                if text:
                    conversation_text += f"assistant: {text}\n\n"
                for tc in msg.get("tool_calls", []):
                    tc_name = tc.get("name", "unknown")
                    tc_args = str(tc.get("arguments", {}))[:tool_result_max_chars]
                    conversation_text += f"assistant [tool_call]: {tc_name}({tc_args})\n\n"
            elif msg["role"] == "tool_result":
                for r in msg["results"]:
                    content = _text_from_content(r.get("content", ""))[:tool_result_max_chars]
                    conversation_text += f"tool_result: {content}\n\n"

        if not conversation_text.strip():
            return

        summary_messages: list[Message] = [
            {"role": "user", "content": (
                f"{compaction_prompt}\n\n---\n\n"
                f"{conversation_text}"
                "--- END OF CONVERSATION ---\n\n"
                "Write ONLY the summary. Do not continue, extend, or "
                "invent conversation turns beyond what appears above."
            )},
        ]
        if system_blocks:
            fmt_system = provider.format_system(system_blocks)
        else:
            fmt_system = provider.format_system([{"text": (
                "You are a conversation summarizer. You receive a conversation "
                "transcript and produce a factual summary. NEVER generate new "
                "dialogue, fake timestamps, or fabricated exchanges. ONLY "
                "summarize content that explicitly appears in the input."
            ), "tier": "stable"}])
        fmt_messages = provider.format_messages(summary_messages)

        try:
            kwargs: dict[str, Any] = {}
            if max_tokens > 0:
                kwargs["max_tokens"] = max_tokens
            response = await provider.complete(fmt_system, fmt_messages, [], **kwargs)
            summary = response.text or ""
        except Exception as e:
            log.error("Compaction failed: %s", e, exc_info=True)
            return

        # Record compaction cost
        if metering and cost_rates and response.usage:
            await metering.record(
                session_id=session.id,
                model=model_name, provider=provider_name,
                usage=response.usage, cost_rates=cost_rates,
                call_type="compaction", trace_id=trace_id,
                converter=converter, currency=currency,
            )

        # Replace old messages with summary + compaction marker
        prefix: list[Message] = [
            {
                "role": "user",
                "content": f"[Previous conversation summary]\n{summary}",
            },
            {
                "role": "user",
                "content": (
                    "[system: This conversation was compacted. The summary above covers "
                    "earlier messages. Some details may be lost. Use memory_search or "
                    "memory_get to find specific information from before compaction.]"
                ),
            },
        ]
        session.messages = prefix + recent_messages

        for msg in session.messages:
            if msg["role"] == "agent":
                msg.pop("usage", None)

        session.compaction_count += 1
        session.warned_about_compaction = False
        await session.replace_all_messages()
        await session.append_event({
            "type": "compaction",
            "summary_tokens": response.usage.output_tokens,
            "removed_messages": len(old_messages),
            "compaction_number": session.compaction_count,
            "summary": summary[:2000],
        })
        log.info("Compacted session %s: %d messages → summary + %d recent",
                 session.id, len(old_messages), len(recent_messages))


# ─── Shared Query Functions ──────────────────────────────────────


async def build_session_info(
    pool: Any,
    client_id: str,
    agent_id: str,
    session_id: str,
    session: Session | None = None,
    max_context_tokens: int = 0,
    metering: Any = None,
) -> dict[str, Any]:
    """Build enriched session info dict. Used by both CLI and HTTP API.

    Returns dict with: session_id, context_tokens, context_pct, cost,
    message_count, compaction_count, event_count.
    """
    info: dict[str, Any] = {"session_id": session_id}

    messages: list[Message] = []
    compaction_count = 0
    if session:
        messages = session.messages
        compaction_count = session.compaction_count
    else:
        row = await pool.fetchrow(
            "SELECT compaction_count FROM sessions.sessions WHERE id = $1",
            session_id,
        )
        if row:
            compaction_count = row["compaction_count"]
        msg_rows = await pool.fetch(
            "SELECT content FROM sessions.messages "
            "WHERE session_id = $1 ORDER BY ordinal",
            session_id,
        )
        messages = [json.loads(r["content"]) for r in msg_rows]

    info["message_count"] = len(messages)
    info["compaction_count"] = compaction_count

    context_tokens = 0
    for msg in reversed(messages):
        if msg["role"] == "agent":
            context_tokens = _context_tokens_from_usage(msg.get("usage", {}))
            break
    info["context_tokens"] = context_tokens
    if context_tokens > 0 and max_context_tokens > 0:
        info["context_pct"] = context_tokens * 100 // max_context_tokens
    else:
        info["context_pct"] = 0

    # Per-session cost
    session_cost = 0.0
    if metering:
        rows = await metering.query(
            "SELECT SUM(cost_eur) AS total FROM metering.costs "
            "WHERE client_id = $1 AND agent_id = $2 AND session_id = $3",
            client_id, agent_id, session_id,
        )
        if rows and rows[0]["total"]:
            session_cost = float(rows[0]["total"])
    info["cost"] = round(session_cost, 6)

    # Event count
    event_count = await pool.fetchval(
        "SELECT COUNT(*) FROM sessions.events WHERE session_id = $1",
        session_id,
    )
    info["event_count"] = event_count or 0

    return info


async def read_history_events(
    pool: Any,
    session_id: str,
    full: bool = False,
) -> list[dict[str, Any]]:
    """Read session history from events table.

    When full=False, returns only message events (user + assistant text).
    When full=True, includes all events.
    Returns chronological list[dict].
    """
    if full:
        rows = await pool.fetch(
            "SELECT payload FROM sessions.events "
            "WHERE session_id = $1 ORDER BY created_at",
            session_id,
        )
        return [json.loads(r["payload"]) for r in rows]

    # Messages only
    rows = await pool.fetch(
        "SELECT payload FROM sessions.events "
        "WHERE session_id = $1 AND event_type = 'message' "
        "ORDER BY created_at",
        session_id,
    )
    events: list[dict[str, Any]] = []
    for r in rows:
        event = json.loads(r["payload"])
        role = event.get("role", "")
        if role == "user":
            events.append({
                "type": "message",
                "role": "user",
                "content": _text_from_content(event.get("content", "")),
                "from": event.get("from", ""),
                "timestamp": event.get("timestamp", 0),
            })
        elif role == "agent":
            events.append({
                "type": "message",
                "role": "agent",
                "text": event.get("text", ""),
                "timestamp": event.get("timestamp", 0),
            })
    return events
