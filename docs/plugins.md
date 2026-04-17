# Plugin & Channel Developer Guide

How to extend Lucyd with custom tools, preprocessors, and channels.

## Tool Plugins

A tool plugin is a Python file in `plugins.d/` that exports a `TOOLS` list.

### File Structure

```
plugins.d/
  elevenlabs.py          # ElevenLabs text-to-speech tool
  whisper.py             # Whisper speech-to-text preprocessor
  elevenlabs.toml        # Plugin config (from elevenlabs.toml.example)
  whisper.toml           # Plugin config (from whisper.toml.example)
  my_tool.py             # Your custom tool
```

### Exports

```python
from tools import ToolSpec

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="my_tool",
        description="What this tool does (shown to the LLM)",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        function=my_tool_fn,
        max_output=0,  # optional: truncation limit (0 = use global default)
    ),
]
```

`ToolSpec` is a frozen dataclass — mypy catches misspelled fields, wrong types, and missing required fields at type-check time.

The `function` must be an async callable:

```python
async def my_tool_fn(query: str) -> dict:
    """Tool functions receive keyword args matching input_schema properties."""
    result = do_something(query)
    return {"text": result, "attachments": []}
```

Return value: `{"text": "...", "attachments": ["/path/to/file"]}`. The `text` is shown to the LLM. Attachments are included in the reply to the user.

### Dependency Injection

If your plugin defines a `configure()` function, it's called at startup with inspect-based injection. Name your parameters to match available dependencies:

```python
def configure(config, metering=None, converter=None, **_):
    """Called once at startup. Pull what you need by parameter name."""
    import tomllib
    # Plugin-local config — lives next to the plugin, not in lucyd.toml
    toml_path = Path(__file__).parent / "my_tool.toml"
    if toml_path.exists():
        with toml_path.open("rb") as f:
            cfg = tomllib.load(f)
    # Or use config.raw() for simple settings in lucyd.toml
    # _my_setting = config.raw("tools", "my_tool", default={}).get("setting", "")
```

Available dependencies:

| Parameter | Type | Description |
|---|---|---|
| `config` | `Config` | Daemon configuration. Use `config.raw("section", "key")` for plugin-specific config. |
| `provider` | `LLMProvider` | Primary LLM provider instance |
| `session_mgr` | `SessionManager` | Session manager |
| `session_manager` | `SessionManager` | Alias for `session_mgr` |
| `tool_registry` | `ToolRegistry` | Tool registry (for introspection) |
| `skill_loader` | `SkillLoader` | Skill loader |
| `memory` | `MemoryInterface` | Memory interface (if memory is configured) |
| `pool` | `asyncpg.Pool` | PostgreSQL connection pool |
| `client_id` | `str` | Tenant client identifier |
| `agent_id` | `str` | Agent identifier |
| `get_provider` | `callable` | `get_provider(role)` — get provider by role |
| `session_getter` | `callable` | Returns current session (lambda) |
| `start_time` | `float` | Daemon start timestamp |
| `metering` | `MeteringDB` | Cost tracking DB |
| `converter` | `CurrencyConverter` | Currency conversion (EUR ↔ other) |

### Gating

Tools are only registered if their name appears in `[tools] enabled` in `lucyd.toml`:

```toml
[tools]
enabled = ["read", "write", "exec", "my_tool"]
```

### Reference Implementation

See `plugins.d/elevenlabs.py` — ElevenLabs text-to-speech. Shows plugin-local TOML config loading via `tomllib`, SDK integration, cost tracking via `metering.record(cost_override=...)`, and structured result with file attachment.

## Preprocessor Plugins

Preprocessors run before the agentic loop. They claim and transform attachments before the agent sees them.

### Exports

```python
PREPROCESSORS = [
    {
        "name": "stt",
        "fn": preprocess_audio,
    },
]
```

### Interface

```python
async def preprocess_audio(
    text: str, attachments: list, config: Any,
) -> tuple[str, list]:
    """
    Receives: message text, list of Attachment objects, daemon config.
    Returns: (modified text, remaining attachments).

    Claim attachments you handle. Pass through ones you don't.
    """
    remaining = []
    for att in attachments:
        if att.content_type.startswith("audio/"):
            transcription = await transcribe(att.local_path)
            text = f"{text}\n[transcription]: {transcription}"
        else:
            remaining.append(att)
    return text, remaining
```

### Execution Model

