"""Which brand the current run is rendering for.

The handle and profile picture on every slide's brand rail used to come from
one global: ``settings.ig_handle`` and a favicon file checked into the repo.
That was correct while the console published to exactly one account. It stops
being correct the moment a second account is connected - the rail would still
say ``@baskaranbuilds`` on a carousel about to be posted somewhere else.

**Why a contextvar rather than a parameter.** The identity is needed at the
very bottom of the render stack - ``brand_layout.apply_body_brand_rail``,
several calls below ``template_design`` and ``cta`` - and that stack is
synchronous and runs inside ``asyncio.to_thread``. Threading an argument
through every frame would churn a dozen signatures for a value none of them
care about. ``asyncio.to_thread`` copies the context, so a contextvar set once
when the run starts is visible in the worker thread that renders the slides.

**Why there is no global fallback.** Falling back to a default account would
publish one brand's carousel under another brand's handle and logo - a
mistake nobody would notice until it was live. ``require_handle`` raises
instead. A run that has no account should never have started; refusing here
turns a silent mis-branding into a loud, local failure.
"""

from __future__ import annotations

import contextvars
import io
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Optional

from PIL import Image, ImageDraw

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.instagram_accounts import Account

logger = logging.getLogger(__name__)


class NoBrandIdentity(RuntimeError):
    """Something tried to render brand furniture with no account in context."""


@dataclass(frozen=True)
class BrandIdentity:
    """The brand marks for the account a run is targeting."""

    handle: str
    favicon_png: bytes
    account_id: str = ""

    @property
    def at_handle(self) -> str:
        """The handle exactly as it is drawn, with its ``@``."""
        text = (self.handle or "").strip()
        if not text:
            return ""
        return text if text.startswith("@") else f"@{text}"


_current: contextvars.ContextVar[Optional[BrandIdentity]] = contextvars.ContextVar(
    "carousel_brand_identity", default=None
)


def from_account(account: "Account", *, favicon_png: bytes = b"") -> BrandIdentity:
    """Build an identity from a connected account and its fetched picture."""
    return BrandIdentity(
        handle=account.handle,
        favicon_png=favicon_png or b"",
        account_id=account.id,
    )


def current() -> Optional[BrandIdentity]:
    """The identity for this run, or ``None`` outside one."""
    return _current.get()


def set_current(identity: Optional[BrandIdentity]) -> contextvars.Token:
    """Set the identity, returning the token that restores the previous one."""
    return _current.set(identity)


@contextmanager
def use(identity: Optional[BrandIdentity]) -> Iterator[None]:
    """Render under this identity, restoring whatever was set before.

    Restores on the way out even when the body raises: a render that fails
    mid-slide must not leave the next run inheriting this one's brand.
    """
    token = _current.set(identity)
    try:
        yield
    finally:
        _current.reset(token)


def require_handle() -> str:
    """The handle to draw, or a named failure.

    Raises:
        NoBrandIdentity: when no account is in context. Deliberately not a
            fallback - see the module docstring.
    """
    identity = _current.get()
    if identity is None or not identity.at_handle:
        raise NoBrandIdentity(
            "No Instagram account is set for this run, so the brand rail has "
            "no handle to draw. A run must be started against a connected "
            "account."
        )
    return identity.at_handle


def require_favicon(size: int) -> Image.Image:
    """The rail's profile mark at ``size`` px, as RGBA.

    Falls back to a generated monogram when the account has no usable picture
    - never to another account's logo, which would be a silent mis-branding
    rather than a visible gap.
    """
    identity = _current.get()
    if identity is None:
        raise NoBrandIdentity(
            "No Instagram account is set for this run, so the brand rail has "
            "no profile picture to draw."
        )

    if identity.favicon_png:
        try:
            with Image.open(io.BytesIO(identity.favicon_png)) as source:
                return source.convert("RGBA").resize(
                    (size, size), Image.Resampling.LANCZOS
                )
        except Exception as exc:  # noqa: BLE001 - any decode failure
            logger.warning(
                "The stored profile picture for %s could not be decoded (%s); "
                "drawing a monogram instead.",
                identity.at_handle,
                exc,
            )

    return _monogram(identity.at_handle, size)


def _monogram(handle: str, size: int) -> Image.Image:
    """A circle with the account's initial - an honest 'no picture yet'.

    Kept deliberately plain. This is a stand-in that should prompt someone to
    reconnect the account, not a design element that looks intentional.
    """
    # Imported here rather than at module scope: brand_layout imports this
    # module for the rail, and a top-level import would close the cycle.
    from app.tools.brand_layout import INK, WARM_WHITE, _font

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(*INK, 255))

    letter = next((c for c in handle.lstrip("@") if c.isalnum()), "?").upper()
    font = _font(max(10, round(size * 0.55)), bold=True)
    box = draw.textbbox((0, 0), letter, font=font)
    draw.text(
        (
            (size - (box[2] - box[0])) / 2 - box[0],
            (size - (box[3] - box[1])) / 2 - box[1],
        ),
        letter,
        font=font,
        fill=(*WARM_WHITE, 255),
    )
    return image


__all__ = [
    "BrandIdentity",
    "NoBrandIdentity",
    "current",
    "from_account",
    "require_favicon",
    "require_handle",
    "set_current",
    "use",
]
