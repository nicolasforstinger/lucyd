# Lucyd Architecture Diagrams

Visual reference for the Lucyd agent framework. Each diagram references real function names and file paths. Renders natively on GitHub and Gitea.

---

## 1. Message Lifecycle

How an inbound message flows from channel to response delivery.

```mermaid
flowchart TD
    subgraph Sources["Inbound Sources"]
        TG["Telegram Channel<br/>telegram.py"]
        HTTP["HTTP API<br/>http_api.py"]
        FIFO["Control FIFO<br/>lucyd.py:73"]
    end

    Q["asyncio.Queue<br/>lucyd.py:286"]

    subgraph Loop["Message Loop — lucyd.py:1361"]
        DEB["Debounce<br/>500ms window"]
        ROUTE["Route Model<br/>config.route_model(source)"]
        SESSION["Get/Create Session<br/>session.py:285"]
        MEDIA["Process Attachments<br/>image / voice / document"]
        CTX["Build System Prompt<br/>context.py:31"]
        AGENTIC["Agentic Loop<br/>agentic.py:99"]
    end

    subgraph Post["Post-Processing"]
        PERSIST["Persist Messages<br/>session.py:167"]
        SILENT{"Silent Token?"}
        DELIVER["Channel Delivery<br/>channel.send()"]
        WEBHOOK["Webhook Callback<br/>lucyd.py:1207"]
        COMPACT{"Compaction<br/>Needed?"}
        DO_COMPACT["Compact Session<br/>session.py:450"]
    end

    TG --> Q
    HTTP --> Q
    FIFO --> Q
    Q --> DEB --> ROUTE --> SESSION --> MEDIA --> CTX --> AGENTIC
    AGENTIC --> PERSIST --> SILENT
    SILENT -->|"HEARTBEAT_OK / NO_REPLY"| WEBHOOK
    SILENT -->|normal| DELIVER --> WEBHOOK
    WEBHOOK --> COMPACT
    COMPACT -->|"> threshold"| DO_COMPACT
    COMPACT -->|under| END(( ))
    DO_COMPACT --> END
```

---

## 2. Agentic Loop

The core thinking-acting cycle that processes each message.

```mermaid
flowchart TD
    START["run_agentic_loop()<br/>agentic.py:99"]
    FORMAT["Format messages + tools<br/>provider.format_*()"]
    API["provider.complete()<br/>agentic.py:154"]
    COST["Record cost<br/>agentic.py:53"]

    COST_CHECK{"Cost limit<br/>exceeded?"}
    APPEND["Append response<br/>to session.messages"]
    CALLBACK_R["on_response<br/>callback"]

    STOP_CHECK{"stop_reason?"}
    TOOL_EXEC["Execute tools<br/>asyncio.gather()<br/>agentic.py:227"]
    TOOL_RESULTS["Append tool_results<br/>to messages"]
    TURN_CHECK{"Turns<br/>remaining?"}
    WARN["Inject warning:<br/>final tool-use turn"]

    RETURN["Return LLMResponse<br/>text + usage + cost"]

    START --> FORMAT --> API --> COST --> COST_CHECK
    COST_CHECK -->|yes| RETURN
    COST_CHECK -->|no| APPEND --> CALLBACK_R --> STOP_CHECK
    STOP_CHECK -->|"end_turn / no tools"| RETURN
    STOP_CHECK -->|tool_use| TOOL_EXEC --> TOOL_RESULTS --> TURN_CHECK
    TURN_CHECK -->|">= 2 left"| FORMAT
    TURN_CHECK -->|"< 2 left"| WARN --> FORMAT
```

---

## 3. Context Building

How the system prompt is assembled from workspace files with cache tiers.

```mermaid
flowchart LR
    subgraph Stable["Stable Tier — cached @ $0.30/Mtok"]
        SOUL["SOUL.md"]
        AGENTS["AGENTS.md"]
        TOOLS_MD["TOOLS.md"]
        IDENTITY["IDENTITY.md"]
        USER_MD["USER.md"]
        TOOL_DESC["Tool Descriptions<br/>name + one-liner"]
    end

    subgraph Semi["Semi-Stable Tier — cached @ $0.30/Mtok"]
        MEMORY_MD["MEMORY.md"]
        SKILLS["Always-on Skill Bodies"]
        SKILL_IDX["Skill Index"]
    end

    subgraph Dynamic["Dynamic Tier — uncached @ $3.00/Mtok"]
        TIME["Date / Time / Sender"]
        SOURCE["Source + Tier Framing"]
        RECALL["Memory Recall Block"]
        LIMITS["Limits + Warnings"]
        VOICE["Voice / Image Hints"]
    end

    BUILD["ContextBuilder.build()<br/>context.py:31"]
    BLOCKS["list of dict<br/>tier-tagged blocks"]
    FORMAT["provider.format_system()<br/>anthropic_compat.py:79"]
    CACHED["cache_control: ephemeral<br/>on stable + semi_stable"]

    Stable --> BUILD
    Semi --> BUILD
    Dynamic --> BUILD
    BUILD --> BLOCKS --> FORMAT --> CACHED
```

