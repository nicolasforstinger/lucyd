#!/usr/bin/env python3
"""Lucyd — a daemon for persona-rich AI agents.

Entry point. Wires config → channel → loop → tools → sessions.
Handles PID file, control FIFO, Unix signals, and the main event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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

from agentic import _init_cost_db, run_agentic_loop
from channels import InboundMessage, create_channel
from config import Config, ConfigError, load_config
from context import ContextBuilder
from providers import create_provider
from session import SessionManager
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


# ─── Daemon ──────────────────────────────────────────────────────

class LucydDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.start_time = time.time()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.providers: dict[str, Any] = {}
        self.channel: Any = None
        self.session_mgr: SessionManager | None = None
        self.context_builder: ContextBuilder | None = None
        self.skill_loader: SkillLoader | None = None
        self.tool_registry: ToolRegistry | None = None
        self._fifo_task: asyncio.Task | None = None
        self._http_api: Any = None
        self._last_inbound_ts: dict[str, int] = {}  # sender → ms timestamp

    def _setup_logging(self) -> None:
        """Configure logging to file + stderr."""
        log_file = self.config.log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # File handler (rotating: 10 MB max, 3 backups)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
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
        self.session_mgr = SessionManager(self.config.sessions_dir)

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
            fs_configure(self.config.filesystem_allowed_paths)
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
            from tools.messaging import set_channel, set_timestamp_getter
            set_channel(self.channel)
            set_timestamp_getter(lambda sender: self._last_inbound_ts.get(sender))
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
                embedding_api_key=self.config.api_key("openai"),
                embedding_model=self.config.model_config("embeddings").get("model", "text-embedding-3-small")
                    if "embeddings" in self.config.all_model_names else "text-embedding-3-small",
                embedding_base_url=self.config.model_config("embeddings").get("base_url", "https://api.openai.com/v1")
                    if "embeddings" in self.config.all_model_names else "https://api.openai.com/v1",
                top_k=self.config.memory_top_k,
            )
            set_memory(mem)
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
        if "tts" in enabled and self.config.api_key("elevenlabs"):
            from tools.tts import TOOLS as tts_tools
            from tools.tts import configure as tts_configure
            tts_cfg = self.config.raw("tools", "tts", default={})
            tts_configure(
                api_key=self.config.api_key("elevenlabs"),
                provider=self.config.tts_provider,
                channel=self.channel,
                default_voice_id=tts_cfg.get("default_voice_id", ""),
                default_model_id=tts_cfg.get("default_model_id", "eleven_v3"),
                speed=tts_cfg.get("speed", 1.0),
                stability=tts_cfg.get("stability", 0.5),
                similarity_boost=tts_cfg.get("similarity_boost", 0.75),
            )
            self.tool_registry.register_many(tts_tools)

        # Scheduling
        if enabled & {"schedule_message", "list_scheduled"}:
            from tools.scheduling import TOOLS as sched_tools
            from tools.scheduling import configure as sched_configure
            sched_configure(channel=self.channel)
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

        log.info("Registered tools: %s", ", ".join(self.tool_registry.tool_names))

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
    ) -> None:
        """Process a single message through the agentic loop."""

        def _resolve(result: dict) -> None:
            """Safely resolve the HTTP response future."""
            if response_future is not None and not response_future.done():
                response_future.set_result(result)

        # Route to model
        model_name = self.config.route_model(source)
        provider = self.providers.get(model_name)
        if provider is None:
            log.error("No provider for model '%s' (source: %s)", model_name, source)
            _resolve({"error": f"no provider for model '{model_name}'"})
            return

        model_cfg = self.config.model_config(model_name)

        # Process attachments into text descriptions + image blocks
        image_blocks = []
        supports_vision = model_cfg.get("supports_vision", True)
        max_image_bytes = self.config.raw("behavior", "max_image_bytes", default=5 * 1024 * 1024)
        if attachments:
            for att in attachments:
                if att.content_type.startswith("image/"):
                    if not supports_vision:
                        text = (text + "\n" if text else "") + "[image received — vision not available with current provider]"
                        continue
                    try:
                        img_path = Path(att.local_path)
                        img_size = img_path.stat().st_size
                        if img_size > max_image_bytes:
                            log.warning("Image too large (%d bytes), skipping vision: %s", img_size, att.local_path)
                            text = (text + "\n" if text else "") + f"[image too large to display — {img_size / (1024*1024):.1f}MB]"
                            continue
                        img_data = img_path.read_bytes()
                        image_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": att.content_type,
                                "data": base64.b64encode(img_data).decode("ascii"),
                            },
                        })
                        text = ("[image] " + text) if text else "[image]"
                    except Exception as e:
                        log.error("Failed to read image %s: %s", att.local_path, e)

                elif att.content_type.startswith("audio/"):
                    try:
                        transcription = await self._transcribe_audio(att.local_path, att.content_type)
                        text = (text + "\n" if text else "") + f"[voice message]: {transcription}"
                    except Exception as e:
                        log.error("Whisper transcription failed: %s", e)
                        text = (text + "\n" if text else "") + "[voice message — transcription failed]"

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

        # Inject timestamp so the agent always knows the current time
        timestamp = time.strftime("[%a, %d. %b %Y - %H:%M %Z]")
        text = f"{timestamp}\n{text}"

        session.add_user_message(text, sender=sender, source=source)

        # Transiently inject image content blocks for the API call
        user_msg_idx = len(session.messages) - 1
        if image_blocks:
            api_content = image_blocks + [{"type": "text", "text": text}]
            session.messages[user_msg_idx]["content"] = api_content

        # Build system prompt
        tool_descs = self.tool_registry.get_brief_descriptions()
        skill_index = self.skill_loader.build_index() if self.skill_loader else ""
        always_on = self.config.always_on_skills
        skill_bodies = self.skill_loader.get_bodies(always_on) if self.skill_loader else {}

        # Inject recall from previous session if this one is fresh
        recall = ""
        if len(session.messages) <= 1:
            recall = self.session_mgr.build_recall(sender)

        system_blocks = self.context_builder.build(
            tier=tier,
            source=source,
            tool_descriptions=tool_descs,
            skill_index=skill_index,
            always_on_skills=always_on,
            skill_bodies=skill_bodies,
            extra_dynamic=recall,
        )
        fmt_system = provider.format_system(system_blocks)

        # Typing indicator
        if self.config.typing_indicators and source not in self._NO_CHANNEL_DELIVERY:
            try:
                await self.channel.send_typing(sender)
            except Exception as e:
                log.debug("Typing indicator failed: %s", e)

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
            )
        except Exception as e:
            log.error("Agentic loop failed: %s", e)
            # Restore text-only content before returning
            if image_blocks:
                session.messages[user_msg_idx]["content"] = text
            _resolve({"error": str(e), "session_id": session.id})
            if source not in self._NO_CHANNEL_DELIVERY:
                try:
                    await self.channel.send(sender, self.config.error_message)
                except Exception as e:
                    log.error("Failed to deliver error message to %s: %s", sender, e)
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

        # Silent token check
        if _is_silent(reply, self.config.silent_tokens):
            log.info("Silent reply suppressed: %s", reply[:100])
            _resolve({
                "reply": reply,
                "silent": True,
                "session_id": session.id,
                "tokens": {
                    "input": response.usage.input_tokens,
                    "output": response.usage.output_tokens,
                },
            })
            return

        # Resolve HTTP future with the response
        _resolve({
            "reply": reply,
            "session_id": session.id,
            "tokens": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        })

        # Deliver reply via channel (skipped for system and HTTP sources)
        if _should_deliver(reply, source, self._NO_CHANNEL_DELIVERY):
            try:
                await self.channel.send(sender, reply)
            except Exception as e:
                log.error("Failed to deliver reply: %s", e)

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

        # Check for compaction
        if session.needs_compaction(self.config.compaction_threshold):
            compaction_model = self.config.compaction_model
            compaction_provider = self.providers.get(compaction_model)
            if compaction_provider:
                await self.session_mgr.compact_session(
                    session, compaction_provider, self.config.compaction_prompt,
                )

    async def _transcribe_audio(self, file_path: str, content_type: str) -> str:
        """Transcribe audio using OpenAI Whisper API."""
        import httpx
        api_key = self.config.api_key("openai")
        if not api_key:
            raise RuntimeError("No OpenAI API key for Whisper")

        whisper_cfg = self.config.raw("tools", "whisper", default={})
        api_url = whisper_cfg.get("api_url", "https://api.openai.com/v1/audio/transcriptions")
        whisper_model = whisper_cfg.get("model", "whisper-1")
        whisper_timeout = whisper_cfg.get("timeout", 60)

        audio_data = Path(file_path).read_bytes()
        filename = Path(file_path).name

        async with httpx.AsyncClient(timeout=whisper_timeout) as client:
            resp = await client.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (filename, audio_data, content_type)},
                data={"model": whisper_model},
            )
            resp.raise_for_status()
            return resp.json().get("text", "")

    def _build_status(self) -> dict:
        """Build status dict for HTTP /status and SIGUSR2."""
        import sqlite3
        today_cost = 0.0
        try:
            cost_path = str(self.config.cost_db)
            if Path(cost_path).exists():
                conn = sqlite3.connect(cost_path)
                from config import today_start_ts
                today_start = today_start_ts()
                row = conn.execute(
                    "SELECT SUM(cost_usd) FROM costs WHERE timestamp >= ?",
                    (today_start,),
                ).fetchone()
                conn.close()
                today_cost = row[0] or 0.0 if row else 0.0
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
            await self._process_message(
                combined_text, sender, source, tier,
                attachments=combined_attachments or None,
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
                response_future=item.get("response_future"),
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
                # Store last inbound timestamp for reaction tool (ms int)
                self._last_inbound_ts[sender] = int(item.timestamp * 1000)
            elif isinstance(item, dict):
                # Handle session reset before normal message processing
                if item.get("type") == "reset":
                    if item.get("all") and self.session_mgr:
                        # Reset all sessions
                        contacts = list(self.session_mgr._index.keys())
                        for contact in contacts:
                            self.session_mgr.close_session(contact)
                        log.info("All sessions reset (%d)", len(contacts))
                    elif item.get("session_id") and self.session_mgr:
                        # Reset by session UUID (from --reset <uuid>)
                        session_id = item["session_id"]
                        if self.session_mgr.close_session_by_id(session_id):
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
                            if self.session_mgr.close_session(target):
                                log.info("Session reset for %s", target)
                            else:
                                log.warning("No session found to reset for %s", target)
                    continue

                # HTTP /chat — process immediately, bypass debouncing
                if item.get("response_future") is not None:
                    await process_http_immediate(item)
                    continue

                # FIFO message
                sender = item.get("sender", "system")
                source = item.get("type", "system")
                text = item.get("text", "")
                tier = item.get("tier", "full" if source == "user" else "operational")
                attachments = None
            else:
                continue

            if not text and not attachments:
                continue

            # Debounce: collect messages from same sender
            if sender not in pending:
                pending[sender] = []
            pending[sender].append({"text": text, "source": source, "tier": tier, "attachments": attachments})

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
        default="./lucyd.toml",
        help="Path to config file (default: ./lucyd.toml)",
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
