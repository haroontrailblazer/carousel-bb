-- ---------------------------------------------------------------------------
-- 005_transfer_baseline: every table this service needs, in one file.
--
-- Written for moving the database somewhere else. Apply this to an EMPTY
-- Postgres and you get the exact structure the old one had - so nothing about
-- the application has to change.
--
-- Idempotent (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS), so it is
-- also safe to run against the existing database. Apply with e.g.
--   psql "$DATABASE_URL" -f db/migrations/005_transfer_baseline.sql
-- (strip the "+asyncpg" marker from the URL first, if present)
--
-- WHY THIS EXISTS AS WELL AS db/schema.sql
--
-- schema.sql and migrations 002-004 describe only the tables the pipeline
-- creates deliberately. Five more are created at RUNTIME by code that assumes
-- an empty database is fine - ADK's session store builds `sessions`, `events`,
-- `app_states`, `user_states` and `adk_internal_metadata` on first use, and
-- `app_config`, `app_users` and `memory_entries` are created by their own
-- modules. That works for a fresh install and is useless for a transfer:
-- restoring a dump needs the tables to exist FIRST, with the same shapes the
-- dump was taken from.
--
-- ORDER MATTERS: create `sessions` before `events`, which references it.
--
-- Verified against the live database on 2026-08-27 (13 tables in `public`).
-- ---------------------------------------------------------------------------

-- Extensions present on the source database. pgcrypto and uuid-ossp are
-- Supabase defaults; both are harmless if the new host already has them.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ===========================================================================
-- ADK's session store.
--
-- google-adk's DatabaseSessionService owns these and will create them itself
-- if they are missing - but only in the shape THAT version expects. They are
-- declared here so a restore has somewhere to land, and so a future ADK
-- upgrade that changes the shape is a visible conflict rather than a silent
-- one. If you upgrade ADK, re-check these against what it builds.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS sessions (
    app_name    varchar(128) NOT NULL,
    user_id     varchar(128) NOT NULL,
    id          varchar(128) NOT NULL,
    state       jsonb        NOT NULL,
    create_time timestamp    NOT NULL,
    update_time timestamp    NOT NULL,
    PRIMARY KEY (app_name, user_id, id)
);

-- The pipeline's own transcript. Every run's trace is read from here, which is
-- why a task started from the CLI or the dev UI still has one.
CREATE TABLE IF NOT EXISTS events (
    id            varchar(128) NOT NULL,
    app_name      varchar(128) NOT NULL,
    user_id       varchar(128) NOT NULL,
    session_id    varchar(128) NOT NULL,
    invocation_id varchar(256) NOT NULL,
    timestamp     timestamp    NOT NULL,
    event_data    jsonb,
    PRIMARY KEY (id, app_name, user_id, session_id),
    FOREIGN KEY (app_name, user_id, session_id)
        REFERENCES sessions (app_name, user_id, id) ON DELETE CASCADE
);

