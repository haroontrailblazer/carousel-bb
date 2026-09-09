"""Which brand a run is rendering for.

The slide renderer is synchronous, several calls deep, and runs inside a
worker thread. It used to read one global handle out of ``settings``; now
every run has its own target account, so the identity travels in a contextvar
that ``asyncio.to_thread`` carries across the thread boundary for us.

The fallback matters more than it looks. When an account has no stored
profile picture, the rail must NOT quietly fall back to some other account's
logo - that would publish one brand's carousel under another brand's mark.
A generated monogram is wrong in an obvious, harmless way instead.
"""

from __future__ import annotations

import asyncio
import io
import unittest

from PIL import Image

from app.tools import brand_identity


def _png(color: tuple[int, int, int], size: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


class ContextTests(unittest.TestCase):
    def test_nothing_is_set_by_default(self) -> None:
        self.assertIsNone(brand_identity.current())

    def test_use_sets_the_identity_and_restores_it_afterwards(self) -> None:
        identity = brand_identity.BrandIdentity(handle="@acme", favicon_png=b"")
        with brand_identity.use(identity):
            current = brand_identity.current()
            assert current is not None
            self.assertEqual(current.handle, "@acme")
        self.assertIsNone(brand_identity.current())

    def test_the_identity_is_restored_even_when_the_body_raises(self) -> None:
        identity = brand_identity.BrandIdentity(handle="@acme", favicon_png=b"")
        with self.assertRaises(RuntimeError):
            with brand_identity.use(identity):
                raise RuntimeError("render blew up")
        self.assertIsNone(brand_identity.current())

    def test_nested_uses_restore_the_outer_identity(self) -> None:
        outer = brand_identity.BrandIdentity(handle="@outer", favicon_png=b"")
        inner = brand_identity.BrandIdentity(handle="@inner", favicon_png=b"")
        with brand_identity.use(outer):
            with brand_identity.use(inner):
                self.assertEqual(brand_identity.current().handle, "@inner")
            self.assertEqual(brand_identity.current().handle, "@outer")


class ThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_identity_survives_asyncio_to_thread(self) -> None:
        """The whole reason this is a contextvar and not a parameter."""
        identity = brand_identity.BrandIdentity(handle="@acme", favicon_png=b"")
        with brand_identity.use(identity):
            handle = await asyncio.to_thread(brand_identity.require_handle)
        self.assertEqual(handle, "@acme")


class HandleTests(unittest.TestCase):
    def test_require_handle_refuses_when_no_account_is_set(self) -> None:
        """Better a named failure at render time than a wrongly branded post."""
        with self.assertRaises(brand_identity.NoBrandIdentity):
            brand_identity.require_handle()

    def test_a_handle_without_an_at_sign_gets_one(self) -> None:
        with brand_identity.use(
            brand_identity.BrandIdentity(handle="acme", favicon_png=b"")
        ):
            self.assertEqual(brand_identity.require_handle(), "@acme")


class FaviconTests(unittest.TestCase):
    def test_the_stored_picture_is_used_at_the_requested_size(self) -> None:
        identity = brand_identity.BrandIdentity(
            handle="@acme", favicon_png=_png((10, 200, 30))
        )
        with brand_identity.use(identity):
            favicon = brand_identity.require_favicon(48)

        self.assertEqual(favicon.size, (48, 48))
        self.assertEqual(favicon.mode, "RGBA")
        # The stored colour survived the resize, so this is the real picture
        # rather than a generated stand-in.
        self.assertEqual(favicon.getpixel((24, 24))[:3], (10, 200, 30))

    def test_an_account_without_a_picture_gets_a_monogram_not_another_brand(
        self,
    ) -> None:
        identity = brand_identity.BrandIdentity(handle="@acme", favicon_png=b"")
        with brand_identity.use(identity):
            favicon = brand_identity.require_favicon(64)

        self.assertEqual(favicon.size, (64, 64))
        self.assertEqual(favicon.mode, "RGBA")
        # Something was actually drawn: the circle covers the middle and the
        # corner stays transparent. A blank square would be a silent hole in
        # the rail rather than a visible stand-in.
        self.assertEqual(favicon.getpixel((0, 0))[3], 0)
        self.assertEqual(favicon.getpixel((32, 32))[3], 255)

    def test_undecodable_picture_bytes_fall_back_to_the_monogram(self) -> None:
        identity = brand_identity.BrandIdentity(
            handle="@acme", favicon_png=b"not a png at all"
        )
        with brand_identity.use(identity):
            favicon = brand_identity.require_favicon(32)
        self.assertEqual(favicon.size, (32, 32))

    def test_require_favicon_refuses_when_no_account_is_set(self) -> None:
        with self.assertRaises(brand_identity.NoBrandIdentity):
            brand_identity.require_favicon(32)


class FromAccountTests(unittest.TestCase):
    def test_an_account_becomes_an_identity(self) -> None:
        from app.services.instagram_accounts import Account

        account = Account(
            id="a",
            ig_user_id="1",
            username="acme",
            name="Acme",
            avatar_key="instagram/a.png",
            auth_kind="instagram_login",
            token="tok",
            token_expires_at=None,
            is_default=True,
            disabled=False,
            connected_by="",
            connected_at=None,
            last_refreshed_at=None,
        )
        identity = brand_identity.from_account(account, favicon_png=_png((1, 2, 3)))
        self.assertEqual(identity.handle, "@acme")
        self.assertEqual(identity.account_id, "a")


if __name__ == "__main__":
    unittest.main()
