-- ---------------------------------------------------------------------------
-- 003_lockdown: take every application table off Supabase's public REST API.
--
-- MUST be applied in the same release as the web console, not after it.
--
-- Why this matters. Supabase runs PostgREST over the `public` schema and
-- serves it at https://<project>.supabase.co/rest/v1/<table> to anyone holding
-- the anon key. Shipping a browser SPA that signs in with supabase-js means
-- shipping that anon key in a JavaScript bundle - it is public by design.
--
-- Every table this project uses lives in `public` with no RLS and no REVOKE:
--   * db/schema.sql creates news_queue, runs, feedback, pending_reviews
--     unqualified, so they land in public;
--   * ADK's DatabaseSessionService creates sessions, events, app_states,
--     user_states and adk_internal_metadata there automatically - `events` is
--     the full agent transcript and `sessions.state` holds the entire carousel
--     bundle, research brief and caption.
--
-- So before this migration, the anon key reads every run's full internal state
-- and can write to pending_reviews. That exposure exists today; the SPA is what
-- makes it trivially reachable.
--
-- What actually fixes it: the REVOKE. PostgREST connects as `anon` or
-- `authenticated`; with no table privilege the request is rejected before any
-- policy is consulted. ENABLE ROW LEVEL SECURITY with zero policies is
-- belt-and-braces (deny-all for non-owners) and silences Supabase's linter.
--
-- What does NOT help: RLS as protection from our own backend. app/services/db.py
-- connects with the DSN's own role - `postgres`, the table owner - which
-- bypasses RLS unless FORCE ROW LEVEL SECURITY is set. Writing policies to
-- defend the backend against itself would be theatre.
--
-- Idempotent: safe to apply repeatedly. Tables that do not exist yet are
-- skipped, and the ALTER DEFAULT PRIVILEGES below covers them when ADK
-- eventually creates them.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    t text;
    tables text[] := ARRAY[
        -- this project's operational tables
        'news_queue', 'runs', 'feedback', 'pending_reviews',
        'memory_entries', 'run_events', 'app_users', 'app_config',
        -- ADK's session store, created automatically by DatabaseSessionService
        'sessions', 'events', 'app_states', 'user_states',
        'adk_internal_metadata'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        IF to_regclass(format('public.%I', t)) IS NULL THEN
            RAISE NOTICE 'skipping %: not created yet', t;
            CONTINUE;
        END IF;

        EXECUTE format(
            'REVOKE ALL ON public.%I FROM anon, authenticated', t
        );
        EXECUTE format(
            'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t
        );
        RAISE NOTICE 'locked down %', t;
    END LOOP;
END $$;

-- Sequences too: a writable sequence is a small leak on its own, and PostgREST
-- exposes them alongside the tables.
DO $$
DECLARE
    s text;
BEGIN
    FOR s IN
        SELECT sequence_name FROM information_schema.sequences
        WHERE sequence_schema = 'public'
    LOOP
        EXECUTE format(
            'REVOKE ALL ON SEQUENCE public.%I FROM anon, authenticated', s
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- The important half: make the NEXT table born locked.
--
-- ADK creates its session tables lazily on first use, and a future ADK version
-- may add more. Without this, the day ADK adds a table is the day a new one is
-- silently published to the REST API. Default privileges apply to tables
-- created by the role that runs this statement, which is the same role the
-- application connects as.
-- ---------------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;

-- ---------------------------------------------------------------------------
-- Verify after applying (should be a permission error, NOT rows):
--
--   curl "https://<project>.supabase.co/rest/v1/events?select=*" \
--        -H "apikey: <anon key>"
--
-- If that returns data, this migration did not apply.
-- ---------------------------------------------------------------------------
