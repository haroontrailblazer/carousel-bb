"""Hard text rules shared by planning, writing, rendering, and QA."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


EM_DASH = "\u2014"

# Reliable signs of broken decoding, model placeholders, or keyboard-mash
# copy. A dictionary check would reject legitimate names, brands, acronyms,
# and technical terms, so this gate deliberately stays conservative.
_BROKEN_TEXT_MARKERS = (
    "\ufffd",
    "Ã©",
    "Ã£",
    "Ã¶",
    "Ã¼",
    "Â ",
    "â€",
    "ðŸ",
)
_PLACEHOLDER_RE = re.compile(
    r"\b(?:lorem\s+ipsum|placeholder|dummy\s+text|asdf(?:gh)?|qwerty)\b",
    re.IGNORECASE,
)
_REPEATED_LETTER_RE = re.compile(r"([A-Za-z])\1{3,}")
_REPEATED_CHUNK_RE = re.compile(r"\b([A-Za-z]{2,4})\1{2,}\b", re.IGNORECASE)


def find_em_dash(value: Any, path: str = "text") -> str | None:
    """Return the first field path containing an em dash, if one exists."""
    if isinstance(value, str):
        return path if EM_DASH in value else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            found = find_em_dash(item, f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            found = find_em_dash(item, f"{path}[{index}]")
            if found:
                return found
    return None


def require_no_em_dash(value: Any, context: str = "text") -> None:
    """Raise when published-facing text contains the forbidden character."""
    found = find_em_dash(value, context)
    if found:
        raise ValueError(
            f"{found} contains a forbidden em dash. Use a period, comma, "
            "colon, or parentheses instead."
        )


def _unreadable_string_reason(value: str) -> str | None:
    """Return a conservative reason when *value* is clearly not publishable."""
    for marker in _BROKEN_TEXT_MARKERS:
        if marker in value:
            return "contains broken or incorrectly decoded characters"

    for character in value:
        category = unicodedata.category(character)
        if category.startswith("C") and character not in "\n\r\t":
            return "contains an invisible or unsupported control character"

    if _PLACEHOLDER_RE.search(value):
        return "contains placeholder or keyboard-mash text"
    if _REPEATED_LETTER_RE.search(value):
        return "contains an implausible repeated-letter word"
    if _REPEATED_CHUNK_RE.search(value):
        return "contains a repeated nonsense word pattern"
    return None


def find_unreadable_text(
    value: Any, path: str = "text"
) -> tuple[str, str] | None:
    """Return the first field path and reason for clearly unreadable text.

    This is intentionally not an English dictionary check. Published copy may
    legitimately contain names, brands, acronyms, versions, and domain terms.
    The function rejects objective corruption and nonsense patterns while the
    writing agents remain responsible for plain, understandable wording.
    """
    if isinstance(value, str):
        reason = _unreadable_string_reason(value)
        return (path, reason) if reason else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            found = find_unreadable_text(item, f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            found = find_unreadable_text(item, f"{path}[{index}]")
            if found:
                return found
    return None


def require_readable_text(value: Any, context: str = "text") -> None:
    """Raise when audience-facing text is clearly corrupted or nonsensical."""
    found = find_unreadable_text(value, context)
    if found:
        path, reason = found
        raise ValueError(
            f"{path} {reason}. Use complete, correctly spelled, "
            "understandable words only."
        )


__all__ = [
    "EM_DASH",
    "find_em_dash",
    "find_unreadable_text",
    "require_no_em_dash",
    "require_readable_text",
]
