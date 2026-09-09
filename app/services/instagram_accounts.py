"""Connected Instagram accounts: encrypted at rest, readable synchronously.

**Why a table and not app_config.** ``app_config`` holds one JSON value per
key, which was right for Telegram - there is exactly one bot. Instagram
accounts are plural, each with its own token, its own 60-day expiry and its
own brand identity, and they are listed, defaulted and disconnected
independently.

**Why the token is encrypted.** Same reason as
``app.services.telegram_config``: this is a Postgres table, so a backup file
or a support session would otherwise hand over a credential that can post to
someone's Instagram. ``SECRETS_KEY`` never goes in the database; losing it
means the stored tokens can no longer be read, which is the point, and the
fix is to connect the accounts again.

**Why a process-level cache.** Both readers are SYNCHRONOUS and deep inside
worker threads: ``app.tools.instagram_tools`` publishes from inside
``asyncio.to_thread``, and ``app.tools.brand_layout`` stamps the handle onto
every slide from the same kind of thread. Neither can await a query. So the
cache is refreshed at startup and on every change, and read synchronously.
Decryption happens on load, so plaintext exists only in memory and only in
this process.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services import db, secret_box

logger = logging.getLogger(__name__)

#: Where each kind of token has to be sent. The host is a property of the
#: TOKEN, not a constant: a token minted by Instagram Login only works against
#: graph.instagram.com, and one from the older Facebook Login path only
#: against graph.facebook.com. Getting this wrong fails at publish time with
#: an opaque permissions error, which is why it is recorded per account rather
#: than inferred.
GRAPH_HOSTS = {
    "instagram_login": "https://graph.instagram.com",
    "facebook_login": "https://graph.facebook.com",
}

DEFAULT_AUTH_KIND = "instagram_login"

#: Refresh anything expiring within this window. Meta's long-lived tokens last
#: 60 days and can be refreshed any time after the first 24 hours, so a
#: fortnight of slack means a fortnight of failed refreshes would have to pass
#: unnoticed before anything breaks.
REFRESH_WINDOW_DAYS = 14

_cache: dict[str, "Account"] = {}


@dataclass(frozen=True)
class Account:
    """One connected Instagram account, with its token already decrypted."""

    id: str
    ig_user_id: str
    username: str
    name: str
    avatar_key: str
    auth_kind: str
    token: str
    token_expires_at: Optional[datetime]
    is_default: bool
    disabled: bool
    connected_by: str
    connected_at: Optional[datetime]
    last_refreshed_at: Optional[datetime]

    @property
    def handle(self) -> str:
        """The handle as it is drawn on the brand rail, with its ``@``."""
        return f"@{self.username.lstrip('@')}" if self.username else ""

    @property
    def graph_host(self) -> str:
        return GRAPH_HOSTS.get(self.auth_kind, GRAPH_HOSTS[DEFAULT_AUTH_KIND])

    @property
    def expires_in_days(self) -> Optional[int]:
        if self.token_expires_at is None:
            return None
        return (self.token_expires_at - datetime.now(timezone.utc)).days

    @property
    def needs_reconnect(self) -> bool:
        """Whether this account can no longer be used to publish.

        Three ways to get here: the token was never readable (SECRETS_KEY was
        rotated), it has lapsed, or someone disabled the account. They are one
        state as far as the console is concerned - the button says reconnect.
        """
        if self.disabled or not self.token:
            return True
        if self.token_expires_at is None:
            return False
        return self.token_expires_at <= datetime.now(timezone.utc)

    @property
    def usable(self) -> bool:
        return not self.needs_reconnect

    def public(self) -> dict:
        """The shape the console renders. Deliberately carries no token."""
        return {
            "id": self.id,
            "ig_user_id": self.ig_user_id,
            "username": self.username,
            "handle": self.handle,
            "name": self.name,
            "avatar_key": self.avatar_key,
            "is_default": self.is_default,
            "disabled": self.disabled,
            "needs_reconnect": self.needs_reconnect,
            "expires_in_days": self.expires_in_days,
            "connected_by": self.connected_by,
            "connected_at": (
                self.connected_at.isoformat() if self.connected_at else ""
            ),
            "last_refreshed_at": (
                self.last_refreshed_at.isoformat() if self.last_refreshed_at else ""
            ),
        }


def _decode(row: dict) -> Account:
    """Turn a stored row into an Account, decrypting the token.

    A token that cannot be decrypted becomes an empty string rather than an
    exception: ``needs_reconnect`` then reports the account honestly, and
    every page that merely LISTS accounts keeps working. Raising here would
    turn a rotated key into a broken console.
    """
    return Account(
        id=str(row.get("id") or ""),
        ig_user_id=str(row.get("ig_user_id") or ""),
        username=str(row.get("username") or ""),
        name=str(row.get("name") or ""),
        avatar_key=str(row.get("avatar_key") or ""),
        auth_kind=str(row.get("auth_kind") or DEFAULT_AUTH_KIND),
        token=secret_box.decrypt(str(row.get("token_enc") or "")),
        token_expires_at=row.get("token_expires_at"),
        is_default=bool(row.get("is_default")),
        disabled=bool(row.get("disabled")),
        connected_by=str(row.get("connected_by") or ""),
        connected_at=row.get("connected_at"),
        last_refreshed_at=row.get("last_refreshed_at"),
    )


# ---------------------------------------------------------------------------
# synchronous readers - safe from inside asyncio.to_thread
# ---------------------------------------------------------------------------
def get(account_id: str) -> Optional[Account]:
    """One account by id, or ``None``."""
    return _cache.get(str(account_id or ""))


def all_accounts() -> list[Account]:
    """Every connected account, default first, then by handle."""
    return sorted(_cache.values(), key=lambda a: (not a.is_default, a.username))


def default() -> Optional[Account]:
    """The account a run targets when nobody chose one.

    A disabled or lapsed account is never returned, even when it still carries
    the flag - handing back an account that cannot publish would only move the
    failure later, into the middle of a run.
    """
    for account in _cache.values():
        if account.is_default and account.usable:
            return account
    return None


def resolve(account_id: str = "") -> Optional[Account]:
    """The account for a run: the one asked for, else the default."""
    if account_id:
        return get(account_id)
    return default()


def configured() -> bool:
    """Whether anything could be published right now."""
    return any(account.usable for account in _cache.values())


def listing() -> list[dict]:
    """Every account in the shape the console renders."""
    return [account.public() for account in all_accounts()]


def reset_cache() -> None:
    """Empty the cache. For tests and for a clean reload."""
    _cache.clear()


def due_for_refresh() -> list[Account]:
    """Accounts whose token expires inside the refresh window.

    A token that has ALREADY lapsed is excluded: Meta cannot refresh one, and
    retrying it daily would be a permanent error in the log for a condition
    only a human reconnecting can fix.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=REFRESH_WINDOW_DAYS)
    return [
        account
        for account in _cache.values()
        if account.token
        and not account.disabled
        and account.token_expires_at is not None
        and now < account.token_expires_at <= cutoff
    ]


