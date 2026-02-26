"""Schema management for all memory tables.

Creates and migrates 10 tables:

  Unstructured (v1) — used by lucyd-index and memory.py search:
    files              — indexed file metadata (path, hash, mtime)
    chunks             — text chunks with embeddings
    chunks_fts         — FTS5 virtual table for keyword search
    embedding_cache    — cached embeddings (provider, model, hash keyed)

  Structured (v2) — used by lucyd-consolidate and recall:
    facts              — entity-attribute-value triples with confidence scoring
    episodes           — timestamped narrative session summaries
    commitments        — promises and obligations with status tracking
    entity_aliases     — canonical name resolution (nickname → entity)
    consolidation_state       — tracks per-session processing progress
    consolidation_file_hashes — tracks file content hashes to avoid reprocessing
"""

from __future__ import annotations

import sqlite3


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all structured memory tables if they don't exist.

    Safe to call on every startup — all statements use IF NOT EXISTS.
    Enables WAL mode for concurrent read/write performance.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")

    conn.executescript("""
        -- ── Unstructured (v1): indexer + search ──────────────

        -- Indexed file metadata for change detection
        CREATE TABLE IF NOT EXISTS files (
            path    TEXT PRIMARY KEY,
            source  TEXT NOT NULL DEFAULT 'memory',
            hash    TEXT NOT NULL,
            mtime   INTEGER NOT NULL,
            size    INTEGER NOT NULL
        );

        -- Text chunks with embeddings for vector search
        CREATE TABLE IF NOT EXISTS chunks (
            id          TEXT PRIMARY KEY,
            path        TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'memory',
            start_line  INTEGER NOT NULL,
            end_line    INTEGER NOT NULL,
            hash        TEXT NOT NULL,
            model       TEXT NOT NULL,
            text        TEXT NOT NULL,
            embedding   TEXT NOT NULL,
            updated_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);

        -- Cached embeddings (composite PK: provider + model + key + hash)
        CREATE TABLE IF NOT EXISTS embedding_cache (
            provider     TEXT NOT NULL,
            model        TEXT NOT NULL,
            provider_key TEXT NOT NULL,
            hash         TEXT NOT NULL,
            embedding    TEXT NOT NULL,
            dims         INTEGER,
            updated_at   INTEGER NOT NULL,
            PRIMARY KEY (provider, model, provider_key, hash)
        );

        -- ── Structured (v2): consolidation + recall ─────────

        -- Entity-attribute-value triples with soft deletion
        CREATE TABLE IF NOT EXISTS facts (
            id             INTEGER PRIMARY KEY,
            entity         TEXT NOT NULL,
            attribute      TEXT NOT NULL,
            value          TEXT NOT NULL,
            confidence     REAL DEFAULT 1.0,
            source_session TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            accessed_at    TEXT NOT NULL DEFAULT (datetime('now')),
            invalidated_at TEXT
        );

        -- Timestamped session summaries
        CREATE TABLE IF NOT EXISTS episodes (
            id             INTEGER PRIMARY KEY,
            session_id     TEXT NOT NULL,
            date           TEXT NOT NULL DEFAULT (date('now')),
            participants   TEXT,
            topics         TEXT,
            decisions      TEXT,
            commitments    TEXT,
            summary        TEXT NOT NULL,
            emotional_tone TEXT
        );

        -- Promises and obligations with status tracking
        CREATE TABLE IF NOT EXISTS commitments (
            id             INTEGER PRIMARY KEY,
            episode_id     INTEGER REFERENCES episodes(id),
            who            TEXT NOT NULL,
            what           TEXT NOT NULL,
            deadline       TEXT,
            status         TEXT DEFAULT 'open',
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Canonical name resolution (lowercase normalized)
        CREATE TABLE IF NOT EXISTS entity_aliases (
            id             INTEGER PRIMARY KEY,
            alias          TEXT NOT NULL UNIQUE,
            canonical      TEXT NOT NULL
        );

        -- Tracks which messages in a session have been consolidated
        CREATE TABLE IF NOT EXISTS consolidation_state (
            session_id            TEXT PRIMARY KEY,
            last_compaction_count INTEGER NOT NULL DEFAULT 0,
            last_message_count    INTEGER NOT NULL DEFAULT 0,
            last_consolidated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Tracks file content hashes to skip unchanged files
        CREATE TABLE IF NOT EXISTS consolidation_file_hashes (
            file_path         TEXT PRIMARY KEY,
            content_hash      TEXT NOT NULL,
            last_processed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Indexes for common query patterns

        -- Fast lookup of current facts by entity (excludes invalidated)
        CREATE INDEX IF NOT EXISTS idx_facts_entity
            ON facts (entity, invalidated_at);

        -- Lookup by entity + attribute for dedup and update checks
        CREATE INDEX IF NOT EXISTS idx_facts_entity_attr
            ON facts (entity, attribute, invalidated_at);

        -- Open commitments query (status='open')
        CREATE INDEX IF NOT EXISTS idx_commitments_status
            ON commitments (status);

        -- Commitment lookup by episode
        CREATE INDEX IF NOT EXISTS idx_commitments_episode
            ON commitments (episode_id);

        -- Episode search by date range
        CREATE INDEX IF NOT EXISTS idx_episodes_date
            ON episodes (date);

        -- Alias resolution
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_canonical
            ON entity_aliases (canonical);
    """)

    # FTS5 virtual table — separate execute because some SQLite builds
    # don't handle virtual tables inside executescript reliably.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text,
            id UNINDEXED,
            path UNINDEXED,
            source UNINDEXED,
            model UNINDEXED,
            start_line UNINDEXED,
            end_line UNINDEXED
        )
    """)
