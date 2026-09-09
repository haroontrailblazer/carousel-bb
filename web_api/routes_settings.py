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

from app.services import avatar_store, secret_box, telegram_config
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
    }


@router.get("/settings/telegram")
async def telegram_status(_identity: Identity = Depends(current_identity)) -> dict:
    """Whether a bot is connected, and which one."""
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


@router.delete("/settings/telegram")
async def telegram_disconnect(
    identity: Identity = Depends(current_identity),
) -> dict:
    """Forget the stored bot. Any .env fallback takes over again."""
    await telegram_config.clear()
    logger.info("Telegram credentials cleared by %s.", identity.email)
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
