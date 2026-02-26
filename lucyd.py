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
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Add lucyd directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import random

from agentic import _init_cost_db, is_transient_error, run_agentic_loop
from channels import Attachment, InboundMessage, create_channel
from config import Config, ConfigError, load_config
from context import ContextBuilder
from providers import create_provider
from session import SessionManager, _text_from_content, set_audit_truncation
from skills import SkillLoader
from tools import ToolRegistry

log = logging.getLogger("lucyd")

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
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: S110 — daemon shutdown cleanup; failure is benign
        pass


# ─── Control FIFO ────────────────────────────────────────────────

async def _fifo_reader(fifo_path: Path, queue: asyncio.Queue) -> None:
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
                        # Reset messages only need type + sender/session_id
                        if msg.get("type") == "reset":
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
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.providers: dict[str, Any] = {}
        self.channel: Any = None
        self.session_mgr: SessionManager | None = None
        self.context_builder: ContextBuilder | None = None
        self.skill_loader: SkillLoader | None = None
        self.tool_registry: ToolRegistry | None = None
        self._fifo_task: asyncio.Task | None = None
        self._http_api: Any = None
        self._memory_conn: Any = None
        self._last_inbound_ts: collections.OrderedDict[str, int] = collections.OrderedDict()  # sender → ms timestamp

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

        # Silence noisy third-party loggers
        for name in ("httpx", "httpcore", "anthropic", "openai"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def _init_providers(self) -> None:
        """Create provider instances from model configs."""
        for name in self.config.all_model_names:
            try:
                model_cfg = self.config.model_config(name)
                provider_type = model_cfg.get("provider", "")

                # Explicit per-model key (from provider file or toml)
                api_key_env = model_cfg.get("api_key_env", "")
                if api_key_env:
                    api_key = os.environ.get(api_key_env, "")
                # Fallback: provider-type default (backward compat)
                elif provider_type == "anthropic-compat":
                    api_key = self.config.api_key("anthropic")
                elif provider_type == "openai-compat":
                    api_key = self.config.api_key("openai")
                else:
                    api_key = ""

                if not api_key and provider_type in ("anthropic-compat", "openai-compat"):
                    log.warning("No API key for model '%s' (%s)", name, provider_type)
                    continue

                self.providers[name] = create_provider(model_cfg, api_key)
                log.info("Provider '%s': %s / %s", name, provider_type,
                         model_cfg.get("model", ""))
            except Exception as e:
                log.error("Failed to create provider '%s': %s", name, e)

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
                api_key=self.config.api_key("brave"),
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
                providers=self.providers,
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
            "providers": self.providers,
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
                self.config.memory_db, timeout=30,
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
            tier_overrides=self.config.context_tiers,
        )

    def _init_skills(self) -> None:
        self.skill_loader = SkillLoader(
            workspace=self.config.workspace,
            skills_dir=self.config.skills_dir,
        )
        self.skill_loader.scan()

    def _init_cost_db(self) -> None:
        _init_cost_db(str(self.config.cost_db))

    # Sources that suppress channel delivery (typing, intermediate text, final reply).
    # The agentic loop still runs — tools execute, cost is recorded, session persists.
    _NO_CHANNEL_DELIVERY = frozenset({"system", "http"})

    async def _process_message(
        self,
        text: str,
        sender: str,
        source: str,
        tier: str = "full",
        attachments: list | None = None,
        response_future: asyncio.Future | None = None,
        notify_meta: dict | None = None,
    ) -> None:
        """Process a single message through the agentic loop."""

        def _resolve(result: dict) -> None:
            """Safely resolve the HTTP response future."""
            if response_future is not None and not response_future.done():
                response_future.set_result(result)

        # Route to model
        model_name = self.config.route_model(source)

        # Route to vision model if message has image attachments
        has_images = attachments and any(
            a.content_type.startswith("image/") for a in attachments
        )
        if has_images:
            vision_model = self.config.route_model("vision")
            if vision_model in self.providers:
                model_name = vision_model

        has_voice = attachments and any(
            a.content_type.startswith("audio/") and a.is_voice for a in attachments
        )

        provider = self.providers.get(model_name)
        if provider is None:
            log.error("No provider for model '%s' (source: %s)", model_name, source)
            _resolve({"error": f"no provider for model '{model_name}'"})
            return

        model_cfg = self.config.model_config(model_name)

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
                        text = (f"[{caption}] " + text) if text else f"[{caption}]"
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
                        transcription = await self._transcribe_audio(att.local_path, att.content_type)
                        text = (text + "\n" if text else "") + f"[{label}]: {transcription}"
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
                        text = (text + "\n" if text else "") + f"[document: {label}]\n{doc_text}"
                    else:
                        text = (text + "\n" if text else "") + f"[attachment: {att.filename or 'file'}, {att.content_type}]"

        # Get or create session
        session = self.session_mgr.get_or_create(sender, model=model_name)

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
            recall_text = self.session_mgr.build_recall(sender)
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
                        if recall_text:
                            recall_text = f"{recall_text}\n\n{memory_context}"
                        else:
                            recall_text = memory_context
                except Exception:
                    log.exception("structured recall at session start failed")
                    if not recall_text:
                        recall_text = (
                            "[Memory recall unavailable — background error. "
                            "Use memory_search or memory_get to access memory manually.]"
                        )

        # Synthesis layer: transform raw recall blocks by style
        # Uses the same provider routed for this message — no model mismatch.
        if recall_text and self.config.recall_synthesis_style != "structured":
            try:
                from synthesis import synthesize_recall
                synth_result = await synthesize_recall(
                    recall_text,
                    self.config.recall_synthesis_style,
                    provider,
                )
                recall_text = synth_result.text
                if synth_result.usage:
                    from agentic import _record_cost
                    _record_cost(
                        str(self.config.cost_db), session.id,
                        model_cfg.get("model", model_name),
                        synth_result.usage,
                        model_cfg.get("cost_per_mtok", []),
                    )
            except Exception:
                log.exception("recall synthesis failed, using raw recall")

        system_blocks = self.context_builder.build(
            tier=tier,
            source=source,
            tool_descriptions=tool_descs,
            skill_index=skill_index,
            always_on_skills=always_on,
            skill_bodies=skill_bodies,
            extra_dynamic=recall_text,
            silent_tokens=self.config.silent_tokens,
            max_turns=self.config.max_turns,
            max_cost=float(self.config.raw("behavior", "max_cost_per_message", default=0.0)),
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

        # Wire current provider for memory_search synthesis (matches routed model)
        if self.config.recall_synthesis_style != "structured":
            from tools.memory_tools import set_synthesis_provider
            set_synthesis_provider(provider)

        # Run agentic loop — it appends to session.messages in place
        tools = self.tool_registry.get_schemas()
        cost_rates = model_cfg.get("cost_per_mtok", [])

        # Snapshot message count to track what the loop added
        msg_count_before = len(session.messages)

        # ── Monitor state callbacks ──────────────────────────────
        monitor_path = self.config.state_dir / "monitor.json"
        monitor_model = model_cfg.get("model", model_name)
        turn_counter = [1]  # mutable closure
        turn_started_at = [time.time()]
        message_started_at = time.time()
        turns_history: list[dict] = []

        def _write_monitor(state: str, tools_in_flight: list[str] | None = None) -> None:
            data = {
                "state": state,
                "contact": sender,
                "session_id": session.id,
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
        response = None

        try:
            for msg_attempt in range(1 + message_retries):
                try:
                    max_cost = self.config.raw("behavior", "max_cost_per_message", default=0.0)
                    response = await run_agentic_loop(
                        provider=provider,
                        system=fmt_system,
                        messages=session.messages,
                        tools=tools,
                        tool_executor=self.tool_registry,
                        max_turns=self.config.max_turns,
                        timeout=self.config.agent_timeout,
                        cost_db=str(self.config.cost_db),
                        session_id=session.id,
                        model_name=model_cfg.get("model", ""),
                        cost_rates=cost_rates,
                        max_cost=float(max_cost),
                        on_response=_on_response,
                        on_tool_results=_on_tool_results,
                        api_retries=self.config.api_retries,
                        api_retry_base_delay=self.config.api_retry_base_delay,
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
                        "Message retry (%d/%d) for %s: %s — waiting %.0fs",
                        msg_attempt + 1, message_retries, sender, e, delay,
                    )
                    _write_monitor("retry_wait")
                    await asyncio.sleep(delay)

                    # Re-inject image blocks for next attempt
                    if image_blocks:
                        api_content = image_blocks + [{"type": "text", "text": text}]
                        session.messages[user_msg_idx]["content"] = api_content

                    _write_monitor("thinking")

        except Exception as e:
            log.error("Agentic loop failed: %s", e)
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
                notify_meta=notify_meta,
            )
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
            session.messages[user_msg_idx]["content"] = text

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
            )
            return

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
        )

        # Two-threshold compaction: warning at 80%, hard compaction at 100%
        if _should_warn_context(
            input_tokens=session.last_input_tokens,
            compaction_threshold=self.config.compaction_threshold,
            needs_compaction=session.needs_compaction(self.config.compaction_threshold),
            already_warned=session.warned_about_compaction,
        ):
            from tools.status import MAX_CONTEXT_TOKENS
            if MAX_CONTEXT_TOKENS > 0:
                pct = session.last_input_tokens * 100 // MAX_CONTEXT_TOKENS
            else:
                pct = 0
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
        if (session.needs_compaction(self.config.compaction_threshold)
                and self.config.consolidation_enabled):
            try:
                import consolidation
                conn = self._get_memory_conn()
                result = await consolidation.consolidate_session(
                    session_id=session.id,
                    messages=session.messages,
                    compaction_count=session.compaction_count,
                    config=self.config,
                    subagent_provider=self.providers.get("subagent"),
                    primary_provider=self.providers.get("primary"),
                    context_builder=self.context_builder,
                    conn=conn,
                )
                if result["facts_added"] or result.get("episode_id"):
                    log.info(
                        "consolidation: %d facts, episode=%s",
                        result["facts_added"], result.get("episode_id"),
                    )
            except Exception:
                log.exception("consolidation failed, continuing without")

        # Check for compaction
        if session.needs_compaction(self.config.compaction_threshold):
            compaction_model = self.config.compaction_model
            compaction_provider = self.providers.get(compaction_model)
            if compaction_provider:
                prompt = self.config.compaction_prompt.replace(
                    "{agent_name}", self.config.agent_name,
                )
                await self.session_mgr.compact_session(
                    session, compaction_provider, prompt,
                )

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
                    subagent_provider=self.providers.get("subagent"),
                    primary_provider=self.providers.get("primary"),
                    context_builder=self.context_builder,
                    conn=conn,
                )
        except Exception:
            log.exception("consolidation on close failed")

    async def _transcribe_audio(self, file_path: str, content_type: str) -> str:
        """Transcribe audio via configured STT backend.

        Dispatches to local (whisper.cpp) or cloud (OpenAI) based on
        [stt] backend config. Returns transcribed text or raises on failure.
        """
        backend = self.config.stt_backend
        if backend == "local":
            return await self._transcribe_local(file_path)
        if backend == "openai":
            return await self._transcribe_openai(file_path, content_type)
        raise RuntimeError(f"Unknown STT backend: {backend}")

    async def _transcribe_openai(self, file_path: str, content_type: str) -> str:
        """Transcribe audio via OpenAI Whisper cloud API."""
        import httpx

        api_key = self.config.api_key("openai")
        if not api_key:
            raise RuntimeError("No OpenAI API key for Whisper")

        api_url = self.config.stt_openai_api_url
        model = self.config.stt_openai_model
        timeout = self.config.stt_openai_timeout

        audio_data = Path(file_path).read_bytes()
        filename = Path(file_path).name

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (filename, audio_data, content_type)},
                data={"model": model},
            )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            if not text:
                raise RuntimeError("Whisper returned empty transcription")
            return text

    async def _transcribe_local(self, file_path: str) -> str:
        """Transcribe audio via local whisper.cpp server.

        Converts OGG/audio to WAV (16kHz mono) via ffmpeg, then POSTs
        to the whisper.cpp HTTP inference endpoint.
        """
        import subprocess
        import tempfile

        import httpx

        endpoint = self.config.stt_local_endpoint
        language = self.config.stt_local_language
        ffmpeg_timeout = self.config.stt_local_ffmpeg_timeout
        request_timeout = self.config.stt_local_request_timeout

        # Convert to WAV (16kHz mono) for whisper.cpp
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(wav_fd)
        try:
            subprocess.run(
                ["ffmpeg", "-i", file_path, "-ar", "16000", "-ac", "1",
                 "-f", "wav", "-y", wav_path],
                capture_output=True, timeout=ffmpeg_timeout, check=True,
            )

            async with httpx.AsyncClient(timeout=request_timeout) as client:
                with open(wav_path, "rb") as f:
                    resp = await client.post(
                        endpoint,
                        files={"file": ("audio.wav", f, "audio/wav")},
                        data={"response_format": "json", "language": language},
                    )
                resp.raise_for_status()
                text = resp.json().get("text", "").strip()
                if not text:
                    raise RuntimeError("Whisper returned empty transcription")
                return text
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    async def _fire_webhook(
        self,
        reply: str,
        session_id: str,
        sender: str,
        source: str,
        silent: bool,
        tokens: dict,
        notify_meta: dict | None,
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
        }
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

    def _build_sessions(self) -> list[dict]:
        """Build session list for HTTP /sessions."""
        if not self.session_mgr:
            return []
        result = []
        for contact, entry in self.session_mgr._index.items():
            session_id = entry.get("session_id", "")
            info = {
                "session_id": session_id,
                "contact": contact,
                "created_at": entry.get("created_at"),
            }
            # Enrich from live session if loaded
            live = self.session_mgr._sessions.get(contact)
            if live:
                info["message_count"] = len(live.messages)
                info["compaction_count"] = live.compaction_count
                info["model"] = live.model
            result.append(info)
        return result

    def _build_cost(self, period: str) -> dict:
        """Build cost breakdown for HTTP /cost."""
        import sqlite3

        from config import today_start_ts

        cost_path = str(self.config.cost_db)
        empty = {"period": period, "total_cost": 0.0, "models": []}

        if not Path(cost_path).exists():
            return empty

        try:
            conn = sqlite3.connect(cost_path)
            try:
                conn.row_factory = sqlite3.Row

                if period == "today":
                    ts_filter = today_start_ts()
                elif period == "week":
                    ts_filter = today_start_ts() - 6 * 86400
                else:  # "all"
                    ts_filter = 0

                rows = conn.execute(
                    """SELECT model,
                              SUM(input_tokens) AS input_tokens,
                              SUM(output_tokens) AS output_tokens,
                              SUM(cost_usd) AS cost_usd
                       FROM costs
                       WHERE timestamp >= ?
                       GROUP BY model""",
                    (ts_filter,),
                ).fetchall()
            finally:
                conn.close()

            models = []
            total = 0.0
            for r in rows:
                cost = r["cost_usd"] or 0.0
                total += cost
                models.append({
                    "model": r["model"],
                    "input_tokens": r["input_tokens"] or 0,
                    "output_tokens": r["output_tokens"] or 0,
                    "cost_usd": round(cost, 6),
                })

            return {
                "period": period,
                "total_cost": round(total, 4),
                "models": models,
            }
        except Exception:
            log.exception("Failed to query cost DB")
            return empty

    def _build_status(self) -> dict:
        """Build status dict for HTTP /status and SIGUSR2."""
        import sqlite3
        today_cost = 0.0
        try:
            cost_path = str(self.config.cost_db)
            if Path(cost_path).exists():
                conn = sqlite3.connect(cost_path)
                try:
                    from config import today_start_ts
                    today_start = today_start_ts()
                    row = conn.execute(
                        "SELECT SUM(cost_usd) FROM costs WHERE timestamp >= ?",
                        (today_start,),
                    ).fetchone()
                    today_cost = row[0] or 0.0 if row else 0.0
                finally:
                    conn.close()
        except Exception:  # noqa: S110 — cost DB query for status; graceful degradation
            pass

        active_sessions = 0
        if self.session_mgr:
            active_sessions = len(self.session_mgr._index)

        return {
            "status": "ok",
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.start_time),
            "channel": self.config.channel_type,
            "models": list(self.providers.keys()),
            "active_sessions": active_sessions,
            "today_cost": round(today_cost, 4),
            "queue_depth": self.queue.qsize(),
        }

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
            tier = msgs[0].get("tier", "full")
            n_meta = msgs[0].get("notify_meta")
            await self._process_message(
                combined_text, sender, source, tier,
                attachments=combined_attachments or None,
                notify_meta=n_meta,
            )

        async def process_http_immediate(item: dict) -> None:
            """Process HTTP /chat messages immediately (no debounce).

            Each /chat request has its own Future — combining messages
            would lose Futures and break response delivery.
            """
            await self._process_message(
                text=item.get("text", ""),
                sender=item.get("sender", "http"),
                source=item.get("type", "http"),
                tier=item.get("tier", "full"),
                attachments=item.get("attachments"),
                response_future=item.get("response_future"),
                notify_meta=item.get("notify_meta"),
            )

        while self.running:
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

            if isinstance(item, InboundMessage):
                sender = item.sender
                source = item.source
                text = item.text
                tier = "full"
                attachments = item.attachments
                notify_meta = None
                # Store last inbound timestamp for reaction tool (ms int)
                self._last_inbound_ts[sender] = int(item.timestamp * 1000)
                self._last_inbound_ts.move_to_end(sender)
                while len(self._last_inbound_ts) > 1000:
                    self._last_inbound_ts.popitem(last=False)
            elif isinstance(item, dict):
                # Handle session reset before normal message processing
                if item.get("type") == "reset":
                    if item.get("all") and self.session_mgr:
                        # Reset all sessions
                        contacts = list(self.session_mgr._index.keys())
                        for contact in contacts:
                            await self.session_mgr.close_session(contact)
                        log.info("All sessions reset (%d)", len(contacts))
                    elif item.get("session_id") and self.session_mgr:
                        # Reset by session UUID (from --reset <uuid>)
                        session_id = item["session_id"]
                        if await self.session_mgr.close_session_by_id(session_id):
                            log.info("Session reset by ID: %s", session_id)
                        else:
                            log.warning("No session found for ID: %s", session_id)
                    else:
                        # Reset by sender name (existing behavior)
                        target = item.get("sender", "")
                        if target == "user" and self.session_mgr:
                            for contact in self.session_mgr._index:
                                if contact not in ("system", "cli"):
                                    target = contact
                                    break
                        if target and self.session_mgr:
                            if await self.session_mgr.close_session(target):
                                log.info("Session reset for %s", target)
                            else:
                                log.warning("No session found to reset for %s", target)
                    continue

                # HTTP /chat — process immediately, bypass debouncing
                if item.get("response_future") is not None:
                    await process_http_immediate(item)
                    continue

                # FIFO / notify message
                sender = item.get("sender", "system")
                source = item.get("type", "system")
                text = item.get("text", "")
                tier = item.get("tier", "full" if source == "user" else "operational")
                attachments = item.get("attachments")
                notify_meta = item.get("notify_meta")
            else:
                continue

            if not text and not attachments:
                continue

            # Debounce: collect messages from same sender
            if sender not in pending:
                pending[sender] = []
            pending[sender].append({"text": text, "source": source, "tier": tier,
                                    "attachments": attachments, "notify_meta": notify_meta})

            # Wait for more messages
            await asyncio.sleep(debounce_s)

            # Drain all pending senders
            for s in list(pending.keys()):
                await drain_pending(s)

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
                "models": list(self.providers.keys()),
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
            self._init_providers()
            self._init_channel()
            self._init_sessions()
            self._init_skills()
            self._init_context()
            self._init_cost_db()
            self._init_tools()
            self._init_plugins()

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
                _fifo_reader(fifo_path, self.queue)
            )

            # Start channel reader
            channel_task = asyncio.create_task(self._channel_reader())

            # Start HTTP API server (if enabled)
            self._http_api = None
            if cfg.http_enabled:
                from channels.http_api import HTTPApi
                self._http_api = HTTPApi(
                    queue=self.queue,
                    host=cfg.http_host,
                    port=cfg.http_port,
                    auth_token=cfg.http_auth_token,
                    agent_timeout=cfg.agent_timeout,
                    get_status=self._build_status,
                    get_sessions=self._build_sessions,
                    get_cost=self._build_cost,
                    download_dir=cfg.http_download_dir,
                    max_body_bytes=cfg.http_max_body_bytes,
                    rate_limit=cfg.http_rate_limit,
                    rate_window=cfg.http_rate_window,
                    status_rate_limit=cfg.http_status_rate_limit,
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
            try:
                await channel_task
            except asyncio.CancelledError:
                pass

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
                    try:
                        session._save_state()
                    except Exception:
                        pass

            # Disconnect channel (close httpx client, clean downloads)
            if self.channel is not None:
                try:
                    await self.channel.disconnect()
                except Exception:  # noqa: S110 — channel cleanup on shutdown; failure is benign
                    pass
            # Close memory DB connection
            if self._memory_conn is not None:
                try:
                    self._memory_conn.close()
                except Exception:  # noqa: S110 — DB close on shutdown; failure is benign
                    pass
            _remove_pid_file(pid_path)
            # Clean up FIFO
            fifo_path = cfg.state_dir / "control.pipe"
            try:
                fifo_path.unlink(missing_ok=True)
            except Exception:  # noqa: S110 — FIFO cleanup on shutdown; failure is benign
                pass
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
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