- Preprocessors run **in registration order** (alphabetical by plugin filename)
- Each receives the output of the previous one
- If a preprocessor raises, the exception is logged and the pipeline continues with the next preprocessor
- If no attachments remain after a preprocessor, the pipeline short-circuits
- If no preprocessors are registered, text/attachments pass through unchanged
- Preprocessor invocations are tracked via `lucyd_preprocessor_total` and `lucyd_preprocessor_duration_seconds` metrics

### Dependency Injection

Preprocessor plugins use the same `configure()` DI pattern as tool plugins. A single plugin file can export both `TOOLS` and `PREPROCESSORS`.

### Gating

All plugins in `plugins.d/` are loaded and their `configure()` is called unconditionally. Preprocessors register automatically — they are not gated by `[tools] enabled`. Only tool registrations from `TOOLS` are filtered by the enabled list.

### Reference Implementation

See `plugins.d/whisper.py` — Whisper speech-to-text. Claims audio attachments, transcribes via OpenAI SDK or local whisper.cpp, appends transcription to message text, records cost via metering.

## Channels

A channel is a standalone process that speaks HTTP to the lucyd API. It does not import framework code.

### Architecture

```
[Channel Process]              [Daemon]
  telegram.py    ──POST /chat──>  api.py
                 <──HTTP 200───
```

Channels:
1. Poll their external source (Telegram getUpdates, IMAP, stdin)
2. POST messages to the daemon's HTTP API
3. Receive the reply in the HTTP response
4. Deliver the reply via their external source (Telegram sendMessage, SMTP, stdout)

There is no outbound push from the daemon to channels.

### Message Envelope

Each endpoint pins the `talker`; the bridge only supplies content.
User-class inbound endpoints auto-inject `sender = config.user.name`.

| Field | Type | Required | Description |
|---|---|---|---|
| `message` | string | yes | Message text |
| `sender` | string | depends | Within-talker identity. Bridges (user talker) don't set it — the daemon injects `config.user.name`. Operator/system/agent callers must send one of the enum values (see `config.OPERATOR_SENDERS`, `SYSTEM_SENDERS`, `AGENT_SENDERS`). |
| `reply_to` | string | no | `"silent"` suppresses delivery (reply still processed and logged). |
| `attachments` | list | no | Base64-encoded file attachments |
| `context` | string | no | Freeform label prepended as `[context]` (operator `/chat` only) |

Example — a channel bridge POSTing a user message:

```python
resp = await client.post(f"{URL}/api/v1/inbound/telegram", json={
    "message": user_text,
})
data = resp.json()
reply = data.get("reply", "")
```

### Config Pattern

Each bridge owns a standalone TOML config file with a consistent structure:

```toml
# ─── Daemon Connection ─────────────────────────────────────
[daemon]
url = "http://127.0.0.1:8100"
token_env = "LUCYD_HTTP_TOKEN"

# ─── Protocol-Specific ─────────────────────────────────────
[telegram]
token_env = "LUCYD_TELEGRAM_TOKEN"
allowed_senders = [123456789]
```

Channel bridges read their config from sections in `lucyd.toml` (path from `LUCYD_CONFIG` env var). Secrets are never in the TOML file — only env var *names* that point to secrets.

### Reference Implementations

| Channel | File | Config | Complexity |
|---|---|---|---|
| **Telegram** | `channels/telegram.py` | `lucyd.toml [telegram]` | Full-featured: long polling, reconnect backoff, media groups, photo albums, contacts |
| **Email** | `channels/email.py` | `lucyd.toml [email]` | IMAP polling + SMTP reply |

Interactive CLI chat is not bundled — API-only operation is the standard.
Callers speak directly to `/api/v1/chat/stream` (or will consume it via
the planned `agentctl web` UI). Use Telegram as the reference for
production channel patterns (error handling, reconnection, config structure).

## Custom Metrics

If `prometheus_client` is installed, plugins can register custom metrics:

```python
from prometheus_client import Counter

MY_METRIC = Counter(
    "lucyd_myplugin_operations_total",
    "Description of what this counts",
    ["label1"],
)

async def my_tool_fn(query: str) -> dict:
    MY_METRIC.labels(label1="value").inc()
    return {"text": "done", "attachments": []}
```

Naming convention: `lucyd_<plugin_name>_<metric_name>`. All metrics are automatically exposed at `GET /metrics`.

Guard metric usage for environments without `prometheus_client`:

```python
try:
    from prometheus_client import Counter
    MY_METRIC = Counter(...)
    _HAS_METRICS = True
except ImportError:
    _HAS_METRICS = False
```
