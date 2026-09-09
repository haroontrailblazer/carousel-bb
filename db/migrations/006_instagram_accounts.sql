-- ---------------------------------------------------------------------------
-- 006_instagram_accounts: connect Instagram accounts from the console.
--
-- Idempotent: safe to apply repeatedly. Apply AFTER 005_transfer_baseline, e.g.
--   psql "$DATABASE_URL" -f db/migrations/006_instagram_accounts.sql
-- (strip the "+asyncpg" marker from the URL first, if present)
--
-- WHY A TABLE AND NOT AN app_config KEY
--
-- app_config holds one JSON value per key, which was the right shape for
-- Telegram: there is exactly one bot. Instagram accounts are plural. Each has
-- its own token, its own 60-day expiry, and its own brand identity, and they
-- are listed, defaulted and disconnected independently of each other.
--
-- WHY THE TOKEN COLUMN IS CALLED token_enc
--
-- It holds Fernet CIPHERTEXT, never a usable token - see
-- app/services/secret_box.py. This is an ordinary Postgres table, so a backup
-- file or a support session would otherwise hand over a credential that can
-- post to somebody's Instagram. SECRETS_KEY does the encrypting and never
-- goes in the database; a dump restored without it decrypts to nothing, and
-- the console correctly reports every account as needing reconnection.
--
-- WHY auth_kind EXISTS WHEN ONLY ONE VALUE IS WRITTEN TODAY
--
-- The Graph host is a property of the TOKEN, not a constant. A token minted
-- by Instagram Login works only against graph.instagram.com; one from the
-- older Facebook Login path only against graph.facebook.com. Sending a token
-- to the wrong host fails with a permissions error that reads like a missing
-- scope and is not one. Recording which kind it is means supporting the other
-- path later is a code change rather than a migration.
--
-- NOTE ON IG_USER_ID / IG_ACCESS_TOKEN
--
-- Those environment variables are GONE, and nothing is seeded from them.
-- Publishing now requires an account connected from Profile -> Instagram.
-- The same call the Telegram credentials made, for the same reason: an
-- environment fallback is a bearer token in plaintext and a second, invisible
-- source of truth that silently overrides whatever the console displays.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS instagram_accounts (
    id                text        PRIMARY KEY,
    -- Instagram's own id for the account; the id the publishing endpoints
    -- want, and the natural key for "reconnecting the same account".
    ig_user_id        text        NOT NULL UNIQUE,
    username          text        NOT NULL,
    name              text        NOT NULL DEFAULT '',
    -- Object key in the media bucket. The profile picture is downloaded once
    -- at connect time rather than hot-linked: Meta's profile_picture_url is a
    -- short-lived signed CDN URL that would be dead long before it was needed
    -- to stamp a brand rail.
    avatar_key        text        NOT NULL DEFAULT '',
    auth_kind         text        NOT NULL DEFAULT 'instagram_login',
    token_enc         text        NOT NULL,
    token_expires_at  timestamptz NOT NULL,
    is_default        boolean     NOT NULL DEFAULT false,
    disabled          boolean     NOT NULL DEFAULT false,
    connected_by      text        NOT NULL DEFAULT '',
    connected_at      timestamptz NOT NULL DEFAULT now(),
    last_refreshed_at timestamptz
);

COMMENT ON COLUMN instagram_accounts.token_enc IS
    'Fernet ciphertext of the long-lived access token. Never a usable token.';
COMMENT ON COLUMN instagram_accounts.auth_kind IS
    'instagram_login | facebook_login - selects the Graph host for this token.';

-- NO unique index on is_default, deliberately.
--
-- A partial unique index would express the rule exactly ("at most one row
-- where is_default"), and it would also break the one statement that
-- maintains it. app/services/db.py sets the default with a single
--   UPDATE instagram_accounts SET is_default = (id = $1)
-- which is atomic and never leaves the table without a default. But Postgres
-- checks unique indexes per ROW as an UPDATE walks the table, not at the end
-- of the statement, and a partial unique index cannot be deferred. So the
-- statement would fail or succeed depending on the order rows happened to be
-- visited - the classic UPDATE-a-unique-column trap.
--
-- The single UPDATE is what actually guarantees the invariant. Adding an
-- index that intermittently rejects it would trade a real guarantee for a
-- flaky one.

-- ---------------------------------------------------------------------------
-- Which account a run is for.
--
-- Nullable, because every run created before this migration has no answer and
-- rewriting history would invent one. Those runs can still be read; they
-- cannot be published, and the publisher says so by name.
--
-- Chosen BEFORE the run starts rather than at publish time: the account's
-- handle and profile picture are composited into every slide as it is
-- generated (app/tools/brand_layout.py), so a late choice would mean either
-- re-rendering the carousel or shipping one brand's artwork to another.
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN IF NOT EXISTS account_id text;

COMMENT ON COLUMN runs.account_id IS
    'instagram_accounts.id this run was generated for and publishes to.';

CREATE INDEX IF NOT EXISTS idx_runs_account_id ON runs (account_id);
