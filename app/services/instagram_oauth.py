"""Instagram Login (Business Login for Instagram) - the OAuth half.

**Why this flow and not Facebook Login.** The older path requires the account
to be linked to a Facebook Page, and that link is the step people abandon on.
Instagram Login, which Meta shipped in mid-2024, needs only that the account
is Professional (Business or Creator) - a free toggle in the Instagram app.
It also lives on a different host: ``graph.instagram.com``, not
``graph.facebook.com``.

**Nobody types a password into this console.** The browser is sent to
Instagram's own authorize page; we receive a ``code`` and trade it for a
token. That is the entire reason to prefer OAuth over the private API
libraries that accept a username and password: those violate Meta's terms,
get the *user's* account disabled rather than ours, and would put someone
else's password in our database.

**Why the state parameter is signed rather than stored.** The callback is a
plain GET that anyone on the internet can hit. Without state, a stranger
could walk their own ``code`` through it and attach THEIR Instagram account
to this console. A server-side nonce table would work, but a short-lived JWT
signed with ``SESSION_SECRET`` needs no storage and no cleanup, and it is the
same trick ``web_api.auth`` already uses for the session cookie.

Every function here is SYNCHRONOUS. The calls are short, they happen once per
connection, and the routes run them through ``asyncio.to_thread`` - matching
``app.services.telegram_connect``, which made the same call for the same
reason.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt

logger = logging.getLogger(__name__)

#: Where the browser goes to authorise. This is the www host, not the api one
#: - Meta moved the authorize dialog there and the old address redirects.
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"

#: Where the short-lived token is minted. Still on the api host.
TOKEN_URL = "https://api.instagram.com/oauth/access_token"

#: Everything after the code exchange lives here.
GRAPH_HOST = "https://graph.instagram.com"

#: What we ask for. ``basic`` identifies the account; ``content_publish`` is
#: what actually posts a carousel. Nothing else is requested - the token is a
#: credential, and asking for reach we do not use only widens what a leak
#: would cost.
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")

#: How long a state token stays valid. Long enough to log in and approve on a
#: phone, short enough that a state captured from a browser history or a proxy
#: log is useless by the time anyone reads it.
STATE_TTL_S = 600

#: Same allowance for clock drift as web_api.auth - see the note there on why
#: this is not optional.
CLOCK_SKEW_LEEWAY_S = 60

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class OAuthError(Exception):
    """A step of the connect flow failed, with a code the route can branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def issue_state(email: str, *, secret: str) -> str:
    """Sign a short-lived state token naming who started the connection.

    The email travels in the token so the callback can record who connected
    the account without holding a server-side session between the two
    requests.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "email": email,
            # A nonce so two connections started seconds apart are not the
            # same string - otherwise a state seen once could be replayed for
            # the rest of its lifetime.
            "jti": secrets.token_urlsafe(16),
            "iat": now,
            "exp": now + STATE_TTL_S,
        },
        secret,
        algorithm="HS256",
    )


def read_state(token: str, *, secret: str) -> str:
    """Return the email a state token was issued for.

    Raises:
        OAuthError: for anything we will not accept. Expiry is separated from
            the other failures because it is the one a person can fix by
            simply clicking connect again, and the message should say so.
    """
    if not token:
        raise OAuthError("bad_state", "That connection request carried no state.")
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], leeway=CLOCK_SKEW_LEEWAY_S
        )
    except jwt.ExpiredSignatureError as exc:
        raise OAuthError(
            "state_expired",
            "That connection request took too long. Start it again.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise OAuthError(
            "bad_state",
            "That connection request could not be verified. Start it again.",
        ) from exc
    email = str(claims.get("email") or "")
    if not email:
        raise OAuthError("bad_state", "That connection request names no user.")
    return email


def authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """The Instagram page to send the browser to."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": ",".join(SCOPES),
            "response_type": "code",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


# ---------------------------------------------------------------------------
# HTTP - two seams, so every test above runs without touching the network
# ---------------------------------------------------------------------------
def _post_form(url: str, data: dict, timeout: Any) -> dict:
    response = httpx.post(url, data=data, timeout=timeout)
    return _decode(response)


def _get_json(url: str, params: dict, timeout: Any) -> dict:
    response = httpx.get(url, params=params, timeout=timeout)
    return _decode(response)


