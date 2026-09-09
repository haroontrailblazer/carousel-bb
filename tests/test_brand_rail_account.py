"""The brand rail draws the RUN's account, not a repo-wide default.

Before accounts existed, the rail's profile mark was a PNG checked into the
repository and its handle came from ``settings.ig_handle``. Both were correct
for exactly one publishing account. With several connected, a rail that still
reads from a global would put one brand's mark on another brand's carousel -
and nobody would catch it before it was live.

So the rails read the run's brand identity, and refuse to draw at all when
there is none.
"""

from __future__ import annotations

import io
import unittest

from PIL import Image

from app.tools import brand_identity
from app.tools.brand_layout import (
    BODY_FAVICON_LEFT,
    BODY_FAVICON_SIZE,
    RAIL_CENTER_Y,
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
    apply_body_brand_rail,
    apply_cta_brand_rail,
)

#: A colour nothing in the palette uses, so finding it in the rail proves it
#: came from the account's own picture.
ACCOUNT_PICTURE_RGB = (255, 0, 255)


def _picture(color: tuple[int, int, int] = ACCOUNT_PICTURE_RGB) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (128, 128), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _slide() -> Image.Image:
    return Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), (247, 247, 245))


def _identity(handle: str = "@acme", picture: bytes = b"") -> brand_identity.BrandIdentity:
    return brand_identity.BrandIdentity(handle=handle, favicon_png=picture)


class BodyRailTests(unittest.TestCase):
    def test_the_rail_draws_the_accounts_own_picture(self) -> None:
        with brand_identity.use(_identity(picture=_picture())):
            rendered = apply_body_brand_rail(_slide(), "@acme", 2)

        centre = rendered.getpixel(
            (BODY_FAVICON_LEFT + BODY_FAVICON_SIZE // 2, RAIL_CENTER_Y)
        )
        self.assertEqual(centre, ACCOUNT_PICTURE_RGB)

    def test_rendering_without_an_account_refuses_rather_than_guessing(self) -> None:
        with self.assertRaises(brand_identity.NoBrandIdentity):
            apply_body_brand_rail(_slide(), "@acme", 2)

    def test_two_accounts_produce_different_rails(self) -> None:
        """The regression this whole change exists to prevent."""
        with brand_identity.use(_identity("@one", _picture((255, 0, 255)))):
            first = apply_body_brand_rail(_slide(), "@one", 1)
        with brand_identity.use(_identity("@two", _picture((0, 255, 255)))):
            second = apply_body_brand_rail(_slide(), "@two", 1)

        probe = (BODY_FAVICON_LEFT + BODY_FAVICON_SIZE // 2, RAIL_CENTER_Y)
        self.assertNotEqual(first.getpixel(probe), second.getpixel(probe))


class CtaRailTests(unittest.TestCase):
    def test_the_cta_rail_draws_the_accounts_own_picture(self) -> None:
        with brand_identity.use(_identity(picture=_picture())):
            rendered = apply_cta_brand_rail(_slide(), "@acme")

        centre = rendered.getpixel(
            (BODY_FAVICON_LEFT + BODY_FAVICON_SIZE // 2, RAIL_CENTER_Y)
        )
        self.assertEqual(centre, ACCOUNT_PICTURE_RGB)

    def test_rendering_without_an_account_refuses(self) -> None:
        with self.assertRaises(brand_identity.NoBrandIdentity):
            apply_cta_brand_rail(_slide(), "@acme")

    def test_an_account_without_a_picture_still_renders(self) -> None:
        """A missing picture is a monogram, not a crashed run."""
        with brand_identity.use(_identity("@acme", b"")):
            rendered = apply_cta_brand_rail(_slide(), "@acme")
        self.assertEqual(rendered.size, (SLIDE_WIDTH, SLIDE_HEIGHT))


if __name__ == "__main__":
    unittest.main()
