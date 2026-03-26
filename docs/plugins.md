# Plugin & Channel Developer Guide

How to extend Lucyd with custom tools, preprocessors, and channels.

## Tool Plugins

A tool plugin is a Python file in `plugins.d/` that exports a `TOOLS` list.

### File Structure

```
plugins.d/
  tts.py          # Text-to-speech tool
  my_tool.py      # Your custom tool
```

### Exports

```python
TOOLS = [
    {
        "name": "my_tool",
        "description": "What this tool does (shown to the LLM)",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        "function": my_tool_fn,
        "max_output": 0,  # optional: truncation limit (0 = use global default)
    },
]
```

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
def configure(config, provider, session_mgr, **_):
    """Called once at startup. Pull what you need by parameter name."""
    global _my_setting
    _my_setting = config.raw("tools", "my_tool", default={}).get("setting", "default")
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
| `memory` | `sqlite3.Connection` | Memory DB connection (if memory is configured) |
| `conn` | `sqlite3.Connection` | Alias for `memory` |
| `get_provider` | `callable` | `get_provider(role)` — get provider by role |
| `session_getter` | `callable` | Returns current session (lambda) |
| `start_time` | `float` | Daemon start timestamp |
| `metering` | `MeteringDB` | Cost tracking DB |

### Gating

Tools are only registered if their name appears in `[tools] enabled` in `lucyd.toml`:

```toml
[tools]
enabled = ["read", "write", "exec", "my_tool"]
```

### Reference Implementation

See `plugins.d/tts.py` — ElevenLabs text-to-speech. Shows config access via `config.raw()`, async tool function, structured result with file attachment.

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

Preprocessor plugins are loaded if their filename (without `.py`) appears in `[tools] enabled`:

```toml
[tools]
enabled = ["stt"]  # loads plugins.d/stt.py, registers its PREPROCESSORS
```

### Reference Implementation

See `plugins.d/stt.py` — speech-to-text. Claims audio attachments, transcribes via the `stt.py` backend, appends transcription to message text.

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

All envelope fields are optional. Missing fields get safe defaults.

| Field | Type | Default | Description |
|---|---|---|---|
| `message` | string | *required* | Message text |
| `sender` | string | `"default"` | Sender identifier (prefixed with `http-` by the API) |
| `channel_id` | string | `"http"` | Channel identifier. Used in session keying and metrics. |
| `task_type` | string | `"conversational"` | `"conversational"` (session stays open), `"task"` (auto-close after response), `"system"` (auto-close, internal) |
| `reply_to` | string | — | Response routing: omit for normal reply, `"silent"` to suppress delivery, or a sender name to redirect the reply into that sender's session |
| `attachments` | list | `[]` | Base64-encoded file attachments |
| `context` | string | — | Freeform label prepended as `[context]` |

Example:

```python
resp = await client.post(f"{URL}/api/v1/chat", json={
    "message": user_text,
    "sender": username,
    "channel_id": "telegram",
    "task_type": "conversational",
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
allow_from = [123456789]
```

Config search order (using Telegram as example):
1. `LUCYD_TELEGRAM_CONFIG` env var
2. `telegram.toml` in working directory
3. `/config/telegram.toml` (container convention)
4. Fall back to individual env vars

Secrets are never in the TOML file — only env var *names* that point to secrets.

### Reference Implementations

| Channel | File | Config | Complexity |
|---|---|---|---|
| **Telegram** | `channels/telegram.py` | `telegram.toml` | Full-featured: long polling, reconnect backoff, media groups, photo albums, contacts |
| **Email** | `channels/email.py` | `email.toml` | IMAP polling + SMTP reply |
| **CLI** | `channels/cli.py` | env var only | Minimal: stdin/stdout + SSE streaming. No config file needed. |

Start with CLI for the simplest example. Use Telegram as the reference for production patterns (error handling, reconnection, config structure).

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