---

## 4. Memory Recall

How relevant context is retrieved from the memory system at session start.

```mermaid
flowchart TD
    QUERY["Session Start<br/>Query: sender + recent text"]

    subgraph Structured["Structured Memory (v2)"]
        ENTITIES["Extract Entities<br/>memory.py:331"]
        FACTS["Lookup Facts<br/>memory.py:379"]
        KEYWORDS["Extract Keywords"]
        EPISODES["Search Episodes<br/>memory.py:414"]
        COMMITMENTS["Open Commitments<br/>memory.py:446"]
    end

    subgraph Unstructured["Unstructured Memory (v1)"]
        FTS["FTS5 Search<br/>memory.py:59"]
        FTS_CHECK{">=3 results?"}
        EMBED["Embed Query<br/>memory.py:168"]
        VECTOR["Vector Search<br/>memory.py:128"]
        MERGE["Merge + Dedup"]
    end

    PRIORITY["Priority Sort<br/>commitments > vector ><br/>episodes > facts"]
    BUDGET["Token Budget<br/>inject_recall()<br/>memory.py:548"]

    SYNTH_CHECK{"synthesis_style?"}
    RAW["Raw blocks<br/>→ system prompt"]
    LLM_SYNTH["Synthesize<br/>synthesis.py:88"]
    PROSE["Prose<br/>→ system prompt"]

    QUERY --> ENTITIES --> FACTS
    QUERY --> KEYWORDS --> EPISODES
    QUERY --> FTS --> FTS_CHECK
    FTS_CHECK -->|yes| MERGE
    FTS_CHECK -->|no| EMBED --> VECTOR --> MERGE

    FACTS --> PRIORITY
    EPISODES --> PRIORITY
    COMMITMENTS --> PRIORITY
    MERGE --> PRIORITY
    PRIORITY --> BUDGET --> SYNTH_CHECK
    SYNTH_CHECK -->|structured| RAW
    SYNTH_CHECK -->|"narrative / factual"| LLM_SYNTH --> PROSE
```

---

## 5. Provider Abstraction

How internal neutral format translates to provider-specific API calls.

```mermaid
flowchart LR
    subgraph Internal["Internal Neutral Format"]
        MSG["Messages<br/>role + content"]
        SYS["System Blocks<br/>tier-tagged dicts"]
        TOOLS_INT["Tool Schemas<br/>name + desc + input_schema"]
        IMG["Image Blocks<br/>type: image + base64"]
    end

    subgraph Protocol["LLMProvider Protocol<br/>providers/__init__.py"]
        FM["format_messages()"]
        FS["format_system()"]
        FT["format_tools()"]
        COMPLETE["complete()"]
    end

    subgraph Anthropic["AnthropicCompatProvider"]
        A_SYS["System: list of dicts<br/>+ cache_control"]
        A_MSG["Content blocks<br/>thinking preservation"]
        A_IMG["source.type: base64<br/>media_type"]
        A_API["messages.create()"]
    end

    subgraph OpenAI["OpenAICompatProvider"]
        O_SYS["System: single string"]
        O_MSG["Standard messages<br/>function_call format"]
        O_IMG["data: URI images"]
        O_API["chat.completions.create()"]
    end

    RESP["LLMResponse<br/>text + tool_calls + usage"]

    MSG --> FM
    SYS --> FS
    TOOLS_INT --> FT
    IMG --> FM

    FM --> A_MSG & O_MSG
    FS --> A_SYS & O_SYS
    FT --> A_IMG & O_IMG

    A_MSG & A_SYS & A_IMG --> A_API --> RESP
    O_MSG & O_SYS & O_IMG --> O_API --> RESP
```

---

## 6. Session Persistence

Dual storage model with compaction lifecycle.

