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
        max_output=0,                # optional: truncation limit (0 = use global default)
        talkers=None,                # optional: gate to specific talker contexts (see below)
    ),
]
```

`ToolSpec` is a frozen dataclass — mypy catches misspelled fields, wrong types, and missing required fields at type-check time.

### Talker gating (`talkers`)

`ToolSpec.talkers: frozenset[str] | None` filters which tools the LLM is
told about per turn. `None` (default) makes the tool visible in every
talker context. A frozenset gates it to specific talkers:

```python
ToolSpec(
    name="send_message",
    ...,
    talkers=frozenset({"agent"}),   # only registered for talker == "agent"
)
```

Use this when a tool only makes sense in a specific context. Example:
`send_message` is the proactive-outbound primitive, only valid in
`agent:self` turns where the reply path is silent. Letting the LLM see
it in user/operator turns would invite wrong-tool calls (the LLM might
call `send_message` instead of just replying).

### Voice / attachment trap in agent:self

In `agent:self` (and any `system:*`) turn the reply is silent — `MessagePipeline._deliver_reply` forces `silent=True` whenever `talker in ("system", "agent")`. Tools
that return user-facing attachments (e.g. `tts` returning an mp3 path)
will silently lose their delivery if the agent uses them in agent:self
expecting reply-path delivery. Document this in any plugin tool whose
output is user-facing — the right pattern in agent:self is **generate
the artifact with your tool, then call `send_message` with the artifact
path as an attachment**.

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
| `bridges_primary` | `str` | Active outbound bridge name from `[bridges] primary` (`""` if unset). Pass to `bridge_client.send_to_user`. |
| `http_auth_token` | `str` | `LUCYD_HTTP_TOKEN` value. Used as the bearer for bridge `/send` calls. |
| `user_session_key` | `str` | `f"user:{config.user_name}"` — target for `SessionManager.append_outbound_to_user`. |
| `allowed_paths` | `list[str]` | Resolved `[tools.filesystem] allowed_paths`. Reuse for path validation in tools that take file paths. |
| `http_client` | `httpx.AsyncClient` | Shared httpx client for proactive outbound. |
| `pipeline_lock_factory` | `(str) -> AsyncContextManager` | Returns the per-session asyncio.Lock for the given session key. Hold it before cross-session writes. |

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
from plugins import PreprocessorSpec

PREPROCESSORS = [
    PreprocessorSpec(
        name="stt",
        fn=preprocess_audio,
        critical=True,            # Critical preprocessors fall back to fallback_text on failure
        fallback_text="[voice message — transcription failed]",
    ),
]
```

`PreprocessorSpec` is a frozen dataclass — mypy catches type errors at registration. The pipeline's loader rejects entries that aren't `PreprocessorSpec` instances.

### Interface

```python
async def preprocess_audio(
    text: str, attachments: list[Any], config: Any,
) -> tuple[str, list[Any]]:
    """
    Receives: message text, list of Attachment objects, daemon config.
    Returns: (modified text, remaining attachments).

    Claim attachments you handle. Pass through ones you don't.
    Raise plugins.PluginError subclasses on failure — the framework
    handles retry, fallback, and metrics emission.
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

For STT specifically, use `plugins.transcribe_audio_attachments(text, attachments, transcribe)` — it implements the standard claim-and-append pattern with the right tagging (`[voice message]` vs `[audio transcription]`).

### Execution Model

- Preprocessors run **in registration order** (alphabetical by plugin filename within `plugins.d/`)
- Each receives the output of the previous one
- On `PluginError`: a `critical` preprocessor substitutes `fallback_text` and clears attachments; a non-critical preprocessor logs + emits a metric and continues
- On `(TimeoutError, RuntimeError, OSError)`: same handling as `PluginError`
- If no attachments remain after a preprocessor, the pipeline short-circuits
- If no preprocessors are registered, text/attachments pass through unchanged
- Preprocessor invocations are tracked via `lucyd_preprocessor_total{name,status}` and `lucyd_preprocessor_duration_seconds{name}` metrics

### Dependency Injection

Preprocessor plugins use the same `configure()` DI pattern as tool plugins. A single plugin file can export both `TOOLS` and `PREPROCESSORS`.

### Gating

All plugins in `plugins.d/` are loaded and their `configure()` is called unconditionally. Preprocessors register automatically — they are not gated by `[tools] enabled`. Only tool registrations from `TOOLS` are filtered by the enabled list.

### Reference Implementation

See `plugins.d/whisper.py` — Whisper speech-to-text. Claims audio attachments, transcribes via OpenAI SDK or local whisper.cpp, appends transcription to message text, records cost via metering.

## Channels

A channel bridge is a standalone process that speaks HTTP to the daemon. It does not import framework code (other than the shared `channels.bridge_outbound_server.build_outbound_app` helper).

### Architecture

```
[Bridge Process]                        [Daemon]
  telegram.py     ──POST /api/v1/inbound/telegram──>  api.py            (inbound)
                  <──reply in body──
                  <──POST /send──         bridge_client.send_to_user    (outbound, proactive)
