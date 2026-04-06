-- Lucyd schema v001 — initial Postgres schema.
-- Replaces SQLite memory.db, metering.db, and JSON session files.
-- Every table includes client_id + agent_id for multi-tenant data partitioning.

-- ── Extensions ──────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Sessions ────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS sessions;

CREATE TABLE sessions.sessions (
    id                      TEXT NOT NULL PRIMARY KEY,
    client_id               TEXT NOT NULL,
    agent_id                TEXT NOT NULL,
    contact                 TEXT NOT NULL,
    model                   TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at               TIMESTAMPTZ,
    total_input_tokens      BIGINT NOT NULL DEFAULT 0,
    total_output_tokens     BIGINT NOT NULL DEFAULT 0,
    compaction_count        INT NOT NULL DEFAULT 0,
    warned_about_compaction BOOLEAN NOT NULL DEFAULT FALSE,
    pending_system_warning  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_sessions_contact
    ON sessions.sessions (client_id, agent_id, contact);

CREATE TABLE sessions.messages (
    id          BIGSERIAL PRIMARY KEY,
    client_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL REFERENCES sessions.sessions (id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     JSONB NOT NULL,
    ordinal     INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_session
    ON sessions.messages (session_id, ordinal);

CREATE TABLE sessions.events (
    id          BIGSERIAL PRIMARY KEY,
    client_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL REFERENCES sessions.sessions (id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    trace_id    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_session
    ON sessions.events (session_id, created_at);

-- ── Knowledge ───────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE knowledge.facts (
    id              SERIAL PRIMARY KEY,
    client_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    entity          TEXT NOT NULL,
    attribute       TEXT NOT NULL,
    value           TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_session  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    accessed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_at  TIMESTAMPTZ
);
CREATE INDEX idx_facts_entity
    ON knowledge.facts (client_id, agent_id, entity)
    WHERE invalidated_at IS NULL;
CREATE INDEX idx_facts_entity_attr
    ON knowledge.facts (client_id, agent_id, entity, attribute)
    WHERE invalidated_at IS NULL;

CREATE TABLE knowledge.episodes (
    id              SERIAL PRIMARY KEY,
    client_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    date            DATE NOT NULL DEFAULT CURRENT_DATE,
    topics          JSONB,
    decisions       JSONB,
    commitments     JSONB,
    summary         TEXT NOT NULL,
    emotional_tone  TEXT
);
CREATE INDEX idx_episodes_date
    ON knowledge.episodes (client_id, agent_id, date);

CREATE TABLE knowledge.commitments (
    id          SERIAL PRIMARY KEY,
    client_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    episode_id  INT REFERENCES knowledge.episodes (id),
    who         TEXT NOT NULL,
    what        TEXT NOT NULL,
    deadline    TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_commitments_status
    ON knowledge.commitments (client_id, agent_id, status);

CREATE TABLE knowledge.entity_aliases (
    client_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    canonical   TEXT NOT NULL,
    PRIMARY KEY (client_id, agent_id, alias)
);

CREATE TABLE knowledge.consolidation_state (
    client_id               TEXT NOT NULL,
    agent_id                TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    last_compaction_count   INT NOT NULL DEFAULT 0,
    last_message_count      INT NOT NULL DEFAULT 0,
    last_consolidated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, agent_id, session_id)
);

CREATE TABLE knowledge.consolidation_file_hashes (
    client_id           TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    file_path           TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    last_processed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, agent_id, file_path)
);

CREATE TABLE knowledge.evolution_state (
    client_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    last_evolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash    TEXT NOT NULL,
    logs_through    TEXT,
    PRIMARY KEY (client_id, agent_id, file_path)
);

-- ── Metering ────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS metering;

CREATE TABLE metering.costs (
    id                  BIGSERIAL PRIMARY KEY,
    client_id           TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id          TEXT NOT NULL,
    model               TEXT NOT NULL,
    provider            TEXT NOT NULL DEFAULT '',
    input_tokens        INT NOT NULL DEFAULT 0,
    output_tokens       INT NOT NULL DEFAULT 0,
    cache_read_tokens   INT NOT NULL DEFAULT 0,
    cache_write_tokens  INT NOT NULL DEFAULT 0,
    cost                NUMERIC(12, 6) NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'EUR',
    call_type           TEXT NOT NULL DEFAULT 'agentic',
    trace_id            TEXT,
    billing_period      TEXT NOT NULL,
    latency_ms          INT,
    success             BOOLEAN NOT NULL DEFAULT TRUE,
    error_type          TEXT
);
CREATE INDEX idx_costs_billing
    ON metering.costs (client_id, agent_id, billing_period);
CREATE INDEX idx_costs_timestamp
    ON metering.costs (client_id, timestamp);

-- ── Search ──────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS search;

CREATE TABLE search.files (
    client_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    path        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'memory',
    hash        TEXT NOT NULL,
    mtime       BIGINT NOT NULL,
    size        BIGINT NOT NULL,
    PRIMARY KEY (client_id, agent_id, path)
);

CREATE TABLE search.chunks (
    client_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    id              TEXT NOT NULL,
    path            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'memory',
    start_line      INT NOT NULL,
    end_line        INT NOT NULL,
    hash            TEXT NOT NULL,
    model           TEXT NOT NULL,
    text            TEXT NOT NULL,
    embedding       vector,
    search_vector   tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, agent_id, id)
);
CREATE INDEX idx_chunks_path
    ON search.chunks (client_id, agent_id, path);
CREATE INDEX idx_chunks_fts
    ON search.chunks USING GIN (search_vector);

CREATE TABLE search.embedding_cache (
    client_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    provider_key    TEXT NOT NULL,
    hash            TEXT NOT NULL,
    embedding       vector NOT NULL,
    dims            INT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, agent_id, provider, model, provider_key, hash)
);