```mermaid
flowchart TD
    CREATE["get_or_create()<br/>session.py:285"]

    subgraph Active["Active Session"]
        USER_MSG["add_user_message()"]
        AGENTIC["run_agentic_loop()"]

        subgraph Persist["Dual Storage"]
            JSONL["Append to JSONL<br/>id.YYYY-MM-DD.jsonl"]
            STATE["Atomic write<br/>id.state.json"]
        end

        THRESHOLD{"Token usage<br/>vs threshold?"}
        NORMAL((" "))
        WARN["Inject warning<br/>pending_system_warning"]
        COMPACT["compact_session()<br/>session.py:450<br/>replace 2/3 messages<br/>with summary"]
    end

    CLOSE["close_session()<br/>session.py:323"]
    ARCHIVE["Move to .archive/"]
    DONE((" "))

    CREATE --> USER_MSG --> AGENTIC --> Persist --> THRESHOLD
    THRESHOLD -->|"under 80%"| NORMAL
    THRESHOLD -->|"80-100%"| WARN --> NORMAL
    THRESHOLD -->|"over 100%"| COMPACT --> NORMAL
    Active --> CLOSE --> ARCHIVE --> DONE
```

---

## 7. Tool System

Registration at startup, dispatch at runtime.

```mermaid
flowchart TD
    subgraph Startup["Registration — lucyd.py:367"]
        CONFIG["lucyd.toml<br/>[tools] enabled list"]
        BUILTIN["12 Built-in Modules<br/>19 tools"]
        PLUGINS["plugins.d/*.py<br/>Custom tools"]
        CONFIG --> FILTER{"tool.name<br/>in enabled?"}
        BUILTIN --> FILTER
        PLUGINS --> FILTER
        FILTER -->|yes| REG["ToolRegistry.register()<br/>tools/__init__.py:19"]
        FILTER -->|no| SKIP["Not loaded"]
    end

    subgraph Configure["Per-module configure()"]
        direction LR
        FS_CFG["filesystem:<br/>allowed_paths"]
        WEB_CFG["web: api_key<br/>SSRF protection"]
        MEM_CFG["memory: db_path<br/>embeddings"]
        MSG_CFG["messaging:<br/>channel, contacts"]
    end

    subgraph Runtime["Dispatch — agentic.py:221"]
        CALL["Tool call from LLM<br/>name + arguments"]
        LOOKUP{"name in<br/>registry?"}
        EXEC["execute()<br/>tools/__init__.py:54"]
        TRUNC["Truncate output<br/>> output_truncation chars"]
        RESULT["String result<br/>→ tool_results message"]
        ERR["Error: tool not available"]
    end

    REG --> Configure

    CALL --> LOOKUP
    LOOKUP -->|yes| EXEC --> TRUNC --> RESULT
    LOOKUP -->|no| ERR
```

---

## 8. Channels and HTTP API

Parallel transports feeding one processing queue.

```mermaid
flowchart TD
    subgraph Telegram["Telegram — telegram.py"]
        POLL["Long Poll<br/>getUpdates"]
        PARSE["Parse Message<br/>text + sender + attachments"]
        DL["Download Media<br/>/tmp/lucyd-telegram/"]
        SEND_TG["send() / send_voice()<br/>Bot API"]
    end

    subgraph HTTP["HTTP API — http_api.py"]
        CHAT["/api/v1/chat<br/>POST → sync response"]
        NOTIFY["/api/v1/notify<br/>POST → 202 accepted"]
        STATUS["/api/v1/status<br/>GET → health"]
        SESSIONS_EP["/api/v1/sessions<br/>GET → list"]
        COST_EP["/api/v1/cost<br/>GET → breakdown"]
        AUTH["Bearer Token Auth<br/>hmac.compare_digest()"]
        RATE["Rate Limiter<br/>per-sender window"]
    end

    subgraph CLI["FIFO — lucyd.py:73"]
        PIPE["control.pipe<br/>JSON lines"]
        SEND_CLI["lucyd-send<br/>bin/lucyd-send"]
    end

    Q["asyncio.Queue<br/>lucyd.py:286<br/>maxsize=1000"]

    POLL --> PARSE --> DL --> Q
    SEND_CLI --> PIPE --> Q
    CHAT --> AUTH --> RATE --> Q
    NOTIFY --> AUTH --> RATE --> Q

    Q --> LOOP["_message_loop()<br/>lucyd.py:1361"]
    LOOP --> PROCESS["_process_message()<br/>lucyd.py:616"]

    PROCESS -->|telegram| SEND_TG
    PROCESS -->|http| FUTURE["Resolve Future<br/>→ HTTP response"]
    PROCESS -->|system| SUPPRESS["No channel delivery<br/>(silent processing)"]
```
