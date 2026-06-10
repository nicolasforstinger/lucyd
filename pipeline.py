"""Message processing pipeline for the Lucyd daemon.

Owns the complete message flow: preprocessing → session setup →
context building → agentic loop dispatch → response finalization.
All runtime dependencies are injected explicitly via the constructor.

Public interface:
    process_message()    — entry point called by the daemon's event loop
    get_session_lock()   — per-sender lock for session mutation safety
    monitor_state        — live agentic loop state (read by /api/v1/monitor)
    error_counts         — error type counters (read by /api/v1/status)
    current_session      — session being processed (read by session_status tool)
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import metrics
import operations
from agentic import LoopConfig, run_agentic_loop, run_single_shot
from attachments import ImageTooLarge, extract_document_text, fit_image
from config import Config, EPHEMERAL_TALKERS, Talker
from context import ContextBuilder, _estimate_tokens
from guardrails import GuardrailTripped, Guardrails
from log_utils import _log_safe, redact_content, set_log_context
from metering import MeteringDB
from memory import MemoryInterface, inject_recall, recall
from plugins import PluginError, PreprocessorSpec
from messages import Message, ToolResultsMessage
from providers import CostContext, LLMProvider, LLMResponse, StreamDelta, SystemPrompt
from session import ConsecutiveRoleError, Session, SessionManager
from skills import SkillLoader
from tools import ToolRegistry

if TYPE_CHECKING:
    from conversion import CurrencyConverter
    from lucyd import PriorityMessageQueue

log = logging.getLogger("lucyd")


# ─── Helpers ─────────────────────────────────────────────────────


def _should_warn_context(
    input_tokens: int,
    compaction_threshold: int,
    needs_compaction: bool,
    already_warned: bool,
    warning_pct: float = 0.8,
) -> bool:
    """Decide whether to set a compaction warning on the session.

    Warns at 80% of compaction threshold, but only if not already
    at hard threshold and not already warned this session.
    """
    warning_threshold = int(compaction_threshold * warning_pct)
    return (
        input_tokens > warning_threshold
        and not needs_compaction
        and not already_warned
    )


def _inject_warning(text: str, warning: str) -> tuple[str, bool]:
    """Prepend pending system warning to user text.

    Returns (modified_text, was_warning_consumed).
    """
    if warning:
        return f"[system: {warning}]\n\n{text}", True
    return text, False


# Max chars of cleaned semantic text kept per recent-exchange line in the
# situational brief. The budget applies to stripped content, not raw
# JSON + metadata overhead.
_BRIEF_SNIPPET_CHARS = 400
# Leading timestamp header the pipeline prepends to every user turn,
# e.g. "[Thu, 21. May 2026 - 21:33 UTC]\n".
_TS_PREFIX_RE = re.compile(r"^\[[^\]]*\]\n")
# Attachment metadata prefix that precedes real content, e.g.
# "[voice message, saved: /tmp/...oga]: " (see plugins.py transcription wrap).
# The "saved:" + closing "]:" discriminates it from ordinary bracketed text.
_ATTACHMENT_PREFIX_RE = re.compile(r"^\[[^\]]*saved:[^\]]*\]:\s*")
# Collapse runs of whitespace to single spaces (was done in SQL before).
_WS_RE = re.compile(r"\s+")


def _brief_snippet(role: str, content_json: str) -> str:
    """Extract a clean, budgeted text snippet from a stored message.

    ``content_json`` is the raw jsonb string as asyncpg returns it (the
    storage layer round-trips message rows with ``json.loads``; jsonb is not
    auto-decoded). User messages keep their spoken/typed text (``content``),
    stripped of the leading ``[timestamp]`` header and any ``[…saved: /path]:``
    attachment prefix so a load-bearing clause isn't crowded out by metadata.
    Agent messages prefer ``text``, fall back to a thinking excerpt, then to a
    ``[tool call: …]`` marker for tool-only turns — never an empty line.
    """
    content: dict[str, Any] = json.loads(content_json)
    if role == "user":
        raw = str(content.get("content", ""))
        raw = _ATTACHMENT_PREFIX_RE.sub("", _TS_PREFIX_RE.sub("", raw))
        text = _WS_RE.sub(" ", raw).strip()
        return text[:_BRIEF_SNIPPET_CHARS] or "[attachment]"
    text = str(content.get("text", "")).strip()
    if not text:
        thinking = str(content.get("thinking", "")).strip()
        if thinking:
            text = f"(thinking) {thinking}"
        else:
            names = [
                str(tc.get("name", "?"))
                for tc in content.get("tool_calls", [])
                if isinstance(tc, dict)
            ]
            text = f"[tool call: {', '.join(names)}]" if names else "[no reply]"
    return _WS_RE.sub(" ", text)[:_BRIEF_SNIPPET_CHARS]


def _time_of_day_steer(now_local: _dt.datetime) -> str:
    """One line of time-of-day awareness for a fired turn.

    Sleeping hours (22:00–08:00 local) get an explicit don't-disturb steer so
    a scheduled job never blasts the user at 4am the way the absolute-time
    fix alone couldn't prevent (a job set for a bad hour still fires).
    """
    line = f"It is {now_local:%H:%M %A} the user's local time."
    if now_local.hour >= 22 or now_local.hour < 8:
        line += (
            " That is his sleeping window (22:00–08:00) — do NOT send a "
            "proactive message now unless it is genuinely urgent; hold it "
            "(reschedule for daytime) or NO_REPLY."
        )
    return line


async def _recent_user_context(
    pool: asyncpg.Pool, user_session_key: str, user_tz: str = "UTC",
    max_msgs: int = 6,
) -> str:
    """Situational brief for a fired reminder / scheduled-task turn.

    A fired ``agent:self`` turn runs in its own ephemeral session and is
    blind to the live user conversation. This surfaces the user's local
    time-of-day (so it won't fire at 4am), recency, and a clipped tail of
    the most recent exchange so the turn weaves its delivery into what's
    actually happening instead of firing context-blind.
    """
    try:
        tz: _dt.tzinfo = ZoneInfo(user_tz)
    except (ZoneInfoNotFoundError, ValueError):
        tz = _dt.timezone.utc
    time_line = _time_of_day_steer(_dt.datetime.now(tz))

    rows = await pool.fetch(
        """SELECT m.role, m.content,
                  EXTRACT(EPOCH FROM m.created_at) AS ts
             FROM sessions.messages m
             JOIN sessions.sessions s ON s.id = m.session_id
            WHERE s.contact = $1 AND m.role IN ('user', 'agent')
            ORDER BY m.created_at DESC
            LIMIT $2""",
        user_session_key, max_msgs,
    )
    if not rows:
        return (
            f"{time_line}\n"
            "No recent conversation with the user — if you send, a clean "
            "standalone."
        )
    age_min = int((time.time() - float(rows[0]["ts"])) / 60)
    tail = "\n".join(
        f"  {r['role']}: {_brief_snippet(r['role'], r['content'])}"
        for r in reversed(rows)
    )
    return (
        f"{time_line}\n"
        f"Last user activity {age_min} min ago. Recent exchange (oldest→newest):\n"
        f"{tail}\n"
        f"Weave this turn's message into that conversation if it's still live; "
        f"a clean standalone if it's gone quiet. Never send a context-blind "
        f"canned message."
    )


def _history_tokens(messages: list[Message]) -> int:
    """Estimate tokens in the conversation history the provider receives.

    Counts every message body that gets serialized into the request: user
    content, agent text + tool-call arguments, and tool_result content. The
    budget math previously counted only user content + agent text, so a turn
    carrying large tool outputs (web fetches, file reads) was undercounted and
    emergency compaction fired late. Still an estimate — role/format overhead
    and thinking blocks are not modelled — but no longer blind to tool traffic.
    """
    total = 0
    for m in messages:
        if m["role"] == "user":
            total += _estimate_tokens(m["content"])
        elif m["role"] == "agent":
            total += _estimate_tokens(m.get("text", ""))
            for tc in m.get("tool_calls", []):
                total += _estimate_tokens(str(tc.get("arguments", "")))
        elif m["role"] == "tool_result":
            for r in m["results"]:
                total += _estimate_tokens(r["content"])
    return total


def _append(text: str, suffix: str) -> str:
    """Append suffix to text with newline separator."""
    return f"{text}\n{suffix}" if text else suffix


def _is_silent(text: str, tokens: list[str]) -> bool:
    """Check if reply starts or ends with a silent token.

    Tokens should be word-character strings (letters, digits, underscores).
    """
    if not text or not tokens:
        return False
    text = text.strip()
    for token in tokens:
        # Starts with token
        if re.match(rf"^\s*{re.escape(token)}(?=$|\W)", text):
            return True
        # Ends with token
        if re.search(rf"\b{re.escape(token)}\b\W*$", text):
            return True
    return False


# ─── Monitor Writer ──────────────────────────────────────────────


class _MonitorWriter:
    """In-memory monitor state tracker for the /api/v1/monitor endpoint.

    Updates a shared dict on the pipeline instead of writing JSON to disk.
    """

    __slots__ = ("_state", "_contact", "_session_id", "_trace_id", "_model",
                 "_turn", "_turn_started_at", "_message_started_at", "_turns")

    def __init__(self, state: dict[str, Any], contact: str, session_id: str,
                 trace_id: str, model: str):
        self._state = state
        self._contact = contact
        self._session_id = session_id
        self._trace_id = trace_id
        self._model = model
        self._turn = 1
        self._turn_started_at = time.time()
        self._message_started_at = self._turn_started_at
        self._turns: list[dict[str, Any]] = []

    def write(self, state: str, tools_in_flight: list[str] | None = None) -> None:
        self._state.update({
            "state": state,
            "contact": self._contact,
            "session_id": self._session_id,
            "trace_id": self._trace_id,
            "model": self._model,
            "turn": self._turn,
            "message_started_at": self._message_started_at,
            "turn_started_at": self._turn_started_at,
            "tools_in_flight": tools_in_flight or [],
            "turns": self._turns,
            "updated_at": time.time(),
        })

    def on_response(self, response: LLMResponse) -> None:
        duration_ms = int((time.time() - self._turn_started_at) * 1000)
        tool_names = [tc.name for tc in response.tool_calls] if response.tool_calls else []
        self._turns.append({
            "duration_ms": duration_ms,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_tokens": response.usage.cache_read_tokens,
            "cache_write_tokens": response.usage.cache_write_tokens,
            "stop_reason": response.stop_reason,
            "tools": tool_names,
        })
        if response.stop_reason == "tool_use" and response.tool_calls:
            self.write("tools", tools_in_flight=tool_names)
        else:
            self.write("idle")

    def on_tool_results(self, results_msg: object) -> None:
        """Advance the turn counter; the results payload itself is unused."""
        self._turn += 1
        self._turn_started_at = time.time()
        self.write("thinking")


# ─── Message State ───────────────────────────────────────────────


@dataclass
class _MessageState:
    """Internal state bag for process_message phases."""
    text: str
    sender: str
    talker: Talker                    # Envelope: who is speaking (user/operator/system/agent)
    trace_id: str
    channel: str = ""                 # Inbound channel for log/metric metadata only
    reply_to: str = ""                # Envelope: response routing ("" = caller, "silent")
    session_key: str = ""             # f"{talker}:{sender}" — computed in process_message
    image_blocks: list[dict[str, Any]] = field(default_factory=list)
    session: Session | None = None
    user_msg_idx: int = 0
    session_preexisted: bool = False
    model_name: str = ""
    provider_name: str = ""
    cost_rates: list[float] = field(default_factory=list)
    currency: str = "EUR"
    fmt_system: SystemPrompt | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    msg_count_before: int = 0
    response: LLMResponse | None = None
    force_compact: bool = False


# ─── Pipeline ────────────────────────────────────────────────────


class MessagePipeline:
    """Core message processing: preprocess → session → context → agentic loop → finalize.

    Takes all runtime dependencies explicitly.  Created once after daemon bootstrap.
    Owns monitor state, error counts, and per-sender session locks.
    """

    def __init__(
        self,
        *,
        config: Config,
        provider: LLMProvider,
        get_provider: Callable[[str], LLMProvider],
        session_mgr: SessionManager,
        context_builder: ContextBuilder,
        tool_registry: ToolRegistry,
        skill_loader: SkillLoader | None,
        metering_db: MeteringDB | None,
        pool: asyncpg.Pool,
        memory_interface: MemoryInterface | None,
        preprocessors: list[PreprocessorSpec],
        queue: PriorityMessageQueue,
        on_pre_close: Callable[[str], Awaitable[None]] | None = None,
        converter: CurrencyConverter | None = None,
        guardrails: Guardrails | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._get_provider = get_provider
        self._session_mgr = session_mgr
        self._context_builder = context_builder
        self._tool_registry = tool_registry
        self._skill_loader = skill_loader
        self._metering_db = metering_db
        self._converter = converter
        self._pool = pool
        self._memory_interface = memory_interface
        self._preprocessors = preprocessors
        self._queue = queue
        self._on_pre_close = on_pre_close
        self._guardrails = guardrails or Guardrails()

        # User wall-clock zone for the per-turn timestamp the agent reads (the
        # same zone ContextBuilder uses for "Current date/time"). Infrastructure
        # stays UTC; the agent's user-facing time is localized.
        try:
            self._user_tz: _dt.tzinfo = ZoneInfo(config.user_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            self._user_tz = _dt.timezone.utc

        # Dispatch mode: single-shot vs agentic loop
        caps = provider.capabilities if provider else None
        self._single_shot = (
            config.agent_strategy == "single_shot"
            or (caps is not None and not caps.supports_tools)
        )

        # Mutable state — daemon reads via properties
        self._monitor_state: dict[str, Any] = {"state": "idle"}
        self._error_counts: dict[str, int] = {}
        self._current_session: Session | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ── Public interface ─────────────────────────────────────────

    @property
    def monitor_state(self) -> dict[str, Any]:
        return self._monitor_state

    @property
    def error_counts(self) -> dict[str, int]:
        return self._error_counts

    @property
    def current_session(self) -> Session | None:
        return self._current_session

    def get_session_lock(self, sender: str) -> asyncio.Lock:
        """Get or create a per-sender lock for session mutation safety."""
        if sender not in self._session_locks:
            self._session_locks[sender] = asyncio.Lock()
        return self._session_locks[sender]

    # ── Preprocessing ────────────────────────────────────────────

    async def _run_preprocessors(self, text: str, attachments: list[Any] | None) -> tuple[str, list[Any] | None]:
        """Run registered preprocessors on inbound message.

        Each preprocessor receives (text, attachments, config) and returns
        (text, attachments). Preprocessors run in registration order.
        A preprocessor claims attachments it handles and passes the rest through.

        Critical preprocessors (e.g. voice transcription) produce fallback
        text on failure so the agent sees an explicit input. Optional
        preprocessors log + metric and continue.
        """
        if not self._preprocessors or not attachments:
            return text, attachments
        for pp in self._preprocessors:
            _pp_start = time.time()
            try:
                text, attachments = await pp.fn(text, attachments, self._config)
                if metrics.ENABLED:
                    metrics.PREPROCESSOR_TOTAL.labels(name=pp.name, status="success").inc()
                    metrics.PREPROCESSOR_DURATION.labels(name=pp.name).observe(time.time() - _pp_start)
            except PluginError as e:
                log.warning("Preprocessor %s plugin error (%s, critical=%s): %s",
                            pp.name, e.code, pp.critical, e)
                if metrics.ENABLED:
                    metrics.PREPROCESSOR_TOTAL.labels(name=pp.name, status="error").inc()
                if pp.critical:
                    fallback = pp.fallback_text or f"[{pp.name} processing failed]"
                    text = f"{text}\n{fallback}" if text else fallback
                    attachments = []
            except (TimeoutError, RuntimeError, OSError) as e:
                log.error("Preprocessor %s failed (critical=%s): %s",
                          pp.name, pp.critical, e, exc_info=True)
                if metrics.ENABLED:
                    metrics.PREPROCESSOR_TOTAL.labels(name=pp.name, status="error").inc()
                if pp.critical:
                    fallback = pp.fallback_text or f"[{pp.name} processing failed]"
                    text = f"{text}\n{fallback}" if text else fallback
                    attachments = []
            if not attachments:
                break
        return text, attachments or None

    async def _process_attachments(self, text: str, attachments: list[Any] | None, provider: LLMProvider) -> tuple[str, list[dict[str, Any]]]:
        """Process attachments into text descriptions + image blocks.

        Returns (text, image_blocks).
        Audio is handled by preprocessor plugins before this runs.
        """
        image_blocks: list[dict[str, Any]] = []
        if attachments:
            supports_vision = provider.capabilities.supports_vision
            for att in attachments:
                if att.content_type.startswith("image/"):
                    text, block = self._process_image(text, att, supports_vision)
                    if block:
                        image_blocks.append(block)
                else:
                    text, doc_blocks = self._process_document(text, att, supports_vision)
                    image_blocks.extend(doc_blocks)
        return text, image_blocks

    def _process_image(self, text: str, att: Any, supports_vision: bool) -> tuple[str, dict[str, Any] | None]:
        """Process a single image attachment. Returns (text, block_or_None)."""
        too_large_msg = "image too large to display"
        if not supports_vision:
            return _append(text, "[image received — vision not available with current provider]"), None
        try:
            img_data = Path(att.local_path).read_bytes()
            try:
                img_data = fit_image(img_data, att.content_type,
                                     self._config.vision_max_image_bytes,
                                     self._config.vision_max_dimension,
                                     self._config.vision_jpeg_quality_steps,
                                     att.local_path)
            except ImageTooLarge as exc:
                return _append(text, f"[{too_large_msg} — {exc}]"), None
            block = {
                "type": "image",
                "media_type": att.content_type,
                "data": base64.b64encode(img_data).decode("ascii"),
            }
            prefix = f"[image, saved: {att.local_path}]"
            text = f"{prefix} {text}" if text else prefix
            return text, block
        except Exception as e:
            log.error("Failed to read image %s: %s", att.local_path, e, exc_info=True)
            return _append(text, f"[{too_large_msg} — could not read file]"), None

    def _process_document(self, text: str, att: Any,
                          supports_vision: bool) -> tuple[str, list[dict[str, Any]]]:
        """Process a single document/file attachment. Returns (text, image_blocks).

        PDFs get a label only — the agent uses the ``pdf_read`` tool
        for explicit text extraction with page control.
        Non-PDF text documents are still auto-extracted as plumbing.
        """
        is_pdf = (att.content_type == "application/pdf"
                  or (att.filename or "").lower().endswith(".pdf"))
        if is_pdf:
            label = att.filename or "document"
            return _append(text, f"[pdf: {label}, saved: {att.local_path} — use pdf_read to extract text]"), []

        # Non-PDF documents: extract text if enabled
        doc_text = None
        if self._config.documents_enabled:
            try:
                doc_text = extract_document_text(
                    att.local_path, att.content_type, att.filename or "",
                    max_chars=self._config.documents_max_chars,
                    max_bytes=self._config.documents_max_file_bytes,
                    text_extensions=self._config.documents_text_extensions,
                )
            except Exception as e:
                log.error("Document extraction failed for %s: %s", _log_safe(att.filename), e, exc_info=True)

        if doc_text:
            label = att.filename or "document"
            return _append(text, f"[document: {label}, saved: {att.local_path}]\n{doc_text}"), []

        return _append(text, f"[attachment: {att.filename or 'file'}, "
                       f"{att.content_type}, saved: {att.local_path}]"), []

    # ── Session Setup ────────────────────────────────────────────

    async def _setup_session(self, ctx: _MessageState) -> None:
        """Set up session state: get/create, inject warnings, add user message."""
        # Track whether session pre-existed (for auto-close decision).
        # Notifications routed to the primary session must not close it.
        ctx.session_preexisted = (
            await self._session_mgr.has_session(ctx.session_key)
        )

        # A fired reminder / scheduled-task turn is blind to the live user
        # conversation (own ephemeral session). Detect it from the frozen
        # markers before any text mutation.
        is_scheduled_fire = ctx.talker == "agent" and ctx.text.lstrip().startswith(
            ("[Reminder] ", "[Scheduled task] "),
        )

        # Get or create session (keyed by talker:sender)
        session = await self._session_mgr.get_or_create(ctx.session_key, model=ctx.model_name)
        ctx.session = session
        # Keep session.model current: resumed sessions carry whatever was
        # persisted last (including "" from before this field was populated).
        if ctx.model_name and session.model != ctx.model_name:
            session.model = ctx.model_name

        # Status tool reads session via callback (configured in _init_tools)
        self._current_session = session

        # Inject pending compaction warning from previous turn
        ctx.text, warning_consumed = _inject_warning(ctx.text, session.pending_system_warning)
        if warning_consumed:
            session.pending_system_warning = ""
            await self._session_mgr.save_state(session)  # Persist cleared warning before agentic loop

        # Give a fired reminder/scheduled-task turn situational awareness so
        # it weaves into the live conversation instead of firing blind.
        if is_scheduled_fire:
            brief = await _recent_user_context(
                self._pool, f"user:{self._config.user_name}",
                user_tz=self._config.user_timezone,
            )
            ctx.text = f"[system: {brief}]\n\n{ctx.text}"

        # Inject timestamp so the agent always knows the current time — in the
        # user's wall-clock zone, matching the dynamic-tier "Current date/time"
        # (no two clocks for the agent to reconcile).
        timestamp = _dt.datetime.now(self._user_tz).strftime("[%a, %d. %b %Y - %H:%M %Z]")
        ctx.text = f"{timestamp}\n{ctx.text}"

        session.trace_id = ctx.trace_id
        await session.add_user_message(ctx.text, sender=ctx.sender, source=ctx.channel or ctx.talker)

        # Transiently inject image content blocks for the API call
        ctx.user_msg_idx = len(session.messages) - 1

        if ctx.image_blocks:
            session.messages[ctx.user_msg_idx]["_image_blocks"] = ctx.image_blocks  # type: ignore[typeddict-unknown-key]  # transient key, stripped before persistence

    # ── Context Building ─────────────────────────────────────────

    async def _build_recall(self, ctx: _MessageState) -> str:
        """Retrieve memory relevant to the current user message, formatted for injection.

        Runs on every user turn, keyed to the message text: structured facts,
        recent episodes, and vector search over the indexed workspace —
        budget-capped by ``recall_max_dynamic_tokens``. Non-user
        talkers (operator/system/agent) carry their own context and get no
        recall; likewise when consolidation is off or no memory subsystem exists.
        """
        if ctx.talker != "user":
            return ""
        if not self._config.consolidation_enabled:
            return ""
        if self._memory_interface is None:
            return ""
        # Key recall on the user's actual words: strip the leading [timestamp]
        # header and any [..saved: /path]: attachment prefix (the same prefixes
        # _brief_snippet strips) so retrieval isn't polluted by metadata tokens.
        query = _ATTACHMENT_PREFIX_RE.sub("", _TS_PREFIX_RE.sub("", ctx.text)).strip()
        try:
            blocks = await recall(
                query=query,
                pool=self._pool,
                memory_interface=self._memory_interface,
                config=self._config,
            )
            result = inject_recall(blocks, self._config.recall_max_dynamic_tokens)
            if result and metrics.ENABLED:
                metrics.MEMORY_OPS_TOTAL.labels(operation="recall_triggered").inc()
            return result
        except Exception:
            log.exception("per-turn recall failed")
            return "[Memory recall unavailable — use memory_search to access memory manually.]"

    async def _build_context(self, ctx: _MessageState, provider: LLMProvider) -> None:
        """Build system prompt, recall, and tools list."""
        session = ctx.session
        assert session is not None  # set by _setup_session before any context build
        tool_descs = self._tool_registry.get_brief_descriptions()
        skill_index = self._skill_loader.build_index() if self._skill_loader else ""
        always_on = self._config.always_on_skills
        skill_bodies = self._skill_loader.get_bodies(always_on) if self._skill_loader else {}

        recall_text = await self._build_recall(ctx)

        system_blocks = self._context_builder.build(
            talker=ctx.talker,
            tool_descriptions=tool_descs,
            skill_index=skill_index,
            always_on_skills=always_on,
            skill_bodies=skill_bodies,
            extra_dynamic=recall_text,
            silent_tokens=self._config.silent_tokens,
            max_turns=self._config.max_turns,
            max_cost=self._config.max_cost_per_message,
            compaction_threshold=self._config.compaction_threshold,
            has_images=bool(ctx.image_blocks),
            sender=ctx.sender,
        )
        ctx.fmt_system = provider.format_system(system_blocks)

        # Runtime context budget report
        max_ctx = provider.capabilities.max_context_tokens
        if max_ctx > 0:
            sys_tokens = sum(_estimate_tokens(b.get("text", "")) for b in system_blocks)
            history_tokens = _history_tokens(session.messages)
            tool_def_tokens = sum(
                _estimate_tokens(t["description"])
                for t in self._tool_registry.get_schemas_for_talker(ctx.talker)
            ) if provider.capabilities.supports_tools else 0
            used = sys_tokens + history_tokens + tool_def_tokens
            remaining = max_ctx - used
            log.debug(
                "Context budget [%s]: total=%d | system=%d | history=%d (%d msgs) "
                "| tools=%d | used=%d | remaining=%d",
                ctx.trace_id[:8], max_ctx, sys_tokens, history_tokens,
                len(session.messages), tool_def_tokens, used, remaining,
            )
            if metrics.ENABLED:
                metrics.CONTEXT_UTILIZATION.labels(
                    talker=ctx.talker,
                    session_id=session.id if session else "",
                    sender=ctx.sender,
                ).observe(used / max_ctx if max_ctx > 0 else 0)

        # Run agentic loop — it appends to session.messages in place
        # If model doesn't support tools, degrade gracefully (no tools sent)
        # Filter by talker: tools with talkers=None are universal; tools with
        # talkers={"agent"} (e.g. send_message) only appear in agent:self turns.
        if provider.capabilities.supports_tools:
            ctx.tools = self._tool_registry.get_schemas_for_talker(ctx.talker)
        else:
            ctx.tools = []

        # Snapshot message count to track what the loop added
        ctx.msg_count_before = len(session.messages)

    # ── Agentic Loop ─────────────────────────────────────────────

    def _build_cost_context(self, ctx: _MessageState) -> CostContext:
        """Build CostContext from message state — shared by agentic loop and compaction."""
        assert ctx.session is not None  # set by _setup_session
        return CostContext(
            metering=self._metering_db,
            session_id=ctx.session.id,
            model_name=ctx.model_name,
            cost_rates=ctx.cost_rates,
            provider_name=ctx.provider_name,
            currency=ctx.currency,
            converter=self._converter,
        )

    async def _run_agentic(
        self, ctx: _MessageState, provider: LLMProvider,
        on_response: Callable[[LLMResponse], Any],  # Any justified: callbacks may be sync or async
        on_tool_results: Callable[[ToolResultsMessage], Any],  # Any justified: callbacks may be sync or async
        on_stream_delta: Callable[[StreamDelta], Any] | None = None,  # Any justified: callbacks may be sync or async
    ) -> None:
        """Run the agentic loop. Sets ctx.response.

        No message-level retry — API-level retry in _call_provider_with_retry
        handles transient errors. If the loop fails, the error propagates.
        """
        session = ctx.session
        assert session is not None and ctx.fmt_system is not None  # set by _setup_session / _build_context
        cost_ctx = self._build_cost_context(ctx)
        loop_cfg = LoopConfig(
            max_turns=self._config.max_turns,
            timeout=self._config.agent_timeout,
            api_retries=self._config.api_retries,
            api_retry_base_delay=self._config.api_retry_base_delay,
            max_cost=float(self._config.max_cost_per_message),
            max_context_for_tools=self._config.max_context_for_tools,
            tool_call_retry=self._config.tool_call_retry,
            trace_id=ctx.trace_id,
        )
        if self._single_shot:
            ctx.response = await run_single_shot(
                provider=provider,
                system=ctx.fmt_system,
                messages=session.messages,
                tools=ctx.tools,
                tool_executor=self._tool_registry,
                config=loop_cfg,
                cost=cost_ctx,
                on_response=on_response,
                on_tool_results=on_tool_results,
                on_stream_delta=on_stream_delta,
            )
        else:
            ctx.response = await run_agentic_loop(
                provider=provider,
                system=ctx.fmt_system,
                messages=session.messages,
                tools=ctx.tools,
                tool_executor=self._tool_registry,
                config=loop_cfg,
                cost=cost_ctx,
                on_response=on_response,
                on_tool_results=on_tool_results,
                on_stream_delta=on_stream_delta,
            )

    async def _handle_agentic_error(
        self, ctx: _MessageState, error: BaseException,
        _resolve: Callable[[dict[str, Any]], None],
    ) -> None:
        """Handle agentic loop failure: cleanup, resolve future, deliver error."""
        session = ctx.session
        assert session is not None  # set by _setup_session before the loop can fail
        log.error("[%s] Agentic loop failed: %s", ctx.trace_id[:8], error)
        err_type = type(error).__name__
        self._error_counts[err_type] = self._error_counts.get(err_type, 0) + 1
        if metrics.ENABLED:
            metrics.ERRORS_TOTAL.labels(error_type=err_type).inc()
        # Strip transient image metadata before returning
        if ctx.image_blocks and ctx.user_msg_idx < len(session.messages):
            session.messages[ctx.user_msg_idx].pop("_image_blocks", None)  # type: ignore[typeddict-item]  # transient key, stripped before persistence
        # Roll back all messages the agentic loop added (assistant, tool_results,
        # system hints) so the session stays in a valid pre-loop state.
        if len(session.messages) > ctx.msg_count_before:
            del session.messages[ctx.msg_count_before:]
        # Remove orphaned user message to prevent consecutive-user corruption
        if session.messages and session.messages[-1]["role"] == "user":
            session.messages.pop()
        await self._session_mgr.save_state(session)
        # Deliver the configured friendly message to the user instead of silence.
        # The user's bridge reads `reply`; an error-only body sends nothing, so a
        # failed turn would otherwise vanish. Operator (agentctl) reads the raw
        # error itself, and system/agent talkers have no delivery path.
        result: dict[str, Any] = {"error": str(error), "session_id": session.id}
        if ctx.talker == "user":
            result["reply"] = self._config.error_message
        _resolve(result)
        # Auto-close ephemeral sessions even on error — but only if the
        # session was created by this event (not a pre-existing session).
        if ctx.talker in EPHEMERAL_TALKERS and not ctx.session_preexisted:
            try:
                await self._session_mgr.close_session(ctx.session_key)
                log.info("Auto-closed %s session for %s", ctx.talker, _log_safe(ctx.sender))
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason=f"auto_{ctx.talker}").inc()
            except Exception:
                log.warning("Auto-close failed for %s session %s",
                            ctx.talker, ctx.sender, exc_info=True)

    # ── Finalization ─────────────────────────────────────────────

    async def _finalize_response(
        self, ctx: _MessageState, _resolve: Callable[[dict[str, Any]], None],
    ) -> None:
        """Post-loop work: persist, deliver, compact."""
        await self._persist_response(ctx)

        # Output guardrail tripwire — rewrite reply text if a rule fires.
        reply_text = ctx.response.text if ctx.response else ""
        if reply_text:
            try:
                await self._guardrails.check_output(reply_text)
            except GuardrailTripped as g:
                log.warning("[%s] output guardrail '%s' tripped: %s",
                            ctx.trace_id[:8], g.name, g.reason)
                if metrics.ENABLED:
                    metrics.ERRORS_TOTAL.labels(error_type="guardrail_output").inc()
                if ctx.response is not None:
                    ctx.response.text = "response withheld by output guardrail"

        await self._deliver_reply(ctx, _resolve)
        await self._check_compaction_warning(ctx)
        await self._run_compaction_if_needed(ctx)
        await self._auto_close_if_ephemeral(ctx)

    async def _persist_response(self, ctx: _MessageState) -> None:
        """Persist new messages and restore text-only content."""
        session = ctx.session
        assert session is not None  # set by _setup_session
        for msg in session.messages[ctx.msg_count_before:]:
            if msg["role"] == "agent":
                await session.add_assistant_message(msg, persist_only=True)
            elif msg["role"] == "tool_result":
                await session.add_tool_results(msg["results"], persist_only=True)
        if ctx.image_blocks and ctx.user_msg_idx < len(session.messages):
            session.messages[ctx.user_msg_idx].pop("_image_blocks", None)  # type: ignore[typeddict-item]  # transient key, stripped before persistence
        await self._session_mgr.save_state(session)

    async def _deliver_reply(
        self, ctx: _MessageState, _resolve: Callable[[dict[str, Any]], None],
    ) -> None:
        """Resolve HTTP future with reply content.

        system/agent talkers have no reply path — the future is resolved
        with an empty body and nothing is delivered.  reply_to="silent"
        suppresses normal reply delivery for user/operator.
        """
        session = ctx.session
        response = ctx.response
        assert session is not None and response is not None  # finalize runs only after a successful loop
        reply = response.text or ""
        if response.cost_limited and not reply.strip():
            reply = ("[cost limit reached — max_cost_per_message in lucyd.toml. "
                     "raise or set to 0 to disable.]")
        silent = _is_silent(reply, self._config.silent_tokens) or ctx.reply_to == "silent"
        if ctx.talker in EPHEMERAL_TALKERS:
            silent = True
        token_info = {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        }
        reply_attachments = response.attachments or []

        if silent:
            log.info("[%s] Silent reply (talker=%s): %s",
                     ctx.trace_id[:8], ctx.talker, redact_content(reply, 100))
            _resolve({"reply": reply, "silent": True, "session_id": session.id,
                       "tokens": token_info, "attachments": reply_attachments})
        else:
            _resolve({"reply": reply, "session_id": session.id,
                       "tokens": token_info, "attachments": reply_attachments})

    async def _check_compaction_warning(self, ctx: _MessageState) -> None:
        """Inject context-pressure warning at 80% threshold."""
        session = ctx.session
        assert session is not None  # set by _setup_session
        if _should_warn_context(
            input_tokens=session.last_input_tokens,
            compaction_threshold=self._config.compaction_threshold,
            needs_compaction=session.needs_compaction(self._config.compaction_threshold),
            already_warned=session.warned_about_compaction,
            warning_pct=0.8,
        ):
            max_ctx = self._provider.capabilities.max_context_tokens
            pct = session.last_input_tokens * 100 // max_ctx if max_ctx > 0 else 0
            session.pending_system_warning = (
                f"[system: context at {session.last_input_tokens:,} tokens "
                f"({pct}% of capacity). compaction will summarize older messages "
                f"at {self._config.compaction_threshold:,}. save anything important "
                f"to memory files, then continue the conversation normally.]"
            )
            session.warned_about_compaction = True
            await self._session_mgr.save_state(session)
            log.info("Compaction warning set for session %s at %d tokens",
                     session.id, session.last_input_tokens)

    async def _compact_session(self, ctx: _MessageState, keep_recent_pct: float) -> None:
        """Run compaction with shared prompt, cost, and config. Emits Prometheus metric."""
        assert ctx.session is not None  # set by _setup_session
        prompt = self._config.compaction_prompt.replace(
            "{agent_name}", self._config.agent_name,
        ).replace("{max_tokens}", str(self._config.compaction_max_tokens))
        await self._session_mgr.compact_session(
            ctx.session, self._get_provider("compaction"), prompt,
            trace_id=ctx.trace_id,
            system_blocks=self._context_builder.build_stable(),
            cost=self._build_cost_context(ctx),
            keep_recent_pct=keep_recent_pct,
            min_messages=4,
            tool_result_max_chars=2000,
            max_tokens=self._config.compaction_max_tokens,
        )
        if metrics.ENABLED:
            metrics.COMPACTION_TOTAL.inc()

    async def _ensure_context_budget(self, ctx: _MessageState) -> bool:
        """Compact session if context utilization exceeds 80%.

        Called before the agentic loop to guarantee context fits.
        Returns True if the message can proceed, False if context
        could not be brought within budget (caller should fail).
        """
        session = ctx.session
        assert session is not None  # set by _setup_session
        max_ctx = self._provider.capabilities.max_context_tokens
        if max_ctx <= 0:
            return True

        for attempt in range(2):
            used = _history_tokens(session.messages)
            ratio = used / max_ctx
            if ratio <= 0.80:
                return True

            keep_pct = self._config.compaction_keep_pct if attempt == 0 else 0.15
            log.warning(
                "[%s] Context at %.0f%% — running %s compaction (keep_recent=%.0f%%)",
                ctx.trace_id[:8], ratio * 100,
                "emergency" if attempt > 0 else "pre-loop",
                keep_pct * 100,
            )
            if metrics.ENABLED:
                metrics.ERRORS_TOTAL.labels(error_type="context_emergency_compaction").inc()

            await self._compact_session(ctx, keep_pct)

            # Rebuild context after compaction rewrote messages
            await self._build_context(ctx, self._provider)

        # Exhausted attempts
        log.error("[%s] Context still over budget after emergency compaction", ctx.trace_id[:8])
        if metrics.ENABLED:
            metrics.ERRORS_TOTAL.labels(error_type="context_budget_exceeded").inc()
        return False

    async def _run_compaction_if_needed(self, ctx: _MessageState) -> None:
        """Run consolidation + compaction if threshold is exceeded.

        Consolidation must succeed before compaction runs — compacting
        unconsolidated messages is permanent fact loss. If consolidation
        fails, compaction is skipped this pass; the next message that
        crosses the threshold retries consolidation.
        """
        session = ctx.session
        assert session is not None  # set by _setup_session
        _needs_compact = ctx.force_compact or session.needs_compaction(
            self._config.compaction_threshold)
        if not _needs_compact:
            return
        if self._config.consolidation_enabled:
            # Harvest the about-to-be-compacted messages (facts + an episode, in
            # her voice) before they're summarized away — the same job the
            # scheduled maintenance pass does, fired here so nothing is lost.
            harvest = await operations.harvest_conversation(
                session, self._config, self._pool,
                self.process_message, self.get_session_lock,
            )
            if not harvest["ok_to_compact"]:
                if metrics.ENABLED:
                    metrics.ERRORS_TOTAL.labels(error_type="consolidation_blocked_compaction").inc()
                return  # Skip compaction — don't summarize unconsolidated messages
        _pre_tokens = session.last_input_tokens
        await self._compact_session(ctx, self._config.compaction_keep_pct)
        if metrics.ENABLED:
            reclaimed = max(0, _pre_tokens - (session.last_input_tokens or 0))
            if reclaimed > 0:
                metrics.COMPACTION_TOKENS_RECLAIMED.observe(reclaimed)

    async def _auto_close_if_ephemeral(self, ctx: _MessageState) -> None:
        """Auto-close ephemeral sessions (talker 'system' or 'agent')."""
        if ctx.talker in EPHEMERAL_TALKERS and not ctx.force_compact and not ctx.session_preexisted:
            if self._on_pre_close is not None:
                await self._on_pre_close(ctx.sender)
            try:
                await self._session_mgr.close_session(ctx.session_key)
                log.info("Auto-closed %s session for %s", ctx.talker, _log_safe(ctx.sender))
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason=f"auto_{ctx.talker}").inc()
            except Exception:
                log.warning("Auto-close failed for %s session %s",
                            ctx.talker, ctx.sender, exc_info=True)

    # ── Entry Point ──────────────────────────────────────────────

    async def process_message(
        self,
        text: str,
        sender: str,
        talker: Talker,
        attachments: list[Any] | None = None,
        response_future: asyncio.Future[dict[str, Any]] | None = None,
        trace_id: str = "",
        force_compact: bool = False,
        stream_queue: asyncio.Queue[dict[str, Any] | None] | None = None,
        channel: str = "",
        reply_to: str = "",
        session_key: str = "",
    ) -> None:
        """Process a single message through the agentic loop."""
        _msg_start = time.time()
        if not trace_id:
            trace_id = str(uuid.uuid4())

        def _resolve(result: dict[str, Any]) -> None:
            """Safely resolve the HTTP response future."""
            if response_future is not None and not response_future.done():
                response_future.set_result(result)

        provider = self._provider
        model_cfg = self._config.model_config("primary")

        # Input guardrail tripwire — halt before preprocessors if tripped.
        try:
            await self._guardrails.check_input(text)
        except GuardrailTripped as g:
            log.warning("[%s] input guardrail '%s' tripped: %s", trace_id[:8], g.name, g.reason)
            if metrics.ENABLED:
                metrics.ERRORS_TOTAL.labels(error_type="guardrail_input").inc()
            _resolve({"error": "request blocked by input guardrail"})
            return

        # Run preprocessors before core attachment handling
        text, attachments = await self._run_preprocessors(text, attachments)

        # Process remaining attachments into text descriptions + image blocks
        text, image_blocks = await self._process_attachments(
            text, attachments, provider,
        )

        # Build context bag
        ctx = _MessageState(
            text=text,
            sender=sender,
            talker=talker,
            trace_id=trace_id,
            channel=channel,
            reply_to=reply_to,
            session_key=session_key or f"{talker}:{sender}",
            image_blocks=image_blocks,
            model_name=model_cfg.get("model", ""),
            provider_name=model_cfg.get("provider", ""),
            cost_rates=model_cfg.get("cost_per_mtok", []),
            currency=model_cfg.get("currency", "EUR"),
            force_compact=force_compact,
        )

        # Session setup: get/create, inject warnings, add user message
        try:
            await self._setup_session(ctx)
        except ConsecutiveRoleError:
            log.error("[%s] Consecutive user messages blocked for %s",
                      trace_id[:8], _log_safe(sender))
            if metrics.ENABLED:
                metrics.ERRORS_TOTAL.labels(error_type="consecutive_role_violation").inc()
            _resolve({"error": "consecutive user messages — upstream bug"})
            return

        # Set structured log context for this message cycle
        set_log_context(
            agent_id=self._config.resolved_agent_id,
            session_id=ctx.session.id if ctx.session else "",
            trace_id=trace_id,
        )

        # Build system prompt, recall, tools
        await self._build_context(ctx, provider)

        # Pre-loop context budget check — compact if over 80%
        if not await self._ensure_context_budget(ctx):
            _resolve({"error": "context budget exceeded after emergency compaction"})
            return

        session = ctx.session
        assert session is not None  # set by _setup_session above

        # ── Monitor ──────────────────────────────────────────────
        monitor = _MonitorWriter(
            state=self._monitor_state,
            contact=sender,
            session_id=session.id,
            trace_id=trace_id,
            model=ctx.model_name,
        )
        monitor.write("thinking")

        # Build SSE streaming callback
        stream_delta_cb = None
        _sse_done_sent = False
        if stream_queue is not None:
            async def _on_stream_delta(delta: StreamDelta) -> None:
                nonlocal _sse_done_sent
                event: dict[str, Any] = {}
                if delta.text:
                    event["text"] = delta.text
                if delta.thinking:
                    event["thinking"] = delta.thinking
                if delta.status:
                    event["status"] = delta.status
                if delta.stop_reason:
                    event["done"] = True
                    event["stop_reason"] = delta.stop_reason
                if delta.usage:
                    event["usage"] = {
                        "input_tokens": delta.usage.input_tokens,
                        "output_tokens": delta.usage.output_tokens,
                    }
                if event:
                    await stream_queue.put(event)
                    if event.get("done"):
                        _sse_done_sent = True
            stream_delta_cb = _on_stream_delta

        try:
            await self._run_agentic(
                ctx, provider, monitor.on_response, monitor.on_tool_results,
                on_stream_delta=stream_delta_cb,
            )
        except Exception as e:
            await self._handle_agentic_error(ctx, e, _resolve)
            if metrics.ENABLED:
                _outcome = "timeout" if isinstance(e, TimeoutError) else "error"
                metrics.MESSAGE_OUTCOME_TOTAL.labels(outcome=_outcome).inc()
            if stream_queue is not None:
                # Emit SSE error event before closing the stream
                await stream_queue.put({"error": str(e), "done": True})
                await stream_queue.put(None)  # sentinel
            return
        finally:
            monitor.write("idle")

        # When the final turn emits both `text` and `tool_calls`, the text
        # is preamble/reasoning by Anthropic + OpenAI convention — not a
        # user-facing reply.  Suppress it before downstream consumers (SSE
        # bridge, _deliver_reply) read response.text.  Persistence already
        # captured the original text via session.messages inside the
        # agentic loop, so the session log retains the model's emission.
        if ctx.response and ctx.response.text and ctx.response.tool_calls:
            ctx.response.text = None

        # Bridge non-streaming responses into SSE: only when the provider
        # didn't stream (no done event was pushed via deltas).  If the
        # provider already streamed text + done, skip to avoid duplicating
        # the reply.
        if stream_queue is not None:
            if not _sse_done_sent:
                reply_text = ctx.response.text if ctx.response else ""
                if reply_text:
                    await stream_queue.put({
                        "text": reply_text,
                        "done": True,
                        "stop_reason": ctx.response.stop_reason if ctx.response else "end_turn",
                    })
            await stream_queue.put(None)  # sentinel

        await self._finalize_response(ctx, _resolve)

        # ── Prometheus metrics ────────────────────────────────────────
        if metrics.ENABLED:
            _sid = ctx.session.id if ctx.session else ""
            _labels = {
                "talker": ctx.talker,
                "session_id": _sid, "sender": ctx.sender,
            }
            metrics.MESSAGES_TOTAL.labels(**_labels).inc()
            metrics.MESSAGE_DURATION.labels(**_labels).observe(time.time() - _msg_start)
            if ctx.response:
                turns = ctx.response.turns
                if turns > 0:
                    metrics.AGENTIC_TURNS.labels(**_labels).observe(turns)
                _total_cost = ctx.response.total_cost
                if isinstance(_total_cost, (int, float)) and _total_cost > 0:
                    metrics.MESSAGE_COST.labels(**_labels).observe(_total_cost)
            # Outcome: the single most important quality signal
            if ctx.response:
                if ctx.response.cost_limited:
                    _outcome = "cost_limited"
                elif ctx.response.stop_reason != "end_turn":
                    _outcome = "max_turns"
                else:
                    _outcome = "resolved"
            else:
                _outcome = "error"
            metrics.MESSAGE_OUTCOME_TOTAL.labels(outcome=_outcome).inc()
