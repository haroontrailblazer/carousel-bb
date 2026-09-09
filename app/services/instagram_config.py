"""One optional Instagram Login account, with its token encrypted at rest."""

from app.services import db, secret_box

CONFIG_KEY = "instagram"
_cache: dict = {}


def credentials() -> dict:
    """Return a copy of the single account's runtime credentials."""
    return {key: str(_cache.get(key) or "") for key in (
        "user_id", "username", "access_token", "connected_by", "connected_at",
    )}


def configured() -> bool:
    """Whether an account and its decrypted token are available."""
    creds = credentials()
    return bool(creds["user_id"] and creds["access_token"])


async def load() -> dict:
    """Refresh from the authoritative row; errors propagate rather than hide it."""
    global _cache
    stored = await db.get_config(CONFIG_KEY, {})
    stored = stored if isinstance(stored, dict) else {}
    token = secret_box.decrypt(str(stored.get("access_token_enc") or ""))
    _cache = {**stored, "access_token": token} if token else {}
    return credentials()


async def save(*, access_token: str, user_id: str, username: str,
               connected_by: str, connected_at: str) -> dict:
    """Store one account atomically; reject replacement until disconnected."""
    global _cache
    stored = dict(user_id=user_id, username=username, connected_by=connected_by,
                  connected_at=connected_at,
                  access_token_enc=secret_box.encrypt(access_token))
    pool = await db.get_pool()
    # The single primary-key row and conditional upsert also enforce the
    # account limit across simultaneous requests and separate web workers.
    result = await pool.fetchval("""
        INSERT INTO app_config (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = now()
        WHERE COALESCE(app_config.value->>'user_id', '') IN ('', $3)
        RETURNING key
    """, CONFIG_KEY, stored, user_id)
    if result is None:
        raise ValueError("Disconnect the current Instagram account before connecting another.")
    _cache = {**stored, "access_token": access_token}
    return credentials()


async def clear() -> None:
    """Remove the connection from storage and this process."""
    global _cache
    await db.set_config(CONFIG_KEY, {})
    _cache = {}
