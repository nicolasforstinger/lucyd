"""Session manager — persistence, routing, and compaction.

Dual storage: JSONL audit trail (append-only) + state file (atomic snapshots).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


AUDIT_TRUNCATION_LIMIT = 500


def _text_from_content(content: Any) -> str:
    """Extract text from string or content block list.

    Handles plain strings, the neutral content block format
    [{"type": "text", "text": "..."}, {"type": "image", ...}],
    and edge cases (None, empty list).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _validate_turn_structure(messages: list[dict]) -> None:
    """Fix orphaned tool_calls or tool_results in a message list (in-place).

    - Assistant messages with tool_calls that are not followed by a
      tool_results message have their tool_calls removed.
    - tool_results messages not preceded by an assistant with tool_calls
      are removed entirely.
    """
    i = len(messages) - 1
    while i >= 0:
        msg = messages[i]
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            # Check if followed by tool_results (possibly with user hints between)
            has_results = False
            for j in range(i + 1, len(messages)):
                jr = messages[j].get("role", "")
                if jr == "tool_results":
                    has_results = True
                    break
                if jr == "assistant":
                    break  # next assistant turn — no results for this one
            if not has_results:
                msg.pop("tool_calls", None)
                log.warning("Stripped orphaned tool_calls from assistant at index %d", i)
        elif role == "tool_results":
            # Check if the immediately preceding assistant (skipping user
            # hints) has tool_calls.  Stop at any assistant or tool_results
            # — those are turn boundaries that break the pairing.
            has_call = False
            for j in range(i - 1, -1, -1):
                jr = messages[j].get("role", "")
                if jr == "assistant":
                    if messages[j].get("tool_calls"):
                        has_call = True
                    break  # nearest assistant — stop regardless
                if jr == "tool_results":
                    break  # previous tool_results — no call for this one
            if not has_call:
                messages.pop(i)
                log.warning("Removed orphaned tool_results at index %d", i)
        i -= 1


def _atomic_write(path: Path, data: str) -> None:
    """Write to temp file then rename — atomic on POSIX."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(path)


def _context_tokens_from_usage(usage: dict) -> int:
    """Extract context token count from a usage dict.

    Uses the normalized ``context_tokens`` field when present.
    Falls back to ``input_tokens + cache_read_tokens`` for messages
    stored before the field was added.
    """
    if "context_tokens" in usage:
        return usage["context_tokens"]
    return usage.get("input_tokens", 0) + usage.get("cache_read_tokens", 0)


class Session:
    """A single conversation session with dual storage."""

    def __init__(self, session_id: str, sessions_dir: Path, model: str = "",
                 contact: str = ""):
        self.id = session_id
        self.dir = sessions_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.dir / f"{session_id}.jsonl"
        self.state_path = self.dir / f"{session_id}.state.json"
        self.messages: list[dict] = []
        self.model = model
        self.contact = contact
        self.created_at = time.time()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.compaction_count = 0
        self.warned_about_compaction = False
        self.pending_system_warning = ""
        self.trace_id = ""  # Set per-message by _process_message; included in JSONL events

    def _dated_jsonl_path(self) -> Path:
        """JSONL path for today's date."""
        today = time.strftime("%Y-%m-%d")
        return self.dir / f"{self.id}.{today}.jsonl"

    def load(self) -> bool:
        """Load from state file if it exists, return True if loaded."""
        if not self.state_path.exists():
            return False
        try:
            with self.state_path.open(encoding="utf-8") as f:
                state = json.load(f)
            self.messages = state.get("messages", [])
            self.model = state.get("model", self.model)
            self.contact = state.get("contact", self.contact)
            self.created_at = state.get("created_at", self.created_at)
            self.total_input_tokens = state.get("total_input_tokens", 0)
            self.total_output_tokens = state.get("total_output_tokens", 0)
            self.compaction_count = state.get("compaction_count", 0)
            self.warned_about_compaction = state.get("warned_about_compaction", False)
            self.pending_system_warning = state.get("pending_system_warning", "")
            log.info("Resumed session %s (%d messages)", self.id, len(self.messages))
            return True
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Corrupt state file for %s, starting fresh: %s", self.id, e)
            return False

    def save_state(self) -> None:
        """Atomically save current state."""
        state = {
            "id": self.id,
            "model": self.model,
            "contact": self.contact,
            "messages": [
                {k: v for k, v in m.items() if not k.startswith("_")}
                for m in self.messages
            ],
            "created_at": self.created_at,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "compaction_count": self.compaction_count,
            "warned_about_compaction": self.warned_about_compaction,
            "pending_system_warning": self.pending_system_warning,
            "updated_at": time.time(),
        }
        _atomic_write(self.state_path, json.dumps(state, ensure_ascii=False))

    def append_event(self, event: dict) -> None:
        """Append event to dated JSONL audit trail with fsync."""
        event["timestamp"] = time.time()
        if self.trace_id:
            event["trace_id"] = self.trace_id
        path = self._dated_jsonl_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def add_user_message(self, text: str, sender: str = "", source: str = "") -> None:
        """Add user message to session."""
        msg = {"role": "user", "content": text}
        self.messages.append(msg)
        self.append_event({
            "type": "message", "role": "user", "content": text,
            "from": sender, "source": source,
        })
        self.save_state()

    def add_assistant_message(self, msg: dict, persist_only: bool = False) -> None:
        """Add assistant response (from LLMResponse.to_internal_message()).

        When persist_only=True, skip appending to self.messages (use when the
        agentic loop already appended to session.messages in-place).
        """
        if not persist_only:
            self.messages.append(msg)
        usage = msg.get("usage", {})
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.append_event({"type": "message", **msg})
        if not persist_only:
            self.save_state()

    def add_tool_results(self, results: list[dict], persist_only: bool = False) -> None:
        """Add tool results to session.

        When persist_only=True, skip appending to self.messages (use when the
        agentic loop already appended to session.messages in-place).
        """
        if not persist_only:
            msg = {"role": "tool_results", "results": results}
            self.messages.append(msg)
        for r in results:
            self.append_event({
                "type": "tool_result",
                "tool_use_id": r.get("tool_call_id", ""),
                "content": _text_from_content(r.get("content", ""))[:AUDIT_TRUNCATION_LIMIT],
            })
        if not persist_only:
            self.save_state()

    @property
    def last_input_tokens(self) -> int:
        """Total context tokens from most recent assistant message."""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                return _context_tokens_from_usage(msg.get("usage", {}))
        return 0

    def needs_compaction(self, threshold: int) -> bool:
        """Check if session needs compaction based on token count."""
        return self.last_input_tokens > threshold


