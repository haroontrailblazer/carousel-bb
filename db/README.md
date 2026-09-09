# Moving the database

Everything needed to stand this service up against a different Postgres, with
the same data and the same media, and change nothing in the application.

## What is where

| File | What it is |
|---|---|
| `schema.sql` | The original hand-written schema. Historical; `005` supersedes it. |
| `migrations/002_web_app.sql` | Console columns on `runs`, `feedback` reconciliation. |
| `migrations/003_lockdown.sql` | Row-level security. |
| `migrations/004_title_lock.sql` | `runs.title_locked`. **Never applied to the live database** — see below. |
| `migrations/005_transfer_baseline.sql` | **Every table, in one file.** Apply this to an empty database and you have the whole structure. |
| `../scripts/db_export.py` | Dump every table to JSONL. |
| `../scripts/db_import.py` | Load a dump into a target. |
| `../scripts/db_verify_baseline.py` | Check `005` against a live database. |
| `../scripts/media_backup.py` | Mirror the storage bucket to disk. |
| `../scripts/media_restore.py` | Upload a mirror into a bucket, then verify it. |

`005` exists because `schema.sql` and `002`–`004` describe only the tables the
pipeline creates on purpose. Five more are created at *runtime*: ADK's session
store builds `sessions`, `events`, `app_states`, `user_states` and
`adk_internal_metadata` on first use, and `app_config`, `app_users` and
`memory_entries` are created by their own modules. That is fine for a fresh
install and useless for a transfer — restoring a dump needs the tables to exist
first, in the shape the dump came from.

## Read this before you start

**`runs.title_locked` is missing from the live database.** Migration `004`
shipped, the code that needs it shipped, and the migration was never applied.
`app/services/db.py:778` and `:807` both reference the column, so renaming a
task fails today with `column "title_locked" does not exist`. Applying `005` to
the *old* database fixes it there; the new one gets it either way.

**The dumps contain secrets.** `app_config` holds the Telegram bot credentials
encrypted with `SECRETS_KEY`, and `sessions` holds whatever the pipeline put in
session state. `backups/` is gitignored — keep it that way, and do not paste
these files anywhere.

**`SECRETS_KEY` must move with the data.** Those credentials are Fernet-encrypted
against it. Restore `app_config` into a deployment with a different key and the
rows decrypt to nothing; the console will tell you to reconnect the bot from the
profile page, which is the honest outcome but means re-entering the token.

**Copy `app_users` or you cannot sign in.** Auth is an allowlist. An empty table
means nobody has access, including you.

## The transfer

Take the backups while nothing is running, so the dump is not a snapshot of a
half-written run.

```bash
# 1. Back up. Both are read-only and re-runnable.
.venv/Scripts/python.exe scripts/db_export.py          # -> backups/db-<timestamp>/
.venv/Scripts/python.exe scripts/media_backup.py       # -> backups/media/<bucket>/

# 2. Confirm the baseline still describes the source. Should print
#    "The migration covers every table and column this database has."
.venv/Scripts/python.exe scripts/db_verify_baseline.py

# 3. Create the structure on the target. Do this BEFORE starting the app -
#    if ADK boots first it creates its own tables, and a shape that disagrees
#    with the dump is a restore that fails halfway.
psql "$NEW_DATABASE_URL" -f db/migrations/005_transfer_baseline.sql

# 4. Rehearse the restore, then do it.
.venv/Scripts/python.exe scripts/db_import.py backups/db-<timestamp> \
    --dsn "$NEW_DATABASE_URL" --dry-run
.venv/Scripts/python.exe scripts/db_import.py backups/db-<timestamp> \
    --dsn "$NEW_DATABASE_URL"

# 5. Create the storage bucket on the new project by hand (same name as
#    MEDIA_BUCKET), then push the media and let the script verify it.
#    Point .env at the NEW project first - media_restore reads the same
#    settings the app does.
.venv/Scripts/python.exe scripts/media_restore.py backups/media --dry-run
.venv/Scripts/python.exe scripts/media_restore.py backups/media

# 6. Point the baseline check at the target. Same sentence as step 2.
.venv/Scripts/python.exe scripts/db_verify_baseline.py --dsn "$NEW_DATABASE_URL"
```

Then update `.env`: `DATABASE_URL`, the four `SUPABASE_S3_*` values,
`SUPABASE_URL` / `SUPABASE_ANON_KEY` if the project changed, and keep
`SECRETS_KEY` and `MEDIA_BUCKET` exactly as they were.

## Things that will bite

**Object keys are addresses, not filenames.** `media_backup` mirrors the bucket
using the keys as directory paths and `media_restore` puts them back under the
same keys. That is what lets existing bundles keep resolving — a run's
`ordered_artifacts` holds keys, not URLs, and a re-keyed object is a broken
carousel.

**Sequences travel separately from rows.** `db_import` calls `setval` after
loading, from the manifest. Skip that and the first new row collides with id 1.

**`events` references `sessions`.** Both scripts load parents first; if you
restore by hand, keep that order.

**Which pooler port.** Supabase serves session mode on `5432` (15 clients for
this project — the ceiling that stranded a run once) and transaction mode on
`6543`. The code detects `:6543` and turns off prepared-statement caching for
both connection pools automatically, so choosing the transaction pooler is a
URL change and nothing else. `DB_STATEMENT_CACHE` overrides the detection.

**Row-level security is on with no policies**, deliberately. The service
connects as the service role, which bypasses RLS; nothing should reach these
tables through Supabase's public API. On Supabase an event trigger enables RLS
for new tables automatically — `005` does it explicitly because that trigger
belongs to the host, not to this schema, and a plain Postgres target has no
such thing.
