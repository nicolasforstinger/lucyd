#!/usr/bin/env python3
"""Lucyd — a daemon for persona-rich AI agents.

Entry point. Wires config → loop → tools → sessions.
Handles PID file, HTTP API, Unix signals, and the main event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import contextlib
import fcntl
import logging
import logging.handlers
import os
import re
import signal
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add lucyd directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import random

from agentic import is_transient_error, run_agentic_loop, run_single_shot
from config import Config, ConfigError, load_config
from context import ContextBuilder, _estimate_tokens
from log_utils import _log_safe
from metering import MeteringDB
from providers import create_provider
from session import SessionManager, _text_from_content
from skills import SkillLoader
from tools import ToolRegistry

log = logging.getLogger("lucyd")


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


# ─── Evolution helpers ────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def get_evolution_state(
    file_path: str,
    conn: "sqlite3.Connection",
) -> dict | None:
    """Return evolution state for *file_path*, or None if never evolved."""
    row = conn.execute(
        "SELECT last_evolved_at, content_hash, logs_through "
        "FROM evolution_state WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    if row is None:
        return None
    return {
        "last_evolved_at": row[0],
        "content_hash": row[1],
        "logs_through": row[2],
    }


def check_new_logs_exist(
    workspace: Path,
    conn: "sqlite3.Connection",
    reference_file: str = "MEMORY.md",
) -> tuple[bool, str]:
    """Check whether new daily logs exist since the last evolution.

    Uses the *reference_file* (default ``MEMORY.md``) to determine the
    ``logs_through`` date from the ``evolution_state`` table.

    Returns ``(has_new_logs, since_date)`` where *since_date* is the
    ``logs_through`` value (empty string if never evolved).
    """
    state = get_evolution_state(reference_file, conn)
    since_date = state["logs_through"] if state else ""

    memory_dir = workspace / "memory"
    if not memory_dir.is_dir():
        return False, since_date

    for entry in memory_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        m = _DATE_RE.search(entry.stem)
        if m and (not since_date or m.group(1) > since_date):
            return True, since_date

    return False, since_date


# ─── PID File ────────────────────────────────────────────────────

_pid_fd: int | None = None  # held for process lifetime


def _acquire_pid_file(path: Path) -> None:
    """Acquire exclusive lock on PID file. Exits if another instance holds it."""
    global _pid_fd
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another process holds the lock
        try:
            existing = os.read(os.open(str(path), os.O_RDONLY), 64).decode().strip()
        except Exception:
            existing = "?"
        sys.stderr.write(f"Another instance is running (PID {existing}). Exiting.\n")
        os.close(fd)
        sys.exit(1)
    # Lock acquired — write our PID (truncate first)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    _pid_fd = fd  # keep fd open → lock held


def _release_pid_file(path: Path) -> None:
    global _pid_fd
    if _pid_fd is not None:
        with contextlib.suppress(Exception):  # daemon shutdown cleanup; failure is benign
            fcntl.flock(_pid_fd, fcntl.LOCK_UN)
            os.close(_pid_fd)
        _pid_fd = None
    with contextlib.suppress(Exception):  # daemon shutdown cleanup; failure is benign
        path.unlink(missing_ok=True)



# ─── Silent Token Check ─────────────────────────────────────────

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


from attachments import ImageTooLarge, extract_document_text, fit_image
import metrics


class _MonitorWriter:
    """In-memory monitor state tracker for the /api/v1/monitor endpoint.

    Updates a shared dict on the daemon instead of writing JSON to disk.
    """

    __slots__ = ("_state", "_contact", "_session_id", "_trace_id", "_model",
                 "_turn", "_turn_started_at", "_message_started_at", "_turns")

    def __init__(self, state: dict, contact: str, session_id: str,
                 trace_id: str, model: str):
        self._state = state
        self._contact = contact
        self._session_id = session_id
        self._trace_id = trace_id
        self._model = model
        self._turn = 1
        self._turn_started_at = time.time()
        self._message_started_at = self._turn_started_at
        self._turns: list[dict] = []

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

    def on_response(self, response) -> None:
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

    def on_tool_results(self, results_msg) -> None:
        self._turn += 1
        self._turn_started_at = time.time()
        self.write("thinking")



@dataclass
class _MessageState:
    """Internal state bag for _process_message phases."""
    text: str
    sender: str
    source: str           # Message origin (channel name, "http", or "system")
    trace_id: str
    channel_id: str = "http"          # Envelope: which channel sent this
    task_type: str = "conversational"  # Envelope: session lifecycle intent
    reply_to: str = ""                # Envelope: response routing ("" = caller, "silent", or sender name)
    session_key: str = ""             # channel_id:sender — computed in _process_message
    deliver: bool = True
    image_blocks: list = field(default_factory=list)
    session: Any = None
    user_msg_idx: int = 0
    session_preexisted: bool = False
    model_cfg: dict = field(default_factory=dict)
    model_name: str = ""
    provider_name: str = ""
    cost_rates: list = field(default_factory=list)
    fmt_system: Any = None
    tools: list = field(default_factory=list)
    msg_count_before: int = 0
    response: Any = None
    force_compact: bool = False
    response_future: Any = None


# ─── Daemon ──────────────────────────────────────────────────────

class LucydDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.start_time = time.time()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._control_queue: asyncio.Queue = asyncio.Queue()
        self.provider: Any = None
        self._single_shot: bool = False
        self.session_mgr: SessionManager | None = None
        self._preprocessors: list[dict] = []
        self.context_builder: ContextBuilder | None = None
        self.skill_loader: SkillLoader | None = None
        self.tool_registry: ToolRegistry | None = None
        self._http_api: Any = None
        self._memory_conn: Any = None
        self._current_session: Any = None  # Set per-message for status tool callback
        self._session_locks: collections.OrderedDict[str, asyncio.Lock] = collections.OrderedDict()  # sender → lock
        self.metering_db: Any = None
        self._error_counts: dict[str, int] = {}  # error_type → count, for /api/v1/errors
        self._monitor_state: dict = {"state": "idle"}

    _MAX_SESSION_LOCKS = 1000  # Bound to prevent unbounded growth (P-018)

    def _get_session_lock(self, sender: str) -> asyncio.Lock:
        """Get or create a per-sender lock for session mutation safety."""
        if sender in self._session_locks:
            self._session_locks.move_to_end(sender)
            return self._session_locks[sender]
        lock = asyncio.Lock()
        self._session_locks[sender] = lock
        # Evict oldest unlocked entries when over hard cap
        while len(self._session_locks) > self._MAX_SESSION_LOCKS:
            oldest_key = next(iter(self._session_locks))
            oldest_lock = self._session_locks[oldest_key]
            if oldest_lock.locked():
                # Don't evict a lock that's actively held — skip it
                self._session_locks.move_to_end(oldest_key)
                break
            self._session_locks.pop(oldest_key)
        return lock

    def _setup_logging(self) -> None:
        """Configure logging to file + stderr.

        Supports log_format: "text" (default) or "json" (one JSON object per line).
        Activates PII-safe mode if configured.
        """
        log_file = self.config.log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # JSON logging format for Docker (stdout → Docker log driver)
        if self.config.log_format == "json":
            from log_utils import StructuredJSONFormatter
            fmt = StructuredJSONFormatter()
        else:
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_485_760,
            backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)

        # Stderr handler (for journald)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(fh)
        root.addHandler(sh)

        # Silence noisy third-party loggers (configurable via [logging] suppress)
        for name in self.config.logging_suppress:
            logging.getLogger(name).setLevel(logging.WARNING)

    def _init_provider(self) -> None:
        """Create the primary provider instance and determine dispatch mode."""
        self._providers: dict[str, Any] = {}
        try:
            self.provider = self._create_provider_for("primary")
            self._providers["primary"] = self.provider
            # Determine dispatch mode based on config + model capabilities
            caps = self.provider.capabilities if self.provider else None
            if self.config.agent_strategy == "single_shot" or (caps and not caps.supports_tools):
                self._single_shot = True
                log.info("Agent strategy: single shot")
            else:
                self._single_shot = False
                log.info("Agent strategy: agentic loop")
        except Exception as e:
            log.error("Failed to create provider: %s", e, exc_info=True)

    def _create_provider_for(self, model_name: str) -> Any:
        """Create a provider instance for a named model config."""
        model_cfg = self.config.model_config(model_name)
        provider_type = model_cfg.get("provider", "")
        api_key_env = model_cfg.get("api_key_env", "")
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not api_key and api_key_env:
            log.debug("API key env var checked: %s", api_key_env)
            raise ValueError(f"Required API key for model '{model_name}' not configured")
        provider = create_provider(model_cfg, api_key)
        log.info("Provider: %s / %s (role: %s)", provider_type, model_cfg.get("model", ""), model_name)
        return provider

    def get_provider(self, role: str = "primary") -> Any:
        """Get provider for a specific role, with lazy creation and caching.

        Roles: "primary", "compaction", "consolidation", "subagent".
        If no model override is configured for a role, returns the primary provider.
        """
        if role in self._providers:
            return self._providers[role]
        # Look up model override for this role
        role_attr = f"{role}_model"
        model_name = getattr(self.config, role_attr, "")
        if not model_name:
            return self.provider  # Default to primary
        try:
            provider = self._create_provider_for(model_name)
            self._providers[role] = provider
            return provider
        except Exception:
            log.warning("Failed to create provider for role '%s' (model '%s'), falling back to primary",
                        role, model_name, exc_info=True)
            return self.provider

    def _init_sessions(self) -> None:
        self.session_mgr = SessionManager(
            self.config.sessions_dir,
            agent_name=self.config.agent_name,
        )

    # Built-in tool modules and the tool names they provide.
    _TOOL_MODULES = [
        ("tools.filesystem",   {"read", "write", "edit"}),
        ("tools.shell",        {"exec"}),
        ("tools.web",          {"web_search", "web_fetch"}),
        ("tools.memory_read",  {"memory_search", "memory_get"}),
        ("tools.memory_write", {"memory_write", "memory_forget", "commitment_update"}),
        ("tools.agents",       {"sessions_spawn"}),
        ("skills",             {"load_skill"}),
        ("tools.status",       {"session_status"}),
    ]

    def _init_tools(self) -> None:
        """Register built-in tools and plugins.

        1. Process _TOOL_MODULES (built-in) via importlib.import_module.
        2. Scan plugins.d/*.py via importlib.util.spec_from_file_location.
        Both paths share the same configure + register logic.
        """
        import importlib
        import importlib.util
        import inspect

        # Derive max_result_tokens: ~25% of context for any single tool result
        max_ctx = self.provider.capabilities.max_context_tokens if self.provider else 0
        max_result_tokens = int(max_ctx * 0.25) if max_ctx > 0 else 0

        self.tool_registry = ToolRegistry(
            truncation_limit=self.config.output_truncation,
            max_result_tokens=max_result_tokens,
        )

        enabled = set(self.config.tools_enabled)

        # Shared resources — created once, passed to modules that need them
        memory = None
        conn = None
        if self.config.memory_db and (enabled & {
            "memory_search", "memory_get",
            "memory_write", "memory_forget", "commitment_update",
        }):
            from memory import MemoryInterface
            memory = MemoryInterface(
                db_path=str(Path(self.config.memory_db).expanduser()),
                embedding_api_key=self.config.embedding_api_key,
                embedding_model=self.config.embedding_model,
                embedding_base_url=self.config.embedding_base_url,
                embedding_timeout=self.config.embedding_timeout,
                top_k=self.config.memory_top_k,
                vector_search_limit=self.config.vector_search_limit,
                fts_min_results=3,
                sqlite_timeout=self.config.sqlite_timeout,
            )
            # Wire metering for embedding cost tracking
            if self.metering_db:
                memory.metering = self.metering_db
            if self.config.consolidation_enabled:
                conn = self._get_memory_conn()

        # Dependency dict — configure() pulls what it needs by parameter name
        deps = {
            "config": self.config,
            "provider": self.provider,
            "session_manager": self.session_mgr,
            "session_mgr": self.session_mgr,
            "tool_registry": self.tool_registry,
            "skill_loader": self.skill_loader,
            "memory": memory,
            "conn": conn,
            "get_provider": self.get_provider,
            "session_getter": lambda: self._current_session,
            "start_time": self.start_time,
            "metering": self.metering_db,
        }

        def _configure_and_register(module: Any, source: str = "") -> None:
            """Call configure() with inspect-based injection, register enabled tools."""
            configure_fn = getattr(module, "configure", None)
            if callable(configure_fn):
                sig = inspect.signature(configure_fn)
                kwargs = {k: v for k, v in deps.items() if k in sig.parameters}
                configure_fn(**kwargs)
            for t in getattr(module, "TOOLS", []):
                name = t.get("name", "")
                if name in enabled:
                    self.tool_registry.register(
                        t["name"], t["description"], t["input_schema"],
                        t["function"], t.get("max_output", 0),
                    )
                    if source:
                        log.info("Plugin tool registered: %s (from %s)", name, source)

        # ── Built-in tools ───────────────────────────────────────────
        for module_path, tool_names in self._TOOL_MODULES:
            if not (enabled & tool_names):
                continue
            module = importlib.import_module(module_path)
            _configure_and_register(module)

        # ── Plugin tools + preprocessors (plugins.d/*.py) ────────────
        plugins_path = self.config.config_dir / self.config.plugins_dir
        if plugins_path.is_dir():
            for plugin_file in sorted(plugins_path.glob("*.py")):
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"lucyd_plugin_{plugin_file.stem}", plugin_file,
                    )
                    if spec is None or spec.loader is None:
                        log.warning("Plugin: cannot load %s (invalid spec)", plugin_file.name)
                        continue

                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    has_tools = isinstance(getattr(module, "TOOLS", None), list)
                    has_preprocessors = isinstance(getattr(module, "PREPROCESSORS", None), list)

                    if not has_tools and not has_preprocessors:
                        log.debug("Plugin: %s has no TOOLS or PREPROCESSORS, skipping", plugin_file.name)
                        continue

                    _configure_and_register(module, source=plugin_file.name)

                    # Collect preprocessors
                    for pp in getattr(module, "PREPROCESSORS", []):
                        name = pp.get("name", plugin_file.stem)
                        fn = pp.get("fn")
                        if callable(fn):
                            self._preprocessors.append({"name": name, "fn": fn})
                            log.info("Plugin preprocessor registered: %s (from %s)", name, plugin_file.name)

                except Exception:
                    log.exception("Plugin: failed to load %s", plugin_file.name)

        log.info("Registered tools: %s", ", ".join(self.tool_registry.tool_names))
        if self._preprocessors:
            log.info("Registered preprocessors: %s",
                     ", ".join(pp["name"] for pp in self._preprocessors))

    def _get_memory_conn(self):
        """Get or create the memory DB connection.

        Connection is created once and reused for the daemon's lifetime.
        Uses WAL mode and Row factory.
        """
        if self._memory_conn is None:
            import sqlite3

            from memory_schema import ensure_schema
            self._memory_conn = sqlite3.connect(
                self.config.memory_db, timeout=self.config.sqlite_timeout,
            )
            self._memory_conn.execute("PRAGMA journal_mode=WAL")
            self._memory_conn.row_factory = sqlite3.Row
            ensure_schema(self._memory_conn)
        return self._memory_conn

    def _init_context(self) -> None:
        self.context_builder = ContextBuilder(
            workspace=self.config.workspace,
            stable_files=self.config.context_stable,
            semi_stable_files=self.config.context_semi_stable,
            max_system_tokens=self.config.max_system_tokens,
        )

    def _init_skills(self) -> None:
        self.skill_loader = SkillLoader(
            workspace=self.config.workspace,
            skills_dir=self.config.skills_dir,
        )
        self.skill_loader.scan()

    def _init_metering(self) -> None:
        """Initialize metering DB."""
        metering_path = str(self.config.metering_db)
        agent_id = self.config.agent_id or self.config.agent_name

        self.metering_db = MeteringDB(
            metering_path, agent_id=agent_id,
            sqlite_timeout=self.config.sqlite_timeout,
        )




    async def _run_preprocessors(self, text: str, attachments: list | None) -> tuple[str, list | None]:
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
                text, attachments = await pp["fn"](text, attachments, self.config)
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

    async def _process_attachments(self, text, attachments, provider):
        """Process attachments into text descriptions + image blocks.

        Returns (text, image_blocks).
        Audio is handled by preprocessor plugins before this runs.
        """
        image_blocks = []
        if attachments:
            supports_vision = provider.capabilities.supports_vision
            for att in attachments:
                if att.content_type.startswith("image/"):
                    text, block = self._process_image(text, att, supports_vision)
                    if block:
                        image_blocks.append(block)
                else:
                    text = self._process_document(text, att)
        return text, image_blocks

    def _process_image(self, text, att, supports_vision):
        """Process a single image attachment. Returns (text, block_or_None)."""
        too_large_msg = "image too large to display"
        if not supports_vision:
            return _append(text, "[image received — vision not available with current provider]"), None
        try:
            img_data = Path(att.local_path).read_bytes()
            try:
                img_data = fit_image(img_data, att.content_type,
                                     self.config.vision_max_image_bytes,
                                     self.config.vision_max_dimension,
                                     self.config.vision_jpeg_quality_steps,
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

    def _process_document(self, text, att):
        """Process a single document/file attachment. Returns updated text."""
        doc_text = None
        if self.config.documents_enabled:
            try:
                doc_text = extract_document_text(
                    att.local_path, att.content_type, att.filename or "",
                    max_chars=self.config.documents_max_chars,
                    max_bytes=self.config.documents_max_file_bytes,
                    text_extensions=self.config.documents_text_extensions,
                )
            except Exception as e:
                log.error("Document extraction failed for %s: %s", _log_safe(att.filename), e, exc_info=True)
        if doc_text:
            label = att.filename or "document"
            return _append(text, f"[document: {label}, saved: {att.local_path}]\n{doc_text}")
        return _append(text, f"[attachment: {att.filename or 'file'}, {att.content_type}, saved: {att.local_path}]")

    async def _setup_session(self, ctx: _MessageState) -> None:
        """Set up session state: get/create, inject warnings, add user message."""
        # Track whether session pre-existed (for auto-close decision).
        # Notifications routed to the primary session must not close it.
        ctx.session_preexisted = (
            self.session_mgr.has_session(ctx.session_key)
        )

        # Get or create session (keyed by channel_id:sender)
        session = self.session_mgr.get_or_create(ctx.session_key)
        ctx.session = session

        # Status tool reads session via callback (configured in _init_tools)
        self._current_session = session

        # Inject pending compaction warning from previous turn
        ctx.text, warning_consumed = _inject_warning(ctx.text, session.pending_system_warning)
        if warning_consumed:
            session.pending_system_warning = ""
            self.session_mgr.save_state(session)  # Persist cleared warning before agentic loop

        # Inject timestamp so the agent always knows the current time
        timestamp = time.strftime("[%a, %d. %b %Y - %H:%M %Z]")
        ctx.text = f"{timestamp}\n{ctx.text}"

        session.trace_id = ctx.trace_id
        session.add_user_message(ctx.text, sender=ctx.sender, source=ctx.source)

        # Transiently inject image content blocks for the API call
        ctx.user_msg_idx = len(session.messages) - 1

        # Merge consecutive user messages (recovery from prior errors, JSONL rebuild)
        while len(session.messages) >= 2 and session.messages[-2].get("role") == "user":
            prev_text = _text_from_content(session.messages[-2].get("content", ""))
            last_text = _text_from_content(session.messages[-1].get("content", ""))
            session.messages[-2]["content"] = prev_text + "\n" + last_text
            session.messages.pop()
            ctx.user_msg_idx = len(session.messages) - 1
            log.warning("Merged consecutive user messages in session %s", session.id)

        if ctx.image_blocks:
            session.messages[ctx.user_msg_idx]["_image_blocks"] = ctx.image_blocks

    async def _build_recall(self, ctx: _MessageState, provider) -> str:
        """Build recall text for fresh sessions via structured memory."""
        session = ctx.session
        if len(session.messages) > 1:
            return ""
        if not self.config.consolidation_enabled:
            return ""
        try:
            from memory import get_session_start_context
            conn = self._get_memory_conn()
            result = get_session_start_context(
                conn=conn, config=self.config,
                max_facts=self.config.recall_max_facts,
                max_episodes=self.config.recall_max_episodes_at_start,
                max_tokens=self.config.recall_max_dynamic_tokens,
            ) or ""
            if result and metrics.ENABLED:
                metrics.MEMORY_OPS_TOTAL.labels(operation="recall_triggered").inc()
            return result
        except Exception:
            log.exception("structured recall at session start failed")
            return "[Memory recall unavailable — use memory_search to access memory manually.]"

    async def _build_context(self, ctx: _MessageState, provider) -> None:
        """Build system prompt, recall, and tools list."""
        session = ctx.session
        tool_descs = self.tool_registry.get_brief_descriptions()
        skill_index = self.skill_loader.build_index() if self.skill_loader else ""
        always_on = self.config.always_on_skills
        skill_bodies = self.skill_loader.get_bodies(always_on) if self.skill_loader else {}

        recall_text = await self._build_recall(ctx, provider)

        system_blocks = self.context_builder.build(
            task_type=ctx.task_type,
            deliver=ctx.deliver,
            tool_descriptions=tool_descs,
            skill_index=skill_index,
            always_on_skills=always_on,
            skill_bodies=skill_bodies,
            extra_dynamic=recall_text,
            silent_tokens=self.config.silent_tokens,
            max_turns=self.config.max_turns,
            max_cost=self.config.max_cost_per_message,
            compaction_threshold=self.config.compaction_threshold,
            has_images=bool(ctx.image_blocks),
            sender=ctx.sender,
        )
        ctx.fmt_system = provider.format_system(system_blocks)

        # Runtime context budget report
        try:
            max_ctx = provider.capabilities.max_context_tokens
            if not isinstance(max_ctx, int):
                max_ctx = 0
        except (AttributeError, TypeError):
            max_ctx = 0
        if max_ctx > 0:
            sys_tokens = sum(_estimate_tokens(b.get("text", "")) for b in system_blocks)
            history_tokens = sum(
                _estimate_tokens(_text_from_content(m.get("text", "") or m.get("content", "")))
                for m in session.messages
            )
            tool_def_tokens = sum(
                _estimate_tokens(t["description"]) for t in self.tool_registry.get_schemas()
            ) if provider.capabilities.supports_tools else 0
            used = sys_tokens + history_tokens + tool_def_tokens
            remaining = max_ctx - used
            log.debug(
                "Context budget [%s]: total=%d | system=%d | history=%d (%d msgs) "
                "| tools=%d | used=%d | remaining=%d",
                ctx.trace_id[:8], max_ctx, sys_tokens, history_tokens,
                len(session.messages), tool_def_tokens, used, remaining,
            )
            if metrics.ENABLED and isinstance(used, (int, float)):
                metrics.CONTEXT_UTILIZATION.labels(
                    channel_id=ctx.channel_id, task_type=ctx.task_type,
                    session_id=session.id if session else "",
                    sender=ctx.sender,
                ).observe(used / max_ctx if max_ctx > 0 else 0)

        # Run agentic loop — it appends to session.messages in place
        # If model doesn't support tools, degrade gracefully (no tools sent)
        try:
            supports_tools = provider.capabilities.supports_tools
        except (AttributeError, TypeError):
            supports_tools = True  # default to true for mock/legacy providers
        if supports_tools:
            ctx.tools = self.tool_registry.get_schemas()
        else:
            ctx.tools = []

        # Snapshot message count to track what the loop added
        ctx.msg_count_before = len(session.messages)

    async def _run_agentic_with_retries(self, ctx: _MessageState, provider,
                                         on_response, on_tool_results,
                                         write_monitor, on_stream_delta=None) -> None:
        """Run the agentic loop with message-level retries. Sets ctx.response."""
        session = ctx.session
        message_retries = self.config.message_retries
        message_retry_delay = self.config.message_retry_base_delay

        # Snapshot message count so retries can restore to a clean state.
        # The agentic loop mutates session.messages in-place; on failure
        # those partial turns must be stripped before the next attempt.
        pre_attempt_len = len(session.messages)

        for msg_attempt in range(1 + message_retries):
            try:
                max_cost = self.config.max_cost_per_message
                from providers import CostContext
                cost_ctx = CostContext(
                    metering=self.metering_db,
                    session_id=session.id,
                    model_name=ctx.model_name,
                    cost_rates=ctx.cost_rates,
                    provider_name=ctx.provider_name,
                )
                from agentic import LoopConfig
                loop_cfg = LoopConfig(
                    max_turns=self.config.max_turns,
                    timeout=self.config.agent_timeout,
                    api_retries=self.config.api_retries,
                    api_retry_base_delay=self.config.api_retry_base_delay,
                    sqlite_timeout=self.config.sqlite_timeout,
                    max_cost=float(max_cost),
                    max_context_for_tools=self.config.max_context_for_tools,
                    tool_call_retry=self.config.tool_call_retry,
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
                        tool_executor=self.tool_registry,
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
                        tool_executor=self.tool_registry,
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

    async def _handle_agentic_error(self, ctx: _MessageState, error, _resolve) -> None:
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
        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.pop()
        self.session_mgr.save_state(session)
        _resolve({"error": str(error), "session_id": session.id})
        # Auto-close ephemeral sessions even on error — but only if the
        # session was created by this event (not a pre-existing session).
        if ctx.task_type in ("task", "system") and not ctx.session_preexisted:
            try:
                await self.session_mgr.close_session(ctx.session_key)
                log.info("Auto-closed %s session for %s", ctx.task_type, _log_safe(ctx.sender))
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason=f"auto_{ctx.task_type}").inc()
            except Exception:
                log.warning("Auto-close failed for %s session %s",
                            ctx.task_type, ctx.sender, exc_info=True)

    async def _finalize_response(self, ctx: _MessageState, _resolve) -> None:
        """Post-loop work: persist, deliver, compact."""
        self._persist_response(ctx)
        await self._deliver_reply(ctx, _resolve)
        self._check_compaction_warning(ctx)
        await self._run_compaction_if_needed(ctx)
        await self._auto_close_if_ephemeral(ctx)

    def _persist_response(self, ctx: _MessageState) -> None:
        """Persist new messages and restore text-only content."""
        session = ctx.session
        for msg in session.messages[ctx.msg_count_before:]:
            role = msg.get("role", "")
            if role == "assistant":
                session.add_assistant_message(msg, persist_only=True)
            elif role == "tool_results":
                session.add_tool_results(msg.get("results", []), persist_only=True)
        if ctx.image_blocks and ctx.user_msg_idx < len(session.messages):
            session.messages[ctx.user_msg_idx].pop("_image_blocks", None)
        self.session_mgr.save_state(session)

    async def _deliver_reply(self, ctx: _MessageState, _resolve) -> None:
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
        silent = _is_silent(reply, self.config.silent_tokens)
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
                await self.queue.put({
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

    def _check_compaction_warning(self, ctx: _MessageState) -> None:
        """Inject context-pressure warning at 80% threshold."""
        session = ctx.session
        if _should_warn_context(
            input_tokens=session.last_input_tokens,
            compaction_threshold=self.config.compaction_threshold,
            needs_compaction=session.needs_compaction(self.config.compaction_threshold),
            already_warned=session.warned_about_compaction,
            warning_pct=0.8,
        ):
            try:
                max_ctx = self.provider.capabilities.max_context_tokens if self.provider else 0
                if not isinstance(max_ctx, int):
                    max_ctx = 0
            except (AttributeError, TypeError):
                max_ctx = 0
            pct = session.last_input_tokens * 100 // max_ctx if max_ctx > 0 else 0
            session.pending_system_warning = (
                f"[system: context at {session.last_input_tokens:,} tokens "
                f"({pct}% of capacity). compaction will summarize older messages "
                f"at {self.config.compaction_threshold:,}. save anything important "
                f"to memory files, then continue the conversation normally.]"
            )
            session.warned_about_compaction = True
            self.session_mgr.save_state(session)
            log.info("Compaction warning set for session %s at %d tokens",
                     session.id, session.last_input_tokens)

    async def _run_compaction_if_needed(self, ctx: _MessageState) -> None:
        """Run consolidation + compaction if threshold is exceeded."""
        session = ctx.session
        _needs_compact = ctx.force_compact or session.needs_compaction(
            self.config.compaction_threshold)
        if not _needs_compact:
            return
        if self.config.consolidation_enabled:
            try:
                import consolidation
                conn = self._get_memory_conn()
                result = await consolidation.consolidate_session(
                    session_id=session.id,
                    messages=session.messages,
                    compaction_count=session.compaction_count,
                    config=self.config,
                    provider=self.get_provider("consolidation"),
                    context_builder=self.context_builder,
                    conn=conn,
                    metering=self.metering_db,
                    trace_id=ctx.trace_id,
                )
                if result["facts_added"] or result.get("episode_id"):
                    log.info("consolidation: %d facts, episode=%s",
                             result["facts_added"], result.get("episode_id"))
            except Exception:
                log.exception("consolidation failed, continuing without")
        prompt = self.config.compaction_prompt.replace(
            "{agent_name}", self.config.agent_name,
        ).replace("{max_tokens}", str(self.config.compaction_max_tokens))
        from providers import CostContext
        cost_ctx = CostContext(
            metering=self.metering_db,
            session_id=session.id,
            model_name=ctx.model_name,
            cost_rates=ctx.cost_rates,
            provider_name=ctx.provider_name,
        )
        _pre_tokens = session.last_input_tokens
        await self.session_mgr.compact_session(
            session, self.get_provider("compaction"), prompt,
            trace_id=ctx.trace_id,
            system_blocks=self.context_builder.build_stable(),
            cost=cost_ctx,
            keep_recent_pct=self.config.compaction_keep_pct,
            min_messages=4,
            tool_result_max_chars=2000,
            max_tokens=self.config.compaction_max_tokens,
        )
        if metrics.ENABLED and isinstance(_pre_tokens, int):
            metrics.COMPACTION_TOTAL.inc()
            reclaimed = max(0, _pre_tokens - (session.last_input_tokens or 0))
            if reclaimed > 0:
                metrics.COMPACTION_TOKENS_RECLAIMED.observe(reclaimed)

    async def _auto_close_if_ephemeral(self, ctx: _MessageState) -> None:
        """Auto-close ephemeral sessions (task_type 'task' or 'system')."""
        if ctx.task_type in ("task", "system") and not ctx.force_compact and not ctx.session_preexisted:
            try:
                await self.session_mgr.close_session(ctx.session_key)
                log.info("Auto-closed %s session for %s", ctx.task_type, _log_safe(ctx.sender))
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason=f"auto_{ctx.task_type}").inc()
            except Exception:
                log.warning("Auto-close failed for %s session %s",
                            ctx.task_type, ctx.sender, exc_info=True)

    async def _process_message(
        self,
        text: str,
        sender: str,
        source: str,
        attachments: list | None = None,
        response_future: asyncio.Future | None = None,
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

        def _resolve(result: dict) -> None:
            """Safely resolve the HTTP response future."""
            if response_future is not None and not response_future.done():
                response_future.set_result(result)

        provider = self.provider
        if provider is None:
            log.error("[%s] No provider configured", trace_id[:8])
            _resolve({"error": "no provider configured"})
            return

        model_cfg = self.config.model_config("primary")

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
            force_compact=force_compact,
            response_future=response_future,
        )

        # Session setup: get/create, inject warnings, add user message
        await self._setup_session(ctx)

        # Set structured log context for this message cycle
        from log_utils import set_log_context
        set_log_context(
            agent_id=self.config.agent_id or self.config.agent_name,
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
            async def _on_stream_delta(delta):
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
            try:
                _sid = ctx.session.id if ctx.session else ""
                _labels = {
                    "channel_id": ctx.channel_id, "task_type": ctx.task_type,
                    "session_id": _sid, "sender": ctx.sender,
                }
                metrics.MESSAGES_TOTAL.labels(**_labels).inc()
                metrics.MESSAGE_DURATION.labels(**_labels).observe(time.time() - _msg_start)
                if ctx.response:
                    turns = getattr(ctx.response, "turns", 0)
                    if isinstance(turns, int) and turns > 0:
                        metrics.AGENTIC_TURNS.labels(**_labels).observe(turns)
                    u = getattr(ctx.response, "usage", None)
                    if u and isinstance(getattr(u, "input_tokens", None), int):
                        _cost = 0.0
                        if ctx.cost_rates and len(ctx.cost_rates) >= 2:
                            _cost = (u.input_tokens * ctx.cost_rates[0]
                                     + u.output_tokens * ctx.cost_rates[1]) / 1_000_000
                            if len(ctx.cost_rates) >= 3:
                                _cost += u.cache_read_tokens * ctx.cost_rates[2] / 1_000_000
                        metrics.MESSAGE_COST.labels(**_labels).observe(_cost)
            except Exception:
                log.debug("Metrics recording failed", exc_info=True)

    async def _consolidate_on_close(self, session) -> None:
        """Consolidation callback fired before session archival."""
        try:
            import consolidation
            conn = self._get_memory_conn()
            start_idx, end_idx = consolidation.get_unprocessed_range(
                session.id, session.messages, session.compaction_count, conn,
            )
            if end_idx > start_idx:
                await consolidation.consolidate_session(
                    session_id=session.id,
                    messages=session.messages,
                    compaction_count=session.compaction_count,
                    config=self.config,
                    provider=self.get_provider("consolidation"),
                    context_builder=self.context_builder,
                    conn=conn,
                    metering=self.metering_db,
                )
        except Exception:
            log.exception("consolidation on close failed")

    async def _reset_session(self, target: str, by_id: bool = False) -> dict:
        """Reset session by target: 'all', session ID, or contact name.

        When by_id=True, target is treated as a session ID directly.
        Otherwise, UUIDs are auto-detected and routed to close_session_by_id.
        Returns result dict with reset status.
        """
        if not self.session_mgr:
            return {"reset": False, "reason": "no session manager"}

        if target == "all":
            contacts = self.session_mgr.list_contacts()
            for contact in contacts:
                await self.session_mgr.close_session(contact)
            if metrics.ENABLED:
                for _ in contacts:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason="manual").inc()
            return {"reset": True, "target": "all", "count": len(contacts)}

        if by_id or _is_uuid(target):
            if await self.session_mgr.close_session_by_id(target):
                if metrics.ENABLED:
                    metrics.SESSION_CLOSE_TOTAL.labels(reason="manual").inc()
                return {"reset": True, "target": target, "type": "session_id"}
            return {"reset": False, "reason": f"no session found for ID: {target}"}

        # "user" shortcut: find primary operator contact (non-HTTP channel)
        if target == "user":
            for contact in self.session_mgr.list_contacts():
                # Skip HTTP API senders (automations, webhooks, system tasks)
                if contact.startswith("http:"):
                    continue
                target = contact
                break
            else:
                return {"reset": False, "reason": "no user session found"}

        if await self.session_mgr.close_session(target):
            if metrics.ENABLED:
                metrics.SESSION_CLOSE_TOTAL.labels(reason="manual").inc()
            return {"reset": True, "target": target, "type": "contact"}
        return {"reset": False, "reason": f"no session found for: {target}"}

    async def _process_reset_item(self, item: dict) -> None:
        """Parse a reset queue item, execute reset, resolve future."""
        if item.get("all"):
            target, by_id = "all", False
        elif item.get("session_id"):
            target, by_id = item["session_id"], True
        else:
            target, by_id = item.get("sender", ""), False
        result = await self._reset_session(target, by_id=by_id)
        log.info("Reset: %s", result)
        reset_future = item.get("response_future")
        if reset_future is not None and not reset_future.done():
            reset_future.set_result(result)

    async def _drain_control_queue(self) -> None:
        """Process all pending control items (resets).

        Called between message processings so resets don't wait behind
        long-running agentic loops in the main message queue.
        """
        while True:
            try:
                item = self._control_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item.get("type") == "reset":
                await self._process_reset_item(item)

    def _build_sessions(self) -> list[dict]:
        """Build session list for HTTP /sessions."""
        from session import build_session_info

        if not self.session_mgr:
            return []

        max_ctx = self.provider.capabilities.max_context_tokens if self.provider else 0

        result = []
        for contact, entry in self.session_mgr.get_index().items():
            session_id = entry.get("session_id", "")
            live = self.session_mgr.get_loaded(contact)
            info = build_session_info(
                sessions_dir=self.session_mgr.dir,
                session_id=session_id,
                session=live,
                metering=self.metering_db,
                max_context_tokens=max_ctx,
            )
            info["contact"] = contact
            info["created_at"] = entry.get("created_at")
            if live:
                info["model"] = live.model
            result.append(info)
        return result

    def _sweep_expired_media(self) -> None:
        """Delete media downloads older than media_ttl_hours."""
        ttl = 24 * 3600
        download_dir = Path(self.config.http_download_dir)
        if not download_dir.exists():
            return
        cutoff = time.time() - ttl
        swept = 0
        for f in download_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    swept += 1
            except OSError:
                pass
        if swept:
            log.info("Media sweep: deleted %d files older than 24h", swept)

    def _build_monitor(self) -> dict:
        """Build monitor data for HTTP /monitor."""
        return dict(self._monitor_state)

    def _build_history(self, target: str, full: bool = False) -> dict:
        """Build session history for HTTP /sessions/{target}/history.

        Target can be a session UUID or a contact name (case-insensitive).
        """
        from session import read_history_events

        if not self.session_mgr:
            return {"session_id": target, "events": []}

        # Resolve contact name to session ID
        session_id = target
        index = self.session_mgr.get_index()
        if target in index:
            session_id = index[target].get("session_id", target)
        else:
            # Case-insensitive lookup
            for key, entry in index.items():
                if key.lower() == target.lower():
                    session_id = entry.get("session_id", target)
                    break

        events = read_history_events(self.session_mgr.dir, session_id, full=full)
        return {"session_id": session_id, "events": events}

    def _build_status(self) -> dict:
        """Build status dict for HTTP /status."""
        from config import today_start_ts

        today_cost = 0.0
        rows = self.metering_db.query(
            "SELECT SUM(cost) FROM costs WHERE timestamp >= ?",
            (today_start_ts(),),
        )
        if rows and rows[0][0]:
            today_cost = rows[0][0]

        active_sessions = 0
        if self.session_mgr:
            active_sessions = self.session_mgr.session_count()

        # Update Prometheus gauges
        if metrics.ENABLED:
            metrics.UPTIME.set(time.time() - self.start_time)
            metrics.ACTIVE_SESSIONS.set(active_sessions)
            metrics.QUEUE_DEPTH.set(self.queue.qsize())

        return {
            "status": "ok",
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.start_time),
            "model": self.config.model_config("primary").get("model", ""),
            "active_sessions": active_sessions,
            "today_cost": round(today_cost, 4),
            "queue_depth": self.queue.qsize(),
            "error_counts": dict(self._error_counts),
        }

    async def _handle_compact(self) -> dict:
        """Force-compact the primary session after agent writes diary."""
        # Find primary session (longest active, non-automation sender)
        primary = None
        for contact in self.session_mgr.list_contacts():
            # Skip HTTP API senders (system tasks, automations, webhooks)
            if contact.startswith("http:"):
                continue
            session = self.session_mgr.get_or_create(contact)
            if primary is None or len(session.messages) > len(primary[1].messages):
                primary = (contact, session)

        if not primary:
            return {"status": "skipped", "reason": "no active session"}

        contact, session = primary
        today = time.strftime("%Y-%m-%d")
        diary_text = self.config.diary_prompt.replace("{date}", today)

        tid = str(uuid.uuid4())
        log.info("[%s] Forced compact: diary + compaction for session %s (%s)",
                 tid[:8], session.id, contact)

        async with self._get_session_lock(contact):
            await self._process_message(
                text=diary_text,
                sender=contact,
                source="system",
                deliver=False,
                trace_id=tid,
                force_compact=True,
                task_type="system",
                session_key=contact,  # already a session key from list_contacts
            )
        return {"status": "completed", "session": session.id}

    async def _handle_evolve(self, *, force: bool = False) -> dict:
        """Handle evolution request — push to queue for self-driven evolution.

        Args:
            force: Skip the pre-check for new daily logs.
        """
        if not force:
            try:
                db_path = self.config.memory_db
                workspace = self.config.workspace
                if db_path and Path(db_path).exists():
                    conn = self._get_memory_conn()
                    has_new, since_date = check_new_logs_exist(workspace, conn)
                    if not has_new:
                        return {"status": "skipped", "reason": f"no new daily logs since {since_date or 'ever'}"}
            except Exception:
                log.warning("Evolution pre-check failed, proceeding anyway", exc_info=True)

        msg = {
            "type": "system",
            "sender": "evolution",
            "text": (
                "[AUTOMATED SYSTEM MESSAGE] "
                "Load the evolution skill and evolve your memory files. "
                "New daily logs are available."
            ),
        }
        await self.queue.put(msg)
        return {"status": "queued", "session": "evolution"}

    async def _handle_index(self, full: bool = False) -> dict:
        """Run workspace indexing in a blocking thread."""
        from async_utils import run_blocking
        from tools.indexer import configure as indexer_configure
        from tools.indexer import index_workspace

        indexer_configure(
            chunk_size=self.config.indexer_chunk_size,
            chunk_overlap=self.config.indexer_chunk_overlap,
            embed_batch_limit=self.config.indexer_embed_batch_limit,
            embedding_model=self.config.embedding_model,
            embedding_base_url=self.config.embedding_base_url,
            embedding_provider=self.config.embedding_provider,
        )

        summary = await run_blocking(
            index_workspace,
            workspace=self.config.workspace,
            db_path=self.config.memory_db,
            api_key=self.config.embedding_api_key,
            force=full,
            embedding_timeout=self.config.embedding_timeout,
            sqlite_timeout=self.config.sqlite_timeout,
        )
        return summary

    async def _handle_index_status(self) -> dict:
        """Return workspace index status."""
        from tools.indexer import get_index_status
        return get_index_status(self.config.memory_db, self.config.workspace)

    async def _handle_consolidate(self) -> dict:
        """Run memory consolidation — extract facts from workspace files."""
        conn = self._get_memory_conn()
        from consolidation import extract_from_file
        from tools.indexer import scan_workspace

        provider = self.get_provider("consolidation")
        fact_model_cfg = self.config.model_config("primary") if hasattr(self.config, "model_config") else {}
        model_name = fact_model_cfg.get("model", "primary")
        cost_rates = fact_model_cfg.get("cost_per_mtok", [])

        file_list = scan_workspace(
            self.config.workspace,
            include_patterns=self.config.indexer_include_patterns,
            exclude_dirs=set(self.config.indexer_exclude_dirs),
        )

        total_facts = 0
        files_with_facts = 0
        for rel_path, abs_path in file_list:
            try:
                count = await extract_from_file(
                    str(abs_path), provider, conn,
                    self.config.consolidation_confidence_threshold,
                    model_name=model_name,
                    cost_rates=cost_rates,
                    metering=self.metering_db,
                )
                if count:
                    files_with_facts += 1
                    log.info("Extracted %d facts from %s", count, rel_path)
                total_facts += count
            except Exception:
                log.exception("Failed to process %s", rel_path)

        return {"status": "completed", "facts": total_facts,
                "files_scanned": len(file_list), "files_with_facts": files_with_facts}

    async def _handle_maintain(self) -> dict:
        """Run memory maintenance + metering retention."""
        from async_utils import run_blocking
        from memory import run_maintenance

        conn = self._get_memory_conn()

        def _run():
            return run_maintenance(conn, self.config.maintenance_stale_threshold_days)

        stats = await run_blocking(_run)

        # Metering retention — runs in async context (safe to use shared instance)
        if self.metering_db:
            stats["metering_deleted"] = self.metering_db.enforce_retention(12)
        else:
            stats["metering_deleted"] = 0

        log.info("Maintenance stats: %s", stats)
        return stats

    async def _message_loop(self) -> None:
        """Main message processing loop — sequential."""
        debounce_s = self.config.debounce_ms / 1000.0
        pending: dict[str, list] = {}  # sender → [messages]

        async def drain_pending(sender: str):
            msgs = pending.pop(sender, [])
            if not msgs:
                return
            # Combine into one
            combined_text = "\n".join(m["text"] for m in msgs if m["text"])
            # Collect attachments from all debounced messages
            combined_attachments = []
            for m in msgs:
                if m.get("attachments"):
                    combined_attachments.extend(m["attachments"])
            first = msgs[0]
            source = first.get("source", "")
            deliver = first.get("deliver", True)
            channel_id = first.get("channel_id", "http")
            task_type = first.get("task_type", "conversational")
            reply_to = first.get("reply_to", "")
            tid = str(uuid.uuid4())
            sk = f"{channel_id}:{sender}"
            log.info("[%s] Processing message from %s (source=%s channel=%s)",
                     tid[:8], _log_safe(sender), _log_safe(source), channel_id)
            async with self._get_session_lock(sk):
                await self._process_message(
                    combined_text, sender, source,
                    attachments=combined_attachments or None,
                    trace_id=tid,
                    deliver=deliver,
                    channel_id=channel_id,
                    task_type=task_type,
                    reply_to=reply_to,
                )

        async def process_http_immediate(item: dict) -> None:
            """Process HTTP /chat messages immediately (no debounce).

            Each /chat request has its own Future — combining messages
            would lose Futures and break response delivery.
            """
            tid = str(uuid.uuid4())
            http_sender = item.get("sender", "http")
            channel_id = item.get("channel_id", "http")
            task_type = item.get("task_type", "conversational")
            reply_to = item.get("reply_to", "")
            sk = f"{channel_id}:{http_sender}"
            log.info("[%s] Processing HTTP message from %s (channel=%s)",
                     tid[:8], _log_safe(http_sender), channel_id)
            async with self._get_session_lock(sk):
                await self._process_message(
                    text=item.get("text", ""),
                    sender=http_sender,
                    source=item.get("type", "http"),
                    attachments=item.get("attachments"),
                    response_future=item.get("response_future"),
                    trace_id=tid,
                    stream_queue=item.get("stream_queue"),
                    deliver=False,
                    channel_id=channel_id,
                    task_type=task_type,
                    reply_to=reply_to,
                )

        while self.running:
            # Drain control queue first (resets bypass message queue)
            await self._drain_control_queue()

            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Sentinel from channel reader — drain pending and exit
            if item is None:
                for s in list(pending.keys()):
                    await drain_pending(s)
                self.running = False
                break

            if not isinstance(item, dict):
                continue

            # Handle session reset (safety net — normally via control queue)
            if item.get("type") == "reset":
                await self._process_reset_item(item)
                continue

            # Forced compact — find primary session, diary + compact
            if item.get("type") == "compact":
                result = await self._handle_compact()
                log.info("Compact: %s", result)
                compact_future = item.get("response_future")
                if compact_future is not None and not compact_future.done():
                    compact_future.set_result(result)
                continue

            # HTTP /chat — process immediately, bypass debouncing
            if item.get("response_future") is not None:
                await process_http_immediate(item)
                continue

            # Queued message (from HTTP /message, /system, /notify)
            sender = item.get("sender", "system")
            source = item.get("type", "system")
            text = item.get("text", "")
            attachments = item.get("attachments")

            # Delivery: user messages deliver, system messages don't
            deliver = source not in ("system", "http")

            # Route notifications to primary session
            if item.get("notify") and self.config.notify_target:
                notify_target = self.config.notify_target
                # Find operator's existing session key (e.g., "telegram:Nicolas")
                for contact in self.session_mgr.list_contacts():
                    if contact.endswith(f":{notify_target}"):
                        sender = contact.split(":", 1)[1]
                        item["channel_id"] = contact.split(":", 1)[0]
                        break
                else:
                    sender = notify_target
                deliver = True

            if not text and not attachments:
                continue

            # Debounce: collect messages from same sender
            if sender not in pending:
                pending[sender] = []
            pending[sender].append({
                "text": text, "source": source, "deliver": deliver,
                "channel_id": item.get("channel_id", "http"),
                "task_type": item.get("task_type", "conversational"),
                "reply_to": item.get("reply_to", ""),
                "attachments": attachments,
            })

            # Wait for more messages
            await asyncio.sleep(debounce_s)

            # Drain all pending senders
            for s in list(pending.keys()):
                await drain_pending(s)

            # Drain control queue after message processing
            await self._drain_control_queue()

    def _setup_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register Unix signal handlers."""
        def handle_sigusr1():
            log.info("SIGUSR1: reloading workspace files")
            if self.skill_loader:
                self.skill_loader.scan()

        def handle_sigterm():
            log.info("SIGTERM: shutting down gracefully")
            self.running = False

        try:
            loop.add_signal_handler(signal.SIGUSR1, handle_sigusr1)
            loop.add_signal_handler(signal.SIGTERM, handle_sigterm)
            loop.add_signal_handler(signal.SIGINT, handle_sigterm)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    async def run(self) -> None:
        """Main entry point — starts all components and runs forever."""
        cfg = self.config
        pid_path = cfg.state_dir / "lucyd.pid"

        self._setup_logging()
        log.info("Starting Lucyd daemon for '%s'", cfg.agent_name)

        # Validate data_dir — all persistent state lives here
        data_dir = cfg.data_dir
        log.info("Data directory: %s", data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(data_dir, os.W_OK):
            raise RuntimeError(f"Data directory not writable: {data_dir}")

        _acquire_pid_file(pid_path)

        try:
            self._init_provider()
            self._init_sessions()
            self._init_skills()
            self._init_context()
            self._init_metering()
            self._init_tools()

            # Sweep expired media downloads
            self._sweep_expired_media()

            # Register consolidation on session close
            if self.config.consolidation_enabled:
                self.session_mgr.on_close(self._consolidate_on_close)

            loop = asyncio.get_event_loop()
            self._setup_signals(loop)

            # Start HTTP API (always on)
            from api import HTTPApi
            self._http_api = HTTPApi(
                queue=self.queue,
                control_queue=self._control_queue,
                host=cfg.http_host,
                port=cfg.http_port,
                auth_token=cfg.http_auth_token,
                agent_timeout=cfg.agent_timeout,
                get_status=self._build_status,
                get_sessions=self._build_sessions,
                get_monitor=self._build_monitor,
                get_history=self._build_history,
                handle_evolve=self._handle_evolve,
                handle_index=self._handle_index,
                handle_index_status=self._handle_index_status,
                handle_consolidate=self._handle_consolidate,
                handle_maintain=self._handle_maintain,
                download_dir=cfg.http_download_dir,
                max_body_bytes=cfg.http_max_body_bytes,
                max_attachment_bytes=cfg.http_max_attachment_bytes,
                rate_limit=cfg.http_rate_limit,
                rate_window=cfg.http_rate_window,
                status_rate_limit=cfg.http_status_rate_limit,
                rate_cleanup_threshold=1000,
                agent_name=cfg.agent_name,
                metering_db=self.metering_db,
            )
            await self._http_api.start()

            log.info("Lucyd daemon running (PID %d)", os.getpid())

            # Main message processing loop
            await self._message_loop()

            # Cleanup
            try:
                await asyncio.wait_for(self._http_api.stop(), timeout=5.0)
            except TimeoutError:
                log.warning("HTTP API shutdown timed out after 5s")
        except Exception as e:
            log.error("Fatal error: %s", e, exc_info=True)
            raise
        finally:
            # Persist active session state before cleanup.
            # Does NOT call close_session() (which triggers LLM consolidation
            # callbacks and archival — wrong during shutdown). Sessions resume
            # from state files on next startup via get_or_create().
            if hasattr(self, "session_mgr") and self.session_mgr:
                for session in self.session_mgr.list_sessions():
                    with contextlib.suppress(Exception):  # session state persist on shutdown; failure is benign
                        self.session_mgr.save_state(session)

            # Close metering DB connection
            if hasattr(self, "metering_db") and self.metering_db:
                with contextlib.suppress(Exception):  # DB close on shutdown; failure is benign
                    self.metering_db.close()
            # Close memory DB connection
            if self._memory_conn is not None:
                with contextlib.suppress(Exception):  # DB close on shutdown; failure is benign
                    self._memory_conn.close()
            _release_pid_file(pid_path)
            log.info("Lucyd daemon stopped")


# ─── CLI Entry Point ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Lucyd — a daemon for persona-rich AI agents",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("LUCYD_CONFIG", "./lucyd.toml"),
        help="Path to config file (default: $LUCYD_CONFIG or ./lucyd.toml)",
    )
    args = parser.parse_args()

    # Build overrides from CLI args
    overrides = {}

    try:
        config = load_config(args.config, overrides=overrides)
    except ConfigError as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(1)

    daemon = LucydDaemon(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
