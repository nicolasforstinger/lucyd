#!/usr/bin/env python3
"""Lucyd — a daemon for persona-rich AI agents.

Entry point. Wires config → loop → tools → sessions.
Handles PID file, HTTP API, Unix signals, and the main event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import importlib
import importlib.util
import inspect
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import asyncpg
import httpx

import db as lucyd_db
import metrics
import operations as ops
from api import HTTPApi
from config import Config, ConfigError, EPHEMERAL_TALKERS, load_config
from context import ContextBuilder, _estimate_tokens
from conversion import CurrencyConverter
from log_utils import StructuredJSONFormatter, _log_safe, set_pii_safe
from memory import MemoryInterface
from metering import MeteringDB
from pipeline import MessagePipeline
from plugins import PreprocessorSpec, verify_plugin_declared_state
from providers import LLMProvider, create_provider
from session import Session, SessionManager, build_session_info, read_history_events
from skills import SkillLoader
from tools import ToolRegistry

log = logging.getLogger("lucyd")


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _workspace_bytes(workspace: Path) -> int:
    """Sum size of every file under workspace/. Best-effort; 0 on any error."""
    if not workspace.is_dir():
        return 0
    total = 0
    try:
        for f in workspace.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


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
        except (OSError, UnicodeDecodeError):
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


# ─── Daemon ──────────────────────────────────────────────────────

_PRIORITY_USER = 0
_PRIORITY_SYSTEM = 1
_PRIORITY_SENTINEL = 2  # drains after all real work — exit cleanly

# The workspace-bytes gauge is refreshed on every /status and /metrics scrape;
# recompute the rglob at most this often so a scrape storm can't turn into a
# filesystem-walk storm.
_WORKSPACE_BYTES_TTL_S = 60.0


class PriorityMessageQueue:
    """Two-tier priority queue: user messages before system tasks, FIFO within tier.

    None is a legal item — used as a shutdown sentinel by the message loop.
    Sentinels go in at USER priority to ensure they drain ahead of queued work.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, int, dict[str, Any] | None]] = (
            asyncio.PriorityQueue(maxsize=maxsize)
        )
        self._seq = 0

    def _prioritize(self, item: dict[str, Any] | None) -> tuple[int, int, dict[str, Any] | None]:
        self._seq += 1
        if item is None:
            # Shutdown sentinel — drains after all queued work so real
            # messages in flight get processed before the loop exits.
            return (_PRIORITY_SENTINEL, self._seq, item)
        talker = item.get("talker", "user")
        priority = _PRIORITY_SYSTEM if talker in EPHEMERAL_TALKERS else _PRIORITY_USER
        item["_queued_at"] = time.time()
        item["_priority"] = priority
        return (priority, self._seq, item)

    async def put(self, item: dict[str, Any] | None) -> None:
        await self._queue.put(self._prioritize(item))

    def put_nowait(self, item: dict[str, Any] | None) -> None:
        self._queue.put_nowait(self._prioritize(item))

    async def get(self) -> dict[str, Any] | None:
        _, _, item = await self._queue.get()
        return item

    def qsize(self) -> int:
        return self._queue.qsize()

    def get_nowait(self) -> dict[str, Any] | None:
        _, _, item = self._queue.get_nowait()
        return item


class _LockFactoryHolder:
    """Indirection so tools can request session locks before the pipeline exists.

    ``_init_tools`` runs before ``self.pipeline`` is constructed, so any tool
    that needs a session lock at call time receives this holder. Once
    ``self.pipeline`` is built, ``set()`` is called and subsequent invocations
    delegate to ``pipeline.get_session_lock``.
    """

    def __init__(self) -> None:
        self._pipeline: MessagePipeline | None = None

    def set(self, pipeline: MessagePipeline) -> None:
        self._pipeline = pipeline

    def __call__(self, key: str) -> asyncio.Lock:
        if self._pipeline is None:
            raise RuntimeError("pipeline_lock_factory called before pipeline init")
        return self._pipeline.get_session_lock(key)


class LucydDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.start_time = time.time()
        self.queue: PriorityMessageQueue = PriorityMessageQueue(maxsize=1000)
        self._control_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.provider: LLMProvider | None = None  # Set by _init_provider; None only during __init__.
        self._single_shot: bool = False
        self.session_mgr: SessionManager = None  # type: ignore[assignment]  # set in _init_sessions
        self._preprocessors: list[PreprocessorSpec] = []
        self.context_builder: ContextBuilder = None  # type: ignore[assignment]  # set in _init_context
        self.skill_loader: SkillLoader | None = None
        self.tool_registry: ToolRegistry = None  # type: ignore[assignment]  # set in _init_tools
        self._http_api: HTTPApi = None  # type: ignore[assignment]  # set in run()
        self.pool: asyncpg.Pool | None = None
        self.metering_db: MeteringDB = None  # type: ignore[assignment]  # set in _init_metering
        self.converter: CurrencyConverter | None = None
        self._memory_interface: MemoryInterface | None = None  # set in _init_tools when memory tools are enabled
        self.pipeline: MessagePipeline = None  # type: ignore[assignment]  # set in run()
        self._lock_factory_holder = _LockFactoryHolder()
        # httpx client used by send_message (in-process) and the
        # /api/v1/outbound/send endpoint (at-job callback) to reach the
        # primary bridge. Created eagerly so _init_tools can wire it.
        self._outbound_http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=15.0)
        self._ws_bytes_cache: tuple[float, int] = (0.0, 0)  # (computed_at, bytes)

    def _setup_logging(self) -> None:
        """Configure logging to file + stderr.

        Supports log_format: "text" (default) or "json" (one JSON object per line).
        Activates PII-safe mode if configured.
        """
        set_pii_safe(self.config.log_pii_safe)
        log_file = self.config.log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # JSON logging format for Docker (stdout → Docker log driver)
        if self.config.log_format == "json":
            fmt: logging.Formatter = StructuredJSONFormatter()
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
        self._providers: dict[str, LLMProvider] = {}
        self.provider = self._create_provider_for("primary")
        self._providers["primary"] = self.provider
        # Determine dispatch mode based on config + model capabilities
        caps = self.provider.capabilities
        if self.config.agent_strategy == "single_shot" or not caps.supports_tools:
            self._single_shot = True
            log.info("Agent strategy: single shot")
        else:
            self._single_shot = False
            log.info("Agent strategy: agentic loop")

    def _create_provider_for(self, model_name: str) -> LLMProvider:
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

    def get_provider(self, role: str = "primary") -> LLMProvider:
        """Get provider for a specific role, with lazy creation and caching.

        Roles: "primary", "compaction", "consolidation", "subagent".
        If no model override is configured for a role, returns the primary provider.
        Only callable after ``_init_provider`` has run; the primary entry is
        guaranteed to exist at that point.
        """
        if role in self._providers:
            return self._providers[role]
        role_attr = f"{role}_model"
        model_name = getattr(self.config, role_attr, "")
        if model_name:
            try:
                provider = self._create_provider_for(model_name)
                self._providers[role] = provider
                return provider
            except (ValueError, ImportError, KeyError) as e:
                log.warning("Failed to create provider for role '%s' (model '%s'), falling back to primary: %s",
                            role, model_name, e)
        return self._providers["primary"]

    def _init_sessions(self) -> None:
        self.session_mgr = SessionManager(
            pool=self.pool,
            agent_name=self.config.agent_name,
        )

    # Built-in tool modules and the tool names they provide.
    _TOOL_MODULES = [
        ("tools.filesystem",    {"read", "write", "edit", "send_file"}),
        ("tools.shell",         {"exec"}),
        ("tools.reminder",      {"remind_user", "schedule_self_task"}),
        ("tools.send_message",  {"send_message"}),
        ("tools.web",           {"web_search", "web_fetch"}),
        ("tools.memory_read",   {"memory_search", "memory_get"}),
        ("tools.memory_write",  {"memory_write", "memory_forget", "record_episode"}),
        ("tools.agents",        {"sessions_spawn"}),
        ("skills",              {"load_skill"}),
        ("tools.status",        {"session_status"}),
        ("tools.gdpr",          {"gdpr_search", "gdpr_redact"}),
        ("tools.pdf",           {"pdf_read"}),
    ]

    def _init_tools(self) -> None:
        """Register built-in tools and plugins.

        1. Process _TOOL_MODULES (built-in) via importlib.import_module.
        2. Scan plugins.d/*.py via importlib.util.spec_from_file_location.
        Both paths share the same configure + register logic.
        """
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
        if self.pool and (enabled & {
            "memory_search", "memory_get",
            "memory_write", "memory_forget", "record_episode",
        }):
            memory = MemoryInterface(
                pool=self.pool,
                embedding_api_key=self.config.embedding_api_key,
                embedding_model=self.config.embedding_model,
                embedding_base_url=self.config.embedding_base_url,
                embedding_provider=self.config.embedding_provider,
                embedding_timeout=self.config.embedding_timeout,
                embedding_cost_rates=self.config.embedding_cost_rates,
                embedding_currency=self.config.embedding_currency,
                top_k=self.config.memory_top_k,
                vector_search_limit=self.config.vector_search_limit,
                fts_min_results=self.config.fts_min_results,
            )
            # Wire metering + conversion for embedding cost tracking
            if self.metering_db:
                memory.metering = self.metering_db
            if self.converter:
                memory.converter = self.converter

        # Stash for the pipeline (None when memory tools are disabled)
        self._memory_interface = memory

        # Dependency dict — configure() pulls what it needs by parameter name
        deps = {
            "config": self.config,
            "provider": self.provider,
            "session_manager": self.session_mgr,
            "session_mgr": self.session_mgr,
            "tool_registry": self.tool_registry,
            "skill_loader": self.skill_loader,
            "memory": memory,
            "pool": self.pool,
            "get_provider": self.get_provider,
            "session_getter": lambda: self.pipeline.current_session if self.pipeline else None,
            "start_time": self.start_time,
            "metering": self.metering_db,
            "converter": self.converter,
            # send_message DI
            "bridges_primary":       self.config.bridges_primary,
            "http_auth_token":       self.config.http_auth_token,
            "user_session_key":      f"user:{self.config.user_name}",
            "allowed_paths":         self.config.filesystem_allowed_paths,
            "http_client":           self._outbound_http_client,
            "pipeline_lock_factory": self._lock_factory_holder,
        }

        def _configure_and_register(module: ModuleType, source: str = "") -> None:
            """Call configure() with inspect-based injection, register enabled tools."""
            configure_fn = getattr(module, "configure", None)
            if callable(configure_fn):
                sig = inspect.signature(configure_fn)
                kwargs = {k: v for k, v in deps.items() if k in sig.parameters}
                configure_fn(**kwargs)
            for t in getattr(module, "TOOLS", []):
                if t.name in enabled:
                    self.tool_registry.register(t)
                    if source:
                        log.info("Plugin tool registered: %s (from %s)", t.name, source)

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

                    # Contract check: plugin must call mark_configured /
                    # mark_unconfigured so the health endpoint + dashboards
                    # see a complete picture. Warn only — runtime still works.
                    if not verify_plugin_declared_state(plugin_file.stem):
                        log.warning(
                            "Plugin %s did not declare configured state. "
                            "Call mark_configured / mark_unconfigured from "
                            "configure() — see docs/plugins.md",
                            plugin_file.stem,
                        )

                    # Collect preprocessors
                    for pp in getattr(module, "PREPROCESSORS", []):
                        if not isinstance(pp, PreprocessorSpec):
                            log.warning(
                                "Plugin %s: PREPROCESSORS entries must be PreprocessorSpec, got %s — skipping",
                                plugin_file.name, type(pp).__name__,
                            )
                            continue
                        self._preprocessors.append(pp)
                        log.info("Plugin preprocessor registered: %s (from %s)", pp.name, plugin_file.name)

                except Exception:
                    log.exception("Plugin: failed to load %s", plugin_file.name)

        log.info("Registered tools: %s", ", ".join(self.tool_registry.tool_names))
        if self._preprocessors:
            log.info("Registered preprocessors: %s",
                     ", ".join(pp.name for pp in self._preprocessors))

        # Pre-initialize tool metrics so all tools appear in dashboards from startup.
        if metrics.ENABLED:
            for name in self.tool_registry.tool_names:
                metrics.TOOL_CALLS_TOTAL.labels(tool_name=name, status="success")
                metrics.TOOL_CALLS_TOTAL.labels(tool_name=name, status="error")

    def _init_context(self) -> None:
        self.context_builder = ContextBuilder(
            workspace=self.config.workspace,
            stable_files=self.config.context_stable,
            semi_stable_files=self.config.context_semi_stable,
            max_system_tokens=self.config.max_system_tokens,
            user_timezone=self.config.user_timezone,
        )

    def _validate_persona_budget(self) -> None:
        """Warn/error at startup if the persona crowds the model context window.

        Cornerstone 5: a persona that fills most of the window starves
        conversation history and recall. Surface it once at startup
        instead of silently trimming on every turn.
        """
        if not self.provider:
            return
        max_ctx = self.provider.capabilities.max_context_tokens
        if max_ctx <= 0:
            return
        persona_tokens = sum(
            _estimate_tokens(b["text"]) for b in self.context_builder.build_stable()
        )
        pct = persona_tokens / max_ctx
        if pct > 0.8:
            log.error(
                "Persona is %d tokens — %.0f%% of the %d-token context window. "
                "Over 80%%: history and recall are starved. Reduce stable "
                "workspace files or use a larger-context model.",
                persona_tokens, pct * 100, max_ctx,
            )
        elif pct > 0.5:
            log.warning(
                "Persona is %d tokens — %.0f%% of the %d-token context window. "
                "Over 50%%: limited room for conversation history and recall.",
                persona_tokens, pct * 100, max_ctx,
            )
        else:
            log.info("Persona budget: %d tokens (%.0f%% of %d-token window)",
                     persona_tokens, pct * 100, max_ctx)

    def _init_skills(self) -> None:
        self.skill_loader = SkillLoader(
            workspace=self.config.workspace,
            skills_dir=self.config.skills_dir,
        )
        self.skill_loader.scan()

    def _write_tools_md(self) -> None:
        """Generate TOOLS.md from registered tools and available skills.

        Called after _init_tools() so the file always reflects the
        actual tool set.  Eliminates manual TOOLS.md maintenance.
        """
        lines = ["# Tools", ""]
        for name, description in self.tool_registry.get_brief_descriptions():
            lines.append(f"- **{name}**: {description}")

        if self.skill_loader:
            index = self.skill_loader.build_index()
            if index:
                lines.append("")
                lines.append("# Skills")
                lines.append("")
                lines.append('Load with `load_skill(name="...")` when you need specialized instructions.')
                lines.append("")
                lines.append(index)

        tools_md = self.config.workspace / "TOOLS.md"
        tools_md.write_text("\n".join(lines) + "\n")
        log.info("Generated TOOLS.md (%d tools, %d skills)",
                 len(self.tool_registry.tool_names),
                 len(self.skill_loader.list_skill_names()) if self.skill_loader else 0)

    def _init_metering(self) -> None:
        self.metering_db = MeteringDB(pool=self.pool)

    def _init_conversion(self) -> None:
        api_url = self.config.conversion_api_url
        static_rate = self.config.conversion_static_rate
        if api_url or static_rate != 1.0:
            self.converter = CurrencyConverter(
                api_url=api_url, static_rate=static_rate,
            )
            log.info("Currency conversion: api=%s static=%.4f",
                     api_url or "(disabled)", static_rate)
        else:
            self.converter = None

    # ── Pipeline delegation ─────────────────────────────────────────

    def _ensure_pipeline(self) -> None:
        """Create pipeline from current daemon state if not yet created.

        Called lazily by _process_message — allows test fixtures to wire
        daemon attributes before the pipeline is built.
        """
        if self.pipeline is not None:
            return
        if self.provider is None:
            raise RuntimeError("Cannot build pipeline before _init_provider() has set self.provider")
        self.pipeline = MessagePipeline(
            config=self.config,
            provider=self.provider,
            get_provider=self.get_provider,
            session_mgr=self.session_mgr,
            context_builder=self.context_builder,
            tool_registry=self.tool_registry,
            skill_loader=self.skill_loader,
            metering_db=self.metering_db,
            pool=self.pool,
            memory_interface=self._memory_interface,
            preprocessors=self._preprocessors,
            queue=self.queue,
            converter=self.converter,
        )
        self._lock_factory_holder.set(self.pipeline)

    async def _process_message(self, **kwargs: Any) -> None:
        """Delegate to pipeline.process_message.

        Thin forwarder that keeps daemon as the entry point for
        tests and internal callers.
        """
        self._ensure_pipeline()
        await self.pipeline.process_message(**kwargs)

    # ── Session-close hooks ────────────────────────────────────────

    async def _consolidate_on_close(self, session: Session) -> None:
        self._ensure_pipeline()
        await ops.harvest_conversation(
            session, self.config, self.pool,
            self._process_message, self.pipeline.get_session_lock,
        )

    async def _reset_session(self, target: str, by_id: bool = False) -> dict[str, Any]:
        """Reset session by target: 'all', session ID, or contact name.

        When by_id=True, target is treated as a session ID directly.
        Otherwise, UUIDs are auto-detected and routed to close_session_by_id.
        Returns result dict with reset status.
        """
        if not self.session_mgr:
            return {"reset": False, "reason": "no session manager"}

        if target == "all":
            contacts = await self.session_mgr.list_contacts()
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

        # "user" shortcut: the single user session
        if target == "user":
            target = f"user:{self.config.user_name}"

        if await self.session_mgr.close_session(target):
            if metrics.ENABLED:
                metrics.SESSION_CLOSE_TOTAL.labels(reason="manual").inc()
            return {"reset": True, "target": target, "type": "contact"}
        return {"reset": False, "reason": f"no session found for: {target}"}

    async def _process_reset_item(self, item: dict[str, Any]) -> None:
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

    async def _build_sessions(self) -> list[dict[str, Any]]:
        if not self.session_mgr:
            return []

        max_ctx = self.provider.capabilities.max_context_tokens if self.provider else 0

        result = []
        for contact, entry in (await self.session_mgr.get_index()).items():
            session_id = str(entry.get("session_id", ""))
            live = self.session_mgr.get_loaded(contact)
            info = await build_session_info(
                pool=self.pool,
                session_id=session_id,
                session=live,
                metering=self.metering_db,
                max_context_tokens=max_ctx,
            )
            info["contact"] = contact
            info["created_at"] = entry.get("created_at")
            result.append(info)
        return result

    def _sweep_expired_media(self) -> None:
        ttl = 24 * 3600  # 24 hours
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
            except OSError as e:
                log.debug("Media sweep: failed to remove %s: %s", f, e)
        if swept:
            log.info("Media sweep: deleted %d files older than 24h", swept)

    def _cached_workspace_bytes(self) -> int:
        """Workspace size for the metrics gauge, recomputed at most once per TTL.

        Refreshed on every /status and /metrics scrape; without the cache a
        scrape storm would rglob the whole workspace on every hit.
        """
        now = time.time()
        ts, value = self._ws_bytes_cache
        if now - ts >= _WORKSPACE_BYTES_TTL_S:
            value = _workspace_bytes(self.config.workspace)
            self._ws_bytes_cache = (now, value)
        return value

    def _build_monitor(self) -> dict[str, Any]:
        return dict(self.pipeline.monitor_state) if self.pipeline else {"state": "idle"}

    async def _build_history(self, target: str, full: bool = False) -> dict[str, Any]:
        """Build session history for HTTP /sessions/{target}/history.

        Target can be a session UUID or a contact name (case-insensitive).
        """
        if not self.session_mgr:
            return {"session_id": target, "events": []}

        # Resolve contact name to session ID
        session_id = target
        index = await self.session_mgr.get_index()
        if target in index:
            session_id = str(index[target].get("session_id", target))
        else:
            # Case-insensitive lookup
            for key, entry in index.items():
                if key.lower() == target.lower():
                    session_id = str(entry.get("session_id", target))
                    break

        events = await read_history_events(self.pool, session_id, full=full)
        return {"session_id": session_id, "events": events}

    async def _build_status(self) -> dict[str, Any]:
        today_cost = await self.metering_db.today_cost()

        active_sessions = 0
        if self.session_mgr:
            active_sessions = await self.session_mgr.session_count()

        # Update Prometheus gauges
        if metrics.ENABLED:
            metrics.UPTIME.set(time.time() - self.start_time)
            metrics.ACTIVE_SESSIONS.set(active_sessions)
            metrics.QUEUE_DEPTH.set(self.queue.qsize())
            metrics.WORKSPACE_BYTES.set(self._cached_workspace_bytes())

        return {
            "status": "ok",
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.start_time),
            "model": self.config.model_config("primary").get("model", ""),
            "active_sessions": active_sessions,
            "today_cost": round(today_cost, 4),
            "queue_depth": self.queue.qsize(),
            "error_counts": dict(self.pipeline.error_counts) if self.pipeline else {},
        }

    async def _handle_compact(self) -> dict[str, Any]:
        self._ensure_pipeline()
        return await ops.handle_compact(
            self.config, self.session_mgr,
            self._process_message, self.pipeline.get_session_lock,
        )

    async def _handle_index(self, full: bool = False) -> dict[str, Any]:
        return await ops.handle_index(
            self.config,
            pool=self.pool,
            full=full,
            metering=self.metering_db, converter=self.converter,
        )

    async def _handle_index_status(self) -> dict[str, Any]:
        return await ops.handle_index_status(self.config, pool=self.pool)

    async def _handle_maintain(self) -> dict[str, Any]:
        self._ensure_pipeline()
        return await ops.handle_maintain(
            self.config, self.pool, self.metering_db, self.session_mgr,
            self._process_message, self.pipeline.get_session_lock,
        )

    async def _handle_session_reset(self) -> dict[str, Any]:
        self._ensure_pipeline()
        return await ops.handle_session_reset(
            self.config, self.session_mgr, self.pool,
            self.pipeline.get_session_lock,
        )

    async def _message_loop(self) -> None:
        """Main message processing loop — sequential."""
        self._ensure_pipeline()
        debounce_s = self.config.debounce_ms / 1000.0
        pending: dict[str, list[dict[str, Any]]] = {}  # session_key → [messages]

        async def drain_pending(session_key: str) -> None:
            msgs = pending.pop(session_key, [])
            if not msgs:
                return
            combined_text = "\n".join(m["text"] for m in msgs if m["text"])
            combined_attachments = []
            for m in msgs:
                if m.get("attachments"):
                    combined_attachments.extend(m["attachments"])
            first = msgs[0]
            talker = first["talker"]
            sender = first["sender"]
            channel = first.get("channel", "")
            reply_to = first.get("reply_to", "")
            tid = str(uuid.uuid4())
            log.info("[%s] Processing %s message from %s (channel=%s)",
                     tid[:8], talker, _log_safe(sender), channel or "-")
            async with self.pipeline.get_session_lock(session_key):
                await self._process_message(
                    text=combined_text, sender=sender, talker=talker,
                    attachments=combined_attachments or None,
                    trace_id=tid,
                    channel=channel,
                    reply_to=reply_to,
                )

        async def process_http_immediate(item: dict[str, Any]) -> None:
            """Process sync HTTP messages immediately (no debounce).

            Each request has its own Future — combining would lose Futures.
            """
            tid = str(uuid.uuid4())
            talker = item["talker"]
            sender = item["sender"]
            channel = item.get("channel", "")
            reply_to = item.get("reply_to", "")
            session_key = f"{talker}:{sender}"
            log.info("[%s] Processing sync %s message from %s",
                     tid[:8], talker, _log_safe(sender))
            async with self.pipeline.get_session_lock(session_key):
                await self._process_message(
                    text=item.get("text", ""),
                    sender=sender,
                    talker=talker,
                    attachments=item.get("attachments"),
                    response_future=item.get("response_future"),
                    trace_id=tid,
                    stream_queue=item.get("stream_queue"),
                    channel=channel,
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

            # Record queue wait time
            if metrics.ENABLED and item is not None:
                queued_at = item.pop("_queued_at", None)
                priority_label = "user" if item.pop("_priority", 0) == 0 else "system"
                if queued_at is not None:
                    metrics.QUEUE_WAIT_SECONDS.labels(priority=priority_label).observe(
                        time.time() - queued_at)

            # Sentinel from channel reader — drain pending and exit
            if item is None:
                for s in list(pending.keys()):
                    await drain_pending(s)
                self.running = False
                break

            # Handle session reset (safety net — normally via control queue)
            if item.get("type") == "reset":
                await self._process_reset_item(item)
                continue

            # Forced compact — diary + compaction on user session
            if item.get("type") == "compact":
                result = await self._handle_compact()
                log.info("Compact: %s", result)
                compact_future = item.get("response_future")
                if compact_future is not None and not compact_future.done():
                    compact_future.set_result(result)
                continue

            # Sync HTTP (chat/stream, inbound bridge) — process immediately
            if item.get("response_future") is not None:
                await process_http_immediate(item)
                continue

            # Async queued message (system/event, agent/action, etc.)
            talker = item.get("talker")
            sender = item.get("sender")
            if not talker or not sender:
                log.warning("Queue item missing talker/sender, dropping: %s", item)
                continue

            text = item.get("text", "")
            attachments = item.get("attachments")
            if not text and not attachments:
                continue

            session_key = f"{talker}:{sender}"
            pending.setdefault(session_key, []).append({
                "text": text,
                "talker": talker,
                "sender": sender,
                "channel": item.get("channel", ""),
                "reply_to": item.get("reply_to", ""),
                "attachments": attachments,
            })

            # Wait for more messages on the same session
            await asyncio.sleep(debounce_s)

            for s in list(pending.keys()):
                await drain_pending(s)

            await self._drain_control_queue()

    def _setup_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register Unix signal handlers."""
        def handle_sigusr1() -> None:
            log.info("SIGUSR1: reloading workspace files")
            if self.skill_loader:
                self.skill_loader.scan()

        def handle_sigterm() -> None:
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
            # Database pool — must be first, all modules depend on it
            if cfg.database_url:
                self.pool = await lucyd_db.create_pool(
                    cfg.database_url,
                    min_size=cfg.database_pool_min,
                    max_size=cfg.database_pool_max,
                )
                await lucyd_db.ensure_schema(self.pool)
                log.info("Database pool created (%d-%d connections)",
                         cfg.database_pool_min, cfg.database_pool_max)
            else:
                log.warning("No [database] url_env configured — running without database")

            self._init_provider()
            self._init_sessions()
            self._init_skills()
            self._init_context()
            self._validate_persona_budget()
            self._init_metering()
            self._init_conversion()
            self._init_tools()
            self._write_tools_md()

            # Create message pipeline — the core runtime path.
            self._ensure_pipeline()

            # Sweep expired media downloads
            self._sweep_expired_media()

            # Register consolidation on session close
            if self.config.consolidation_enabled:
                self.session_mgr.on_close(self._consolidate_on_close)

            loop = asyncio.get_event_loop()
            self._setup_signals(loop)

            # Start HTTP API (always on)
            self._http_api = HTTPApi(
                queue=self.queue,
                control_queue=self._control_queue,
                host=cfg.http_host,
                port=cfg.http_port,
                auth_token=cfg.http_auth_token,
                agent_timeout=cfg.agent_timeout,
                user_name=cfg.user_name,
                get_status=self._build_status,
                get_sessions=self._build_sessions,
                get_monitor=self._build_monitor,
                get_history=self._build_history,
                handle_index=self._handle_index,
                handle_index_status=self._handle_index_status,
                handle_maintain=self._handle_maintain,
                handle_session_reset=self._handle_session_reset,
                download_dir=cfg.http_download_dir,
                max_body_bytes=cfg.http_max_body_bytes,
                max_attachment_bytes=cfg.http_max_attachment_bytes,
                rate_limit=cfg.http_rate_limit,
                rate_window=cfg.http_rate_window,
                status_rate_limit=cfg.http_status_rate_limit,
                rate_cleanup_threshold=1000,
                agent_name=cfg.agent_name,
                metering_db=self.metering_db,
                trust_localhost=cfg.http_trust_localhost,
                bridges_primary=cfg.bridges_primary,
                outbound_http_client=self._outbound_http_client,
                session_mgr=self.session_mgr,
                pipeline_lock_factory=self._lock_factory_holder,
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
            # from Postgres on next startup via get_or_create().
            if self.session_mgr:
                for session in self.session_mgr.list_sessions():
                    with contextlib.suppress(Exception):  # session state persist on shutdown; failure is benign
                        await self.session_mgr.save_state(session)

            # Close database pool
            if self.pool is not None:
                with contextlib.suppress(Exception):  # pool close on shutdown; failure is benign
                    await lucyd_db.close_pool(self.pool)
            # Close outbound httpx client
            if self._outbound_http_client is not None:
                with contextlib.suppress(Exception):  # outbound client close on shutdown; failure is benign
                    await self._outbound_http_client.aclose()
            _release_pid_file(pid_path)
            log.info("Lucyd daemon stopped")


# ─── CLI Entry Point ─────────────────────────────────────────────

def main() -> None:
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
    overrides: dict[str, Any] = {}

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