```

Bridges are bidirectional:
1. **Inbound**: poll the external source (Telegram getUpdates, IMAP), POST to `/api/v1/inbound/telegram` or `/api/v1/inbound/email`, deliver the reply via the channel.
2. **Outbound**: run a `POST /send` listener on a conventional localhost port. The daemon calls it via `bridge_client.send_to_user` when a tool (`send_message`) or endpoint (`/api/v1/outbound/send`, e.g. from an `at`-job) needs to push a proactive message.

### Outbound `/send` contract

Every bridge implements the same shape via the shared helper
`channels.bridge_outbound_server.build_outbound_app(...)`:

```
POST http://127.0.0.1:<bridge_port>/send
Authorization: Bearer <LUCYD_HTTP_TOKEN>
Content-Type: application/json

{
  "text": "...",
  "attachments": [
    {"filename": "...", "content_type": "...", "data_b64": "..."}
  ]
}

200 OK { "delivered": true }
```

Conventional ports + per-bridge attachment caps live in
`bridge_client.BRIDGE_LIMITS`:

| Bridge | Port | Max attachment |
|---|---|---|
| telegram | 8101 | 50 MB |
| email | 8102 | 20 MB |

The bridge process owns its own credentials; the daemon never touches a
provider API directly. Adding a new channel:
1. Implement inbound polling + POST to the daemon (existing pattern).
2. Add a row to `BRIDGE_LIMITS`.
3. Wire `build_outbound_app(token, chat_id, send_text, send_attachment)` in your bridge's `main()` alongside the poll loop, listening on the conventional port.

### Inbound message envelope

The bridge POSTs to `/api/v1/inbound/{telegram,email}`. Talker is pinned to `user` server-side; sender is auto-injected from `[user] name`. The envelope is intentionally narrow:

| Field | Type | Required | Description |
|---|---|---|---|
| `message` | string | conditional | Message text. Required if `attachments` is empty. |
| `attachments` | list | no | Base64-encoded file attachments (`{filename, content_type, data}`). |

Example:

```python
resp = await client.post(
    f"{URL}/api/v1/inbound/telegram",
    json={"message": user_text},
    headers={"Authorization": f"Bearer {token}"},
)
data = resp.json()
reply = data.get("reply", "")
attachments = data.get("attachments", [])  # bridge re-sends these via its native channel
```

For operator surfaces (CLI tooling, agentctl, etc.) use `/api/v1/chat` with `sender="agentctl"` instead — that's the `operator` talker.

### Config pattern

Each bridge reads its config from the same `lucyd.toml` as the daemon, located via the `LUCYD_CONFIG` env var. The bridge owns a top-level section (`[telegram]` or `[email]`).

```toml
# In lucyd.toml
[telegram]
token_env = "LUCYD_TELEGRAM_TOKEN"
allowed_senders = [123456789]

[telegram.contacts]
Nicolas = 123456789               # name = chat_id (NOT chat_id = name)
```

Secrets are never in the TOML file — only env var *names* that point to secrets. Bridges connect back to the daemon at `http://127.0.0.1:8100` and authenticate with `LUCYD_HTTP_TOKEN`.

### Reference Implementations

| Bridge | File | Config section | Complexity |
|---|---|---|---|
| **Telegram** | `channels/telegram.py` | `[telegram]` | Full-featured: long polling, reconnect backoff, media groups, photo albums, contacts |
| **Email** | `channels/email.py` | `[email]` | IMAP polling + SMTP reply |

The daemon ships no CLI client — operators talk to the API directly with `curl` (or via agentctl). Use Telegram as the reference for production bridge patterns (error handling, reconnection, outbound listener wiring).

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
