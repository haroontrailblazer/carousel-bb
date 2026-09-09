"""Encryption at rest for credentials the console stores.

Right now that is the Telegram bot token: a bearer credential that can post as
your bot to anyone who holds it. It lives in ``app_config``, which is a
Postgres table - so anything with read access to that database, including a
backup file or a support session, would otherwise be able to read it in the
clear.

**Fernet, not a bare HMAC.** An HMAC is one-way: it can prove a value has not
been tampered with, but it cannot give the value back - and the review
dispatcher has to send the real token to Telegram on every review. So this
uses ``cryptography.fernet``, which is AES-128-CBC for confidentiality WITH an
HMAC-SHA256 over the ciphertext for integrity, keyed from one secret. That is
encryption and an HMAC, from a single key in ``.env``.

The key never goes in the database. Losing it means the stored token can no
longer be read - which is the point - and the fix is to connect the bot again
from the profile page, a ten-second job.
"""

from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

#: Prefix on stored ciphertext, so a value's shape says what it is. Anything
#: without it was written before encryption existed and is treated as absent
#: rather than silently trusted.
PREFIX = "fernet:"


class SecretsNotConfigured(RuntimeError):
    """Raised when SECRETS_KEY is missing or malformed."""


def generate_key() -> str:
    """A fresh key, suitable for pasting into ``.env``."""
    return Fernet.generate_key().decode("ascii")


def configured() -> bool:
    """Whether encryption can be performed at all."""
    try:
        _cipher()
    except SecretsNotConfigured:
        return False
    return True


def _cipher() -> Fernet:
    key = (settings.secrets_key or "").strip()
    if not key:
        raise SecretsNotConfigured(
            "SECRETS_KEY is not set, so credentials cannot be stored safely. "
            "Generate one with:  python -c \"from app.services.secret_box "
            "import generate_key; print(generate_key())\"  and put it in .env."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretsNotConfigured(
            "SECRETS_KEY is not a valid Fernet key (44 url-safe base64 "
            "characters). Generate a fresh one rather than inventing a string."
        ) from exc


def encrypt(value: str) -> str:
    """Encrypt a credential for storage. Empty in, empty out."""
    if not value:
        return ""
    return PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(stored: Optional[str]) -> str:
    """Read a stored credential back.

    Returns an empty string - never raises - when the value is missing,
    unencrypted, or was written under a different key. A credential that
    cannot be read is functionally absent, and the console already knows how
    to say "nothing is connected"; raising here would instead break every page
    that merely asks whether Telegram is set up.
    """
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        # Written before encryption, or hand-edited. Not trusted, not used.
        logger.warning("Ignoring a stored credential that is not encrypted.")
        return ""
    try:
        return _cipher().decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except SecretsNotConfigured:
        logger.error("A credential is stored encrypted but SECRETS_KEY is not set.")
        return ""
    except InvalidToken:
        logger.error(
            "A stored credential could not be decrypted - SECRETS_KEY has "
            "changed since it was saved. Reconnect from the profile page."
        )
        return ""


__all__ = [
    "PREFIX",
    "SecretsNotConfigured",
    "configured",
    "decrypt",
    "encrypt",
    "generate_key",
]
