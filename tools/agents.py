"""Sub-agent tool — sessions_spawn.

Spawns a sub-agent with scoped prompt, scoped tools, and a
provider instance for the specified model. Provider-agnostic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Set at daemon startup
_config: Any = None
_providers: dict = {}
_tool_registry: Any = None
_session_manager: Any = None

# Tools that sub-agents should not have access to by default
_DEFAULT_SUBAGENT_DENY = frozenset({"sessions_spawn", "tts", "react", "schedule_message"})

# Active deny set — overridden by config if tools.subagent_deny is set
_subagent_deny: set[str] = set(_DEFAULT_SUBAGENT_DENY)

# Sub-agent defaults — resolved from config at configure() time
_default_model: str = "primary"
_default_max_turns: int = 50
_default_timeout: float = 600.0


def configure(config: Any, providers: dict, tool_registry: Any,
              session_manager: Any) -> None:
    global _config, _providers, _tool_registry, _session_manager, _subagent_deny
    global _default_model, _default_max_turns, _default_timeout
    _config = config
    _providers = providers
    _tool_registry = tool_registry
    _session_manager = session_manager
    # Apply configurable deny-list
    custom_deny = getattr(config, "subagent_deny", None)
    if custom_deny is not None:
        _subagent_deny = set(custom_deny)
    else:
        _subagent_deny = set(_DEFAULT_SUBAGENT_DENY)
    # Resolve sub-agent defaults from config
    _default_model = getattr(config, "subagent_model", "primary")
    _default_max_turns = getattr(config, "subagent_max_turns", 50)
    _default_timeout = getattr(config, "subagent_timeout", 600.0)


def _build_subagent_preamble(
    scoped_tools: list[dict],
    denied_names: list[str],
    max_turns: int,
) -> str:
    """Build explicit preamble so sub-agents know their environment."""
    import time as _time
    now = _time.strftime("%a, %d. %b %Y - %H:%M %Z")
    parts = [
        "You are a sub-agent spawned to complete a specific task. "
        "Complete the task and return a clear, concise text summary of what you did.",
        "",
        f"Current date/time: {now}",
        "",
        "## Your Available Tools",
        "",
    ]
    for t in scoped_tools:
        parts.append(f"- **{t['name']}**: {t['description']}")

    if denied_names:
        parts.append("")
        parts.append("## Denied Tools (do NOT call these)")
        parts.append("")
        for name in sorted(denied_names):
            parts.append(f"- {name}")

    parts.append("")
    parts.append("## Limits")
    parts.append("")
    parts.append(f"- You have **{max_turns} tool-use turns**. Work efficiently.")
    parts.append("- When done, respond with a clear text answer summarizing what you did and the result.")

    # Contextual hints based on available tools
    tool_names = {t["name"] for t in scoped_tools}

    if tool_names & {"memory_search", "memory_get", "memory_write", "memory_forget"}:
        parts.append("")
        parts.append("## Memory Conventions")
        parts.append("")
        parts.append("- Entity names: lowercase with underscores (e.g., 'nicolas', 'lucy')")
        parts.append("- memory_get paths are workspace-relative (e.g., 'memory/2026-02-23.md')")
        parts.append("- Indexed files: memory/*.md, MEMORY.md")

    if "message" in tool_names and _config:
        contact_names = getattr(_config, "contact_names", [])
        if contact_names:
            parts.append("")
            parts.append(f"## Contacts: {', '.join(contact_names)}")

    if tool_names & {"read", "write", "edit"} and _config:
        paths = getattr(_config, "filesystem_allowed_paths", [])
        if paths:
            parts.append("")
            parts.append(f"## Allowed file paths: {', '.join(paths)}")

    parts.append("")
    parts.append("## Session")
    parts.append("")
    parts.append("Your session is ephemeral — context is discarded after this task.")

    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("## Task")
    parts.append("")

    return "\n".join(parts)


async def tool_sessions_spawn(
    prompt: str,
    model: str = "",
    tools: list[str] | None = None,
    max_turns: int = 0,
    timeout: float = 0.0,
    parent_session_id: str = "",
) -> str:
    """Spawn a sub-agent for delegated work.

    Args:
        prompt: Task description / instructions for the sub-agent.
        model: Model name from [models.*] config (default: primary).
        tools: Tool names to make available (default: all except denied).
        max_turns: Max agentic loop iterations (0 = use config default).
        timeout: Timeout per API call in seconds (0 = use config default).
        parent_session_id: Parent session ID for audit trail.
    """
    if _config is None:
        return "Error: Agent system not initialized"

    # Resolve defaults from config
    model = model or _default_model
    max_turns = max_turns if max_turns > 0 else _default_max_turns
    timeout = timeout if timeout > 0 else _default_timeout

    provider = _providers.get(model)
    if provider is None:
        return f"Error: No provider configured for model '{model}'"

    # Scope tools — always apply deny-list
    available = _tool_registry.get_schemas()
    if tools is not None:
        scoped_tools = [t for t in available if t["name"] in tools and t["name"] not in _subagent_deny]
    else:
        scoped_tools = [t for t in available if t["name"] not in _subagent_deny]

    # Build denied tool names list for the preamble
    all_names = {t["name"] for t in available}
    scoped_names = {t["name"] for t in scoped_tools}
    denied_names = sorted(all_names - scoped_names)

    # Build system prompt with explicit preamble
    preamble = _build_subagent_preamble(scoped_tools, denied_names, max_turns)
    system_text = preamble + prompt
    system_blocks = [{"text": system_text, "tier": "stable"}]
    fmt_system = provider.format_system(system_blocks)

    messages = [{"role": "user", "content": prompt}]

    start_time = time.time()

    # Extract cost tracking info for sub-agent
    model_cfg = _config.model_config(model)
    cost_db = str(_config.cost_db)
    model_name = model_cfg.get("model", "")
    cost_rates = model_cfg.get("cost_per_mtok", [])

    try:
        # Import agentic loop
        from agentic import run_agentic_loop
        response = await run_agentic_loop(
            provider=provider,
            system=fmt_system,
            messages=messages,
            tools=scoped_tools,
            tool_executor=_tool_registry,
            max_turns=max_turns,
            timeout=timeout,
            cost_db=cost_db,
            session_id=f"sub-{parent_session_id}" if parent_session_id else "",
            model_name=model_name,
            cost_rates=cost_rates,
        )
        elapsed = time.time() - start_time
        result = response.text or "(no output)"
        tokens = f"in:{response.usage.input_tokens} out:{response.usage.output_tokens}"
        log.info("Sub-agent completed in %.1fs (%s): %s...",
                 elapsed, tokens, result[:100])
        return result

    except TimeoutError:
        return f"Error: Sub-agent timed out after {timeout}s"
    except Exception as e:
        log.error("Sub-agent failed: %s", e)
        return f"Error: Sub-agent failed: {e}"


TOOLS = [
    {
        "name": "sessions_spawn",
        "description": (
            "Spawn a sub-agent for delegated work. Same model and tools as you, but ephemeral — "
            "context is discarded after the task. Use for heavy tool work (document editing, "
            "bulk file operations) to keep your main session clean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task description / instructions for the sub-agent"},
                "model": {"type": "string", "description": "Model name from config (default: primary)"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names to make available (default: all except sessions_spawn, tts, react, schedule_message)",
                },
                "timeout": {"type": "number", "description": "Timeout per API call in seconds (default: same as parent agent)"},
            },
            "required": ["prompt"],
        },
        "function": tool_sessions_spawn,
    },
]
