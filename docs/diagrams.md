# Lucyd Architecture Diagrams

Visual reference for the Lucyd agent framework. Each diagram references real function names and file paths. Renders natively on GitHub and Gitea.

---

## 1. Message Lifecycle

How an inbound message flows from channel to response delivery.

```mermaid
flowchart TD
    subgraph Sources["Inbound Sources"]
        TG["Telegram Channel\ntelegram.py"]
        HTTP["HTTP API\nhttp_api.py"]
        FIFO["Control FIFO\nlucyd.py:73"]
    end

    Q["asyncio.Queue\nlucyd.py:286"]

    subgraph Loop["Message Loop — lucyd.py:1361"]
        DEB["Debounce\n500ms window"]
        ROUTE["Route Model\nconfig.route_model(source)"]
        SESSION["Get/Create Session\nsession.py:285"]
        MEDIA["Process Attachments\nimage / voice / document"]
        CTX["Build System Prompt\ncontext.py:31"]
        AGENTIC["Agentic Loop\nagentic.py:99"]
    end

    subgraph Post["Post-Processing"]
        PERSIST["Persist Messages\nsession.py:167"]
        SILENT{"Silent Token?"}
        DELIVER["Channel Delivery\nchannel.send()"]
        WEBHOOK["Webhook Callback\nlucyd.py:1207"]
        COMPACT{"Compaction\nNeeded?"}
        DO_COMPACT["Compact Session\nsession.py:450"]
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
    START["run_agentic_loop()\nagentic.py:99"]
    FORMAT["Format messages + tools\nprovider.format_*()"]
    API["provider.complete()\nagentic.py:154"]
    COST["Record cost\nagentic.py:53"]

    COST_CHECK{"Cost limit\nexceeded?"}
    APPEND["Append response\nto session.messages"]
    CALLBACK_R["on_response\ncallback"]

    STOP_CHECK{"stop_reason?"}
    TOOL_EXEC["Execute tools\nasyncio.gather()\nagentic.py:227"]
    TOOL_RESULTS["Append tool_results\nto messages"]
    TURN_CHECK{"Turns\nremaining?"}
    WARN["Inject warning:\nfinal tool-use turn"]

    RETURN["Return LLMResponse\ntext + usage + cost"]

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
        TOOL_DESC["Tool Descriptions\nname + one-liner"]
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

    BUILD["ContextBuilder.build()\ncontext.py:31"]
    BLOCKS["list of dict\ntier-tagged blocks"]
    FORMAT["provider.format_system()\nanthropic_compat.py:79"]
    CACHED["cache_control: ephemeral\non stable + semi_stable"]

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
    QUERY["Session Start\nQuery: sender + recent text"]

    subgraph Structured["Structured Memory (v2)"]
        ENTITIES["Extract Entities\nmemory.py:331"]
        FACTS["Lookup Facts\nmemory.py:379"]
        KEYWORDS["Extract Keywords"]
        EPISODES["Search Episodes\nmemory.py:414"]
        COMMITMENTS["Open Commitments\nmemory.py:446"]
    end

    subgraph Unstructured["Unstructured Memory (v1)"]
        FTS["FTS5 Search\nmemory.py:59"]
        FTS_CHECK{">=3 results?"}
        EMBED["Embed Query\nmemory.py:168"]
        VECTOR["Vector Search\nmemory.py:128"]
        MERGE["Merge + Dedup"]
    end

    PRIORITY["Priority Sort\ncommitments > vector >\nepisodes > facts"]
    BUDGET["Token Budget\ninject_recall()\nmemory.py:548"]

    SYNTH_CHECK{"synthesis_style?"}
    RAW["Raw blocks\n→ system prompt"]
    LLM_SYNTH["Synthesize\nsynthesis.py:88"]
    PROSE["Prose\n→ system prompt"]

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
        MSG["Messages\nrole + content"]
        SYS["System Blocks\ntier-tagged dicts"]
        TOOLS_INT["Tool Schemas\nname + desc + input_schema"]
        IMG["Image Blocks\ntype: image + base64"]
    end

    subgraph Protocol["LLMProvider Protocol\nproviders/__init__.py"]
        FM["format_messages()"]
        FS["format_system()"]
        FT["format_tools()"]
        COMPLETE["complete()"]
    end

    subgraph Anthropic["AnthropicCompatProvider"]
        A_SYS["System: list of dicts\n+ cache_control"]
        A_MSG["Content blocks\nthinking preservation"]
        A_IMG["source.type: base64\nmedia_type"]
        A_API["messages.create()"]
    end

    subgraph OpenAI["OpenAICompatProvider"]
        O_SYS["System: single string"]
        O_MSG["Standard messages\nfunction_call format"]
        O_IMG["data: URI images"]
        O_API["chat.completions.create()"]
    end

    RESP["LLMResponse\ntext + tool_calls + usage"]

    MSG --> FM
    SYS --> FS
    TOOLS_INT --> FT
    IMG --> FM

    FM --> A_MSG & O_MSG
    FS --> A_SYS & O_SYS
    FT --> Anthropic & OpenAI

    A_API --> RESP
    O_API --> RESP
```

