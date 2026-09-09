-- ---------------------------------------------------------------------------
-- 004_title_lock: let the agents name a task without overwriting a human.
--
-- Idempotent: safe to apply repeatedly. Apply AFTER db/schema.sql, with e.g.
--   psql "$DATABASE_URL" -f db/migrations/004_title_lock.sql
-- (strip the "+asyncpg" marker from the URL first, if present)
--
-- Why a column and not a comparison.
--
-- A topic run is created with `title = <whatever the user typed>`, because at
-- that moment nothing better exists - the planner has not run and there is no
-- story yet. Once it has, `hook_title` is a real name for the carousel, and
-- that is what belongs in the sidebar.
--
-- But a rework re-runs the planner, so "write the planner's title" cannot mean
-- "every time the planner finishes" - it would replace a name the user chose,
-- silently, mid-task. Inferring the difference by comparing strings does not
-- work either: after the first automatic write the stored title no longer
-- matches the typed prompt, which is indistinguishable from a rename.
--
-- So the fact is recorded rather than guessed. `title_locked` means A PERSON
-- NAMED THIS, and only PATCH /api/runs/{id} sets it.
-- ---------------------------------------------------------------------------

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS title_locked boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN runs.title_locked IS
    'A human renamed this task; automated writers must leave the title alone.';
