"""Telegram tools - review-request and publish-confirmation messages.

The review channel for the Carousel Factory, and the only one. Used by the
Review Dispatcher agent (:func:`send_review_message`) and the Publisher agent
(:func:`send_confirmation_message`).

It replaced a Gmail-based review mail, and was chosen because the setup is a
bot token and a chat id - no Google Cloud project, no OAuth consent screen, no
browser round trip, and no 7-day refresh-token expiry to re-do every week.
Nothing in this console sends mail any more; ``app/tools/gmail_tools.py`` and
the Gmail newsletter source in the fetcher have both been removed.

The review button is an inline keyboard **URL** button pointing at the console's
own review screen, which is behind the login. URL buttons are handled entirely
client-side, so nothing here needs a webhook or ``getUpdates`` polling - the
human-in-the-loop resume protocol is untouched.

Nothing in this module touches the network at import time.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services import telegram_config

logger = logging.getLogger(__name__)

# (connect, read) budget for every Bot API call - never hang the pipeline.
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

#: Bot API hard limits.
MEDIA_GROUP_LIMIT = 10  # media per sendMediaGroup call
MESSAGE_LIMIT = 4096  # characters per sendMessage


def _api_base() -> str:
    """Bot API base URL, or a RuntimeError naming what to configure.

    Credentials come from ``app.services.telegram_config`` and nowhere else -
    there is no environment fallback. The bot is connected on the profile
    page, and the token is stored encrypted rather than sitting in plaintext
    in a file.
    """
    token = telegram_config.credentials()["bot_token"]
    if not token:
        raise RuntimeError(
            "No Telegram bot is connected - cannot send the review message. "
            "Connect one from the console: Account -> Profile -> Telegram."
        )
    return f"https://api.telegram.org/bot{token}"


def _chat_id() -> str:
    """Destination chat; fail loudly if none configured."""
    chat_id = telegram_config.credentials()["chat_id"]
    if not chat_id:
        raise RuntimeError(
            "No Telegram chat is connected - cannot send the review message. "
            "Connect the bot from the console's profile page, which discovers "
            "the chat id for you."
        )
    return chat_id


def _request(
    client: httpx.Client,
    method: str,
    *,
    data: Optional[dict] = None,
    files: Optional[dict] = None,
) -> dict[str, Any]:
    """POST one Bot API method and return its ``result`` object.

    Telegram reports failures as ``{"ok": false, "description": ...}``. That
    envelope is checked before ``raise_for_status`` because the description is
    the only actionable part - the HTTP status alone says nothing useful.

    Raises:
        RuntimeError: on an API-level error or an unreadable body.
    """
    response = client.post(f"/{method}", data=data or {}, files=files)
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict) and not payload.get("ok", False):
        raise RuntimeError(
            f"Telegram API error on {method}: "
            f"{payload.get('description') or 'unknown error'} "
            f"(error_code={payload.get('error_code')})"
        )

    response.raise_for_status()

    if not isinstance(payload, dict):
        body = response.text[:500] if response.text else "(empty body)"
        raise RuntimeError(f"Telegram returned a non-JSON body on {method}: {body}")
    result = payload.get("result")
    return result if isinstance(result, dict) else {"result": result}


def _preview_paths(bundle: dict) -> list[Path]:
    """Local preview files from ``bundle["preview_paths"]``, poster first.

    Accepts the same two shapes the mail path did: a plain list (first entry is
    the poster/cover frame) or ``{"poster": path, "slides": [paths...]}``.
    Missing files are logged and skipped, so a partial preview still sends.
    """
    raw = bundle.get("preview_paths") or []
    candidates: list[Path] = []

    if isinstance(raw, dict):
        poster = raw.get("poster") or raw.get("cover")
        if poster:
            candidates.append(Path(str(poster)))
        candidates.extend(Path(str(p)) for p in (raw.get("slides") or []))
    else:
        candidates.extend(Path(str(p)) for p in raw)

    existing: list[Path] = []
    for path in candidates:
        if path.is_file():
            existing.append(path)
        else:
            logger.warning("Preview missing on disk, skipping: %s", path)
    return existing


def _console_review_url(run_id: str) -> str:
    """The console's own review screen for this run.

    Both Telegram buttons open THIS, not the standalone review pages under
    ``/review-api``. Two reasons, and the second is the important one:

    * The console screen shows the actual carousel - the cover video and the
      still side by side, every slide at full size, the caption with its
      character count against Instagram's limit. The standalone page could
      only show a confirmation prompt. Approving is a decision about the
      artwork, so the artwork should be on screen when it is made.
    * It is behind the login. The ``/review-api`` pages deliberately were not,
      because a Telegram link opens where nobody can sign in - which meant
      anyone holding the URL could approve, and approving auto-publishes to
      Instagram. Sending reviewers to the console makes an identity mandatory
      before anything is posted publicly.

    PUBLIC_BASE_URL is the only source. There used to be a fallback to
    REVIEW_API_BASE_URL's origin, but that setting named a service that no
    longer exists; guessing an origin from it would produce a link that looks
    configured and is not. With PUBLIC_BASE_URL unset the URL is relative,
    _buttons_supported rejects it, and the caller logs what to set.
    """
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/tasks/{run_id}?tab=review"


def _buttons_supported(url: str) -> bool:
    """Whether Telegram will accept this URL in an inline keyboard button.

    Telegram validates button URLs server-side and rejects anything that is not
    publicly resolvable ("Bad Request: ... is invalid: Wrong HTTP URL"), which
    would otherwise fail the whole review message and strand the run.

    A local address is useless in a button anyway: the link opens on the
    reviewer's phone, where ``localhost`` is the phone itself. So when
    ``PUBLIC_BASE_URL`` points somewhere local, the link goes in the message
    text instead - visible and copyable, just not tappable. Expose the console
    on a public URL (a tunnel, or a deployed host) to get a real button.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost", "::1") or host.endswith(".local"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname rather than a literal IP - assume it resolves publicly.
        return True


