-- ============================================================
-- JARVIS — Database Initialisation
-- Runs automatically via docker-entrypoint-initdb.d
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Users & Profiles ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       VARCHAR UNIQUE NOT NULL,
    hashed_api_key VARCHAR,
    active_mode VARCHAR DEFAULT 'work',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_profile (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
    key        VARCHAR NOT NULL,
    value      TEXT NOT NULL,
    confidence FLOAT DEFAULT 1.0,
    source     VARCHAR DEFAULT 'explicit',   -- 'explicit' | 'inferred'
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, key)
);

-- ── Conversations ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    mode        VARCHAR NOT NULL DEFAULT 'work',
    started_at  TIMESTAMPTZ DEFAULT NOW(),
    ended_at    TIMESTAMPTZ,
    summary     TEXT,
    channel     VARCHAR DEFAULT 'chat'   -- 'voice' | 'chat' | 'mobile'
);

CREATE TABLE IF NOT EXISTS messages (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  UUID REFERENCES conversations(id) ON DELETE CASCADE,
    role             VARCHAR NOT NULL,   -- 'user' | 'assistant' | 'tool'
    content          TEXT NOT NULL,
    tool_calls       JSONB,
    tokens_used      INTEGER,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages(conversation_id, created_at);

-- ── Memory ─────────────────────────────────────────────────
-- Structured metadata lives here; vectors live in ChromaDB.
-- The chroma_id column links the two stores.

CREATE TABLE IF NOT EXISTS memories (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID REFERENCES users(id) ON DELETE CASCADE,
    type             VARCHAR NOT NULL,  -- 'episodic' | 'semantic' | 'procedural'
    content          TEXT NOT NULL,
    chroma_id        VARCHAR,           -- ID inside the ChromaDB collection
    metadata         JSONB DEFAULT '{}',
    importance_score FLOAT DEFAULT 0.5,
    access_count     INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS memories_user_type_idx ON memories(user_id, type);
CREATE INDEX IF NOT EXISTS memories_chroma_idx    ON memories(chroma_id);

-- ── Tasks ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tasks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    title       VARCHAR NOT NULL,
    description TEXT,
    status      VARCHAR DEFAULT 'pending',   -- 'pending' | 'in_progress' | 'done' | 'cancelled'
    priority    VARCHAR DEFAULT 'medium',    -- 'low' | 'medium' | 'high'
    due_date    TIMESTAMPTZ,
    source      VARCHAR DEFAULT 'user_explicit',  -- 'user_explicit' | 'agent_created' | 'inferred'
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tasks_user_status_idx ON tasks(user_id, status);

-- ── Tool Execution Log ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS tool_executions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    tool_name       VARCHAR NOT NULL,
    parameters      JSONB,
    result          JSONB,
    success         BOOLEAN NOT NULL,
    error_message   TEXT,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Seed: Default single-user personal instance ────────────

INSERT INTO users (email, hashed_api_key, active_mode)
VALUES ('user@jarvis.local', 'local-dev-key', 'work')
ON CONFLICT (email) DO NOTHING;

INSERT INTO user_profile (user_id, key, value, source)
SELECT id, 'name',     'Boss',         'explicit' FROM users WHERE email = 'user@jarvis.local'
ON CONFLICT (user_id, key) DO NOTHING;

INSERT INTO user_profile (user_id, key, value, source)
SELECT id, 'timezone', 'Asia/Kolkata', 'explicit' FROM users WHERE email = 'user@jarvis.local'
ON CONFLICT (user_id, key) DO NOTHING;
