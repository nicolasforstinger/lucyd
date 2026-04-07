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
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import consolidation as consolidation_mod
import metrics
from agentic import LoopConfig, is_transient_error, run_agentic_loop, run_single_shot
from attachments import ImageTooLarge, extract_document_text, fit_image, render_pdf_pages
from config import Config
from context import ContextBuilder, _estimate_tokens
from log_utils import _log_safe, set_log_context
from metering import MeteringDB
from memory import get_session_start_context
from providers import CostContext, LLMProvider
from session import SessionManager, _text_from_content
from skills import SkillLoader
from tools import ToolRegistry

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

    def on_response(self, response: Any) -> None:
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

    def on_tool_results(self, results_msg: Any) -> None:
        self._turn += 1
        self._turn_started_at = time.time()
        self.write("thinking")


# ─── Message State ───────────────────────────────────────────────


@dataclass
class _MessageState:
    """Internal state bag for process_message phases."""
    text: str
    sender: str
    source: str           # Message origin (channel name, "http", or "system")
    trace_id: str
    channel_id: str = "http"          # Envelope: which channel sent this
    task_type: str = "conversational"  # Envelope: session lifecycle intent
    reply_to: str = ""                # Envelope: response routing ("" = caller, "silent", or sender name)
    session_key: str = ""             # channel_id:sender — computed in process_message
    deliver: bool = True
    image_blocks: list[dict[str, Any]] = field(default_factory=list)
    session: Any = None
    user_msg_idx: int = 0
    session_preexisted: bool = False
    model_cfg: dict[str, Any] = field(default_factory=dict)
    model_name: str = ""
    provider_name: str = ""
    cost_rates: list[float] = field(default_factory=list)
    currency: str = "EUR"
    fmt_system: Any = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    msg_count_before: int = 0
    response: Any = None
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
        pool: Any,  # asyncpg.Pool — no stubs available
        client_id: str,
        agent_id: str,
        preprocessors: list[dict[str, Any]],
        queue: asyncio.Queue[dict[str, Any]],
        on_pre_close: Callable[[str], Awaitable[None]] | None = None,
        converter: Any = None,
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
        self._client_id = client_id
        self._agent_id = agent_id
        self._preprocessors = preprocessors
        self._queue = queue
        self._on_pre_close = on_pre_close

        # Dispatch mode: single-shot vs agentic loop
        caps = provider.capabilities if provider else None
        self._single_shot = (
            config.agent_strategy == "single_shot"
            or (caps is not None and not caps.supports_tools)
        )

        # Mutable state — daemon reads via properties
        self._monitor_state: dict[str, Any] = {"state": "idle"}
        self._error_counts: dict[str, int] = {}
        self._current_session: Any = None
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ── Public interface ─────────────────────────────────────────

    @property
    def monitor_state(self) -> dict[str, Any]:
        return self._monitor_state

    @property
    def error_counts(self) -> dict[str, int]:
        return self._error_counts

    @property
    def current_session(self) -> Any:
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
        """
        if not self._preprocessors or not attachments:
            return text, attachments
        for pp in self._preprocessors:
            _pp_start = time.time()
            try:
                text, attachments = await pp["fn"](text, attachments, self._config)
                if metrics.ENABLED:
                    metrics.PREPROCESSOR_TOTAL.labels(name=pp["name"], status="success").inc()
                    metrics.PREPROCESSOR_DURATION.labels(name=pp["name"]).observe(time.time() - _pp_start)
            except Exception:
                log.exception("Preprocessor %s failed", pp["name"])
                if metrics.ENABLED:
                    metrics.PREPROCESSOR_TOTAL.labels(name=pp["name"], status="error").inc()
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
        """Process a single document/file attachment. Returns (text, image_blocks)."""
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

        # Scanned PDF fallback — render pages as images for vision
        is_pdf = (att.content_type == "application/pdf"
                  or (att.filename or "").lower().endswith(".pdf"))
        if is_pdf and supports_vision and self._config.documents_enabled:
            blocks = self._render_pdf_as_images(att)
            if blocks:
                label = att.filename or "document"
                return _append(text, f"[scanned document: {label}, "
                               f"{len(blocks)} page(s) as images, "
                               f"saved: {att.local_path}]"), blocks

        return _append(text, f"[attachment: {att.filename or 'file'}, "
                       f"{att.content_type}, saved: {att.local_path}]"), []

    def _render_pdf_as_images(self, att: Any) -> list[dict[str, Any]]:
        """Render PDF pages as images for vision. Returns image blocks."""
        pages = render_pdf_pages(
            att.local_path,
            max_pages=self._config.documents_pdf_max_render_pages,
            max_dimension=self._config.vision_max_dimension,
        )
        if not pages:
            return []
        blocks: list[dict[str, Any]] = []
        for page_data in pages:
            try:
                page_data = fit_image(
                    page_data, "image/jpeg",
                    self._config.vision_max_image_bytes,
                    self._config.vision_max_dimension,
                    self._config.vision_jpeg_quality_steps,
                    att.local_path,
                )
                blocks.append({
                    "type": "image",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(page_data).decode("ascii"),
                })
            except ImageTooLarge:
                log.warning("PDF page too large after compression, skipping")
        return blocks

    # ── Session Setup ────────────────────────────────────────────

    async def _setup_session(self, ctx: _MessageState) -> None:
        """Set up session state: get/create, inject warnings, add user message."""
        # Track whether session pre-existed (for auto-close decision).
        # Notifications routed to the primary session must not close it.
        ctx.session_preexisted = (
            await self._session_mgr.has_session(ctx.session_key)
        )

        # Get or create session (keyed by channel_id:sender)
        session = await self._session_mgr.get_or_create(ctx.session_key)
        ctx.session = session

        # Status tool reads session via callback (configured in _init_tools)
        self._current_session = session

        # Inject pending compaction warning from previous turn
        ctx.text, warning_consumed = _inject_warning(ctx.text, session.pending_system_warning)
        if warning_consumed:
            session.pending_system_warning = ""
            await self._session_mgr.save_state(session)  # Persist cleared warning before agentic loop

        # Inject timestamp so the agent always knows the current time
        timestamp = time.strftime("[%a, %d. %b %Y - %H:%M %Z]")
        ctx.text = f"{timestamp}\n{ctx.text}"

        session.trace_id = ctx.trace_id
        await session.add_user_message(ctx.text, sender=ctx.sender, source=ctx.source)

        # Transiently inject image content blocks for the API call
        ctx.user_msg_idx = len(session.messages) - 1

        # Merge consecutive user messages (recovery from prior errors, JSONL rebuild)
        while len(session.messages) >= 2 and session.messages[-2]["role"] == "user":
            last = session.messages[-1]
            if last["role"] != "user":
                break
            prev = session.messages[-2]
            prev["content"] = _text_from_content(prev["content"]) + "\n" + _text_from_content(last["content"])
            session.messages.pop()
            ctx.user_msg_idx = len(session.messages) - 1
            log.warning("Merged consecutive user messages in session %s", session.id)

        if ctx.image_blocks:
            session.messages[ctx.user_msg_idx]["_image_blocks"] = ctx.image_blocks  # type: ignore[typeddict-unknown-key]  # transient key, stripped before persistence

    # ── Context Building ─────────────────────────────────────────

    async def _build_recall(self, ctx: _MessageState, provider: LLMProvider) -> str:
        """Build recall text for fresh sessions via structured memory."""
        session = ctx.session
        if len(session.messages) > 1:
            return ""
        if not self._config.consolidation_enabled:
            return ""
        try:
            result = await get_session_start_context(
                pool=self._pool,
                client_id=self._client_id,
                agent_id=self._agent_id,
                config=self._config,
                max_facts=self._config.recall_max_facts,
                max_episodes=self._config.recall_max_episodes_at_start,
                max_tokens=self._config.recall_max_dynamic_tokens,
            ) or ""
            if result and metrics.ENABLED:
                metrics.MEMORY_OPS_TOTAL.labels(operation="recall_triggered").inc()
            return result
        except Exception:
            log.exception("structured recall at session start failed")
            return "[Memory recall unavailable — use memory_search to access memory manually.]"

    async def _build_context(self, ctx: _MessageState, provider: LLMProvider) -> None:
        """Build system prompt, recall, and tools list."""
        session = ctx.session
        tool_descs = self._tool_registry.get_brief_descriptions()
        skill_index = self._skill_loader.build_index() if self._skill_loader else ""
        always_on = self._config.always_on_skills
        skill_bodies = self._skill_loader.get_bodies(always_on) if self._skill_loader else {}

        recall_text = await self._build_recall(ctx, provider)

        system_blocks = self._context_builder.build(
            task_type=ctx.task_type,
            deliver=ctx.deliver,
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
            history_tokens = 0
            for m in session.messages:
                if m["role"] == "user":
                    history_tokens += _estimate_tokens(m["content"])
                elif m["role"] == "assistant":
                    history_tokens += _estimate_tokens(m.get("text", ""))
            tool_def_tokens = sum(
                _estimate_tokens(t["description"]) for t in self._tool_registry.get_schemas()
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
                    channel_id=ctx.channel_id, task_type=ctx.task_type,
                    session_id=session.id if session else "",
                    sender=ctx.sender,
                ).observe(used / max_ctx if max_ctx > 0 else 0)

        # Run agentic loop — it appends to session.messages in place
        # If model doesn't support tools, degrade gracefully (no tools sent)
        if provider.capabilities.supports_tools:
            ctx.tools = self._tool_registry.get_schemas()
        else:
            ctx.tools = []

        # Snapshot message count to track what the loop added
        ctx.msg_count_before = len(session.messages)

    # ── Agentic Loop ─────────────────────────────────────────────

    async def _run_agentic_with_retries(self, ctx: _MessageState, provider: LLMProvider,
                                         on_response: Any, on_tool_results: Any,
                                         write_monitor: Any, on_stream_delta: Any = None) -> None:
        """Run the agentic loop with message-level retries. Sets ctx.response."""
        session = ctx.session
        message_retries = self._config.message_retries
        message_retry_delay = self._config.message_retry_base_delay

        # Snapshot message count so retries can restore to a clean state.
        # The agentic loop mutates session.messages in-place; on failure
        # those partial turns must be stripped before the next attempt.
        pre_attempt_len = len(session.messages)

        for msg_attempt in range(1 + message_retries):
            try:
                max_cost = self._config.max_cost_per_message
                cost_ctx = CostContext(
                    metering=self._metering_db,
                    session_id=session.id,
                    model_name=ctx.model_name,
                    cost_rates=ctx.cost_rates,
                    provider_name=ctx.provider_name,
                    currency=ctx.currency,
                    converter=self._converter,
                )
                loop_cfg = LoopConfig(
                    max_turns=self._config.max_turns,
                    timeout=self._config.agent_timeout,
                    api_retries=self._config.api_retries,
                    api_retry_base_delay=self._config.api_retry_base_delay,
                    max_cost=float(max_cost),
                    max_context_for_tools=self._config.max_context_for_tools,
                    tool_call_retry=self._config.tool_call_retry,
                    tool_success_warn_threshold=0.5,
                    thinking_concise_hint=False,
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
                return  # Success
            except Exception as e:
                if not is_transient_error(e) or msg_attempt >= message_retries:
                    raise  # Non-transient or exhausted — propagate

                # Roll back messages added by the failed attempt
                if len(session.messages) > pre_attempt_len:
                    del session.messages[pre_attempt_len:]

                delay = message_retry_delay * (2 ** msg_attempt) * (0.5 + random.random())  # noqa: S311
                log.warning(
                    "[%s] Message retry (%d/%d) for %s: %s — waiting %.0fs",
                    ctx.trace_id[:8], msg_attempt + 1, message_retries, ctx.sender, e, delay,
                )
                write_monitor("retry_wait")
                await asyncio.sleep(delay)

                write_monitor("thinking")

    async def _handle_agentic_error(self, ctx: _MessageState, error: Any, _resolve: Any) -> None:
        """Handle agentic loop failure: cleanup, resolve future, deliver error."""
        session = ctx.session
        log.error("[%s] Agentic loop failed: %s", ctx.trace_id[:8], error)
        err_type = type(error).__name__ if isinstance(error, BaseException) else "unknown"
        self._error_counts[err_type] = self._error_counts.get(err_type, 0) + 1
        if metrics.ENABLED:
            metrics.ERRORS_TOTAL.labels(error_type=err_type).inc()
        # Strip transient image metadata before returning
        if ctx.image_blocks and ctx.user_msg_idx < len(session.messages):
            session.messages[ctx.user_msg_idx].pop("_image_blocks", None)
        # Roll back all messages the agentic loop added (assistant, tool_results,
        # system hints) so the session stays in a valid pre-loop state.
        if len(session.messages) > ctx.msg_count_before:
            del session.messages[ctx.msg_count_before:]
        # Remove orphaned user message to prevent consecutive-user corruption
        if session.messages and session.messages[-1]["role"] == "user":
            session.messages.pop()
        await self._session_mgr.save_state(session)
        _resolve({"error": str(error), "session_id": session.id})
        # Auto-close ephemeral sessions even on error — but only if the
        # session was created by this event (not a pre-existing session).
        if ctx.task_type in ("task", "system") and not ctx.session_preexisted:
            try:
                await self._session_mgr.close_session(ctx.session_key)
                log.info("Auto-closed %s session for %s", ctx.task_type, _log_safe(ctx.sender))
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason=f"auto_{ctx.task_type}").inc()
            except Exception:
                log.warning("Auto-close failed for %s session %s",
                            ctx.task_type, ctx.sender, exc_info=True)

    # ── Finalization ─────────────────────────────────────────────

    async def _finalize_response(self, ctx: _MessageState, _resolve: Any) -> None:
        """Post-loop work: persist, deliver, compact."""
        await self._persist_response(ctx)
        await self._deliver_reply(ctx, _resolve)
        await self._check_compaction_warning(ctx)
        await self._run_compaction_if_needed(ctx)
        await self._auto_close_if_ephemeral(ctx)

    async def _persist_response(self, ctx: _MessageState) -> None:
        """Persist new messages and restore text-only content."""
        session = ctx.session
        for msg in session.messages[ctx.msg_count_before:]:
            if msg["role"] == "assistant":
                await session.add_assistant_message(msg, persist_only=True)
            elif msg["role"] == "tool_results":
                await session.add_tool_results(msg["results"], persist_only=True)
        if ctx.image_blocks and ctx.user_msg_idx < len(session.messages):
            session.messages[ctx.user_msg_idx].pop("_image_blocks", None)
        await self._session_mgr.save_state(session)

    async def _deliver_reply(self, ctx: _MessageState, _resolve: Any) -> None:
        """Resolve HTTP future with reply content.

        Routing via reply_to:
        - "" (default): resolve the caller's HTTP future with the reply
        - "silent": process and persist, but mark reply as silent (log only)
        - "<sender>": resolve caller's future AND enqueue reply as system
          message into <sender>'s session through the normal pipeline
        """
        session = ctx.session
        response = ctx.response
        reply = response.text or ""
        if response.cost_limited and not reply.strip():
            reply = ("[cost limit reached — max_cost_per_message in lucyd.toml. "
                     "raise or set to 0 to disable.]")
        silent = _is_silent(reply, self._config.silent_tokens)
        token_info = {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        }
        reply_attachments = response.attachments or []

        # Route: silent — suppress delivery
        if ctx.reply_to == "silent":
            log.info("[%s] reply_to=silent — reply logged, not delivered", ctx.trace_id[:8])
            _resolve({"reply": reply, "silent": True, "session_id": session.id,
                       "tokens": token_info, "attachments": reply_attachments})
            return

        # Route: redirect — resolve caller AND enqueue reply into target session
        if ctx.reply_to:
            log.info("[%s] reply_to=%s — redirecting reply", ctx.trace_id[:8], _log_safe(ctx.reply_to))
            _resolve({"reply": reply, "session_id": session.id,
                       "tokens": token_info, "attachments": reply_attachments,
                       "redirected_to": ctx.reply_to})
            if reply.strip():
                await self._queue.put({
                    "text": reply,
                    "sender": ctx.reply_to,
                    "type": "system",
                    "channel_id": ctx.channel_id,
                    "task_type": "system",
                })
            return

        # Route: default — resolve HTTP future
        if silent:
            log.info("Silent reply suppressed: %s", _log_safe(reply[:100]))
            _resolve({"reply": reply, "silent": True, "session_id": session.id,
                       "tokens": token_info, "attachments": reply_attachments})
        else:
            _resolve({"reply": reply, "session_id": session.id,
                       "tokens": token_info, "attachments": reply_attachments})

    async def _check_compaction_warning(self, ctx: _MessageState) -> None:
        """Inject context-pressure warning at 80% threshold."""
        session = ctx.session
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

    async def _run_compaction_if_needed(self, ctx: _MessageState) -> None:
        """Run consolidation + compaction if threshold is exceeded."""
        session = ctx.session
        _needs_compact = ctx.force_compact or session.needs_compaction(
            self._config.compaction_threshold)
        if not _needs_compact:
            return
        if self._config.consolidation_enabled:
            try:
                result = await consolidation_mod.consolidate_session(
                    session_id=session.id,
                    messages=session.messages,
                    compaction_count=session.compaction_count,
                    config=self._config,
                    provider=self._get_provider("consolidation"),
                    context_builder=self._context_builder,
                    pool=self._pool,
                    client_id=self._client_id,
                    agent_id=self._agent_id,
                    metering=self._metering_db,
                    trace_id=ctx.trace_id,
                    converter=self._converter,
                )
                if result["facts_added"] or result.get("episode_id"):
                    log.info("consolidation: %d facts, episode=%s",
                             result["facts_added"], result.get("episode_id"))
            except Exception:
                log.warning("consolidation failed, continuing without", exc_info=True)
        prompt = self._config.compaction_prompt.replace(
            "{agent_name}", self._config.agent_name,
        ).replace("{max_tokens}", str(self._config.compaction_max_tokens))
        cost_ctx = CostContext(
            metering=self._metering_db,
            session_id=session.id,
            model_name=ctx.model_name,
            cost_rates=ctx.cost_rates,
            provider_name=ctx.provider_name,
            currency=ctx.currency,
            converter=self._converter,
        )
        _pre_tokens = session.last_input_tokens
        await self._session_mgr.compact_session(
            session, self._get_provider("compaction"), prompt,
            trace_id=ctx.trace_id,
            system_blocks=self._context_builder.build_stable(),
            cost=cost_ctx,
            keep_recent_pct=self._config.compaction_keep_pct,
            min_messages=4,
            tool_result_max_chars=2000,
            max_tokens=self._config.compaction_max_tokens,
        )
        if metrics.ENABLED:
            metrics.COMPACTION_TOTAL.inc()
            reclaimed = max(0, _pre_tokens - (session.last_input_tokens or 0))
            if reclaimed > 0:
                metrics.COMPACTION_TOKENS_RECLAIMED.observe(reclaimed)

    async def _auto_close_if_ephemeral(self, ctx: _MessageState) -> None:
        """Auto-close ephemeral sessions (task_type 'task' or 'system')."""
        if ctx.task_type in ("task", "system") and not ctx.force_compact and not ctx.session_preexisted:
            # Fire pre-close hook (e.g., evolution validation + rollback)
            if self._on_pre_close is not None:
                await self._on_pre_close(ctx.sender)
            try:
                await self._session_mgr.close_session(ctx.session_key)
                log.info("Auto-closed %s session for %s", ctx.task_type, _log_safe(ctx.sender))
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason=f"auto_{ctx.task_type}").inc()
            except Exception:
                log.warning("Auto-close failed for %s session %s",
                            ctx.task_type, ctx.sender, exc_info=True)

    # ── Entry Point ──────────────────────────────────────────────

    async def process_message(
        self,
        text: str,
        sender: str,
        source: str,
        attachments: list[Any] | None = None,
        response_future: asyncio.Future[dict[str, Any]] | None = None,
        trace_id: str = "",
        force_compact: bool = False,
        stream_queue: Any = None,
        deliver: bool = True,
        channel_id: str = "http",
        task_type: str = "conversational",
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
        if provider is None:
            log.error("[%s] No provider configured", trace_id[:8])
            _resolve({"error": "no provider configured"})
            return

        model_cfg = self._config.model_config("primary")

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
            source=source,
            trace_id=trace_id,
            channel_id=channel_id,
            task_type=task_type,
            reply_to=reply_to,
            session_key=session_key or f"{channel_id}:{sender}",
            deliver=deliver,
            image_blocks=image_blocks,
            model_cfg=model_cfg,
            model_name=model_cfg.get("model", ""),
            provider_name=model_cfg.get("provider", ""),
            cost_rates=model_cfg.get("cost_per_mtok", []),
            currency=model_cfg.get("currency", "EUR"),
            force_compact=force_compact,
        )

        # Session setup: get/create, inject warnings, add user message
        await self._setup_session(ctx)

        # Set structured log context for this message cycle
        set_log_context(
            agent_id=self._config.agent_id or self._config.agent_name,
            session_id=ctx.session.id if ctx.session else "",
            trace_id=trace_id,
        )

        # Build system prompt, recall, tools
        await self._build_context(ctx, provider)

        session = ctx.session

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
            async def _on_stream_delta(delta: Any) -> None:
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
            await self._run_agentic_with_retries(
                ctx, provider, monitor.on_response, monitor.on_tool_results, monitor.write,
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
                "channel_id": ctx.channel_id, "task_type": ctx.task_type,
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
