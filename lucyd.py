#!/usr/bin/env python3
"""Lucyd — a daemon for persona-rich AI agents.

Entry point. Wires config → channel → loop → tools → sessions.
Handles PID file, control FIFO, Unix signals, and the main event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import contextlib
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Add lucyd directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import random

from agentic import _init_cost_db, cost_db_query, is_transient_error, run_agentic_loop
from channels import Attachment, InboundMessage, create_channel
from config import Config, ConfigError, load_config
from context import ContextBuilder
from providers import create_provider
from session import SessionManager, _text_from_content, set_audit_truncation
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

def _check_pid_file(path: Path) -> None:
    """Refuse to start if another instance is live."""
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            print(f"Another instance is running (PID {pid}). Exiting.", file=sys.stderr)
            sys.exit(1)
        except PermissionError:
            print(f"Another instance is running (PID {pid}). Exiting.", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            log.info("Stale PID file found, removing")
            path.unlink()


def _write_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _remove_pid_file(path: Path) -> None:
    with contextlib.suppress(Exception):  # daemon shutdown cleanup; failure is benign
        path.unlink(missing_ok=True)


# ─── Control FIFO ────────────────────────────────────────────────

async def _fifo_reader(fifo_path: Path, queue: asyncio.Queue,
                       control_queue: asyncio.Queue | None = None) -> None:
    """Read JSON messages from the control FIFO."""
    fifo_path.parent.mkdir(parents=True, exist_ok=True)
    if fifo_path.exists():
        fifo_path.unlink()
    os.mkfifo(fifo_path, mode=0o600)
    log.info("Control FIFO: %s", fifo_path)

    while True:
        try:
            # Open FIFO — blocks until a writer connects
            fd = await asyncio.to_thread(os.open, str(fifo_path), os.O_RDONLY)
            with os.fdopen(fd, "r") as f:
                data = await asyncio.to_thread(f.read)
            if data.strip():
                for line in data.strip().split("\n"):
                    try:
                        msg = json.loads(line)
                        if not isinstance(msg, dict):
                            log.warning("FIFO message not a dict, ignoring")
                            continue
                        # Reset messages → control queue (priority over messages)
                        if msg.get("type") == "reset":
                            await (control_queue or queue).put(msg)
                            continue
                        # Compact messages → message queue (no text/sender needed)
                        if msg.get("type") == "compact":
                            await queue.put(msg)
                            continue
                        # Normal messages need text + sender
                        if not {"text", "sender"}.issubset(msg.keys()):
                            log.warning("FIFO message missing required fields, ignoring: %s",
                                        list({"text", "sender"} - msg.keys()))
                            continue
                        # Reconstruct Attachment objects from serialized dicts
                        raw_atts = msg.get("attachments")
                        if raw_atts and isinstance(raw_atts, list):
                            msg["attachments"] = [
                                Attachment(
                                    content_type=a.get("content_type", ""),
                                    local_path=a.get("local_path", ""),
                                    filename=a.get("filename", ""),
                                    size=a.get("size", 0),
                                )
                                for a in raw_atts
                                if isinstance(a, dict) and a.get("local_path")
                            ] or None
                        await queue.put(msg)
                    except json.JSONDecodeError:
                        log.warning("Invalid JSON from FIFO: %s", line[:200])
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("FIFO reader error: %s", e)
            await asyncio.sleep(1)


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


def _should_deliver(reply: str, source: str, no_delivery_sources: frozenset[str]) -> bool:
    """Decide whether to deliver the reply via channel."""
    return bool(reply.strip()) and source not in no_delivery_sources


def _inject_warning(text: str, warning: str) -> tuple[str, bool]:
    """Prepend pending system warning to user text.

    Returns (modified_text, was_warning_consumed).
    """
    if warning:
        return f"[system: {warning}]\n\n{text}", True
    return text, False


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


# ─── Image Caption Enrichment ────────────────────────────────────

def _enrich_image_caption(
    text: str,
    caption: str,
    messages: list[dict],
    msg_count_before: int,
    max_desc_len: int = 200,
) -> str:
    """Replace bare [caption...] with [caption: description] from first assistant response.

    After transient image injection is removed, the stored user message
    only has a tag like ``[Image from user, saved: /path]``. This extracts a
    truncated summary from the assistant's first reply (which describes the
    image) and embeds it so image context survives compaction.
    """
    import re
    # Match [caption] or [caption, saved: /path] — capture full tag
    pattern = re.compile(r"\[" + re.escape(caption) + r"(?:,\s*saved:\s*[^\]]+)?\]")
    m = pattern.search(text)
    if not m:
        return text

    # Find first assistant text from this turn
    desc = ""
    for msg in messages[msg_count_before:]:
        if msg.get("role") == "assistant":
            desc = msg.get("text", "") or ""
            break

    if not desc:
        return text

    if len(desc) > max_desc_len:
        cut = desc[:max_desc_len].rsplit(" ", 1)[0]
        desc = cut + "..." if cut else desc[:max_desc_len] + "..."

    desc = " ".join(desc.split())
    return text.replace(m.group(0), f"[{caption}: {desc}]", 1)


# ─── Image Fitting ───────────────────────────────────────────────

class _ImageTooLarge(Exception):
    """Raised when an image can't be fit within API limits."""


def _fit_image(data: bytes, content_type: str, max_bytes: int,
               max_dimension: int, quality_steps: list[int] | None = None,
               path: str = "") -> bytes:
    """Scale dimensions and reduce quality to fit within API limits.

    Strategy: (1) shrink to max_dimension per side, (2) step down JPEG quality.
    Raises _ImageTooLarge if nothing works.
    """
    from io import BytesIO

    from PIL import Image

    is_jpeg = content_type == "image/jpeg"
    img = Image.open(BytesIO(data))

    # Step 1: scale dimensions if any side exceeds max_dimension
    if max(img.size) > max_dimension:
        log.info("Scaling %dx%d to fit %dpx: %s", img.size[0], img.size[1],
                 max_dimension, path)
        img.thumbnail((max_dimension, max_dimension))
        buf = BytesIO()
        if is_jpeg:
            img.save(buf, format="JPEG", quality=90)
        else:
            img.save(buf, format="PNG")
        data = buf.getvalue()

    # Already fits?
    if len(data) <= max_bytes:
        img.close()
        return data

    # Step 2: reduce JPEG quality (only works for JPEG — PNG is lossless)
    if is_jpeg:
        steps = quality_steps if quality_steps is not None else [85, 60, 40]
        for q in steps:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=q)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                log.info("JPEG quality %d brought size to %d bytes: %s", q, len(data), path)
                img.close()
                return data

    img.close()
    raise _ImageTooLarge(f"{len(data) / (1024*1024):.1f}MB after compression")


# ─── Document Text Extraction ────────────────────────────────────


