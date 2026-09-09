"""Runtime Telegram credentials, set from the console and encrypted at rest.

There is exactly ONE source: what someone connected on the profile page,
stored in ``app_config``. The environment fallback that used to exist is gone
on purpose - it meant a bearer token sitting in plaintext in a file and in
every shell that inherited it, and it was a second, invisible source of truth
that could quietly override whatever the console displayed.

The token is stored ENCRYPTED (see ``app.services.secret_box``): app_config is
an ordinary Postgres table, so a database backup or a support session would
otherwise hand over a credential that can post as your bot.

The awkward part is that ``app.tools.telegram_tools`` is SYNCHRONOUS - the
dispatcher calls it through ``asyncio.to_thread`` - so it cannot await a
database read to find out where to send. Hence a small process-level cache:
refreshed at startup and whenever the credentials change, read synchronously
by the tools. Decryption happens on load, so the plaintext exists only in
memory and only in this process.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services import db, secret_box

logger = logging.getLogger(__name__)

#: app_config key. Holds the ENCRYPTED token plus the plain metadata.
CONFIG_KEY = "telegram"

_cache: Optional[dict] = None


def credentials() -> dict:
    """Current bot token / chat id. Empty strings when nothing is connected.

    Synchronous on purpose - see the module docstring.
    """
    cached = _cache or {}
    return {
        "bot_token": str(cached.get("bot_token") or ""),
        "chat_id": str(cached.get("chat_id") or ""),
        "bot_username": str(cached.get("bot_username") or ""),
        "connected_by": str(cached.get("connected_by") or ""),
        "connected_at": str(cached.get("connected_at") or ""),
    }


def configured() -> bool:
    """Whether a review message can actually be sent right now."""
    creds = credentials()
    return bool(creds["bot_token"] and creds["chat_id"])


def source() -> str:
    """Where the live credentials came from - for the console to display."""
    return "console" if configured() else "unset"


def _decode(stored: object) -> Optional[dict]:
    """Turn a stored row into usable credentials, or None."""
    if not isinstance(stored, dict) or not stored:
        return None
    token = secret_box.decrypt(str(stored.get("bot_token_enc") or ""))
    if not token:
        # Either nothing is stored, or it was written under a different
        # SECRETS_KEY. Both mean "not connected" rather than a broken console.
        return None
    return {
        "bot_token": token,
        "chat_id": str(stored.get("chat_id") or ""),
        "bot_username": str(stored.get("bot_username") or ""),
        "connected_by": str(stored.get("connected_by") or ""),
        "connected_at": str(stored.get("connected_at") or ""),
    }


async def load() -> dict:
    """Refresh the cache from the database. Never raises."""
    global _cache
    try:
        stored = await db.get_config(CONFIG_KEY, None)
    except Exception as exc:  # a missing database must not break startup
        logger.warning("Could not load Telegram credentials: %s", exc)
        return credentials()
    _cache = _decode(stored)
    if _cache:
        logger.info("Telegram bot @%s is connected.", _cache.get("bot_username") or "?")
    return credentials()


async def save(
    *,
    bot_token: str,
    chat_id: str,
    bot_username: str = "",
    connected_by: str = "",
    connected_at: str = "",
) -> dict:
    """Encrypt and persist credentials, and update the cache in one step.

    Raises:
        secret_box.SecretsNotConfigured: when SECRETS_KEY is absent. Storing a
            bearer token in the clear is not offered as a fallback.
    """
    global _cache
    encrypted = secret_box.encrypt(bot_token)
    await db.set_config(
        CONFIG_KEY,
        {
            "bot_token_enc": encrypted,
            "chat_id": str(chat_id),
            "bot_username": bot_username,
            "connected_by": connected_by,
            "connected_at": connected_at,
        },
    )
    _cache = {
        "bot_token": bot_token,
        "chat_id": str(chat_id),
        "bot_username": bot_username,
        "connected_by": connected_by,
        "connected_at": connected_at,
    }
    return credentials()


async def clear() -> None:
    """Forget the connected bot."""
    global _cache
    await db.set_config(CONFIG_KEY, {})
    _cache = None


__all__ = [
    "CONFIG_KEY",
    "clear",
    "configured",
    "credentials",
    "load",
    "save",
    "source",
]
