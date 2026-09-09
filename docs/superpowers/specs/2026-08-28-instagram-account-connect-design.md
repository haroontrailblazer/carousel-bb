# Instagram account connection (Business Login for Instagram)

**Date:** 2026-08-28
**Status:** approved, in implementation
**Branch:** `feat/instagram-account-connect`

## The problem

Publishing credentials are a single Instagram account in `.env`
(`IG_USER_ID`, `IG_ACCESS_TOKEN`), read directly by
`app/tools/instagram_tools.py`. That means one account for the whole console,
a bearer token sitting in plaintext in a file and in every shell that inherits
it, and no way for anyone to connect an account without a redeploy.

Telegram already solved the same problem the right way
(`app/services/telegram_config.py`): connect from the console, store
encrypted in Postgres, no environment fallback. Instagram is the last
plaintext credential left.

## What we are building

Several Instagram accounts can be connected to the console through Meta's
**Instagram Login (Business Login for Instagram)** OAuth flow. Nobody types a
password into our console - they authenticate on Instagram's own page and we
receive a token. A run picks its target account **before it starts**, because
the account's handle and profile picture are rendered into the slide artwork.

Out of scope, planned separately: the brand/variant design page that will let
someone edit the logo, display name and layout skeleton per account. The
schema leaves room for it.

## Decisions

**Instagram Login, not Facebook Login.** The newer flow (mid-2024) needs no
linked Facebook Page, which is the step that loses people. It talks to
`graph.instagram.com`, not `graph.facebook.com`.

**No environment fallback.** `IG_USER_ID` and `IG_ACCESS_TOKEN` are deleted,
not deprecated, and nothing is seeded from them. Publishing requires a
connected account and says so plainly when there is none. Same reasoning as
the Telegram note in `app/config.py`: an environment fallback is a second,
invisible source of truth that quietly overrides what the console displays.

**A table, not `app_config`.** `app_config` is one JSON value per key. Each
account has its own token and its own expiry, and they are listed, defaulted
and disconnected independently.

**The account is chosen before the run starts.** `apply_body_brand_rail` and
`apply_cta_brand_rail` (`app/tools/brand_layout.py:600,620`) stamp the handle
and favicon onto every slide during generation. Choosing later would mean
either re-rendering or dropping the brand rail from the design; choosing up
front costs nothing and cannot produce a mismatch.

**Brand identity reaches the renderer through a contextvar.** The rendering
path is synchronous, several calls deep, and reads module-level `settings`.
Threading an account through `template_design` and `cta` would churn a lot of
signatures; `asyncio.to_thread` copies the context, and both render paths
already cross that boundary.

## Data model

`db/migrations/006_instagram_accounts.sql`:

```sql
CREATE TABLE IF NOT EXISTS instagram_accounts (
    id                text        PRIMARY KEY,
    ig_user_id        text        NOT NULL UNIQUE,
    username          text        NOT NULL,
    name              text        NOT NULL DEFAULT '',
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

ALTER TABLE runs ADD COLUMN IF NOT EXISTS account_id text;
```

`auth_kind` exists because the Graph host is a property of the token, not a
constant: an `instagram_login` token goes to `graph.instagram.com`, a
`facebook_login` one to `graph.facebook.com`. Only the former is issued today;
the column means adding the latter later is not a migration.

`avatar_key` points into the existing media bucket. The profile picture is
downloaded once at connect time rather than hot-linked, because Meta's
`profile_picture_url` is a short-lived signed CDN URL.

`runs.account_id` is nullable so existing rows stay readable; a run without
one cannot publish.

## OAuth flow

Two routes, both under the existing auth middleware:

- `GET /api/settings/instagram/authorize` - 302 to
  `https://www.instagram.com/oauth/authorize` with `client_id`,
  `redirect_uri`, `response_type=code`, `scope=instagram_business_basic,
  instagram_business_content_publish`, and `state`.
- `GET /api/settings/instagram/callback?code=&state=` - verify state,
  exchange, store, then 302 to `/profile?instagram=connected` (or
  `?instagram_error=<code>`) so the SPA can toast.