# ---------------------------------------------------------------------------
# async writers
# ---------------------------------------------------------------------------
async def load() -> list[Account]:
    """Refresh the cache from the database. Never raises.

    A missing or unreachable database must not stop the process booting - the
    console has its own way of saying nothing is connected, and that is a far
    better outcome than a crash loop.
    """
    try:
        rows = await db.list_instagram_accounts()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load Instagram accounts: %s", exc)
        return all_accounts()

    _cache.clear()
    for row in rows or []:
        account = _decode(row)
        if account.id:
            _cache[account.id] = account

    usable = sum(1 for a in _cache.values() if a.usable)
    logger.info(
        "Instagram: %d account(s) connected, %d usable.", len(_cache), usable
    )
    return all_accounts()


async def save(
    *,
    ig_user_id: str,
    username: str,
    name: str,
    token: str,
    expires_in: int,
    connected_by: str,
    avatar_key: str = "",
    auth_kind: str = DEFAULT_AUTH_KIND,
) -> Account:
    """Encrypt and persist an account, then refresh the cache.

    Keyed on ``ig_user_id``, so reconnecting an account already present
    replaces its token rather than creating a second row for the same handle.

    Raises:
        secret_box.SecretsNotConfigured: when ``SECRETS_KEY`` is absent.
            Storing the token in the clear is not offered as a fallback.
    """
    token_enc = secret_box.encrypt(token)
    if not token_enc:
        raise ValueError("Refusing to store an empty Instagram token.")

    existing = next(
        (a for a in _cache.values() if a.ig_user_id == ig_user_id), None
    )
    account_id = existing.id if existing else uuid.uuid4().hex

    # The first account connected has to become the default, or every run
    # would refuse to start for want of a target nobody was ever asked to
    # choose. Later ones do not steal it.
    is_default = existing.is_default if existing else not _cache

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in or 0))

    await db.upsert_instagram_account(
        id=account_id,
        ig_user_id=ig_user_id,
        username=username,
        name=name,
        avatar_key=avatar_key or (existing.avatar_key if existing else ""),
        auth_kind=auth_kind,
        token_enc=token_enc,
        token_expires_at=expires_at,
        is_default=is_default,
        connected_by=connected_by,
    )
    await load()
    return get(account_id) or _decode(
        {
            "id": account_id,
            "ig_user_id": ig_user_id,
            "username": username,
            "name": name,
            "avatar_key": avatar_key,
            "auth_kind": auth_kind,
            "token_enc": token_enc,
            "token_expires_at": expires_at,
            "is_default": is_default,
            "connected_by": connected_by,
        }
    )


async def record_refreshed(account_id: str, token: str, expires_in: int) -> None:
    """Store a refreshed token against an existing account."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in or 0))
    await db.update_instagram_token(
        id=account_id,
        token_enc=secret_box.encrypt(token),
        token_expires_at=expires_at,
    )
    await load()


async def set_default(account_id: str) -> None:
    """Move the default flag. Exactly one account carries it."""
    await db.set_default_instagram_account(account_id)
    await load()


async def delete(account_id: str) -> None:
    """Forget an account entirely."""
    await db.delete_instagram_account(account_id)
    _cache.pop(str(account_id or ""), None)
    await load()


__all__ = [
    "DEFAULT_AUTH_KIND",
    "GRAPH_HOSTS",
    "REFRESH_WINDOW_DAYS",
    "Account",
    "all_accounts",
    "configured",
    "default",
    "delete",
    "due_for_refresh",
    "get",
    "listing",
    "load",
    "record_refreshed",
    "reset_cache",
    "resolve",
    "save",
    "set_default",
]