def _send_album(client: httpx.Client, chat_id: str, paths: list[Path]) -> int:
    """Upload previews as one photo album.

    Telegram caps an album at :data:`MEDIA_GROUP_LIMIT`; a full carousel
    (cover + up to 10 slides + CTA) can exceed that, so the extras are dropped
    here and the message below says so rather than failing the send.

    Returns:
        How many images were actually uploaded.
    """
    usable = paths[:MEDIA_GROUP_LIMIT]
    if not usable:
        return 0

    with ExitStack() as stack:
        media: list[dict] = []
        files: dict[str, tuple] = {}
        for index, path in enumerate(usable):
            key = f"file{index}"
            handle = stack.enter_context(path.open("rb"))
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            files[key] = (path.name, handle, mime)
            media.append({"type": "photo", "media": f"attach://{key}"})
        _request(
            client,
            "sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
        )
    return len(usable)


def send_review_message(run_id: str, bundle: dict, round_no: int) -> dict:
    """Send the reviewers a carousel preview with Approve/Reject buttons.

    Args:
        run_id: Pipeline run id - becomes part of the review URLs.
        bundle: The assembled ``Bundle`` as a dict. Must additionally carry
            ``preview_paths``: local file paths of the poster frame and the
            slide PNGs (list, poster first; or ``{"poster": ..., "slides":
            [...]}``). These are uploaded as a photo album.
        round_no: 1-based review round number, shown in the message.

    Returns:
        ``{"message_id": <telegram message id>, "previews_sent": int}``.

    Raises:
        RuntimeError: if the bot token or chat id is missing, or the API
            rejects the call.
    """
    chat_id = _chat_id()
    cover = bundle.get("cover") or {}
    news_title = (
        bundle.get("news_title") or cover.get("title") or "Untitled carousel"
    )
    caption = (bundle.get("caption") or "").strip()
    review_url = _console_review_url(run_id)
    previews = _preview_paths(bundle)

    with httpx.Client(base_url=_api_base(), timeout=_TIMEOUT) as client:
        # Album first so the slides sit above the decision prompt in the chat,
        # which is how the review mail read.
        sent_previews = _send_album(client, chat_id, previews)

        lines = [f"Carousel review needed - round {int(round_no)}", news_title]
        if caption:
            lines += ["", "Caption:", caption]
        if len(previews) > sent_previews:
            lines += [
                "",
                f"Showing {sent_previews} of {len(previews)} images "
                f"(Telegram allows {MEDIA_GROUP_LIMIT} per album).",
            ]
        use_buttons = _buttons_supported(review_url)
        if use_buttons:
            lines += [
                "",
                "Open the review screen to approve or reject.",
                "Sign-in required - approving publishes to Instagram.",
            ]
        else:
            # Telegram refuses a non-public URL in a button, which would fail
            # the whole message and strand the paused run. Put the link in the
            # body instead so the carousel is still reviewable.
            logger.warning(
                "PUBLIC_BASE_URL (%s) is not publicly reachable, so Telegram "
                "cannot render a Review button; sending a plain link instead. "
                "Expose the console publicly for one-tap review.",
                settings.public_base_url or "(unset)",
            )
            lines += ["", f"Review and decide: {review_url}"]

        text = "\n".join(lines)
        if len(text) > MESSAGE_LIMIT:
            text = text[: MESSAGE_LIMIT - 3].rstrip() + "..."

        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            # Plain text on purpose: a news headline can contain _ * [ or `
            # and would break Markdown parsing for no benefit here.
            "disable_web_page_preview": "true",
        }
        if use_buttons:
            data["reply_markup"] = json.dumps(
                {
                    # One button, not two. Approve and Reject both had to
                    # land on the same screen now that the decision is made in
                    # the console, and two buttons opening the identical page
                    # would imply the choice was already made by tapping.
                    "inline_keyboard": [
                        [{"text": "REVIEW CAROUSEL", "url": review_url}],
                    ]
                }
            )
        result = _request(client, "sendMessage", data=data)

    message_id = str(result.get("message_id", ""))
    logger.info(
        "Review message sent for run %s round %s (%d preview(s), message %s)",
        run_id,
        round_no,
        sent_previews,
        message_id,
    )
    return {"message_id": message_id, "previews_sent": sent_previews}


def send_confirmation_message(run_id: str, ig_permalink: str) -> dict:
    """Tell the reviewers the carousel was published to Instagram.

    Args:
        run_id: Pipeline run id, for traceability.
        ig_permalink: Public Instagram permalink of the published carousel.

    Returns:
        ``{"message_id": <telegram message id>}``.
    """
    chat_id = _chat_id()
    text = f"Published to Instagram\n{ig_permalink}\n\nRun {run_id}"
    data: dict[str, Any] = {"chat_id": chat_id, "text": text[:MESSAGE_LIMIT]}
    if _buttons_supported(ig_permalink):
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "VIEW ON INSTAGRAM", "url": ig_permalink}]]}
        )
    with httpx.Client(base_url=_api_base(), timeout=_TIMEOUT) as client:
        result = _request(client, "sendMessage", data=data)
    return {"message_id": str(result.get("message_id", ""))}


__all__ = [
    "MEDIA_GROUP_LIMIT",
    "MESSAGE_LIMIT",
    "send_confirmation_message",
    "send_review_message",
]