def _decode(response: httpx.Response) -> dict:
    """Body as a dict, whatever the status.

    Meta reports failures in the BODY with a useful message and in the status
    with a number, so raising on status alone would throw away the only part
    anyone can act on. ``_raise_for_error`` reads the body instead.
    """
    try:
        payload = response.json()
    except ValueError:
        raise OAuthError(
            "bad_response",
            f"Instagram replied with {response.status_code} and no JSON body.",
        ) from None
    return payload if isinstance(payload, dict) else {}


def _raise_for_error(payload: dict) -> dict:
    """Turn Meta's two different error shapes into one exception.

    The token endpoints answer with ``error_type``/``error_message``; the
    graph endpoints answer with a nested ``error`` object. Both are handled
    here so callers never have to know which one they are talking to.
    """
    message = str(payload.get("error_message") or "").strip()
    if not message:
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(
                error.get("error_user_msg") or error.get("message") or ""
            ).strip()
        elif isinstance(error, str):
            message = error.strip()
    if message:
        raise OAuthError("instagram_error", f"Instagram refused: {message}")
    return payload


# ---------------------------------------------------------------------------
# the exchange
# ---------------------------------------------------------------------------
def exchange_code(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict:
    """Trade the callback's ``code`` for a short-lived (~1 hour) token."""
    payload = _post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        _TIMEOUT,
    )
    _raise_for_error(payload)
    if not payload.get("access_token"):
        raise OAuthError("no_token", "Instagram returned no access token.")
    return payload


def exchange_long_lived(short_lived_token: str, client_secret: str) -> tuple[str, int]:
    """Trade a short-lived token for a 60-day one.

    Returns:
        ``(token, expires_in_seconds)``.
    """
    payload = _get_json(
        f"{GRAPH_HOST}/access_token",
        {
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": short_lived_token,
        },
        _TIMEOUT,
    )
    _raise_for_error(payload)
    token = str(payload.get("access_token") or "")
    if not token:
        raise OAuthError("no_token", "Instagram returned no long-lived token.")
    return token, int(payload.get("expires_in") or 0)


def refresh_long_lived(token: str) -> tuple[str, int]:
    """Extend a long-lived token for another 60 days.

    Only works while the token is still valid; one that has already lapsed
    cannot be revived, and the account has to be connected again.
    """
    payload = _get_json(
        f"{GRAPH_HOST}/refresh_access_token",
        {"grant_type": "ig_refresh_token", "access_token": token},
        _TIMEOUT,
    )
    _raise_for_error(payload)
    fresh = str(payload.get("access_token") or "")
    if not fresh:
        raise OAuthError("no_token", "Instagram returned no refreshed token.")
    return fresh, int(payload.get("expires_in") or 0)


def fetch_identity(token: str) -> dict:
    """Who this token belongs to: id, handle, display name and picture."""
    payload = _get_json(
        f"{GRAPH_HOST}/me",
        {
            "fields": "user_id,username,name,profile_picture_url",
            "access_token": token,
        },
        _TIMEOUT,
    )
    _raise_for_error(payload)
    # ``user_id`` is the id the publishing endpoints want. ``id`` is also
    # present on some responses and is NOT interchangeable, so it is not used
    # as a fallback here - a wrong id would fail much later, at publish time.
    ig_user_id = str(payload.get("user_id") or "")
    if not ig_user_id:
        raise OAuthError(
            "no_identity",
            "Instagram did not identify that account. It must be a "
            "Professional (Business or Creator) account.",
        )
    return {
        "ig_user_id": ig_user_id,
        "username": str(payload.get("username") or ""),
        "name": str(payload.get("name") or ""),
        "profile_picture_url": str(payload.get("profile_picture_url") or ""),
    }


def fetch_avatar(url: str) -> Optional[bytes]:
    """Download a profile picture, or ``None`` if it cannot be had.

    Best-effort on purpose: the picture becomes the favicon on the brand rail,
    but failing to fetch it must not fail the connection - the account is
    already authorised by this point, and a missing picture is a cosmetic
    problem someone can fix by reconnecting.
    """
    if not url:
        return None
    try:
        response = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        if response.status_code != 200:
            logger.warning(
                "Profile picture fetch returned %s.", response.status_code
            )
            return None
        return response.content
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS
        logger.warning("Could not fetch the profile picture: %s", exc)
        return None


__all__ = [
    "AUTHORIZE_URL",
    "CLOCK_SKEW_LEEWAY_S",
    "GRAPH_HOST",
    "SCOPES",
    "STATE_TTL_S",
    "TOKEN_URL",
    "OAuthError",
    "authorize_url",
    "exchange_code",
    "exchange_long_lived",
    "fetch_avatar",
    "fetch_identity",
    "issue_state",
    "read_state",
    "refresh_long_lived",
]
