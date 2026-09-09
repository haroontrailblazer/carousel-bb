"""Gmail OAuth for the newsletter fetcher.

What is left here is the credential half of the old Gmail integration:
``fetcher.fetch_news`` reads a newsletter inbox and reuses these helpers so
both paths share one cached token.

This module used to also SEND mail - a review request with Approve/Reject
buttons, and a publish confirmation. Both are gone. They were unreachable (no
agent registered them; review notifications go out over Telegram), and their
buttons pointed at the standalone review pages, which were deleted because an
anonymous URL that publishes to Instagram is not a thing to leave lying about.
Anything that replaces them should link to the console's review screen, which
is behind the login.

Auth is the OAuth "installed app" flow: client secrets at
``settings.gmail_credentials_path``, cached user token at
``settings.gmail_token_path`` (refreshed automatically when expired). The
interactive browser flow only runs on attended local sessions (see
``GMAIL_ALLOW_INTERACTIVE_AUTH``); unattended runs with a missing or expired
token raise ``RuntimeError`` instead of blocking on a browser redirect.

Nothing here touches the network at import time.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request as _GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import settings

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
HTTP_TIMEOUT_S = 60
# Interactive browser OAuth is only for attended local runs (gated by
# GMAIL_ALLOW_INTERACTIVE_AUTH / a TTY) and always bounded: run_local_server
# raises WSGITimeoutError after this many seconds instead of waiting forever.
INTERACTIVE_AUTH_TIMEOUT_S = 300
# Inline previews are downscaled so the whole mail stays far below Gmail's
# 25 MB message cap even with 10 slides attached.


# ---------------------------------------------------------------------------
# Auth / service plumbing
# ---------------------------------------------------------------------------
class _TimeoutRequest(_GoogleAuthRequest):
    """google-auth transport Request that always applies an explicit timeout."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", HTTP_TIMEOUT_S)
        return super().__call__(*args, **kwargs)


def _interactive_auth_allowed() -> bool:
    """True when the interactive browser OAuth flow may be started.

    Interactive auth is only for attended local runs: it is allowed when
    ``GMAIL_ALLOW_INTERACTIVE_AUTH`` is explicitly truthy ("1"/"true"/"yes"/
    "on"), forbidden when explicitly falsy ("0"/"false"/"no"/"off"), and
    otherwise falls back to whether stdin is a TTY (a human at a terminal).
    Headless runs (Cloud Run, schedulers) must fail loudly instead of blocking
    on a browser redirect that will never arrive.
    """
    flag = os.getenv("GMAIL_ALLOW_INTERACTIVE_AUTH", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _load_credentials() -> Credentials:
    """Load (or interactively create) Gmail send-only OAuth credentials.

    Order: cached token file -> refresh if expired -> full InstalledAppFlow
    (opens a browser; only needed on first run or after token revocation).
    The refreshed/new token is written back to ``settings.gmail_token_path``.

    The InstalledAppFlow step only runs when interactive auth is allowed (see
    :func:`_interactive_auth_allowed`) and is bounded by
    ``INTERACTIVE_AUTH_TIMEOUT_S``; unattended runs raise ``RuntimeError``
    immediately instead of hanging on a browser OAuth redirect.
    """
    token_path = Path(settings.gmail_token_path)
    credentials_path = Path(settings.gmail_credentials_path)

    creds: Optional[Credentials] = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        except (ValueError, OSError) as exc:  # corrupt/partial token file
            logger.warning("Ignoring unreadable Gmail token file %s: %s", token_path, exc)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(_TimeoutRequest())
        except Exception as exc:  # refresh token revoked/expired -> re-auth
            logger.warning("Gmail token refresh failed (%s); starting OAuth flow.", exc)
            creds = None

    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                "Gmail OAuth client secrets not found at "
                f"{credentials_path}. Set GMAIL_CREDENTIALS_PATH in .env."
            )
        if not _interactive_auth_allowed():
            raise RuntimeError(
                "Gmail token missing/expired - re-run interactive auth locally "
                "(set GMAIL_ALLOW_INTERACTIVE_AUTH=1) to regenerate the token "
                f"file at {token_path} (settings.gmail_token_path). Interactive "
                "OAuth is disabled in unattended runs so the pipeline fails "
                "loudly instead of blocking on a browser redirect."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), GMAIL_SCOPES
        )
        # Bounded wait: raises WSGITimeoutError after the timeout instead of
        # blocking forever if nobody completes the browser flow.
        creds = flow.run_local_server(
            port=0, timeout_seconds=INTERACTIVE_AUTH_TIMEOUT_S
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds
