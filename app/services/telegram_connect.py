"""Connecting a Telegram bot, as a plain program.

No agent and no model. Everything here is a fixed sequence of Bot API calls
with fixed outcomes - exactly the kind of work an LLM can only make less
reliable and more expensive.

The flow the console drives:

1. ``verify_token``  - is this a real bot token? Returns its @username.
2. ``discover_chat`` - which chat should reviews go to? Telegram will only
   tell us this AFTER a human has messaged the bot: ``getUpdates`` returns
   messages, and a bot that has received none has no chat to report. There is
   no API that maps a token to "the owner's chat", so the console has to ask
   the person to send the bot a message. That is a Telegram constraint, not a
   shortcut being taken here.
3. ``send_welcome``  - proves the whole path works by using it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

#: Short: this runs inside a request the user is waiting on.
TIMEOUT_S = 15.0

WELCOME_TEXT = (
    "Hi - you are successfully connected.\n\n"
    "Carousel Factory will send carousel reviews to this chat. Each one comes "
    "with the slides and a button that opens the review screen."
)


@dataclass(frozen=True)
class ConnectError(Exception):
    """A failure worth showing to the person setting this up."""

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _call(client: httpx.Client, token: str, method: str, **params) -> dict:
    """One Bot API call, with Telegram's own error text preserved."""
    try:
        response = client.get(_api(token, method), params=params or None)
    except httpx.HTTPError as exc:
        raise ConnectError(
            "unreachable",
            f"Could not reach Telegram: {exc}",
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ConnectError(
            "bad_response", "Telegram returned something that is not JSON."
        ) from exc

    if not payload.get("ok"):
        description = str(payload.get("description") or "").strip()
        if response.status_code == 401 or "unauthorized" in description.lower():
            raise ConnectError(
                "invalid_token",
                "Telegram rejected that token. Copy it again from @BotFather - "
                "it looks like 123456789:AA... and has no spaces.",
            )
        raise ConnectError(
            "telegram_error", description or "Telegram refused the request."
        )
    return payload.get("result") or {}


def verify_token(token: str) -> dict:
    """Confirm the token is a real bot and return ``{id, username, name}``."""
    token = (token or "").strip()
    if not token:
        raise ConnectError("missing_token", "Paste the token @BotFather gave you.")
    with httpx.Client(timeout=TIMEOUT_S) as client:
        me = _call(client, token, "getMe")
    return {
        "id": me.get("id"),
        "username": str(me.get("username") or ""),
        "name": str(me.get("first_name") or ""),
    }


def discover_chat(token: str) -> Optional[str]:
    """The most recent chat that has messaged this bot, if any.

    Newest first: if several people have talked to the bot, the one who just
    pressed Start while setting this up is the one who means it.
    """
    with httpx.Client(timeout=TIMEOUT_S) as client:
        updates = _call(client, token, "getUpdates", limit=100, timeout=0)
    if not isinstance(updates, list):
        return None
    for update in reversed(updates):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = ((update or {}).get(key) or {}).get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is not None:
                return str(chat_id)
    return None


def send_welcome(token: str, chat_id: str) -> dict:
    """Send the confirmation message. Proves the path by using it."""
    with httpx.Client(timeout=TIMEOUT_S) as client:
        try:
            response = client.post(
                _api(token, "sendMessage"),
                data={"chat_id": str(chat_id), "text": WELCOME_TEXT},
            )
        except httpx.HTTPError as exc:
            raise ConnectError("unreachable", f"Could not reach Telegram: {exc}") from exc
    payload = response.json() if response.content else {}
    if not payload.get("ok"):
        raise ConnectError(
            "send_failed",
            str(payload.get("description") or "Telegram would not deliver the message."),
        )
    return payload.get("result") or {}


__all__ = [
    "ConnectError",
    "WELCOME_TEXT",
    "discover_chat",
    "send_welcome",
    "verify_token",
]
