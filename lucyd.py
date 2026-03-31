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
import logging
import logging.handlers
import os
import re
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Add lucyd directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from config import Config, ConfigError, load_config
from context import ContextBuilder
from log_utils import _log_safe
from metering import MeteringDB
from providers import create_provider
from session import SessionManager
from skills import SkillLoader
from tools import ToolRegistry

log = logging.getLogger("lucyd")


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


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



import metrics


# ─── Daemon ──────────────────────────────────────────────────────

class LucydDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.start_time = time.time()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._control_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.provider: Any = None
        self._single_shot: bool = False
        self.session_mgr: SessionManager = None  # type: ignore[assignment]  # set in _init_sessions
        self._preprocessors: list[dict[str, Any]] = []
        self.context_builder: ContextBuilder = None  # type: ignore[assignment]  # set in _init_context
        self.skill_loader: SkillLoader | None = None
        self.tool_registry: ToolRegistry = None  # type: ignore[assignment]  # set in _init_tools
        self._http_api: Any = None
        self._memory_conn: Any = None
        self.metering_db: Any = None
        self._evolve_rollback_tag: str | None = None
        self.pipeline: Any = None  # MessagePipeline, set in run()

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
                fts_min_results=self.config.fts_min_results,
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
            "session_getter": lambda: self.pipeline.current_session if self.pipeline else None,
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

    def _get_memory_conn(self) -> Any:
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


    # ── Pipeline delegation ─────────────────────────────────────────

    def _ensure_pipeline(self) -> None:
        """Create pipeline from current daemon state if not yet created.

        Called lazily by _process_message — allows test fixtures to wire
        daemon attributes before the pipeline is built.
        """
        if self.pipeline is not None:
            return
        from pipeline import MessagePipeline
        self.pipeline = MessagePipeline(
            config=self.config,
            provider=self.provider,
            get_provider=self.get_provider,
            session_mgr=self.session_mgr,
            context_builder=self.context_builder,
            tool_registry=self.tool_registry,
            skill_loader=self.skill_loader,
            metering_db=self.metering_db,
            get_memory_conn=self._get_memory_conn,
            preprocessors=self._preprocessors,
            queue=self.queue,
            on_pre_close=self._pre_close_hook,
        )

    async def _process_message(self, **kwargs: Any) -> None:
        """Delegate to pipeline.process_message.

        Thin forwarder that keeps daemon as the entry point for
        tests and internal callers.
        """
        self._ensure_pipeline()
        await self.pipeline.process_message(**kwargs)

    # ── Pre-close hook (evolution validation) ──────────────────────

    def _pre_close_hook(self, sender: str) -> None:
        """Called by the pipeline before closing ephemeral sessions.

        Handles evolution-specific validation and rollback.
        """
        if sender == "evolution" and self._evolve_rollback_tag:
            tag = self._evolve_rollback_tag
            self._evolve_rollback_tag = None
            if not self._validate_evolution():
                self._git_rollback(tag)
                log.error("Evolution rolled back to %s due to validation failure", tag)

    def _validate_evolution(self) -> bool:
        import operations as ops
        return ops.validate_evolution(self.config)

    async def _consolidate_on_close(self, session: Any) -> None:
        import operations as ops
        await ops.consolidate_on_close(
            session, self.config, self._get_memory_conn,
            self.get_provider, self.context_builder, self.metering_db,
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

    def _build_sessions(self) -> list[dict[str, Any]]:
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

    def _build_monitor(self) -> dict[str, Any]:
        """Build monitor data for HTTP /monitor."""
        return dict(self.pipeline.monitor_state) if self.pipeline else {"state": "idle"}

    def _build_history(self, target: str, full: bool = False) -> dict[str, Any]:
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

    def _build_status(self) -> dict[str, Any]:
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
            "error_counts": dict(self.pipeline.error_counts) if self.pipeline else {},
        }

    async def _handle_compact(self) -> dict[str, Any]:
        import operations as ops
        self._ensure_pipeline()
        return await ops.handle_compact(
            self.config, self.session_mgr,
            self._process_message, self.pipeline.get_session_lock,
        )

    def _git_snapshot(self, label: str) -> str | None:
        import operations as ops
        return ops.git_snapshot(self.config.workspace, label)

    def _git_rollback(self, tag: str) -> bool:
        import operations as ops
        return ops.git_rollback(self.config.workspace, tag)

    async def _handle_evolve(self, *, force: bool = False) -> dict[str, Any]:
        import operations as ops

        def _set_tag(tag: str) -> None:
            self._evolve_rollback_tag = tag

        return await ops.handle_evolve(
            force=force, config=self.config,
            get_memory_conn=self._get_memory_conn,
            queue=self.queue, set_rollback_tag=_set_tag,
        )

    async def _handle_index(self, full: bool = False) -> dict[str, Any]:
        import operations as ops
        return await ops.handle_index(self.config, full=full)

    async def _handle_index_status(self) -> dict[str, Any]:
        import operations as ops
        return ops.handle_index_status(self.config)

    async def _handle_consolidate(self) -> dict[str, Any]:
        import operations as ops
        return await ops.handle_consolidate(
            self.config, self._get_memory_conn,
            self.get_provider, self.metering_db,
        )

    async def _handle_maintain(self) -> dict[str, Any]:
        import operations as ops
        return await ops.handle_maintain(
            self.config, self._get_memory_conn, self.metering_db,
        )

    async def _message_loop(self) -> None:
        """Main message processing loop — sequential."""
        self._ensure_pipeline()
        debounce_s = self.config.debounce_ms / 1000.0
        pending: dict[str, list[dict[str, Any]]] = {}  # sender → [messages]

        async def drain_pending(sender: str) -> None:
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
            async with self.pipeline.get_session_lock(sk):
                await self._process_message(
                    text=combined_text, sender=sender, source=source,
                    attachments=combined_attachments or None,
                    trace_id=tid,
                    deliver=deliver,
                    channel_id=channel_id,
                    task_type=task_type,
                    reply_to=reply_to,
                )

        async def process_http_immediate(item: dict[str, Any]) -> None:
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
            async with self.pipeline.get_session_lock(sk):
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
            self._init_provider()
            self._init_sessions()
            self._init_skills()
            self._init_context()
            self._init_metering()
            self._init_tools()

            # Create message pipeline — the core runtime path
            from pipeline import MessagePipeline
            self.pipeline = MessagePipeline(
                config=cfg,
                provider=self.provider,
                get_provider=self.get_provider,
                session_mgr=self.session_mgr,
                context_builder=self.context_builder,
                tool_registry=self.tool_registry,
                skill_loader=self.skill_loader,
                metering_db=self.metering_db,
                get_memory_conn=self._get_memory_conn,
                preprocessors=self._preprocessors,
                queue=self.queue,
                on_pre_close=self._pre_close_hook,
            )

            # Patch session_getter to point at pipeline's current_session
            # (tool deps were wired before pipeline existed)
            import tools.status as _status_mod
            _status_mod._session_getter = lambda: self.pipeline.current_session

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
                trust_localhost=cfg.http_trust_localhost,
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
            if self.session_mgr:
                for session in self.session_mgr.list_sessions():
                    with contextlib.suppress(Exception):  # session state persist on shutdown; failure is benign
                        self.session_mgr.save_state(session)

            # Close metering DB connection
            if self.metering_db:
                with contextlib.suppress(Exception):  # DB close on shutdown; failure is benign
                    self.metering_db.close()
            # Close memory DB connection
            if self._memory_conn is not None:
                with contextlib.suppress(Exception):  # DB close on shutdown; failure is benign
                    self._memory_conn.close()
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
