"""Sub-agent tool — sessions_spawn.

Spawns a sub-agent with scoped prompt, scoped tools, and a
provider instance for the specified model. Provider-agnostic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import ToolSpec

from messages import Message

log = logging.getLogger(__name__)

# Set at daemon startup
_config: Any = None
_provider: Any = None
_get_provider: Any = None  # callback(role) → provider; uses routed subagent model
_tool_registry: Any = None
_metering: Any = None  # MeteringDB instance
_converter: Any = None  # CurrencyConverter instance
# Active deny set — set by config at configure() time
_subagent_deny: set[str] = set()

# Sub-agent defaults — resolved from config at configure() time
_default_max_turns: int = 50
_default_timeout: float = 600.0

# Memory conventions injected into sub-agent preamble when memory tools are available
_MEMORY_CONVENTIONS: list[str] = [
    "- Entity names: lowercase with underscores (e.g., 'nicolas', 'lucy')",
    "- memory_get paths are workspace-relative (e.g., 'memory/2026-02-23.md')",
    "- Indexed files: memory/*.md, MEMORY.md",
]


def configure(config: Any = None, provider: Any = None, tool_registry: Any = None,
              session_manager: Any = None, get_provider: Any = None,
              metering: Any = None, converter: Any = None, **_: Any) -> None:
    global _config, _provider, _get_provider, _tool_registry, _subagent_deny
    global _default_max_turns, _default_timeout, _metering, _converter
    if config is not None:
        _config = config
    if provider is not None:
        _provider = provider
    if get_provider is not None:
        _get_provider = get_provider
    if tool_registry is not None:
        _tool_registry = tool_registry
    if metering is not None:
        _metering = metering
    if converter is not None:
        _converter = converter
    # Apply deny-list from config
    if config is not None:
        deny = config.subagent_deny
        _subagent_deny = set(deny) if deny is not None else set()
        _default_max_turns = config.subagent_max_turns
        _default_timeout = config.subagent_timeout


def _build_subagent_preamble(
    scoped_tools: list[dict[str, Any]],
    denied_names: list[str],
    max_turns: int,
) -> str:
    """Build explicit preamble so sub-agents know their environment."""
    now = time.strftime("%a, %d. %b %Y - %H:%M %Z")
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
        parts.extend(_MEMORY_CONVENTIONS)

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
    tools: list[str] | None = None,
    max_turns: int = 0,
    timeout: float = 0.0,
    parent_session_id: str = "",
) -> str:
    """Spawn a sub-agent for delegated work.

    Args:
        prompt: Task description / instructions for the sub-agent.
        tools: Tool names to make available (default: all except denied).
        max_turns: Max agentic loop iterations (0 = use config default).
        timeout: Timeout per API call in seconds (0 = use config default).
        parent_session_id: Parent session ID for audit trail.
    """
    if _config is None:
        return "Error: Agent system not initialized"

    # Resolve defaults from config
    max_turns = max_turns if max_turns > 0 else _default_max_turns
    timeout = timeout if timeout > 0 else _default_timeout

    # Use routed subagent provider if configured, else primary
    provider = _get_provider("subagent") if _get_provider else _provider
    if provider is None:
        return "Error: No provider configured"

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

    messages: list[Message] = [{"role": "user", "content": prompt}]

    start_time = time.time()

    # Extract cost tracking info — model name comes from the provider,
    # cost rates from primary config (routed provider inherits billing).
    from providers import CostContext
    model_name = getattr(provider, "model", "")
    model_cfg = _config.model_config("primary")
    cost_ctx = CostContext(
        metering=_metering,
        session_id=f"sub-{parent_session_id}" if parent_session_id else "",
        model_name=model_name or model_cfg.get("model", ""),
        cost_rates=model_cfg.get("cost_per_mtok", []),
        currency=model_cfg.get("currency", "EUR"),
        converter=_converter,
    )

    try:
        from agentic import LoopConfig, run_agentic_loop
        loop_cfg = LoopConfig(
            max_turns=max_turns,
            timeout=timeout,
            api_retries=_config.api_retries,
            api_retry_base_delay=_config.api_retry_base_delay,
        )
        response = await run_agentic_loop(
            provider=provider,
            system=fmt_system,
            messages=messages,
            tools=scoped_tools,
            tool_executor=_tool_registry,
            config=loop_cfg,
            cost=cost_ctx,
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


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="sessions_spawn",
        description=(
            "Spawn a sub-agent for delegated work. Same model and tools as you, but ephemeral — "
            "context is discarded after the task. Use for heavy tool work (document editing, "
            "bulk file operations) to keep your main session clean."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task description / instructions for the sub-agent"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names to make available (default: all except sessions_spawn and subagent_deny list)",
                },
                "timeout": {"type": "number", "description": "Timeout per API call in seconds (default: same as parent agent)"},
            },
            "required": ["prompt"],
        },
        function=tool_sessions_spawn,
    ),
]
