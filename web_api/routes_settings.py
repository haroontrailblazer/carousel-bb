"""Console settings: connecting the Telegram bot.

Deliberately NOT agent-driven. Connecting a bot is three fixed API calls with
three fixed outcomes; a model in that loop could only add latency, cost and
new ways to be wrong.

The token is a credential, so it is written but never read back: every
response carries a masked form, the bot's @username and whether it is
connected. Anyone who needs the real value already has it - they pasted it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services import avatar_store, secret_box, telegram_config, instagram_config
from app.services.instagram_connect import verify_token as verify_instagram_token
from app.services.telegram_connect import (
    ConnectError,
    discover_chat,
    send_welcome,
    verify_token,
)
from web_api.auth import Identity
from web_api.deps import current_identity

logger = logging.getLogger(__name__)

router = APIRouter()


class InstagramConnectRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096, repr=False)


def _instagram_status() -> dict:
    creds = instagram_config.credentials()
    return {
        "connected": instagram_config.configured(),
        "secrets_ready": secret_box.configured(),
        "user_id": creds["user_id"], "username": creds["username"],
        "connected_at": creds["connected_at"],
    }


@router.get("/settings/instagram")
async def instagram_status(_identity: Identity = Depends(current_identity)) -> dict:
    await instagram_config.load()
    return _instagram_status()


@router.post("/settings/instagram")
async def instagram_connect(payload: InstagramConnectRequest,
                            identity: Identity = Depends(current_identity)) -> dict:
    if not secret_box.configured():
        raise HTTPException(503, {"code": "secrets_unconfigured", "message": "Set SECRETS_KEY before connecting Instagram."})
    try:
        account = await asyncio.to_thread(verify_instagram_token, payload.token.strip())
    except ValueError as exc:
        raise HTTPException(400, {"code": "invalid_token", "message": str(exc)}) from exc
    try:
        await instagram_config.save(
            access_token=payload.token.strip(), **account,
            connected_by=identity.email, connected_at=datetime.now(timezone.utc).isoformat(),
        )
    except ValueError as exc:
        raise HTTPException(409, {"code": "account_connected", "message": str(exc)}) from exc
    return _instagram_status()


@router.delete("/settings/instagram")
async def instagram_disconnect(_identity: Identity = Depends(current_identity)) -> dict:
    await instagram_config.clear()
    return _instagram_status()


class TelegramConnectRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


def _mask(token: str) -> str:
    """``8665967247:AAG...tNg`` - enough to recognise, not enough to use."""
    if not token:
        return ""
    head, _, tail = token.partition(":")
    if not tail:
        return f"{token[:4]}…{token[-3:]}" if len(token) > 10 else "…"
    return f"{head}:{tail[:3]}…{tail[-3:]}"


def _status() -> dict:
    creds = telegram_config.credentials()
    return {
        "secrets_ready": secret_box.configured(),
        "connected": telegram_config.configured(),
        "source": telegram_config.source(),
        "bot_username": creds["bot_username"],
        "chat_id": creds["chat_id"],
        "token_masked": _mask(creds["bot_token"]),
        "connected_by": creds["connected_by"],
        "connected_at": creds["connected_at"],
        "bots": [{
            "bot_id": bot["bot_id"], "bot_username": bot["bot_username"],
            "chat_id": bot["chat_id"], "token_masked": _mask(bot["bot_token"]),
            "connected_by": bot["connected_by"], "connected_at": bot["connected_at"],
        } for bot in telegram_config.all_credentials()],
    }


@router.get("/settings/telegram")
async def telegram_status(_identity: Identity = Depends(current_identity)) -> dict:
    """List connected bots without returning their tokens."""
    await telegram_config.load()
    return _status()


@router.post("/settings/telegram")
async def telegram_connect(
    payload: TelegramConnectRequest,
    identity: Identity = Depends(current_identity),
) -> dict:
    """Verify a bot token, find its chat, say hello, and store it.

    The chat id is discovered rather than asked for, because typing a numeric
    chat id is the step everyone gets wrong. Telegram will only reveal it once
    a human has messaged the bot, so a token that has never been messaged
    comes back as ``no_chat`` with instructions rather than an error - it is a
    step to complete, not a mistake.
    """
    token = payload.token.strip()

    # Every call is blocking httpx inside a request; keep the loop free.
    try:
        bot = await asyncio.to_thread(verify_token, token)
    except ConnectError as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message}) from exc

    try:
        chat_id = await asyncio.to_thread(discover_chat, token)
    except ConnectError as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message}) from exc

    if not chat_id:
        username = bot.get("username") or ""
        raise HTTPException(
            409,
            {
                "code": "no_chat",
                "message": (
                    "The bot is real, but it has never been messaged, so "
                    "Telegram will not say which chat to use. Open "
                    f"t.me/{username} and send it /start, then connect again."
                ),
                "bot_username": username,
            },
        )

    try:
        await asyncio.to_thread(send_welcome, token, chat_id)
    except ConnectError as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message}) from exc

    try:
        await telegram_config.save(
            bot_id=str(bot.get("id") or ""),
            bot_token=token,
            chat_id=chat_id,
            bot_username=str(bot.get("username") or ""),
            connected_by=identity.email,
            connected_at=datetime.now(timezone.utc).isoformat(),
        )
    except secret_box.SecretsNotConfigured as exc:
        # Refuse rather than fall back to storing it in the clear: the whole
        # reason the token moved out of .env was to stop it living in plain
        # text somewhere.
        raise HTTPException(
            503, {"code": "secrets_unconfigured", "message": str(exc)}
        ) from exc
    logger.info(
        "Telegram bot @%s connected to chat %s by %s.",
        bot.get("username"),
        chat_id,
        identity.email,
    )
    return {"result": "connected", **_status()}


@router.delete("/settings/telegram/{bot_id}")
async def telegram_disconnect(
    bot_id: str,
    identity: Identity = Depends(current_identity),
) -> dict:
    """Remove one connected bot."""
    await telegram_config.clear(bot_id)
    logger.info("Telegram bot %s disconnected by %s.", bot_id, identity.email)
    return {"result": "disconnected", **_status()}


@router.post("/profile/avatar")
async def upload_avatar(
    request: Request, identity: Identity = Depends(current_identity)
) -> dict:
    """Store the signed-in person's profile picture.

    The BROWSER compresses before sending - a phone camera photo is several
    megabytes and an avatar is displayed at 56px, so shipping the original
    would waste the upload, the storage and every page load afterwards. This
    end only enforces the ceiling.

    The returned URL is one of ours, not a storage URL: the media bucket is
    private, and a presigned link would expire long before a profile picture
    should.
    """
    payload = await request.body()
    try:
        await avatar_store.save(identity.email, payload)
    except ValueError as exc:
        raise HTTPException(400, {"code": "bad_image", "message": str(exc)}) from exc
    except Exception as exc:
        logger.exception("Storing the avatar for %s failed.", identity.email)
        raise HTTPException(
            502,
            {"code": "storage_error", "message": f"Could not store that image: {exc}"},
        ) from exc

    key = avatar_store.key_for(identity.email)
    digest = key.rsplit("/", 1)[-1].removesuffix(".webp")
    # The cache-buster is what makes a re-upload visible: the URL is otherwise
    # stable per person, so browsers would keep showing the previous face.
    return {"url": f"/api/profile/avatar/{digest}?v={len(payload)}"}


@router.get("/profile/avatar/{digest}")
async def get_avatar(
    digest: str, _identity: Identity = Depends(current_identity)
) -> Response:
    """Serve a stored avatar from the private bucket."""
    if not digest.isalnum() or len(digest) != 64:
        raise HTTPException(404, {"code": "not_found", "message": "No such avatar."})
    try:
        payload = await avatar_store.load(f"{avatar_store.PREFIX}/{digest}.webp")
    except Exception as exc:
        logger.warning("Reading avatar %s failed: %s", digest, exc)
        raise HTTPException(
            502, {"code": "storage_error", "message": "Could not read that image."}
        ) from exc
    if payload is None:
        raise HTTPException(404, {"code": "not_found", "message": "No such avatar."})
    return Response(
        content=payload,
        media_type=avatar_store.CONTENT_TYPE,
        # Private: these are behind the login and must not sit in a shared
        # proxy. Immutable within a version because the URL carries ?v=.
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.delete("/profile/avatar")
async def delete_avatar(identity: Identity = Depends(current_identity)) -> dict:
    """Remove the stored picture; the generated default takes over again."""
    await avatar_store.delete(identity.email)
    return {"result": "removed"}


__all__ = ["router"]
