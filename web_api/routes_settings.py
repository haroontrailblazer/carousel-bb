"""Console settings: connecting the Telegram bot and Instagram accounts.

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
from io import BytesIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from PIL import Image
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse

from app.config import settings
from app.services import (
    avatar_store,
    instagram_accounts,
    instagram_oauth,
    secret_box,
    telegram_config,
)
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

# ---------------------------------------------------------------------------
# Instagram: connecting publishing accounts.
#
# Nobody types an Instagram password into this console. The browser is sent to
# Instagram's own authorize page and we receive a code, which is traded for a
# token. That is the whole reason to prefer OAuth over the private-API
# libraries that accept a username and password: those break Meta's terms, get
# the USER's account disabled rather than ours, and would put somebody else's
# password in our database.
#
# Like the Telegram routes above, this is deliberately not agent-driven. It is
# a fixed sequence of HTTP calls with fixed outcomes.
# ---------------------------------------------------------------------------
class AccountRef(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)


def _redirect_uri() -> str:
    """The callback Meta must have allowlisted, absolute."""
    return f"{settings.public_base_url.rstrip('/')}/api/settings/instagram/callback"


def _require_meta_app() -> None:
    """Refuse early when this console has no Meta app credentials.

    Raises:
        HTTPException: 503 with a code the SPA branches on, rather than
            bouncing someone to Instagram only for Meta to reject the
            client_id with an error page nobody can act on.
    """
    if not settings.ig_app_id or not settings.ig_app_secret:
        raise HTTPException(
            503,
            {
                "code": "not_configured",
                "message": (
                    "This console has no Meta app credentials. Set IG_APP_ID "
                    "and IG_APP_SECRET, then restart."
                ),
            },
        )
    if not settings.public_base_url:
        raise HTTPException(
            503,
            {
                "code": "no_public_url",
                "message": (
                    "PUBLIC_BASE_URL is not set, so there is no absolute "
                    "redirect URI to hand Instagram. Set it to this service's "
                    "public URL and allowlist "
                    "<PUBLIC_BASE_URL>/api/settings/instagram/callback in the "
                    "Meta app."
                ),
            },
        )


def _instagram_status() -> dict:
    """Connected accounts plus whether connecting is possible at all."""
    return {
        "app_configured": bool(settings.ig_app_id and settings.ig_app_secret),
        "public_base_url_set": bool(settings.public_base_url),
        "secrets_ready": secret_box.configured(),
        "redirect_uri": _redirect_uri() if settings.public_base_url else "",
        "accounts": instagram_accounts.listing(),
    }


def _back_to_profile(**params: str) -> RedirectResponse:
    """Send the browser back to the profile page carrying an outcome.

    A redirect rather than a JSON body because the callback is a NAVIGATION -
    the person is looking at Instagram's page and expects to land back in the
    console, not at a wall of JSON.
    """
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"/profile?{query}", status_code=302)


async def _store_avatar(ig_user_id: str, payload: bytes) -> str:
    """Put an account's profile picture in the bucket; return its key.

    Re-encoded to PNG rather than stored as fetched: the bytes become the
    favicon on every slide's brand rail, and normalising here means the render
    path never has to guess a format. Best effort - a picture that cannot be
    decoded leaves the account without one, and the rail draws a monogram.
    """
    try:
        with Image.open(BytesIO(payload)) as source:
            buffer = BytesIO()
            source.convert("RGBA").save(buffer, format="PNG")
        return await avatar_store.save_at(
            f"instagram/{ig_user_id}.png", buffer.getvalue(), "image/png"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not store the profile picture: %s", exc)
        return ""


@router.get("/settings/instagram")
async def instagram_status(_identity: Identity = Depends(current_identity)) -> dict:
    """Which accounts are connected, and whether more can be."""
    return _instagram_status()


@router.get("/settings/instagram/authorize")
async def instagram_authorize(
    identity: Identity = Depends(current_identity),
) -> RedirectResponse:
    """Send the browser to Instagram to authorise an account."""
    _require_meta_app()
    state = instagram_oauth.issue_state(
        identity.email, secret=settings.session_secret
    )
    url = instagram_oauth.authorize_url(
        client_id=settings.ig_app_id,
        redirect_uri=_redirect_uri(),
        state=state,
    )
    logger.info("Instagram connect started by %s.", identity.email)
    return RedirectResponse(url)


@router.get("/settings/instagram/callback")
async def instagram_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
) -> RedirectResponse:
    """Finish the connection Instagram is redirecting back from.

    There is no ``Depends(current_identity)`` parameter here on purpose: who
    connected is read from the SIGNED STATE, which is the only thing that ties
    this GET to a flow this console started. The route still sits behind the
    auth middleware, so a session is required as well - the state is what
    stops a stranger walking their own code through it, and the middleware is
    what stops an anonymous request reaching it at all.
    """
    if error:
        # Somebody pressed Cancel on Instagram's page. Not a fault - report it
        # as an outcome and let the profile page say so quietly.
        logger.info("Instagram connect declined: %s (%s)", error, error_description)
        return _back_to_profile(instagram_error=error)

    try:
        connected_by = instagram_oauth.read_state(
            state, secret=settings.session_secret
        )
    except instagram_oauth.OAuthError as exc:
        logger.warning("Instagram callback refused: %s", exc.code)
        return _back_to_profile(instagram_error=exc.code)

    if not code:
        return _back_to_profile(instagram_error="no_code")

    try:
        _require_meta_app()
    except HTTPException as exc:
        return _back_to_profile(instagram_error=str(exc.detail.get("code", "error")))

    # Three blocking HTTP calls inside a request; keep the loop free.
    try:
        short = await asyncio.to_thread(
            lambda: instagram_oauth.exchange_code(
                code=code,
                client_id=settings.ig_app_id,
                client_secret=settings.ig_app_secret,
                redirect_uri=_redirect_uri(),
            )
        )
        token, expires_in = await asyncio.to_thread(
            instagram_oauth.exchange_long_lived,
            str(short.get("access_token") or ""),
            settings.ig_app_secret,
        )
        identity_payload = await asyncio.to_thread(
            instagram_oauth.fetch_identity, token
        )
    except instagram_oauth.OAuthError as exc:
        logger.warning("Instagram token exchange failed: %s", exc.message)
        return _back_to_profile(instagram_error=exc.code, detail=exc.message)

    avatar_key = ""
    picture_url = identity_payload.get("profile_picture_url") or ""
    if picture_url:
        picture = await asyncio.to_thread(instagram_oauth.fetch_avatar, picture_url)
        if picture:
            avatar_key = await _store_avatar(
                identity_payload["ig_user_id"], picture
            )

    try:
        account = await instagram_accounts.save(
            ig_user_id=identity_payload["ig_user_id"],
            username=identity_payload["username"],
            name=identity_payload["name"],
            token=token,
            expires_in=expires_in,
            connected_by=connected_by,
            avatar_key=avatar_key,
        )
    except secret_box.SecretsNotConfigured:
        # Refuse rather than fall back to storing it in the clear - the whole
        # reason these tokens live in the database is that they are encrypted
        # there.
        logger.error("Instagram token not stored: SECRETS_KEY is not set.")
        return _back_to_profile(instagram_error="secrets_unconfigured")

    logger.info(
        "Instagram account @%s connected by %s.", account.username, connected_by
    )
    return _back_to_profile(instagram="connected", account=account.username)


@router.get("/settings/instagram/{account_id}/avatar")
async def instagram_avatar(
    account_id: str, _identity: Identity = Depends(current_identity)
) -> Response:
    """Serve a connected account's profile picture from the private bucket.

    Served by us rather than linked from Meta's CDN: ``profile_picture_url``
    is a short-lived signed URL that would be dead by the time anyone loaded
    the profile page.
    """
    account = instagram_accounts.get(account_id)
    if account is None or not account.avatar_key:
        raise HTTPException(404, {"code": "not_found", "message": "No such picture."})
    try:
        payload = await avatar_store.load(account.avatar_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reading the picture for %s failed: %s", account_id, exc)
        raise HTTPException(
            502, {"code": "storage_error", "message": "Could not read that image."}
        ) from exc
    if payload is None:
        raise HTTPException(404, {"code": "not_found", "message": "No such picture."})
    return Response(
        content=payload,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/settings/instagram/default")
async def instagram_set_default(
    payload: AccountRef, identity: Identity = Depends(current_identity)
) -> dict:
    """Choose which account new runs target unless told otherwise."""
    await instagram_accounts.set_default(payload.account_id)
    logger.info(
        "Instagram default set to %s by %s.", payload.account_id, identity.email
    )
    return {"result": "updated", **_instagram_status()}


@router.delete("/settings/instagram/{account_id}")
async def instagram_disconnect(
    account_id: str, identity: Identity = Depends(current_identity)
) -> dict:
    """Forget an account. Runs already made for it keep their account_id.

    Those runs cannot publish afterwards, and the publisher says exactly that
    rather than silently posting somewhere else.
    """
    await instagram_accounts.delete(account_id)
    logger.info("Instagram account %s disconnected by %s.", account_id, identity.email)
    return {"result": "disconnected", **_instagram_status()}


__all__ = ["router"]

