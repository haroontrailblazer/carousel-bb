-- Carousel Factory - operational schema (Supabase / Postgres 14+).
-- Idempotent: safe to apply repeatedly (CREATE ... IF NOT EXISTS only).
-- Apply with e.g.:  psql "$DATABASE_URL" -f db/schema.sql
-- (strip the "+asyncpg" marker from the URL first, if present)

-- ---------------------------------------------------------------------------
-- news_queue: fetched news items waiting for (or consumed by) a pipeline run.
-- status: 'queued' -> 'processing' -> 'done' | 'failed'
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_queue (
    id         text        PRIMARY KEY,
    url_hash   text        NOT NULL UNIQUE,
    payload    jsonb       NOT NULL,
    status     text        NOT NULL DEFAULT 'queued',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_queue_status_created
    ON news_queue (status, created_at);

-- ---------------------------------------------------------------------------
-- runs: one row per pipeline run; phase mirrors the orchestrator state machine
-- (generate | qa | review | rework | publish | done).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id       text        PRIMARY KEY,
    news_id      text,
    phase        text        NOT NULL DEFAULT 'generate',
    review_round integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_news_id
    ON runs (news_id);

-- ---------------------------------------------------------------------------
-- feedback: every human verdict (approve/reject) with its feedback text.
-- targets holds the rework targets (agent names) as a JSON array.
-- ---------------------------------------------------------------------------
-- NOTE: this definition is mirrored in app/services/memory_service.py's
-- _SCHEMA_DDL, which auto-creates the table so a fresh database works before
-- this file is applied by hand. Both are CREATE TABLE IF NOT EXISTS, so
-- whichever runs first wins - they MUST stay identical. They previously
-- disagreed (serial here vs BIGSERIAL there, and two different index names on
-- created_at); db/migrations/002_web_app.sql repairs databases created under
-- that split. Change one, change the other.
CREATE TABLE IF NOT EXISTS feedback (
    id         bigserial   PRIMARY KEY,
    run_id     text        NOT NULL,
    verdict    text        NOT NULL,
    feedback   text        NOT NULL DEFAULT '',
    targets    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    news_title text        NOT NULL DEFAULT '',
    decided_by text,
    source     text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_feedback_run_id
    ON feedback (run_id);

CREATE INDEX IF NOT EXISTS idx_feedback_created_at
    ON feedback (created_at DESC);

-- ---------------------------------------------------------------------------
-- pending_reviews: the paused review invocation, one per run.
-- The review API loads this row to resume the ADK session with a
-- function_response matching function_call_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_reviews (
    run_id           text        PRIMARY KEY,
    session_id       text        NOT NULL,
    function_call_id text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
