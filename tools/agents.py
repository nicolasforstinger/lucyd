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
_DEFAULT_SUBAGENT_DENY = frozenset({"sessions_spawn", "tts", "load_skill", "react", "schedule_message"})

# Active deny set — overridden by config if tools.subagent_deny is set
_subagent_deny: set[str] = set(_DEFAULT_SUBAGENT_DENY)


def configure(config: Any, providers: dict, tool_registry: Any,
              session_manager: Any) -> None:
    global _config, _providers, _tool_registry, _session_manager, _subagent_deny
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


async def tool_sessions_spawn(
    prompt: str,
    model: str = "subagent",
    tools: list[str] | None = None,
    max_turns: int = 10,
    timeout: float = 120.0,
    parent_session_id: str = "",
) -> str:
    """Spawn a sub-agent for delegated work.

    Args:
        prompt: Task description / instructions for the sub-agent.
        model: Model name from [models.*] config (default: "subagent").
        tools: Tool names to make available (default: all except denied).
        max_turns: Max agentic loop iterations.
        timeout: Timeout per API call in seconds.
        parent_session_id: Parent session ID for audit trail.
    """
    if _config is None:
        return "Error: Agent system not initialized"

    provider = _providers.get(model)
    if provider is None:
        return f"Error: No provider configured for model '{model}'"

    # Scope tools — always apply deny-list
    available = _tool_registry.get_schemas()
    if tools is not None:
        scoped_tools = [t for t in available if t["name"] in tools and t["name"] not in _subagent_deny]
    else:
        scoped_tools = [t for t in available if t["name"] not in _subagent_deny]

    # Build minimal system prompt
    system_blocks = [{"text": prompt, "tier": "stable"}]
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
        "description": "Spawn a sub-agent for delegated or parallel work. Uses a cheaper/specialized model for the task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task description / instructions for the sub-agent"},
                "model": {"type": "string", "description": "Model name from config (default: 'subagent')", "default": "subagent"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names to make available (default: all except sessions_spawn, tts, load_skill, react, schedule_message)",
                },
                "timeout": {"type": "number", "description": "Timeout per API call in seconds (default: 120)", "default": 120},
            },
            "required": ["prompt"],
        },
        "function": tool_sessions_spawn,
    },
]