-- Reading a run's trace is always "this session, newest first", so the index
-- carries the sort as well as the filter.
CREATE INDEX IF NOT EXISTS idx_events_app_user_session_ts
    ON events (app_name, user_id, session_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS app_states (
    app_name    varchar(128) NOT NULL PRIMARY KEY,
    state       jsonb        NOT NULL,
    update_time timestamp    NOT NULL
);

CREATE TABLE IF NOT EXISTS user_states (
    app_name    varchar(128) NOT NULL,
    user_id     varchar(128) NOT NULL,
    state       jsonb        NOT NULL,
    update_time timestamp    NOT NULL,
    PRIMARY KEY (app_name, user_id)
);

-- ADK records its own schema version here. Copy the row: a mismatch makes ADK
-- think it is looking at a database from a different version of itself.
CREATE TABLE IF NOT EXISTS adk_internal_metadata (
    key   varchar(128) NOT NULL PRIMARY KEY,
    value varchar(256) NOT NULL
);


-- ===========================================================================
-- The pipeline's operational tables (mirrors db/schema.sql + 002/003/004).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS news_queue (
    id         text        PRIMARY KEY,
    url_hash   text        NOT NULL UNIQUE,
    payload    jsonb       NOT NULL,
    status     text        NOT NULL DEFAULT 'queued',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_queue_status_created
    ON news_queue (status, created_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id       text        PRIMARY KEY,
    news_id      text,
    phase        text        NOT NULL DEFAULT 'generate',
    review_round integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Added by 002_web_app. Repeated here so a fresh database gets them without
-- having to replay the whole migration history.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS title        text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS source       text;   -- topic|url|queue|schedule
ALTER TABLE runs ADD COLUMN IF NOT EXISTS status       text NOT NULL DEFAULT 'running';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS requested_by text;

-- Added by 004_title_lock.
--
-- NOT PRESENT on the source database as of 2026-08-27, although the code that
-- needs it has shipped: app/services/db.py writes `title_locked` when someone
-- renames a task and reads it before letting an agent rename one. Both of
-- those statements fail today with "column title_locked does not exist". The
-- new database gets it, and applying this file to the OLD one fixes it there.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS title_locked boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN runs.title_locked IS
    'A human renamed this task; automated writers must leave the title alone.';

CREATE INDEX IF NOT EXISTS idx_runs_news_id    ON runs (news_id);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs (status);

-- Mirrored in app/services/memory_service.py's _SCHEMA_DDL, which creates this
-- table on a fresh database so the app works before anyone applies SQL by
-- hand. Both are CREATE TABLE IF NOT EXISTS, so whichever runs first wins -
-- they MUST stay identical. Change one, change the other.
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

CREATE INDEX IF NOT EXISTS idx_feedback_run_id     ON feedback (run_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback (created_at DESC);

CREATE TABLE IF NOT EXISTS pending_reviews (
    run_id           text        PRIMARY KEY,
    session_id       text        NOT NULL,
    function_call_id text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- The console's distilled timeline. Keyed on (run_id, seq) so a resumed leg
-- continuing the numbering cannot collide with the first leg's rows.
CREATE TABLE IF NOT EXISTS run_events (
    run_id     text        NOT NULL,
    seq        integer     NOT NULL,
    kind       text        NOT NULL,
    author     text        NOT NULL DEFAULT '',
    text       text        NOT NULL DEFAULT '',
    data       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_seq ON run_events (run_id, seq);


-- ===========================================================================
-- Console configuration and access.
-- ===========================================================================

-- Settings that must be changeable without a redeploy - the fetch schedule,
-- and the encrypted Telegram bot credentials. `settings` is a frozen dataclass
-- read once at import, so anything kept there would need a restart to change.
--
-- The value is ENCRYPTED with SECRETS_KEY (Fernet). Restoring these rows into
-- a deployment with a different SECRETS_KEY leaves credentials that cannot be
-- decrypted - reconnect the bot from the profile page instead.
CREATE TABLE IF NOT EXISTS app_config (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Who may sign in. Empty means nobody, so copy these rows or add yourself
-- before deploying against the new database.
CREATE TABLE IF NOT EXISTS app_users (
    email      text        PRIMARY KEY,
    role       text        NOT NULL DEFAULT 'reviewer',
    disabled   boolean     NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Long-term memory for the Learner. Mirrored in memory_service.py's
-- _SCHEMA_DDL, same rule as `feedback` above.
CREATE TABLE IF NOT EXISTS memory_entries (
    id           bigserial   PRIMARY KEY,
    app_name     text        NOT NULL,
    user_id      text        NOT NULL,
    session_id   text        NOT NULL,
    event_id     text        NOT NULL DEFAULT '',
    author       text,
    role         text,
    text_content text        NOT NULL,
    event_ts     text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS memory_entries_event_uq
    ON memory_entries (app_name, user_id, session_id, event_id);
CREATE INDEX IF NOT EXISTS memory_entries_scope_idx
    ON memory_entries (app_name, user_id);


-- ===========================================================================
-- Row-level security.
--
-- Every table on the source has RLS ENABLED and NO POLICIES, which denies all
-- access to the anon and authenticated roles. That is deliberate (003_lockdown):
-- this service talks to Postgres as the service role, which bypasses RLS, and
-- nothing should reach these tables through Supabase's public API.
--
-- On Supabase an event trigger (`ensure_rls` -> `public.rls_auto_enable`) turns
-- RLS on for every new table in `public` automatically, so these statements are
-- usually redundant. They are here because that trigger is a property of the
-- HOST, not of this schema, and a plain Postgres target will not have it.
-- ===========================================================================

ALTER TABLE sessions              ENABLE ROW LEVEL SECURITY;
ALTER TABLE events                ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_states            ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_states           ENABLE ROW LEVEL SECURITY;
ALTER TABLE adk_internal_metadata ENABLE ROW LEVEL SECURITY;
ALTER TABLE news_queue            ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback              ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_reviews       ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events            ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_config            ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_users             ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_entries        ENABLE ROW LEVEL SECURITY;
