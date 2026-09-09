"""Multiple console-connected Telegram bots, encrypted at rest.

Legacy single-bot rows remain readable. Row locking prevents simultaneous
connections from overwriting each other. Sending reads an in-memory snapshot.
"""
from __future__ import annotations

import logging
from typing import Optional
from app.services import db, secret_box

logger = logging.getLogger(__name__)
CONFIG_KEY = "telegram"
_cache: Optional[dict] = None


def credentials() -> dict:
    """First connection for older callers; broadcasts use all_credentials."""
    cached = _cache or {}
    if "bots" in cached:
        cached = next(iter(cached["bots"]), {})
    return {key: str(cached.get(key) or "") for key in (
        "bot_id", "bot_token", "chat_id", "bot_username", "connected_by", "connected_at",
    )}


def all_credentials() -> list[dict]:
    """Snapshot every usable connection without sharing mutable cache values."""
    cached = _cache or {}
    bots = cached["bots"] if "bots" in cached else [credentials()]
    return [dict(bot) for bot in bots if bot.get("bot_token") and bot.get("chat_id")]


def configured() -> bool:
    """Whether at least one destination can receive messages."""
    return bool(all_credentials())


def source() -> str:
    """The console is the only source of credentials."""
    return "console" if configured() else "unset"


def _stored_bots(stored: object) -> list[dict]:
    """Normalize the encrypted legacy row or current list format."""
    if not isinstance(stored, dict):
        return []
    bots = stored.get("bots", [stored] if stored.get("bot_token_enc") else [])
    return [dict(bot) for bot in bots if isinstance(bot, dict)] if isinstance(bots, list) else []


def _bot_id(stored: dict) -> str:
    """Recover the stable bot ID from old encrypted credentials if necessary."""
    return str(stored.get("bot_id") or secret_box.decrypt(stored.get("bot_token_enc") or "").partition(":")[0])


def _decode(stored: object) -> dict:
    """Decrypt usable bots; never accept plaintext credentials from storage."""
    bots = []
    for entry in _stored_bots(stored):
        token = secret_box.decrypt(str(entry.get("bot_token_enc") or ""))
        if token and entry.get("chat_id"):
            bots.append({
                **{key: str(entry.get(key) or "") for key in (
                    "chat_id", "bot_username", "connected_by", "connected_at",
                )},
                "bot_id": str(entry.get("bot_id") or token.partition(":")[0]),
                "bot_token": token,
            })
    return {"bots": bots}


async def load() -> dict:
    """Refresh connections; a database outage preserves the cached list."""
    global _cache
    try:
        stored = await db.get_config(CONFIG_KEY, None)
    except Exception as exc:
        logger.warning("Could not load Telegram connections: %s", exc)
        return credentials()
    _cache = _decode(stored)
    return credentials()


async def _update(bot_id: str, replacement: Optional[dict]) -> None:
    """Update one bot under a row lock, preserving every other bot."""
    global _cache
    pool = await db.get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO app_config (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                CONFIG_KEY, {"bots": []},
            )
            stored = await connection.fetchval("SELECT value FROM app_config WHERE key = $1 FOR UPDATE", CONFIG_KEY)
            bots = _stored_bots(stored)
            index = next((i for i, bot in enumerate(bots) if _bot_id(bot) == bot_id), None)
            if index is not None:
                if replacement is None:
                    bots.pop(index)
                else:
                    bots[index] = replacement
            elif replacement is not None:
                bots.append(replacement)
            updated = {"bots": bots}
            await connection.execute("UPDATE app_config SET value = $2, updated_at = now() WHERE key = $1", CONFIG_KEY, updated)
    _cache = _decode(updated)


async def save(*, bot_token: str, chat_id: str, bot_id: str = "",
               bot_username: str = "", connected_by: str = "", connected_at: str = "") -> dict:
    """Add a bot or refresh the same bot without duplicating its connection."""
    bot_id = bot_id or bot_token.partition(":")[0]
    await _update(bot_id, {
        "bot_id": bot_id, "bot_token_enc": secret_box.encrypt(bot_token),
        "chat_id": str(chat_id), "bot_username": bot_username,
        "connected_by": connected_by, "connected_at": connected_at,
    })
    return credentials()


async def clear(bot_id: str) -> None:
    """Disconnect only the named bot."""
    await _update(bot_id, None)