def _extract_document_text(path: str, content_type: str, filename: str,
                           max_chars: int, max_bytes: int,
                           text_extensions: list[str]) -> str | None:
    """Extract text from a document. Returns None if not a readable format."""
    file_path = Path(path)

    # Skip files too large to bother reading
    if file_path.stat().st_size > max_bytes:
        return None

    ext = Path(filename).suffix.lower() if filename else ""

    # Plain text — by extension or text/* MIME
    if ext in text_extensions or content_type.startswith("text/"):
        text = file_path.read_bytes().decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[… truncated at {max_chars:,} chars]"
        return text

    # PDF
    if content_type == "application/pdf" or ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return None  # pypdf not installed — fall through to label
        reader = PdfReader(path)
        parts = []
        total = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if total + len(page_text) > max_chars:
                parts.append(page_text[:max_chars - total])
                parts.append(f"\n[… truncated at {max_chars:,} chars]")
                break
            parts.append(page_text)
            total += len(page_text)
        return "\n".join(parts) or None

    return None


# ─── Daemon ──────────────────────────────────────────────────────

class LucydDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.start_time = time.time()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=config.queue_capacity)
        self._control_queue: asyncio.Queue = asyncio.Queue()
        self.provider: Any = None
        self.channel: Any = None
        self.session_mgr: SessionManager | None = None
        self.context_builder: ContextBuilder | None = None
        self.skill_loader: SkillLoader | None = None
        self.tool_registry: ToolRegistry | None = None
        self._fifo_task: asyncio.Task | None = None
        self._http_api: Any = None
        self._memory_conn: Any = None
        self._last_inbound_ts: collections.OrderedDict[str, int] = collections.OrderedDict()  # sender → ms timestamp
        self._telemetry_buffer: dict[str, dict] = {}  # ref → latest passive notification
        self._passive_refs: frozenset[str] = frozenset()

    def _setup_logging(self) -> None:
        """Configure logging to file + stderr."""
        log_file = self.config.log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=self.config.log_max_bytes,
            backupCount=self.config.log_backup_count, encoding="utf-8",
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
        """Create the primary provider instance."""
        try:
            model_cfg = self.config.model_config("primary")
            provider_type = model_cfg.get("provider", "")

            api_key_env = model_cfg.get("api_key_env", "")
            api_key = os.environ.get(api_key_env, "") if api_key_env else ""

            if not api_key and api_key_env:
                log.error("No API key for primary model (env var '%s' not set)", api_key_env)
                return

            self.provider = create_provider(model_cfg, api_key)
            log.info("Provider: %s / %s", provider_type, model_cfg.get("model", ""))
        except Exception as e:
            log.error("Failed to create provider: %s", e)

    def _init_channel(self) -> None:
        self.channel = create_channel(self.config)

    def _init_sessions(self) -> None:
        set_audit_truncation(self.config.audit_truncation_limit)
        self.session_mgr = SessionManager(
            self.config.sessions_dir,
            agent_name=self.config.agent_name,
        )

    def _init_tools(self) -> None:
        """Register enabled tools."""
        self.tool_registry = ToolRegistry(
            truncation_limit=self.config.output_truncation,
        )

        enabled = set(self.config.tools_enabled)

        # Filesystem tools
        if enabled & {"read", "write", "edit"}:
            from tools.filesystem import TOOLS as fs_tools
            from tools.filesystem import configure as fs_configure
            fs_configure(self.config.filesystem_allowed_paths,
                        default_read_limit=self.config.filesystem_default_read_limit)
            for t in fs_tools:
                if t["name"] in enabled:
                    self.tool_registry.register_many([t])

        # Shell
        if "exec" in enabled:
            from tools.shell import TOOLS as sh_tools
            from tools.shell import configure as sh_configure
            sh_configure(self.config.exec_timeout, self.config.exec_max_timeout)
            self.tool_registry.register_many(sh_tools)

        # Messaging
        if enabled & {"message", "react"}:
            from tools.messaging import TOOLS as msg_tools
            from tools.messaging import configure as msg_configure
            from tools.messaging import set_channel, set_timestamp_getter
            set_channel(self.channel)
            set_timestamp_getter(lambda sender: self._last_inbound_ts.get(sender))
            msg_configure(contact_names=self.config.contact_names)
            for t in msg_tools:
                if t["name"] in enabled:
                    self.tool_registry.register_many([t])

        # Web tools
        if enabled & {"web_search", "web_fetch"}:
            from tools.web import TOOLS as web_tools
            from tools.web import configure as web_configure
            web_configure(
                api_key=self.config.web_search_api_key,
                provider=self.config.web_search_provider,
                search_timeout=self.config.web_search_timeout,
                fetch_timeout=self.config.web_fetch_timeout,
            )
            for t in web_tools:
                if t["name"] in enabled:
                    self.tool_registry.register_many([t])

        # Memory tools
        if enabled & {"memory_search", "memory_get"} and self.config.memory_db:
            from memory import MemoryInterface
            from tools.memory_tools import TOOLS as mem_tools
            from tools.memory_tools import set_memory
            mem = MemoryInterface(
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
            set_memory(mem)
            # Wire structured recall if consolidation enabled
            if self.config.consolidation_enabled:
                from tools.memory_tools import set_structured_memory
                set_structured_memory(self._get_memory_conn(), self.config)
            for t in mem_tools:
                if t["name"] in enabled:
                    self.tool_registry.register_many([t])

        # Sub-agents
        if "sessions_spawn" in enabled:
            from tools.agents import TOOLS as agent_tools
            from tools.agents import configure as agent_configure
            agent_configure(
                config=self.config,
                provider=self.provider,
                tool_registry=self.tool_registry,
                session_manager=self.session_mgr,
            )
            self.tool_registry.register_many(agent_tools)

        # TTS
        if "tts" in enabled and self.config.tts_api_key:
            from tools.tts import TOOLS as tts_tools
            from tools.tts import configure as tts_configure
            tts_cfg = self.config.raw("tools", "tts", default={})
            tts_configure(
                api_key=self.config.tts_api_key,
                provider=self.config.tts_provider,
                channel=self.channel,
                default_voice_id=tts_cfg.get("default_voice_id", ""),
                default_model_id=tts_cfg.get("default_model_id", ""),
                speed=tts_cfg.get("speed", 1.0),
                stability=tts_cfg.get("stability", 0.5),
                similarity_boost=tts_cfg.get("similarity_boost", 0.75),
                timeout=self.config.tts_timeout,
                api_url=self.config.tts_api_url,
                contact_names=self.config.contact_names,
            )
            self.tool_registry.register_many(tts_tools)

        # Scheduling
        if enabled & {"schedule_message", "list_scheduled"}:
            from tools.scheduling import TOOLS as sched_tools
            from tools.scheduling import configure as sched_configure
            sched_configure(channel=self.channel, contact_names=self.config.contact_names,
                           max_scheduled=self.config.scheduling_max_scheduled,
                           max_delay=self.config.scheduling_max_delay)
            for t in sched_tools:
                if t["name"] in enabled:
                    self.tool_registry.register_many([t])

        # Skills
        if "load_skill" in enabled:
            from tools.skills_tool import TOOLS as skill_tools
            from tools.skills_tool import set_skill_loader
            set_skill_loader(self.skill_loader)
            self.tool_registry.register_many(skill_tools)

        # Session status
        if "session_status" in enabled:
            from tools.status import TOOLS as status_tools
            from tools.status import configure as status_configure
            primary_cfg = self.config.raw("models", "primary", default={})
            status_configure(
                session_manager=self.session_mgr,
                cost_db=str(self.config.cost_db),
                start_time=self.start_time,
                max_context_tokens=primary_cfg.get("max_context_tokens", 0),
                sqlite_timeout=self.config.sqlite_timeout,
            )
            self.tool_registry.register_many(status_tools)

        # Structured memory tools
        if enabled & {"memory_write", "memory_forget", "commitment_update"} and self.config.memory_db:
            from tools import structured_memory
            structured_memory.configure(conn=self._get_memory_conn())
            for t in structured_memory.TOOLS:
                if t["name"] in enabled:
                    self.tool_registry.register_many([t])

        log.info("Registered tools: %s", ", ".join(self.tool_registry.tool_names))

    def _init_plugins(self) -> None:
        """Load tool plugins from plugins.d/ directory.

        Each plugin is a .py file with a TOOLS list (same format as built-in tools).
        Optional configure() function receives deps via inspect.signature().
        Only tools whose names are in [tools] enabled are registered.
        """
        import importlib.util
        import inspect

        plugins_path = self.config.config_dir / self.config.plugins_dir
        if not plugins_path.is_dir():
            return

        enabled = set(self.config.tools_enabled)

        # Available deps for plugin configure() injection
        deps = {
            "config": self.config,
            "channel": self.channel,
            "session_mgr": self.session_mgr,
            "provider": self.provider,
            "tool_registry": self.tool_registry,
        }

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

                tools_list = getattr(module, "TOOLS", None)
                if not isinstance(tools_list, list):
                    log.debug("Plugin: %s has no TOOLS list, skipping", plugin_file.name)
                    continue

                # Call configure() if it exists, injecting requested deps
                configure_fn = getattr(module, "configure", None)
                if callable(configure_fn):
                    sig = inspect.signature(configure_fn)
                    kwargs = {
                        name: deps[name]
                        for name in sig.parameters
                        if name in deps
                    }
                    configure_fn(**kwargs)

                # Register only enabled tools
                for t in tools_list:
                    name = t.get("name", "")
                    if name in enabled:
                        self.tool_registry.register_many([t])
                        log.info("Plugin tool registered: %s (from %s)", name, plugin_file.name)

            except Exception:
                log.exception("Plugin: failed to load %s", plugin_file.name)

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
        )

    def _init_skills(self) -> None:
        self.skill_loader = SkillLoader(
            workspace=self.config.workspace,
            skills_dir=self.config.skills_dir,
        )
        self.skill_loader.scan()

    def _init_cost_db(self) -> None:
        _init_cost_db(str(self.config.cost_db), sqlite_timeout=self.config.sqlite_timeout)

    def _check_context_budget(self) -> None:
        """Estimate system prompt size and warn if it consumes too much context.

        Builds a representative system prompt (stable + semi-stable
        files, tool descriptions, skill index) and estimates token count.
        Warns at >40% of max_context_tokens.
        """
        primary_cfg = self.config.model_config("primary")
        max_ctx = primary_cfg.get("max_context_tokens", 0)
        if not max_ctx:
            return

        # Build representative system blocks
        tool_descs = self.tool_registry.get_brief_descriptions()
        skill_index = self.skill_loader.build_index() if self.skill_loader else ""
        blocks = self.context_builder.build(
            tool_descriptions=tool_descs,
            skill_index=skill_index,
        )

        total_chars = sum(len(b.get("text", "")) for b in blocks)
        est_tokens = total_chars // 4  # conservative estimate: ~4 chars/token

        pct = est_tokens * 100 // max_ctx
        log.info(
            "Context budget: system prompt ~%d tokens (%d%% of %d max)",
            est_tokens, pct, max_ctx,
        )
        if pct > 40:
            log.warning(
                "System prompt uses %d%% of context window "
                "(%d of %d tokens). This leaves limited room for "
                "conversation history and tool output. Consider reducing "
                "workspace files or increasing max_context_tokens.",
                pct, est_tokens, max_ctx,
            )

    # Sources that suppress channel delivery (typing, intermediate text, final reply).
    # The agentic loop still runs — tools execute, cost is recorded, session persists.
    _NO_CHANNEL_DELIVERY = frozenset({"system", "http"})

    def _drain_telemetry(self) -> str:
        """Read and clear passive telemetry buffer. Returns formatted string."""
        if not self._telemetry_buffer:
            return ""
        max_age = self.config.telemetry_max_age
        now = time.time()
        lines = []
        to_remove = []
        for ref, entry in self._telemetry_buffer.items():
            age = now - entry["timestamp"]
            if age > max_age:
                to_remove.append(ref)
                continue
            lines.append(entry["text"])
            to_remove.append(ref)
        for ref in to_remove:
            del self._telemetry_buffer[ref]
        return " | ".join(lines)

    async def _process_message(
        self,
        text: str,
        sender: str,
        source: str,
        attachments: list | None = None,
        response_future: asyncio.Future | None = None,
        notify_meta: dict | None = None,
        trace_id: str = "",
        force_compact: bool = False,
    ) -> None:
        """Process a single message through the agentic loop."""
        if not trace_id:
            trace_id = str(uuid.uuid4())

        def _resolve(result: dict) -> None:
            """Safely resolve the HTTP response future."""
            if response_future is not None and not response_future.done():
                response_future.set_result(result)

        has_voice = attachments and any(
            a.content_type.startswith("audio/") and a.is_voice for a in attachments
        )

        provider = self.provider
        if provider is None:
            log.error("[%s] No provider configured", trace_id[:8])
            _resolve({"error": "no provider configured"})
            return

        model_cfg = self.config.model_config("primary")
        model_name = model_cfg.get("model", "")
        cost_rates = model_cfg.get("cost_per_mtok", [])

        # Process attachments into text descriptions + image blocks
        # Image blocks use neutral format: {"type": "image", "media_type": ..., "data": ...}
        # Provider adapters convert to their native API format in format_messages().
        image_blocks = []
        supports_vision = model_cfg.get("supports_vision", False)
        max_image_bytes = self.config.vision_max_image_bytes
        if attachments:
            caption = self.config.vision_default_caption
            too_large_msg = self.config.vision_too_large_msg
            for att in attachments:
                if att.content_type.startswith("image/"):
                    if not supports_vision:
                        text = (text + "\n" if text else "") + "[image received — vision not available with current provider]"
                        continue
                    try:
                        img_path = Path(att.local_path)
                        img_data = img_path.read_bytes()
                        try:
                            img_data = _fit_image(img_data, att.content_type, max_image_bytes,
                                                 self.config.vision_max_dimension,
                                                 self.config.vision_jpeg_quality_steps,
                                                 att.local_path)
                        except _ImageTooLarge as exc:
                            text = (text + "\n" if text else "") + f"[{too_large_msg} — {exc}]"
                            continue
                        image_blocks.append({
                            "type": "image",
                            "media_type": att.content_type,
                            "data": base64.b64encode(img_data).decode("ascii"),
                        })
                        text = (f"[{caption}, saved: {att.local_path}] " + text) if text else f"[{caption}, saved: {att.local_path}]"
                    except Exception as e:
                        log.error("Failed to read image %s: %s", att.local_path, e)
                        text = (text + "\n" if text else "") + f"[{too_large_msg} — could not read file]"

                elif att.content_type.startswith("audio/"):
                    if att.is_voice:
                        label = self.config.stt_voice_label
                        fail_label = self.config.stt_voice_fail_msg
                    else:
                        label = self.config.stt_audio_label
                        fail_label = self.config.stt_audio_fail_msg
                    try:
                        import stt as stt_mod
                        transcription = await stt_mod.transcribe(
                            self.config.raw("stt", default={}),
                            att.local_path, att.content_type,
                        )
                        text = (text + "\n" if text else "") + f"[{label}, saved: {att.local_path}]: {transcription}"
                    except Exception as e:
                        log.error("STT transcription failed (%s): %s",
                                  self.config.stt_backend, e)
                        text = (text + "\n" if text else "") + f"[{fail_label}]"

                else:
                    doc_text = None
                    if self.config.documents_enabled:
                        try:
                            doc_text = _extract_document_text(
                                att.local_path, att.content_type, att.filename or "",
                                max_chars=self.config.documents_max_chars,
                                max_bytes=self.config.documents_max_file_bytes,
                                text_extensions=self.config.documents_text_extensions,
                            )
                        except Exception as e:
                            log.error("Document extraction failed for %s: %s", att.filename, e)
                    if doc_text:
                        label = att.filename or "document"
                        text = (text + "\n" if text else "") + f"[document: {label}, saved: {att.local_path}]\n{doc_text}"
                    else:
                        text = (text + "\n" if text else "") + f"[attachment: {att.filename or 'file'}, {att.content_type}, saved: {att.local_path}]"

        # Track whether session pre-existed (for auto-close decision).
        # Notifications routed to the primary session must not close it.
        session_preexisted = (
            sender in self.session_mgr._sessions
            or sender in self.session_mgr._index
        )

        # Get or create session
        session = self.session_mgr.get_or_create(sender)

        # Expose current session to status tool
        from tools.status import set_current_session
        set_current_session(session)

        # Inject pending compaction warning from previous turn
        text, warning_consumed = _inject_warning(text, session.pending_system_warning)
        if warning_consumed:
            session.pending_system_warning = ""
            session._save_state()  # Persist cleared warning before agentic loop

        # Inject timestamp so the agent always knows the current time
        timestamp = time.strftime("[%a, %d. %b %Y - %H:%M %Z]")
        text = f"{timestamp}\n{text}"

        # Inject passive telemetry (latest HR, etc.) — zero-cost context
        telemetry = self._drain_telemetry()
        if telemetry:
            text = f"{text}\n[telemetry: {telemetry}]"

        session.trace_id = trace_id
        session.add_user_message(text, sender=sender, source=source)

        # Transiently inject image content blocks for the API call
        user_msg_idx = len(session.messages) - 1

        # Merge consecutive user messages (recovery from prior errors, JSONL rebuild)
        while len(session.messages) >= 2 and session.messages[-2].get("role") == "user":
            prev_text = _text_from_content(session.messages[-2].get("content", ""))
            last_text = _text_from_content(session.messages[-1].get("content", ""))
            session.messages[-2]["content"] = prev_text + "\n" + last_text
            session.messages.pop()
            user_msg_idx = len(session.messages) - 1
            log.warning("Merged consecutive user messages in session %s", session.id)

        if image_blocks:
            api_content = image_blocks + [{"type": "text", "text": text}]
            session.messages[user_msg_idx]["content"] = api_content

        # Build system prompt
        tool_descs = self.tool_registry.get_brief_descriptions()
        skill_index = self.skill_loader.build_index() if self.skill_loader else ""
        always_on = self.config.always_on_skills
        skill_bodies = self.skill_loader.get_bodies(always_on) if self.skill_loader else {}

        # Inject recall from previous session if this one is fresh
        recall_text = ""
        if len(session.messages) <= 1:
            recall_text = self.session_mgr.build_recall(
                sender, self.config.recall_archive_messages
            )
            # Structured memory context
            if self.config.consolidation_enabled:
                try:
                    from memory import get_session_start_context
                    conn = self._get_memory_conn()
                    memory_context = get_session_start_context(
                        conn=conn,
                        config=self.config,
                        max_facts=self.config.recall_max_facts,
                        max_episodes=self.config.recall_max_episodes_at_start,
                        max_tokens=self.config.recall_max_dynamic_tokens,
                    )
                    if memory_context:
                        recall_text = f"{recall_text}\n\n{memory_context}" if recall_text else memory_context
                except Exception:
                    log.exception("structured recall at session start failed")
                    if not recall_text:
                        recall_text = (
                            "[Memory recall unavailable — background error. "
                            "Use memory_search or memory_get to access memory manually.]"
                        )

        # Synthesis layer: transform raw recall blocks by style
        # Uses the primary provider for synthesis.
        if recall_text and self.config.recall_synthesis_style != "structured":
            try:
                from synthesis import synthesize_recall
                style = self.config.recall_synthesis_style
                prompt_map = {
                    "narrative": self.config.synthesis_prompt_narrative,
                    "factual": self.config.synthesis_prompt_factual,
                }
                synth_result = await synthesize_recall(
                    recall_text,
                    style,
                    provider,
                    prompt_override=prompt_map.get(style, ""),
                )
                recall_text = synth_result.text
                if synth_result.usage:
                    from agentic import _record_cost
                    _record_cost(
                        str(self.config.cost_db), session.id,
                        model_name,
                        synth_result.usage,
                        cost_rates,
                        call_type="synthesis",
                        trace_id=trace_id,
                        sqlite_timeout=self.config.sqlite_timeout,
                    )
            except Exception:
                log.exception("recall synthesis failed, using raw recall")

        system_blocks = self.context_builder.build(
            source=source,
            tool_descriptions=tool_descs,
            skill_index=skill_index,
            always_on_skills=always_on,
            skill_bodies=skill_bodies,
            extra_dynamic=recall_text,
            silent_tokens=self.config.silent_tokens,
            max_turns=self.config.max_turns,
            max_cost=self.config.max_cost_per_message,
            compaction_threshold=self.config.compaction_threshold,
            has_images=bool(image_blocks),
            has_voice=has_voice,
            sender=sender,
        )
        fmt_system = provider.format_system(system_blocks)

        # Typing indicator
        if self.config.typing_indicators and source not in self._NO_CHANNEL_DELIVERY:
            try:
                await self.channel.send_typing(sender)
            except Exception as e:
                log.debug("Typing indicator failed: %s", e)

        # Wire provider for memory_search inline synthesis
        if self.config.recall_synthesis_style != "structured":
            from tools.memory_tools import set_synthesis_provider
            set_synthesis_provider(provider)

        # Run agentic loop — it appends to session.messages in place
        tools = self.tool_registry.get_schemas()

        # Snapshot message count to track what the loop added
        msg_count_before = len(session.messages)

        # ── Monitor state callbacks ──────────────────────────────
        monitor_path = self.config.state_dir / "monitor.json"
        monitor_model = model_name
        turn_counter = [1]  # mutable closure
        turn_started_at = [time.time()]
        message_started_at = time.time()
        turns_history: list[dict] = []

        def _write_monitor(state: str, tools_in_flight: list[str] | None = None) -> None:
            data = {
                "state": state,
                "contact": sender,
                "session_id": session.id,
                "trace_id": trace_id,
                "model": monitor_model,
                "turn": turn_counter[0],
                "message_started_at": message_started_at,
                "turn_started_at": turn_started_at[0],
                "tools_in_flight": tools_in_flight or [],
                "turns": turns_history,
                "updated_at": time.time(),
            }
            try:
                tmp = monitor_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                tmp.rename(monitor_path)
            except Exception as exc:
                log.warning("Monitor write failed: %s", exc)

        def _on_response(response) -> None:
            duration_ms = int((time.time() - turn_started_at[0]) * 1000)
            tool_names = [tc.name for tc in response.tool_calls] if response.tool_calls else []
            turn_info = {
                "duration_ms": duration_ms,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "cache_write_tokens": response.usage.cache_write_tokens,
                "stop_reason": response.stop_reason,
                "tools": tool_names,
            }
            turns_history.append(turn_info)
            if response.stop_reason == "tool_use" and response.tool_calls:
                _write_monitor("tools", tools_in_flight=tool_names)
            else:
                _write_monitor("idle")

        def _on_tool_results(results_msg) -> None:
            turn_counter[0] += 1
            turn_started_at[0] = time.time()
            _write_monitor("thinking")

        _write_monitor("thinking")

        message_retries = self.config.message_retries
        message_retry_delay = self.config.message_retry_base_delay

        try:
            for msg_attempt in range(1 + message_retries):
                try:
                    max_cost = self.config.max_cost_per_message
                    response = await run_agentic_loop(
                        provider=provider,
                        system=fmt_system,
                        messages=session.messages,
                        tools=tools,
                        tool_executor=self.tool_registry,
                        max_turns=self.config.max_turns,
                        timeout=self.config.agent_timeout,
                        api_retries=self.config.api_retries,
                        api_retry_base_delay=self.config.api_retry_base_delay,
                        sqlite_timeout=self.config.sqlite_timeout,
                        cost_db=str(self.config.cost_db),
                        session_id=session.id,
                        model_name=model_name,
                        cost_rates=cost_rates,
                        max_cost=float(max_cost),
                        on_response=_on_response,
                        on_tool_results=_on_tool_results,
                        trace_id=trace_id,
                    )
                    break  # Success
                except Exception as e:
                    if not is_transient_error(e) or msg_attempt >= message_retries:
                        raise  # Non-transient or exhausted — fall through to outer except

                    # Restore text-only content before waiting
                    if image_blocks:
                        session.messages[user_msg_idx]["content"] = text

                    delay = message_retry_delay * (2 ** msg_attempt) * (0.5 + random.random())  # noqa: S311
                    log.warning(
                        "[%s] Message retry (%d/%d) for %s: %s — waiting %.0fs",
                        trace_id[:8], msg_attempt + 1, message_retries, sender, e, delay,
                    )
                    _write_monitor("retry_wait")
                    await asyncio.sleep(delay)

                    # Re-inject image blocks for next attempt
                    if image_blocks:
                        api_content = image_blocks + [{"type": "text", "text": text}]
                        session.messages[user_msg_idx]["content"] = api_content

                    _write_monitor("thinking")

        except Exception as e:
            log.error("[%s] Agentic loop failed: %s", trace_id[:8], e)
            # Restore text-only content before returning
            if image_blocks:
                session.messages[user_msg_idx]["content"] = text
            # Remove orphaned user message to prevent consecutive-user corruption
            if session.messages and session.messages[-1].get("role") == "user":
                session.messages.pop()
                session._save_state()
            _resolve({"error": str(e), "session_id": session.id})
            if source not in self._NO_CHANNEL_DELIVERY:
                try:
                    await self.channel.send(sender, self.config.error_message)
                except Exception as e:
                    log.error("Failed to deliver error message to %s: %s", sender, e)
            await self._fire_webhook(
                reply="", session_id=session.id, sender=sender, source=source,
                silent=False, tokens={"input": 0, "output": 0},
                notify_meta=notify_meta, trace_id=trace_id,
            )
            # Auto-close system sessions even on error — but only if the
            # session was created by this event (not a pre-existing session).
            if source == "system" and not session_preexisted:
                try:
                    await self.session_mgr.close_session(sender)
                    log.info("Auto-closed system session for %s", sender)
                except Exception:
                    log.warning("Auto-close failed for system session %s",
                                sender, exc_info=True)
            return
        finally:
            _write_monitor("idle")

        # Persist all new messages the loop added (loop already appended to session.messages)
        for msg in session.messages[msg_count_before:]:
            role = msg.get("role", "")
            if role == "assistant":
                session.persist_assistant_message(msg)
            elif role == "tool_results":
                session.persist_tool_results(msg.get("results", []))

        # Restore text-only content before state persistence
        if image_blocks:
            enriched = _enrich_image_caption(
                text, caption, session.messages, msg_count_before,
                max_desc_len=self.config.vision_caption_max_chars,
            )
            session.messages[user_msg_idx]["content"] = enriched

        session._save_state()

        reply = response.text or ""

        # Cost limit: deliver agent text if available, else friendly fallback
        if response.cost_limited and not reply.strip():
            reply = ("[cost limit reached — max_cost_per_message in lucyd.toml. "
                     "raise or set to 0 to disable.]")

        # Silent token check
        silent = _is_silent(reply, self.config.silent_tokens)
        token_info = {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        }
        if silent:
            log.info("Silent reply suppressed: %s", reply[:100])
            _resolve({
                "reply": reply,
                "silent": True,
                "session_id": session.id,
                "tokens": token_info,
            })
            await self._fire_webhook(
                reply=reply, session_id=session.id, sender=sender, source=source,
                silent=True, tokens=token_info, notify_meta=notify_meta,
                trace_id=trace_id,
            )
        else:
            # Resolve HTTP future with the response
            _resolve({
                "reply": reply,
                "session_id": session.id,
                "tokens": token_info,
            })

            # Deliver reply via channel (skipped for system and HTTP sources)
            if _should_deliver(reply, source, self._NO_CHANNEL_DELIVERY):
                try:
                    await self.channel.send(sender, reply)
                except Exception as e:
                    log.error("Failed to deliver reply: %s", e)

            await self._fire_webhook(
                reply=reply, session_id=session.id, sender=sender, source=source,
                silent=False, tokens=token_info, notify_meta=notify_meta,
                trace_id=trace_id,
            )

        # Two-threshold compaction: warning at 80%, hard compaction at 100%
        if _should_warn_context(
            input_tokens=session.last_input_tokens,
            compaction_threshold=self.config.compaction_threshold,
            needs_compaction=session.needs_compaction(self.config.compaction_threshold),
            already_warned=session.warned_about_compaction,
        ):
            max_ctx = model_cfg.get("max_context_tokens", 0)
            pct = session.last_input_tokens * 100 // max_ctx if max_ctx > 0 else 0
            session.pending_system_warning = (
                f"[system: context at {session.last_input_tokens:,} tokens "
                f"({pct}% of capacity). compaction will summarize older messages "
                f"at {self.config.compaction_threshold:,}. save anything important "
                f"to memory files, then continue the conversation normally.]"
            )
            session.warned_about_compaction = True
            session._save_state()
            log.info("Compaction warning set for session %s at %d tokens",
                     session.id, session.last_input_tokens)

        # Pre-compaction consolidation: extract structured data before compaction
        _needs_compact = force_compact or session.needs_compaction(
            self.config.compaction_threshold)
        if _needs_compact and self.config.consolidation_enabled:
            try:
                import consolidation
                conn = self._get_memory_conn()
                result = await consolidation.consolidate_session(
                    session_id=session.id,
                    messages=session.messages,
                    compaction_count=session.compaction_count,
                    config=self.config,
                    provider=self.provider,
                    context_builder=self.context_builder,
                    conn=conn,
                    cost_db=str(self.config.cost_db),
                    trace_id=trace_id,
                )
                if result["facts_added"] or result.get("episode_id"):
                    log.info(
                        "consolidation: %d facts, episode=%s",
                        result["facts_added"], result.get("episode_id"),
                    )
            except Exception:
                log.exception("consolidation failed, continuing without")

        # Check for compaction
        if _needs_compact:
            prompt = self.config.compaction_prompt.replace(
                "{agent_name}", self.config.agent_name,
            ).replace(
                "{max_tokens}", str(self.config.compaction_max_tokens),
            )
            await self.session_mgr.compact_session(
                session, provider, prompt,
                cost_db=str(self.config.cost_db),
                model_name=model_name,
                cost_rates=cost_rates,
                trace_id=trace_id,
                keep_recent_pct=self.config.compaction_keep_pct,
                min_messages=self.config.compaction_min_messages,
                tool_result_max_chars=self.config.compaction_tool_result_max_chars,
                max_tokens=self.config.compaction_max_tokens,
                system_blocks=self.context_builder.build_stable(),
                verify_enabled=self.config.verify_enabled,
                verify_max_turn_labels=self.config.verify_max_turn_labels,
                verify_grounding_threshold=self.config.verify_grounding_threshold,
                sqlite_timeout=self.config.sqlite_timeout,
            )

        # Auto-close one-shot system sessions (evolution, heartbeat).
        # System-sourced messages creating fresh sessions are fire-and-forget —
        # no operator on the other end, so the session would linger indefinitely.
        # Skip for pre-existing sessions (notifications routed to primary session)
        # and for force_compact (primary session stays open).
        if source == "system" and not force_compact and not session_preexisted:
            try:
                await self.session_mgr.close_session(sender)
                log.info("Auto-closed system session for %s", sender)
            except Exception:
                log.warning("Auto-close failed for system session %s",
                            sender, exc_info=True)

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
                    provider=self.provider,
                    context_builder=self.context_builder,
                    conn=conn,
                    cost_db=str(self.config.cost_db),
                )
        except Exception:
            log.exception("consolidation on close failed")

    async def _fire_webhook(
        self,
        reply: str,
        session_id: str,
        sender: str,
        source: str,
        silent: bool,
        tokens: dict,
        notify_meta: dict | None,
        trace_id: str = "",
    ) -> None:
        """POST webhook callback to configured URL. No-op when unconfigured."""
        url = self.config.http_callback_url
        if not url:
            return

        import httpx

        payload = {
            "reply": reply,
            "session_id": session_id,
            "sender": sender,
            "source": source,
            "silent": silent,
            "tokens": tokens,
            "agent": self.config.agent_name,
        }
        if trace_id:
            payload["trace_id"] = trace_id
        if notify_meta:
            payload["notify_meta"] = notify_meta

        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = self.config.http_callback_token
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=self.config.http_callback_timeout) as client:
                await client.post(url, json=payload, headers=headers)
        except Exception as e:
            log.warning("Webhook callback failed (%s): %s", url, e)

    async def _reset_session(self, target: str, by_id: bool = False) -> dict:
        """Reset session by target: 'all', session ID, or contact name.

        When by_id=True, target is treated as a session ID directly.
        Otherwise, UUIDs are auto-detected and routed to close_session_by_id.
        Returns result dict with reset status.
        """
        if not self.session_mgr:
            return {"reset": False, "reason": "no session manager"}

        if target == "all":
            contacts = list(self.session_mgr._index.keys())
            for contact in contacts:
                await self.session_mgr.close_session(contact)
            return {"reset": True, "target": "all", "count": len(contacts)}

        if by_id or _is_uuid(target):
            if await self.session_mgr.close_session_by_id(target):
                return {"reset": True, "target": target, "type": "session_id"}
            return {"reset": False, "reason": f"no session found for ID: {target}"}

        # "user" shortcut: find primary operator contact
        if target == "user":
            for contact in self.session_mgr._index:
                # Skip framework-internal senders
                if contact in ("system",) or contact.startswith(("http-", "cli")):
                    continue
                target = contact
                break
            else:
                return {"reset": False, "reason": "no user session found"}

        if await self.session_mgr.close_session(target):
            return {"reset": True, "target": target, "type": "contact"}
        return {"reset": False, "reason": f"no session found for: {target}"}

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
                if item.get("all"):
                    target = "all"
                    by_id = False
                elif item.get("session_id"):
                    target = item["session_id"]
                    by_id = True
                else:
                    target = item.get("sender", "")
                    by_id = False
                result = await self._reset_session(target, by_id=by_id)
                log.info("Reset: %s", result)
                reset_future = item.get("response_future")
                if reset_future is not None and not reset_future.done():
                    reset_future.set_result(result)

    def _build_sessions(self) -> list[dict]:
        """Build session list for HTTP /sessions."""
        from session import build_session_info

        if not self.session_mgr:
            return []

        max_ctx = 0
        try:
            model_cfg = self.config.model_config("primary")
            max_ctx = model_cfg.get("max_context_tokens", 0)
        except Exception:  # noqa: S110 — config lookup for session listing; graceful degradation to 0
            pass

        result = []
        for contact, entry in self.session_mgr._index.items():
            session_id = entry.get("session_id", "")
            live = self.session_mgr._sessions.get(contact)
            info = build_session_info(
                sessions_dir=self.session_mgr.dir,
                session_id=session_id,
                session=live,
                cost_db_path=str(self.config.cost_db),
                max_context_tokens=max_ctx,
            )
            info["contact"] = contact
            info["created_at"] = entry.get("created_at")
            if live:
                info["model"] = live.model
            result.append(info)
        return result

    def _build_cost(self, period: str) -> dict:
        """Build cost breakdown for HTTP /cost."""
        from config import today_start_ts

        cost_path = str(self.config.cost_db)
        empty = {"period": period, "total_cost": 0.0, "models": []}

        if period == "today":
            ts_filter = today_start_ts()
        elif period == "week":
            ts_filter = int(time.time()) - 7 * 86400
        else:  # "all"
            ts_filter = 0

        rows = cost_db_query(
            cost_path,
            """SELECT model,
                      SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens,
                      SUM(cache_read_tokens) AS cache_read_tokens,
                      SUM(cache_write_tokens) AS cache_write_tokens,
                      SUM(cost_usd) AS cost_usd
               FROM costs
               WHERE timestamp >= ?
               GROUP BY model""",
            (ts_filter,),
            sqlite_timeout=self.config.sqlite_timeout,
        )

        if not rows:
            return empty

        models = []
        total = 0.0
        for r in rows:
            cost = r["cost_usd"] or 0.0
            total += cost
            models.append({
                "model": r["model"],
                "input_tokens": r["input_tokens"] or 0,
                "output_tokens": r["output_tokens"] or 0,
                "cache_read_tokens": r["cache_read_tokens"] or 0,
                "cache_write_tokens": r["cache_write_tokens"] or 0,
                "cost_usd": round(cost, 6),
            })

        return {
            "period": period,
            "total_cost": round(total, 4),
            "models": models,
        }

    def _build_monitor(self) -> dict:
        """Build monitor data for HTTP /monitor."""
        monitor_path = self.config.state_dir / "monitor.json"
        if not monitor_path.exists():
            return {"state": "unknown"}
        try:
            return json.loads(monitor_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"state": "unknown"}

    def _build_history(self, session_id: str, full: bool = False) -> dict:
        """Build session history for HTTP /sessions/{id}/history."""
        from session import read_history_events

        if not self.session_mgr:
            return {"session_id": session_id, "events": []}

        events = read_history_events(self.session_mgr.dir, session_id, full=full)
        return {"session_id": session_id, "events": events}

    def _build_status(self) -> dict:
        """Build status dict for HTTP /status and SIGUSR2."""
        from config import today_start_ts

        today_cost = 0.0
        rows = cost_db_query(
            str(self.config.cost_db),
            "SELECT SUM(cost_usd) FROM costs WHERE timestamp >= ?",
            (today_start_ts(),),
            sqlite_timeout=self.config.sqlite_timeout,
        )
        if rows and rows[0][0]:
            today_cost = rows[0][0]

        active_sessions = 0
        if self.session_mgr:
            active_sessions = len(self.session_mgr._index)

        return {
            "status": "ok",
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.start_time),
            "channel": self.config.channel_type,
            "model": self.config.model_config("primary").get("model", ""),
            "active_sessions": active_sessions,
            "today_cost": round(today_cost, 4),
            "queue_depth": self.queue.qsize(),
        }

    async def _handle_compact(self) -> dict:
        """Force-compact the primary session after agent writes diary."""
        # Find primary session (longest active, non-system sender)
        primary = None
        system_senders = frozenset(("evolution", "system"))
        for sender in list(self.session_mgr._index):
            if sender in system_senders:
                continue
            session = self.session_mgr.get_or_create(sender)
            if primary is None or len(session.messages) > len(primary[1].messages):
                primary = (sender, session)

        if not primary:
            return {"status": "skipped", "reason": "no active session"}

        sender, session = primary
        today = time.strftime("%Y-%m-%d")
        diary_text = self.config.diary_prompt.replace("{date}", today)

        tid = str(uuid.uuid4())
        log.info("[%s] Forced compact: diary + compaction for session %s (%s)",
                 tid[:8], session.id, sender)

        await self._process_message(
            text=diary_text,
            sender=sender,
            source="system",
            trace_id=tid,
            force_compact=True,
        )
        return {"status": "completed", "session": session.id}

    async def _handle_evolve(self) -> dict:
        """Handle evolution request — push to queue for self-driven evolution."""
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
            source = msgs[0].get("source", "")
            n_meta = msgs[0].get("notify_meta")
            tid = str(uuid.uuid4())
            log.info("[%s] Processing message from %s (source=%s)",
                     tid[:8], sender, source)
            await self._process_message(
                combined_text, sender, source,
                attachments=combined_attachments or None,
                notify_meta=n_meta,
                trace_id=tid,
            )

        async def process_http_immediate(item: dict) -> None:
            """Process HTTP /chat messages immediately (no debounce).

            Each /chat request has its own Future — combining messages
            would lose Futures and break response delivery.
            """
            tid = str(uuid.uuid4())
            log.info("[%s] Processing HTTP message from %s",
                     tid[:8], item.get("sender", "http"))
            await self._process_message(
                text=item.get("text", ""),
                sender=item.get("sender", "http"),
                source=item.get("type", "http"),
                attachments=item.get("attachments"),
                response_future=item.get("response_future"),
                notify_meta=item.get("notify_meta"),
                trace_id=tid,
            )

        while self.running:
            # Drain control queue first (resets bypass message queue)
            await self._drain_control_queue()

            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=self.config.queue_poll_interval)
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

            if isinstance(item, InboundMessage):
                sender = item.sender
                source = item.source
                text = item.text
                # Inject quote context so the LLM sees what the user replied to
                if item.quote:
                    max_q = self.config.quote_max_chars
                    q = item.quote if len(item.quote) <= max_q else item.quote[:max_q] + "…"
                    text = f"[replying to: {q}]\n{text}"
                attachments = item.attachments
                notify_meta = None
                # Store last inbound timestamp for reaction tool (ms int)
                self._last_inbound_ts[sender] = int(item.timestamp * 1000)
                self._last_inbound_ts.move_to_end(sender)
                while len(self._last_inbound_ts) > 1000:
                    self._last_inbound_ts.popitem(last=False)
            elif isinstance(item, dict):
                # Handle session reset (safety net — normally via control queue)
                if item.get("type") == "reset":
                    if item.get("all"):
                        target = "all"
                        by_id = False
                    elif item.get("session_id"):
                        target = item["session_id"]
                        by_id = True
                    else:
                        target = item.get("sender", "")
                        by_id = False
                    result = await self._reset_session(target, by_id=by_id)
                    log.info("Reset: %s", result)
                    # Resolve HTTP future if this reset came through the API
                    reset_future = item.get("response_future")
                    if reset_future is not None and not reset_future.done():
                        reset_future.set_result(result)
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

                # FIFO / notify message
                sender = item.get("sender", "system")
                source = item.get("type", "system")
                text = item.get("text", "")
                attachments = item.get("attachments")
                notify_meta = item.get("notify_meta")

                # Route notifications to primary session when configured
                if item.get("notify") and self.config.primary_sender:
                    sender = self.config.primary_sender

                # Passive telemetry — buffer and skip processing
                ref = (notify_meta or {}).get("ref", "")
                if ref and ref in self._passive_refs:
                    priority = ((notify_meta or {}).get("data") or {}).get(
                        "priority", "")
                    if priority != "active":
                        self._telemetry_buffer[ref] = {
                            "text": text,
                            "notify_meta": notify_meta,
                            "timestamp": time.time(),
                        }
                        continue
            else:
                continue

            if not text and not attachments:
                continue

            # Debounce: collect messages from same sender
            if sender not in pending:
                pending[sender] = []
            pending[sender].append({"text": text, "source": source,
                                    "attachments": attachments, "notify_meta": notify_meta})

            # Wait for more messages
            await asyncio.sleep(debounce_s)

            # Drain all pending senders
            for s in list(pending.keys()):
                await drain_pending(s)

            # Drain control queue after message processing
            await self._drain_control_queue()

    async def _channel_reader(self) -> None:
        """Read messages from channel and push to queue."""
        try:
            async for msg in self.channel.receive():
                await self.queue.put(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("Channel reader failed: %s", e)
        # Channel exhausted (e.g., piped stdin EOF) — signal shutdown
        # Push a sentinel so the message loop can drain and exit
        await self.queue.put(None)

    def _setup_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register Unix signal handlers."""
        def handle_sigusr1():
            log.info("SIGUSR1: reloading workspace files")
            if self.context_builder:
                self.context_builder.reload()
            if self.skill_loader:
                self.skill_loader.scan()

        def handle_sigusr2():
            log.info("SIGUSR2: writing status")
            status = {
                "pid": os.getpid(),
                "uptime_s": time.time() - self.start_time,
                "tools": self.tool_registry.tool_names if self.tool_registry else [],
                "channel": self.config.channel_type,
                "model": self.config.model_config("primary").get("model", ""),
            }
            status_path = self.config.state_dir / "status.json"
            status_path.write_text(json.dumps(status, indent=2))

        def handle_sigterm():
            log.info("SIGTERM: shutting down gracefully")
            self.running = False

        try:
            loop.add_signal_handler(signal.SIGUSR1, handle_sigusr1)
            loop.add_signal_handler(signal.SIGUSR2, handle_sigusr2)
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

        _check_pid_file(pid_path)
        _write_pid_file(pid_path)

        try:
            self._init_provider()
            self._init_channel()
            self._init_sessions()
            self._init_skills()
            self._init_context()
            self._init_cost_db()
            self._init_tools()
            self._init_plugins()

            # Passive telemetry refs
            self._passive_refs = frozenset(self.config.passive_notify_refs)

            # Context budget startup check
            self._check_context_budget()

            # Register consolidation on session close
            if self.config.consolidation_enabled:
                self.session_mgr.on_close(self._consolidate_on_close)

            await self.channel.connect()
            log.info("Channel connected: %s", cfg.channel_type)

            loop = asyncio.get_event_loop()
            self._setup_signals(loop)

            # Start FIFO reader
            fifo_path = cfg.state_dir / "control.pipe"
            self._fifo_task = asyncio.create_task(
                _fifo_reader(fifo_path, self.queue, self._control_queue)
            )

            # Start channel reader
            channel_task = asyncio.create_task(self._channel_reader())

            # Start HTTP API server (if enabled)
            self._http_api = None
            if cfg.http_enabled:
                from channels.http_api import HTTPApi
                self._http_api = HTTPApi(
                    queue=self.queue,
                    control_queue=self._control_queue,
                    host=cfg.http_host,
                    port=cfg.http_port,
                    auth_token=cfg.http_auth_token,
                    agent_timeout=cfg.agent_timeout,
                    get_status=self._build_status,
                    get_sessions=self._build_sessions,
                    get_cost=self._build_cost,
                    get_monitor=self._build_monitor,
                    handle_reset=None,  # Resets route through control queue now
                    get_history=self._build_history,
                    handle_evolve=self._handle_evolve,
                    download_dir=cfg.http_download_dir,
                    max_body_bytes=cfg.http_max_body_bytes,
                    rate_limit=cfg.http_rate_limit,
                    rate_window=cfg.http_rate_window,
                    status_rate_limit=cfg.http_status_rate_limit,
                    rate_cleanup_threshold=cfg.http_rate_cleanup_threshold,
                    agent_name=cfg.agent_name,
                )
                await self._http_api.start()

            log.info("Lucyd daemon running (PID %d)", os.getpid())

            # Main message processing loop
            await self._message_loop()

            # Cleanup
            if self._http_api:
                await self._http_api.stop()
            channel_task.cancel()
            if self._fifo_task:
                self._fifo_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channel_task

        except Exception as e:
            log.error("Fatal error: %s", e, exc_info=True)
            raise
        finally:
            # Persist active session state before cleanup.
            # Does NOT call close_session() (which triggers LLM consolidation
            # callbacks and archival — wrong during shutdown). Sessions resume
            # from state files on next startup via get_or_create().
            if hasattr(self, "session_mgr") and self.session_mgr:
                for session in list(self.session_mgr._sessions.values()):
                    with contextlib.suppress(Exception):  # session state persist on shutdown; failure is benign
                        session._save_state()

            # Disconnect channel (close httpx client, clean downloads)
            if self.channel is not None:
                with contextlib.suppress(Exception):  # channel cleanup on shutdown; failure is benign
                    await self.channel.disconnect()
            # Close memory DB connection
            if self._memory_conn is not None:
                with contextlib.suppress(Exception):  # DB close on shutdown; failure is benign
                    self._memory_conn.close()
            _remove_pid_file(pid_path)
            # Clean up FIFO
            fifo_path = cfg.state_dir / "control.pipe"
            with contextlib.suppress(Exception):  # FIFO cleanup on shutdown; failure is benign
                fifo_path.unlink(missing_ok=True)
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
    parser.add_argument(
        "--channel",
        help="Override channel type (e.g., 'cli' for testing)",
    )
    args = parser.parse_args()

    # Build overrides from CLI args
    overrides = {}
    if args.channel:
        overrides["channel.type"] = args.channel

    try:
        config = load_config(args.config, overrides=overrides)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    daemon = LucydDaemon(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