class SessionManager:
    """Manages session routing and lifecycle."""

    def __init__(self, sessions_dir: Path, agent_name: str = "Assistant"):
        self.dir = sessions_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "sessions.json"
        self.agent_name = agent_name
        self._index: dict[str, dict] = {}
        self._sessions: dict[str, Session] = {}
        self._on_close_callbacks: list = []
        self._load_index()

    def _load_index(self) -> None:
        """Load session index mapping contacts to session IDs."""
        if self.index_path.exists():
            try:
                with self.index_path.open(encoding="utf-8") as f:
                    self._index = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._index = {}

    def _save_index(self) -> None:
        """Save session index."""
        _atomic_write(self.index_path, json.dumps(self._index, ensure_ascii=False, indent=2))

    # ── Public API (used by daemon instead of accessing _sessions/_index) ──

    def has_session(self, sender: str) -> bool:
        """Check if a sender has an active or indexed session."""
        return sender in self._sessions or sender in self._index

    def list_contacts(self) -> list[str]:
        """Return list of contacts with indexed sessions."""
        return list(self._index.keys())

    def list_sessions(self) -> list[Session]:
        """Return list of currently loaded sessions."""
        return list(self._sessions.values())

    def session_count(self) -> int:
        """Number of indexed sessions."""
        return len(self._index)

    def get_index(self) -> dict[str, dict]:
        """Return a copy of the session index."""
        return dict(self._index)

    def get_loaded(self, contact: str) -> Session | None:
        """Return loaded session for contact, or None if not loaded."""
        return self._sessions.get(contact)

    def save_state(self, session: Session) -> None:
        """Persist session state to disk."""
        session.save_state()

    def get_or_create(self, contact: str, model: str = "") -> Session:
        """Get existing session for contact, or create new one."""
        if contact in self._sessions:
            return self._sessions[contact]

        entry = self._index.get(contact, {})
        session_id = entry.get("session_id", "")

        if session_id:
            session = Session(session_id, self.dir, model=model, contact=contact)
            if session.load():
                self._sessions[contact] = session
                return session

        # Create new session
        session_id = str(uuid.uuid4())
        session = Session(session_id, self.dir, model=model, contact=contact)
        session.append_event({
            "type": "session", "id": session_id, "model": model,
            "contact": contact,
        })
        self._index[contact] = {
            "session_id": session_id,
            "created_at": time.time(),
        }
        self._save_index()
        self._sessions[contact] = session
        log.info("Created session %s for %s", session_id, contact)
        return session

    def on_close(self, callback: Callable) -> None:
        """Register a callback for session close.

        Callback signature: async def cb(session) or def cb(session).
        Callbacks fire before archiving — messages still accessible.
        """
        self._on_close_callbacks.append(callback)

    async def close_session(self, contact: str) -> bool:
        """Close and archive the session for a contact. Next message starts fresh."""
        session = self._sessions.pop(contact, None)

        entry = self._index.pop(contact, None)
        if not entry:
            return False

        session_id = entry["session_id"]

        # Update index FIRST — session disappears from --sessions immediately,
        # before slow callbacks (consolidation LLM calls) run.
        self._save_index()

        # Fire callbacks (consolidation). Session object still valid in memory.
        if session:
            for cb in self._on_close_callbacks:
                try:
                    result = cb(session)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("on_close callback failed")

        # Archive session files (don't delete — move to .archive/)
        archive = self.dir / ".archive"
        archive.mkdir(exist_ok=True)
        for f in self.dir.glob(f"{session_id}*"):
            f.rename(archive / f.name)

        return True

    async def close_session_by_id(self, session_id: str) -> bool:
        """Close a session by its UUID (linear scan over index)."""
        for contact, entry in self._index.items():
            if entry.get("session_id") == session_id:
                return await self.close_session(contact)
        return False

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
        system_blocks: list[dict] | None = None,
        cost: Any = None,
    ) -> None:
        """Compact old messages using a summarization model."""
        metering = None
        if cost is not None:
            metering = cost.metering
            model_name = cost.model_name
            cost_rates = cost.cost_rates
            provider_name = getattr(cost, "provider_name", "")
        if len(session.messages) < min_messages:
            return

        # Summarize older messages, keep newest fraction verbatim
        split_point = int(len(session.messages) * (1 - keep_recent_pct))

        # Ensure split doesn't orphan tool_results — each tool_result
        # needs a matching tool_use in the preceding assistant message.
        # After compaction, the preceding message is a user (marker), so
        # any tool_results at the boundary would break the API contract.
        while (split_point < len(session.messages) - 1
               and session.messages[split_point].get("role") == "tool_results"):
            split_point += 1

        old_messages = session.messages[:split_point]
        recent_messages = session.messages[split_point:]

        # Build summary prompt — include tool calls and results for context
        conversation_text = ""
        for msg in old_messages:
            role = msg.get("role", "")
            text = _text_from_content(msg.get("content", msg.get("text", "")))
            if role and text:
                conversation_text += f"{role}: {text}\n\n"
            # Include tool calls in compaction context
            for tc in msg.get("tool_calls", []):
                tc_name = tc.get("name", "unknown")
                tc_args = str(tc.get("arguments", {}))[:tool_result_max_chars]
                conversation_text += f"assistant [tool_call]: {tc_name}({tc_args})\n\n"
            # Include tool results in compaction context
            if role == "tool_results":
                for r in msg.get("results", []):
                    content = _text_from_content(r.get("content", ""))[:tool_result_max_chars]
                    conversation_text += f"tool_result: {content}\n\n"

        if not conversation_text.strip():
            return

        summary_messages = [
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
            kwargs = {}
            if max_tokens > 0:
                kwargs["max_tokens"] = max_tokens
            response = await provider.complete(fmt_system, fmt_messages, [], **kwargs)
            summary = response.text or ""
        except Exception as e:
            log.error("Compaction failed: %s", e, exc_info=True)
            return

        # Record compaction cost
        if metering and cost_rates and response.usage:
            metering.record(
                session_id=session.id,
                model=model_name, provider=provider_name,
                usage=response.usage, cost_rates=cost_rates,
                call_type="compaction", trace_id=trace_id,
            )

        # Replace old messages with summary + compaction marker
        summary_msg = {
            "role": "user",
            "content": f"[Previous conversation summary]\n{summary}",
        }
        compaction_marker = {
            "role": "user",
            "content": (
                "[system: This conversation was compacted. The summary above covers "
                "earlier messages. Some details may be lost. Use memory_search or "
                "memory_get to find specific information from before compaction.]"
            ),
        }
        session.messages = [summary_msg, compaction_marker] + recent_messages

        # Invalidate stale usage — context_tokens no longer reflects
        # post-compaction state.  Accurate stats resume on next API call.
        for msg in session.messages:
            if msg.get("role") == "assistant":
                msg.pop("usage", None)

        session.compaction_count += 1
        session.warned_about_compaction = False
        session.save_state()
        session.append_event({
            "type": "compaction",
            "summary_tokens": response.usage.output_tokens,
            "removed_messages": len(old_messages),
            "compaction_number": session.compaction_count,
            "summary": summary[:2000],
        })
        log.info("Compacted session %s: %d messages → summary + %d recent",
                 session.id, len(old_messages), len(recent_messages))


# ─── Shared Query Functions ──────────────────────────────────────


def build_session_info(
    sessions_dir: Path,
    session_id: str,
    session: Session | None = None,
    max_context_tokens: int = 0,
    metering=None,
) -> dict:
    """Build enriched session info dict. Used by both CLI and HTTP API.

    Returns dict with: session_id, context_tokens, context_pct, cost,
    message_count, compaction_count, log_files, log_bytes.
    """
    info: dict[str, Any] = {"session_id": session_id}

    # Load from live session or state file
    messages: list[dict] = []
    compaction_count = 0
    if session:
        messages = session.messages
        compaction_count = session.compaction_count
    else:
        state_path = sessions_dir / f"{session_id}.state.json"
        if state_path.exists():
            try:
                with state_path.open(encoding="utf-8") as f:
                    state = json.load(f)
                messages = state.get("messages", [])
                compaction_count = state.get("compaction_count", 0)
            except (json.JSONDecodeError, OSError):
                pass

    info["message_count"] = len(messages)
    info["compaction_count"] = compaction_count

    # Context tokens from last assistant message (normalized by provider)
    context_tokens = 0
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
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
        rows = metering.query(
            "SELECT SUM(cost) FROM costs WHERE session_id = ?",
            (session_id,),
        )
        if rows and rows[0][0]:
            session_cost = rows[0][0]
    info["cost"] = round(session_cost, 6)

    # Log file metadata
    log_files = sorted(sessions_dir.glob(f"{session_id}.????-??-??.jsonl"))
    info["log_files"] = len(log_files)
    info["log_bytes"] = sum(f.stat().st_size for f in log_files)

    return info


def read_history_events(
    sessions_dir: Path,
    session_id: str,
    full: bool = False,
) -> list[dict]:
    """Read session history from JSONL files.

    Globs active + archive directories. Deduplicates by timestamp.
    When full=False, returns only message events (user + assistant text).
    When full=True, includes tool calls/results and session metadata.
    Returns chronological list[dict].
    """
    archive_dir = sessions_dir / ".archive"
    all_files: list[Path] = []

    # Active session logs
    all_files.extend(sorted(sessions_dir.glob(f"{session_id}.????-??-??.jsonl")))
    # Archived session logs
    if archive_dir.exists():
        all_files.extend(sorted(archive_dir.glob(f"{session_id}.????-??-??.jsonl")))

    if not all_files:
        return []

    seen_ts: set[float] = set()
    events: list[dict] = []

    for path in all_files:
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Deduplicate by timestamp
                    ts = event.get("timestamp", 0)
                    if ts and ts in seen_ts:
                        continue
                    if ts:
                        seen_ts.add(ts)

                    if full:
                        events.append(event)
                    else:
                        etype = event.get("type", "")
                        if etype == "message":
                            role = event.get("role", "")
                            if role == "user":
                                events.append({
                                    "type": "message",
                                    "role": "user",
                                    "content": _text_from_content(
                                        event.get("content", ""),
                                    ),
                                    "from": event.get("from", ""),
                                    "timestamp": ts,
                                })
                            elif role == "assistant":
                                events.append({
                                    "type": "message",
                                    "role": "assistant",
                                    "text": event.get("text", ""),
                                    "timestamp": ts,
                                })
        except OSError:
            continue

    # Sort chronologically
    events.sort(key=lambda e: e.get("timestamp", 0))
    return events