`state` is a short-lived HS256 JWT signed with `SESSION_SECRET`, carrying the
connecting user's email, a nonce and a 10-minute expiry. That is CSRF cover
with no server-side session store - the same reasoning behind our own session
cookie in `web_api/auth.py`.

Token exchange is three calls:

1. `POST https://api.instagram.com/oauth/access_token` gives a short-lived
   token (~1 hour).
2. `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token`
   trades it for a long-lived one (60 days).
3. `GET https://graph.instagram.com/me` with
   `fields=user_id,username,name,profile_picture_url` identifies the account.

`IG_APP_ID` and `IG_APP_SECRET` are new environment variables. They are
app-level identifiers, not user credentials - unlike the per-account tokens,
which are Fernet-encrypted in Postgres. When they are missing the connect
route fails with a `not_configured` code and a message naming them, the same
shape as `secrets_unconfigured`.

## Modules

| Module | Responsibility |
|---|---|
| `app/services/instagram_accounts.py` | CRUD over the table, `secret_box` for the token, process-level cache so synchronous callers need no await |
| `app/services/instagram_oauth.py` | State token, authorize URL, code exchange, long-lived exchange, refresh, identity lookup |
| `app/tools/brand_identity.py` | Contextvar carrying handle + favicon bytes for the current run |
| `web_api/routes_settings.py` | Authorize, callback, list, set-default, disconnect |
| `app/scheduler.py` | Daily refresh job |

Changed: `app/tools/instagram_tools.py` (per-account credentials and host),
`app/agents/publisher.py` (reads the run's account),
`app/tools/image_gen.py:480,522,567`, `app/agents/cta.py:196`,
`app/tools/brand_layout.py` (`_favicon_from_source`), `app/runs/service.py`
(persist `account_id`, set the contextvar), `web_api/routes_runs.py`
(`account_id` on create, `publish_configured`), `app/config.py`.

## Token refresh

A daily APScheduler job under an advisory lock, the same shape as
`_FETCH_LOCK_ID`. Any account whose `token_expires_at` is within 14 days gets
refreshed through `GET https://graph.instagram.com/refresh_access_token` with
`grant_type=ig_refresh_token`, which returns a fresh 60-day token.

A token that goes 60 days without use dies and cannot be refreshed. The
console shows days-to-expiry per account and a "reconnect" state, so that is
visible before it becomes a failed publish.

## Failure behaviour

- No accounts connected: `POST /api/runs` refuses with `no_account`, because a
  run cannot render its brand rail or publish without one.
- Account disabled or token expired: same refusal, naming the account.
- `SECRETS_KEY` missing: connect refuses rather than storing a token in the
  clear (`secret_box.SecretsNotConfigured`), matching the Telegram route.
- A token that cannot be decrypted (key rotated) is treated as absent, and the
  account shows as needing reconnection - `secret_box.decrypt` already returns
  an empty string rather than raising, so no page breaks.
- Publish-time failures keep the existing `PublishUncertain` / `PublishAborted`
  handling in `app/agents/publisher.py` unchanged.

## Testing

Unit, with no live Meta calls anywhere:

- State token: sign, verify, reject tampered, reject expired.
- Exchange and refresh against a faked HTTP layer; error payloads surface
  Meta's message.
- `instagram_accounts`: encrypt/decrypt round trip, cache coherence after save
  and delete, exactly one default.
- `brand_identity`: resolution and fallback, and that the value survives
  `asyncio.to_thread`.
- Regression: publishing uses the run's account rather than `settings`,
  extending the pattern in `tests/test_session_regressions.py:257`.

## Operator prerequisites

Not code, and not something the implementation can do:

1. A Meta app with the **Instagram** product added; App ID and secret into
   `IG_APP_ID` / `IG_APP_SECRET`.
2. `{PUBLIC_BASE_URL}/api/settings/instagram/callback` allowlisted as a valid
   OAuth redirect URI.
3. Every account being connected must be a Professional (Business or Creator)
   account.
4. App Review for `instagram_business_content_publish` before accounts outside
   the app's test users can be connected.