---

## 6. Session Persistence

Dual storage model with compaction lifecycle.

```mermaid
flowchart TD
    CREATE["get_or_create()\nsession.py:285"]

    subgraph Active["Active Session"]
        USER_MSG["add_user_message()"]
        AGENTIC["run_agentic_loop()"]

        subgraph Persist["Dual Storage"]
            JSONL["Append to JSONL\nid.YYYY-MM-DD.jsonl"]
            STATE["Atomic write\nid.state.json"]
        end

        THRESHOLD{"Token usage\nvs threshold?"}
        NORMAL((" "))
        WARN["Inject warning\npending_system_warning"]
        COMPACT["compact_session()\nsession.py:450\nreplace 2/3 messages\nwith summary"]
    end

    CLOSE["close_session()\nsession.py:323"]
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
        CONFIG["lucyd.toml\n[tools] enabled list"]
        BUILTIN["12 Built-in Modules\n19 tools"]
        PLUGINS["plugins.d/*.py\nCustom tools"]
        CONFIG --> FILTER{"tool.name\nin enabled?"}
        BUILTIN --> FILTER
        PLUGINS --> FILTER
        FILTER -->|yes| REG["ToolRegistry.register()\ntools/__init__.py:19"]
        FILTER -->|no| SKIP["Not loaded"]
    end

    subgraph Configure["Per-module configure()"]
        direction LR
        FS_CFG["filesystem:\nallowed_paths"]
        WEB_CFG["web: api_key\nSSRF protection"]
        MEM_CFG["memory: db_path\nembeddings"]
        MSG_CFG["messaging:\nchannel, contacts"]
    end

    subgraph Runtime["Dispatch — agentic.py:221"]
        CALL["Tool call from LLM\nname + arguments"]
        LOOKUP{"name in\nregistry?"}
        EXEC["execute()\ntools/__init__.py:54"]
        TRUNC["Truncate output\n> output_truncation chars"]
        RESULT["String result\n→ tool_results message"]
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
        POLL["Long Poll\ngetUpdates"]
        PARSE["Parse Message\ntext + sender + attachments"]
        DL["Download Media\n/tmp/lucyd-telegram/"]
        SEND_TG["send() / send_voice()\nBot API"]
    end

    subgraph HTTP["HTTP API — http_api.py"]
        CHAT["/api/v1/chat\nPOST → sync response"]
        NOTIFY["/api/v1/notify\nPOST → 202 accepted"]
        STATUS["/api/v1/status\nGET → health"]
        SESSIONS_EP["/api/v1/sessions\nGET → list"]
        COST_EP["/api/v1/cost\nGET → breakdown"]
        AUTH["Bearer Token Auth\nhmac.compare_digest()"]
        RATE["Rate Limiter\nper-sender window"]
    end

    subgraph CLI["FIFO — lucyd.py:73"]
        PIPE["control.pipe\nJSON lines"]
        SEND_CLI["lucyd-send\nbin/lucyd-send"]
    end

    Q["asyncio.Queue\nlucyd.py:286\nmaxsize=1000"]

    POLL --> PARSE --> DL --> Q
    SEND_CLI --> PIPE --> Q
    CHAT --> AUTH --> RATE --> Q
    NOTIFY --> AUTH --> RATE --> Q

    Q --> LOOP["_message_loop()\nlucyd.py:1361"]
    LOOP --> PROCESS["_process_message()\nlucyd.py:616"]

    PROCESS -->|telegram| SEND_TG
    PROCESS -->|http| FUTURE["Resolve Future\n→ HTTP response"]
    PROCESS -->|system| SUPPRESS["No channel delivery\n(silent processing)"]
```
