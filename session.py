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

# Truncation limit for tool output in audit trail
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


def _atomic_write(path: Path, data: str) -> None:
    """Write to temp file then rename — atomic on POSIX."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(path)


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

    def _dated_jsonl_path(self) -> Path:
        """JSONL path for today's date."""
        today = time.strftime("%Y-%m-%d")
        return self.dir / f"{self.id}.{today}.jsonl"

    def _migrate_legacy_jsonl(self) -> None:
        """Migrate undated legacy JSONL to dated format (runs once)."""
        legacy = self.dir / f"{self.id}.jsonl"
        if not legacy.exists():
            return
        try:
            with open(legacy, encoding="utf-8") as f:
                first_line = f.readline().strip()
                if not first_line:
                    return
                first = json.loads(first_line)
            ts = first.get("timestamp", time.time())
            start_date = time.strftime("%Y-%m-%d", time.localtime(ts))
            target = self.dir / f"{self.id}.{start_date}.jsonl"
            if not target.exists():
                legacy.rename(target)
            else:
                with open(legacy, encoding="utf-8") as src, \
                     open(target, "a", encoding="utf-8") as dst:
                    dst.write(src.read())
                legacy.unlink()
            log.info("Migrated legacy JSONL to %s", target.name)
        except Exception as e:
            log.warning("Legacy JSONL migration failed for %s: %s", self.id, e)

    def load(self) -> bool:
        """Load from state file if it exists, return True if loaded."""
        self._migrate_legacy_jsonl()
        if self.state_path.exists():
            try:
                with open(self.state_path, encoding="utf-8") as f:
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
                log.warning("Corrupt state file for %s, rebuilding: %s", self.id, e)
                return self._rebuild_from_jsonl()
        return False

    def _rebuild_from_jsonl(self) -> bool:
        """Rebuild messages from JSONL audit trail (legacy + dated chunks)."""
        legacy = self.dir / f"{self.id}.jsonl"
        dated = sorted(self.dir.glob(f"{self.id}.????-??-??.jsonl"))
        chunks = ([legacy] if legacy.exists() else []) + dated
        if not chunks:
            return False
        self.messages = []
        try:
            for chunk in chunks:
                with open(chunk, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        event = json.loads(line)
                        etype = event.get("type", "")
                        if etype == "message":
                            self.messages.append(event)
                            if event.get("role") == "assistant":
                                usage = event.get("usage", {})
                                self.total_input_tokens += usage.get("input_tokens", 0)
                                self.total_output_tokens += usage.get("output_tokens", 0)
                        elif etype == "compaction":
                            self.compaction_count += 1
                            summary = event.get("summary", "")
                            if summary:
                                self.messages = [
                                    {"role": "user", "content": f"[Previous conversation summary]\n{summary}"}
                                ]
            log.info("Rebuilt session %s from JSONL (%d chunks, %d messages)",
                     self.id, len(chunks), len(self.messages))
            return True
        except Exception as e:
            log.error("Failed to rebuild session %s: %s", self.id, e)
            return False

    def _save_state(self) -> None:
        """Atomically save current state."""
        state = {
            "id": self.id,
            "model": self.model,
            "contact": self.contact,
            "messages": self.messages,
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
        path = self._dated_jsonl_path()
        with open(path, "a", encoding="utf-8") as f:
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
        self._save_state()

    def add_assistant_message(self, msg: dict) -> None:
        """Add assistant response (from LLMResponse.to_internal_message())."""
        self.messages.append(msg)
        usage = msg.get("usage", {})
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.append_event({"type": "message", **msg})
        self._save_state()

    def add_tool_results(self, results: list[dict]) -> None:
        """Add tool results to session."""
        msg = {"role": "tool_results", "results": results}
        self.messages.append(msg)
        for r in results:
            self.append_event({
                "type": "tool_result",
                "tool_use_id": r.get("tool_call_id", ""),
                "content": _text_from_content(r.get("content", ""))[:AUDIT_TRUNCATION_LIMIT],
            })
        self._save_state()

    def persist_assistant_message(self, msg: dict) -> None:
        """Persist assistant message to JSONL + update tokens (no append to messages list).

        Use when the agentic loop already appended to session.messages in-place.
        """
        usage = msg.get("usage", {})
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.append_event({"type": "message", **msg})

    def persist_tool_results(self, results: list[dict]) -> None:
        """Persist tool results to JSONL (no append to messages list).

        Use when the agentic loop already appended to session.messages in-place.
        """
        for r in results:
            self.append_event({
                "type": "tool_result",
                "tool_use_id": r.get("tool_call_id", ""),
                "content": _text_from_content(r.get("content", ""))[:AUDIT_TRUNCATION_LIMIT],
            })

    @property
    def last_input_tokens(self) -> int:
        """Input tokens from most recent assistant message."""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                return msg.get("usage", {}).get("input_tokens", 0)
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
                with open(self.index_path, encoding="utf-8") as f:
                    self._index = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._index = {}

    def _save_index(self) -> None:
        """Save session index."""
        _atomic_write(self.index_path, json.dumps(self._index, ensure_ascii=False, indent=2))

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
        # Fire callbacks before archiving (session still accessible)
        session = self._sessions.get(contact)
        if session:
            for cb in self._on_close_callbacks:
                try:
                    result = cb(session)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("on_close callback failed")

        # Clear memory cache
        if contact in self._sessions:
            del self._sessions[contact]

        entry = self._index.get(contact)
        if not entry:
            return False

        session_id = entry["session_id"]

        # Archive session files (don't delete — move to .archive/)
        archive = self.dir / ".archive"
        archive.mkdir(exist_ok=True)
        for f in self.dir.glob(f"{session_id}*"):
            f.rename(archive / f.name)

        # Remove from index
        del self._index[contact]
        self._save_index()
        return True

    async def close_session_by_id(self, session_id: str) -> bool:
        """Close a session by its UUID (linear scan over index)."""
        for contact, entry in self._index.items():
            if entry.get("session_id") == session_id:
                return await self.close_session(contact)
        return False

    def build_recall(self, contact: str, count: int = 20) -> str:
        """Build recall text from the most recent archived session for a contact.

        Returns formatted conversation excerpt, or empty string if none found.
        """
        archive = self.dir / ".archive"
        if not archive.exists():
            return ""

        # Find archived state files for this contact
        best_file = None
        best_mtime = 0.0
        for state_file in archive.glob("*.state.json"):
            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                file_contact = state.get("contact")
                # Fallback: check JSONL session event if state lacks contact
                if not file_contact:
                    session_id = state.get("id", "")
                    jsonl_files = sorted(archive.glob(f"{session_id}.*.jsonl"))
                    for jf in jsonl_files[:1]:
                        try:
                            first_line = jf.open("r", encoding="utf-8").readline()
                            event = json.loads(first_line)
                            file_contact = event.get("contact", "")
                        except Exception:  # noqa: S110 — session discovery; skip unreadable JSONL files
                            pass
                if file_contact != contact:
                    continue
                mtime = state_file.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_file = state_file
            except (json.JSONDecodeError, OSError):
                continue

        if not best_file:
            return ""

        try:
            with open(best_file, encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return ""

        messages = state.get("messages", [])
        if not messages:
            return ""

        # Filter to user + assistant messages only
        conversation = [m for m in messages if m.get("role") in ("user", "assistant")]
        tail = conversation[-count:]
        if not tail:
            return ""

        lines = []
        for msg in tail:
            role = msg["role"]
            if role == "user":
                content = _text_from_content(msg.get("content", ""))
                # Strip timestamp prefix
                if content.startswith("[") and "]\n" in content[:60]:
                    content = content[content.index("]\n") + 2:]
                lines.append(f"**{contact}:** {content}")
            elif role == "assistant":
                text = msg.get("text", "")
                if text:
                    lines.append(f"**{self.agent_name}:** {text}")

        if not lines:
            return ""

        return "Session recall (last conversation):\n\n" + "\n\n".join(lines)

    def create_subagent_session(self, parent_id: str, model: str = "") -> Session:
        """Create a one-off sub-agent session."""
        session_id = f"sub-{uuid.uuid4()}"
        session = Session(session_id, self.dir, model=model)
        session.append_event({
            "type": "session", "id": session_id, "model": model,
            "parent_session": parent_id,
        })
        return session

    async def compact_session(
        self,
        session: Session,
        provider: Any,
        compaction_prompt: str,
    ) -> None:
        """Compact old messages using a summarization model."""
        if len(session.messages) < 4:
            return

        # Take oldest 2/3 of messages
        split_point = len(session.messages) * 2 // 3
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
                tc_args = str(tc.get("arguments", {}))[:2000]
                conversation_text += f"assistant [tool_call]: {tc_name}({tc_args})\n\n"
            # Include tool results in compaction context
            if role == "tool_results":
                for r in msg.get("results", []):
                    content = _text_from_content(r.get("content", ""))[:2000]
                    conversation_text += f"tool_result: {content}\n\n"

        if not conversation_text.strip():
            return

        summary_messages = [
            {"role": "user", "content": f"{compaction_prompt}\n\n---\n\n{conversation_text}"}
        ]
        fmt_system = provider.format_system([{"text": "You are a conversation summarizer.", "tier": "stable"}])
        fmt_messages = provider.format_messages(summary_messages)

        try:
            response = await provider.complete(fmt_system, fmt_messages, [])
            summary = response.text or ""
        except Exception as e:
            log.error("Compaction failed: %s", e)
            return

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

        session.compaction_count += 1
        session.warned_about_compaction = False
        session._save_state()
        session.append_event({
            "type": "compaction",
            "summary_tokens": response.usage.output_tokens,
            "removed_messages": len(old_messages),
            "compaction_number": session.compaction_count,
            "summary": summary[:2000],
        })
        log.info("Compacted session %s: %d messages → summary + %d recent",
                 session.id, len(old_messages), len(recent_messages))
