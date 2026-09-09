-- ---------------------------------------------------------------------------
-- 002_web_app: tables and columns the web console needs.
--
-- Idempotent: safe to apply repeatedly. Apply AFTER db/schema.sql, with e.g.
--   psql "$DATABASE_URL" -f db/migrations/002_web_app.sql
-- (strip the "+asyncpg" marker from the URL first, if present)
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- run_events: the DISTILLED, product-level timeline shown in the web UI.
--
-- Deliberately not the raw ADK transcript - that already lives in ADK's own
-- `events` table and is what the /dev inspector is for. This holds one row per
-- event a human would want to see, so the run page can replay a run's history
-- without loading megabytes of function-call payloads.
--
-- seq is assigned in-process by the single asyncio task driving the run, which
-- is safe because exactly one task ever owns one run (see the single-instance
-- constraint in the design). It doubles as the SSE `id:` cursor, so a
-- reconnecting EventSource resumes from Last-Event-ID with no gap and no
-- duplicates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_events (
    run_id     text        NOT NULL,
    seq        integer     NOT NULL,
    kind       text        NOT NULL,   -- phase|progress|tool|error|terminal|gap
    author     text        NOT NULL DEFAULT '',
    text       text        NOT NULL DEFAULT '',
    data       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

-- The SSE replay query is always "this run, seq > cursor, in order".
CREATE INDEX IF NOT EXISTS idx_run_events_run_seq
    ON run_events (run_id, seq);

-- ---------------------------------------------------------------------------
-- app_users: the authorization allowlist.
--
-- The PRIMARY gate is Supabase itself (public signup off; users created by
-- invite). This table is defence in depth, checked when a session cookie is
-- minted: even if signup were accidentally re-enabled, a stranger holding a
-- valid Supabase JWT still gets a 403. It also carries `role`, which gates
-- admin-only routes later.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_users (
    email      text        PRIMARY KEY,
    role       text        NOT NULL DEFAULT 'reviewer',   -- reviewer | admin
    disabled   boolean     NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- app_config: runtime-editable settings.
--
-- `app.config.settings` is a frozen dataclass read once at import, so anything
-- stored there needs a restart to change. The fetch schedule lives here
-- instead, so changing the cadence is a UI action rather than a redeploy.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_config (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- runs: metadata the web console lists and filters on.
--
-- status is distinct from phase. phase mirrors the orchestrator state machine
-- (generate|qa|review|rework|publish|done); status is the run's lifecycle from
-- the operator's point of view (running|awaiting_review|done|interrupted|
-- failed|cancelled). A run killed by a redeploy is phase='generate',
-- status='interrupted' - re-enterable, and that is what the Resume button uses.
--
-- Runs started from the ADK dev UI bypass the web API and leave these NULL, so
-- every consumer must tolerate nulls.
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN IF NOT EXISTS title        text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS source       text;   -- topic|url|queue|schedule
ALTER TABLE runs ADD COLUMN IF NOT EXISTS status       text NOT NULL DEFAULT 'running';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS requested_by text;

-- The history list is "newest first, optionally filtered by phase/status".
CREATE INDEX IF NOT EXISTS idx_runs_created_at
    ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status
    ON runs (status);

-- ---------------------------------------------------------------------------
-- feedback: who decided, and through which channel.
--
-- With both Telegram and the web able to decide a run, "approved" alone no
-- longer says who did it.
-- ---------------------------------------------------------------------------
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS decided_by text;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS source     text;   -- telegram|web|api

-- ---------------------------------------------------------------------------
-- Reconcile the duplicate `feedback` definition.
--
-- feedback was declared twice with different id types: `id serial` in
-- db/schema.sql and `id BIGSERIAL` in app/services/memory_service.py's
-- _SCHEMA_DDL. Both are CREATE TABLE IF NOT EXISTS, so on a fresh database
-- whichever ran first silently won - and the two also used different index
-- names, leaving two indexes on created_at. Both files now declare BIGSERIAL
-- and the same index names; this drops the orphan index left on databases
-- created under the old pair.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS feedback_created_at_idx;

ALTER TABLE feedback ALTER COLUMN id TYPE bigint;
DO $$
BEGIN
    EXECUTE format(
        'ALTER SEQUENCE %s AS bigint',
        pg_get_serial_sequence('feedback', 'id')
    );
EXCEPTION WHEN others THEN
    -- No sequence (already bigint identity, or a non-standard setup): the
    -- column type change above is the part that matters.
    RAISE NOTICE 'feedback.id sequence widening skipped: %', SQLERRM;
END $$;
