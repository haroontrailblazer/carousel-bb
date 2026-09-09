"""Instagram Graph API publishing tool.

Publishes the reviewed carousel bundle to Instagram via the Content Publishing
API (Graph API version ``settings.ig_api_version``):

1. Create one child media container per public URL - the FIRST url is the
   cover VIDEO (``media_type=VIDEO`` + ``video_url``), the rest are images
   (``image_url``); every child is created with ``is_carousel_item=true``.
2. Poll each child container's ``status_code`` until ``FINISHED`` (bounded
   retries; video transcoding is asynchronous on Instagram's side).
3. Create the parent container with ``media_type=CAROUSEL``, the ordered
   ``children`` ids and the caption, and poll it to ``FINISHED`` as well.
4. ``POST /{ig_user_id}/media_publish`` with the parent container id.
5. ``GET /{media_id}?fields=permalink`` and return both ids.

Credentials come from the ACCOUNT the run was started against - see
:class:`PublishTarget` and :mod:`app.services.instagram_accounts`. They used
to be one id and one token read from the environment, which meant a single
publishing target for the whole console; with several accounts connectable,
the id, the token AND the Graph host are all properties of the account. Only
the API version is still configuration.

Every HTTP call carries an explicit timeout, and any Graph API error payload
is raised as a ``RuntimeError`` that includes Instagram's error message.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

import httpx

from app.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.instagram_accounts import Account

# NOTE: there is no module-level Graph host any more. Which host to use is a
# property of the TOKEN: one minted by Instagram Login works only against
# graph.instagram.com, one from the older Facebook Login path only against
# graph.facebook.com. Sending a token to the wrong host fails with a
# permissions error that reads like a missing scope and is not one, so the
# host travels with the credentials in PublishTarget.

# Explicit network timeouts (seconds) for every request.
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# Container status polling: video children can take a while to transcode.
_POLL_INTERVAL_S = 5.0
_MAX_POLL_ATTEMPTS = 60  # 60 * 5 s = up to 5 minutes per container

# Instagram requires 2-10 children in a carousel.
_MIN_CAROUSEL_CHILDREN = 2

_TERMINAL_FAILURE_STATUSES = {"ERROR", "EXPIRED"}


@dataclass(frozen=True)
class PublishTarget:
    """Where a carousel is being published, and with whose credentials.

    Built from a connected account rather than read from settings, so two runs
    against two different accounts cannot pick up each other's token.
    """

    ig_user_id: str
    access_token: str
    graph_host: str
    api_version: str

    @classmethod
    def from_account(cls, account: "Account") -> "PublishTarget":
        """Build a target from a connected account.

        Raises:
            ValueError: when the account cannot publish - no id, or a token
                that is missing, lapsed or unreadable. Refusing here keeps the
                failure at the very start of the publish, before any container
                has been created and while stopping is still free.
        """
        if not account.ig_user_id:
            raise ValueError("That Instagram account has no user id.")
        if not account.token:
            raise ValueError(
                f"The stored token for {account.handle or account.id} cannot "
                f"be used. Reconnect the account from the profile page."
            )
        return cls(
            ig_user_id=account.ig_user_id,
            access_token=account.token,
            graph_host=account.graph_host,
            api_version=settings.ig_api_version,
        )

    @property
    def api_base(self) -> str:
        """The versioned Graph API base URL (no trailing slash)."""
        return f"{self.graph_host}/{self.api_version}"


def _extract_error_message(payload: Any) -> Optional[str]:
    """Pull a human-readable error message out of a Graph API JSON payload.

    Graph API errors look like ``{"error": {"message": ..., "type": ...,
    "code": ..., "error_subcode": ..., "error_user_msg": ...}}``. Returns
    ``None`` when the payload carries no error object.
    """
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    parts: list[str] = []
    message = error.get("message")
    if message:
        parts.append(str(message))
    user_msg = error.get("error_user_msg")
    if user_msg and user_msg != message:
        parts.append(str(user_msg))
    code = error.get("code")
    subcode = error.get("error_subcode")
    if code is not None:
        code_txt = f"code={code}"
        if subcode is not None:
            code_txt += f", subcode={subcode}"
        parts.append(f"({code_txt})")
    return " ".join(parts) if parts else "Unknown Instagram Graph API error"


def _graph_request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    target: Optional[PublishTarget] = None,
) -> dict[str, Any]:
    """Issue one Graph API request and return the decoded JSON object.

    Raises :class:`RuntimeError` with Instagram's own error message when the
    response carries an ``error`` payload (regardless of HTTP status), and
    falls back to ``raise_for_status`` for non-JSON failures.
    """
    if target is None:
        raise RuntimeError("A Graph API call was made with no publish target.")
    url = f"{target.api_base}{path}"
    response = client.request(method, url, params=params, data=data)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if payload is not None:
        error_message = _extract_error_message(payload)
        if error_message is not None:
            raise RuntimeError(
                f"Instagram Graph API error on {method} {path}: {error_message}"
            )
    response.raise_for_status()
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Instagram Graph API returned a non-JSON-object response on "
            f"{method} {path}: {response.text[:500]}"
        )
    return payload


#: Extensions Instagram treats as video in a carousel child container.
_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v")


def _is_video_url(public_url: str) -> bool:
    """Whether a signed URL points at a video.

    Presigned URLs carry a query string, so the extension has to be read from
    the path alone - ``...cover.mp4?X-Amz-Signature=...`` must still register
    as a video.
    """
    from urllib.parse import urlparse

    path = urlparse(public_url).path.lower()
    return path.endswith(_VIDEO_SUFFIXES)


def _create_child_container(
    client: httpx.Client,
    public_url: str,
    *,
    is_video: bool,
    target: PublishTarget,
) -> str:
    """Create one carousel child container and return its container id."""
    data: dict[str, Any] = {
        "is_carousel_item": "true",
        "access_token": target.access_token,
    }
    if is_video:
        data["media_type"] = "VIDEO"
        data["video_url"] = public_url
    else:
        data["image_url"] = public_url
    payload = _graph_request(
        client, "POST", f"/{target.ig_user_id}/media", data=data, target=target
    )
    container_id = payload.get("id")
    if not container_id:
        raise RuntimeError(
            f"Instagram did not return a container id for child media "
            f"(url={public_url}): {payload}"
        )
    return str(container_id)


class PublishUncertain(Exception):
    """The publish request failed in a way that cannot be undone or retried.

    Every other failure in this module happens BEFORE anything is public, so
    trying again is free. This one is different: the POST to
    ``media_publish`` left the client, and a transport-level failure - a read
    timeout, a dropped connection - says nothing about whether Instagram
    accepted it. The carousel may be live.

    Retrying is the one thing that must not happen, because the retry builds
    fresh containers and publishes a SECOND identical carousel to a real
    account. A missed post is recoverable and visible; a duplicate is neither.

    Carries ``creation_id`` so an operator can reconcile against the account.
    """

    def __init__(self, creation_id: str, cause: str) -> None:
        super().__init__(
            f"The carousel was sent to Instagram but the reply was lost "
            f"({cause}). It MAY already be live - check the account before "
            f"doing anything else. Container id: {creation_id}"
        )
        self.creation_id = creation_id


class PublishAborted(Exception):
    """The caller withdrew consent part-way through a publish.

    Raised only from the checkpoints below, each of which sits BEFORE an
    irreversible Graph API call. Nothing has been posted when this is raised.
    """


def _still_wanted(should_continue: Optional[Callable[[], bool]]) -> None:
    """Abort unless the caller still wants this publish to happen.

    Uploading a carousel is a sequence of slow steps - create each child
    container, wait for the video to transcode, create the parent, wait again,
    then publish - and the whole thing runs in a worker thread that
    ``task.cancel()`` cannot interrupt. Without a check between the steps,
    pressing Stop during a publish stops nothing: the post appears anyway, on a
    carousel someone had already abandoned.

    Raises:
        PublishAborted: when the caller says stop.
    """
    if should_continue is not None and not should_continue():
        raise PublishAborted("The run was stopped before the carousel was posted.")


def _wait_until_finished(
    client: httpx.Client,
    container_id: str,
    should_continue: Optional[Callable[[], bool]] = None,
    *,
    target: PublishTarget,
) -> None:
    """Poll a media container until its ``status_code`` is ``FINISHED``.

    Bounded to ``_MAX_POLL_ATTEMPTS`` polls, ``_POLL_INTERVAL_S`` seconds
    apart. Raises :class:`RuntimeError` on ``ERROR``/``EXPIRED`` statuses or
    when the container never finishes within the polling budget.
    """
    last_status = "UNKNOWN"
    for attempt in range(1, _MAX_POLL_ATTEMPTS + 1):
        payload = _graph_request(
            client,
            "GET",
            f"/{container_id}",
            params={
                "fields": "status_code,status",
                "access_token": target.access_token,
            },
            target=target,
        )
        last_status = str(payload.get("status_code", "UNKNOWN"))
        if last_status == "FINISHED":
            return
        if last_status in _TERMINAL_FAILURE_STATUSES:
            detail = payload.get("status", "")
            raise RuntimeError(
                f"Instagram media container {container_id} failed with status "
                f"{last_status}: {detail}"
            )
        if attempt < _MAX_POLL_ATTEMPTS:
            _still_wanted(should_continue)
            time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        f"Instagram media container {container_id} did not reach FINISHED "
        f"after {_MAX_POLL_ATTEMPTS} polls "
        f"({_MAX_POLL_ATTEMPTS * _POLL_INTERVAL_S:.0f} s); "
        f"last status_code={last_status}"
    )


def _create_parent_container(
    client: httpx.Client,
    child_ids: list[str],
    caption: str,
    target: PublishTarget,
) -> str:
    """Create the CAROUSEL parent container and return its id."""
    payload = _graph_request(
        client,
        "POST",
        f"/{target.ig_user_id}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": target.access_token,
        },
        target=target,
    )
    parent_id = payload.get("id")
    if not parent_id:
        raise RuntimeError(
            f"Instagram did not return a container id for the carousel "
            f"parent: {payload}"
        )
    return str(parent_id)


def publish_carousel(
    bundle: dict,
    public_urls: list[str],
    should_continue: Optional[Callable[[], bool]] = None,
    *,
    account: Optional["Account"] = None,
) -> dict:
    """Publish the approved carousel to Instagram and return its identity.

    Args:
        bundle: The assembled ``Bundle`` as a plain dict (see
            :class:`app.schemas.Bundle`); only ``caption`` is read here - the
            media itself arrives through ``public_urls``.
        public_urls: Publicly reachable HTTPS URLs for every slide in order.
            The FIRST url must be the cover VIDEO (mp4); all remaining urls
            are slide images (PNG/JPEG, 1080x1350).
        should_continue: Asked before each irreversible step. Return False to
            abandon the publish; nothing will have been posted. This runs in a
            worker thread, so it must be cheap and thread-safe.
        account: The connected Instagram account to publish to - the one the
            run was started against. Required; there is no default, because
            guessing would post to the wrong brand.

    Returns:
        ``{"media_id": <published IG media id>, "permalink": <post url>}``.

    Raises:
        ValueError: When ``public_urls`` is out of Instagram's allowed range
            (2 to ``settings.max_carousel_slides`` items), or when no usable
            account was given.
        RuntimeError: When the Graph API returns an error payload, or a
            container fails or times out processing.
        PublishAborted: When ``should_continue`` said to stop. Nothing was
            posted.
    """
    if len(public_urls) > settings.max_carousel_slides:
        raise ValueError(
            f"Carousel has {len(public_urls)} slides; Instagram allows at "
            f"most {settings.max_carousel_slides}."
        )
    if len(public_urls) < _MIN_CAROUSEL_CHILDREN:
        raise ValueError(
            f"Carousel needs at least {_MIN_CAROUSEL_CHILDREN} slides; "
            f"got {len(public_urls)}."
        )
    if account is None:
        raise ValueError(
            "No Instagram account was given for this publish. A run must be "
            "started against a connected account; connect one from Profile -> "
            "Instagram."
        )
    target = PublishTarget.from_account(account)

    caption = str(bundle.get("caption", "") or "")

    with httpx.Client(timeout=_TIMEOUT) as client:
        # (1) Child containers, typed by what each URL actually is.
        #
        # This used to assume the first item was always a video, because the
        # cover always was. It no longer is: a reviewer can choose to publish
        # the cover STILL instead of the clip. Sending a PNG with
        # media_type=VIDEO makes Instagram reject the whole carousel, so the
        # decision has to come from the file rather than its position.
        child_ids: list[str] = []
        for url in public_urls:
            _still_wanted(should_continue)
            child_ids.append(
                _create_child_container(
                    client, url, is_video=_is_video_url(url), target=target
                )
            )

        # (2) Wait for every child (video transcode is asynchronous).
        for child_id in child_ids:
            _wait_until_finished(client, child_id, should_continue, target=target)

        # (3) Parent CAROUSEL container; poll it too before publishing.
        _still_wanted(should_continue)
        parent_id = _create_parent_container(client, child_ids, caption, target)
        _wait_until_finished(client, parent_id, should_continue, target=target)

        # (4) Publish. The last checkpoint - after this the post is live and
        # no amount of stopping takes it back.
        _still_wanted(should_continue)
        try:
            publish_payload = _graph_request(
                client,
                "POST",
                f"/{target.ig_user_id}/media_publish",
                data={
                    "creation_id": parent_id,
                    "access_token": target.access_token,
                },
                target=target,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The request left; the answer did not come back. Whether the
            # carousel is live is genuinely unknown from here, and nothing in
            # the Graph API lets us ask "did creation_id already publish?"
            # cheaply. So this is raised as its own type, which the publisher
            # agent is told never to retry - a duplicate post cannot be taken
            # back, a missing one can be published again on purpose.
            raise PublishUncertain(str(parent_id), type(exc).__name__) from exc
        media_id = publish_payload.get("id")
        if not media_id:
            raise RuntimeError(
                f"Instagram media_publish returned no media id: "
                f"{publish_payload}"
            )
        media_id = str(media_id)

        # (5) Permalink of the published post.
        permalink_payload = _graph_request(
            client,
            "GET",
            f"/{media_id}",
            params={
                "fields": "permalink",
                "access_token": target.access_token,
            },
            target=target,
        )
        permalink = str(permalink_payload.get("permalink", ""))

    return {"media_id": media_id, "permalink": permalink}
