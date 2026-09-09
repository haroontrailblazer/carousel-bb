"""Profile pictures: keys, limits, and why they are not served by a URL.

The browser compresses before uploading, so the interesting decisions here are
about what the server refuses and how the file is addressed - not about image
processing, which never reaches this side.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import avatar_store


class KeyTests(unittest.TestCase):
    def test_the_email_is_hashed_not_embedded(self) -> None:
        """Object keys leak into logs, storage browsers and error messages.

        A bucket listing should not be a list of everyone's email address.
        """
        key = avatar_store.key_for("haroon@closefuture.io")
        self.assertNotIn("haroon", key)
        self.assertNotIn("@", key)
        self.assertTrue(key.startswith("avatars/"))
        self.assertTrue(key.endswith(".webp"))

    def test_it_is_stable_for_a_person(self) -> None:
        """Same key every time: an upload REPLACES, never accumulates."""
        first = avatar_store.key_for("a@b.com")
        second = avatar_store.key_for("a@b.com")
        self.assertEqual(first, second)

    def test_case_and_padding_do_not_make_a_second_avatar(self) -> None:
        self.assertEqual(
            avatar_store.key_for("  A@B.com "), avatar_store.key_for("a@b.com")
        )

    def test_different_people_get_different_keys(self) -> None:
        self.assertNotEqual(
            avatar_store.key_for("a@b.com"), avatar_store.key_for("c@d.com")
        )

    def test_the_digest_is_a_full_sha256(self) -> None:
        """The serve route validates on exactly this shape."""
        digest = avatar_store.key_for("a@b.com").rsplit("/", 1)[-1][: -len(".webp")]
        self.assertEqual(len(digest), 64)
        self.assertTrue(digest.isalnum())


class SaveGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_body_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            await avatar_store.save("a@b.com", b"")

    async def test_an_oversized_image_is_refused_before_any_upload(self) -> None:
        """The browser compresses to ~20 KB; this is a backstop, not a target.

        It must reject BEFORE touching storage, so a client that ignores the
        compression step cannot push megabytes into the bucket.
        """
        called = False

        def _put(*_args, **_kwargs):  # pragma: no cover - must not run
            nonlocal called
            called = True

        with patch.object(avatar_store, "_put", _put):
            with self.assertRaises(ValueError) as ctx:
                await avatar_store.save("a@b.com", b"x" * (avatar_store.MAX_BYTES + 1))
        self.assertFalse(called)
        self.assertIn("limit", str(ctx.exception))

    async def test_an_accepted_image_is_stored_under_the_hashed_key(self) -> None:
        seen: dict = {}

        def _put(key: str, payload: bytes) -> None:
            seen["key"] = key
            seen["payload"] = payload

        with patch.object(avatar_store, "_put", _put):
            key = await avatar_store.save("a@b.com", b"webp-bytes")

        self.assertEqual(key, avatar_store.key_for("a@b.com"))
        self.assertEqual(seen["key"], key)
        self.assertEqual(seen["payload"], b"webp-bytes")


class MissingObjectTests(unittest.IsolatedAsyncioTestCase):
    """A person with no picture is a normal state, not a storage failure."""

    async def test_supabase_404_with_an_empty_code_reads_as_missing(self) -> None:
        """Measured against the live endpoint, which does NOT send NoSuchKey.

        Supabase answers a missing object with HTTP 404 and
        ``{'Message': '', 'Code': ''}``. Matching on the code alone - which is
        what AWS documents - turned "no picture yet" into a 502 the moment
        anyone removed theirs.
        """
        from botocore.exceptions import ClientError

        def _raise(*_args, **_kwargs):
            raise ClientError(
                {"Error": {"Message": "", "Code": ""},
                 "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )

        service = type("S", (), {"bucket_name": "b", "_client": type(
            "C", (), {"get_object": staticmethod(_raise)})()})()
        with patch.object(avatar_store, "_service", lambda: service):
            self.assertIsNone(await avatar_store.load("avatars/x.webp"))

    async def test_the_aws_style_code_still_reads_as_missing(self) -> None:
        from botocore.exceptions import ClientError

        def _raise(*_args, **_kwargs):
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {}}, "GetObject"
            )

        service = type("S", (), {"bucket_name": "b", "_client": type(
            "C", (), {"get_object": staticmethod(_raise)})()})()
        with patch.object(avatar_store, "_service", lambda: service):
            self.assertIsNone(await avatar_store.load("avatars/x.webp"))

    async def test_a_real_storage_failure_still_raises(self) -> None:
        """Swallowing every error would hide an outage as "no picture"."""
        from botocore.exceptions import ClientError

        def _raise(*_args, **_kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied"},
                 "ResponseMetadata": {"HTTPStatusCode": 403}},
                "GetObject",
            )

        service = type("S", (), {"bucket_name": "b", "_client": type(
            "C", (), {"get_object": staticmethod(_raise)})()})()
        with patch.object(avatar_store, "_service", lambda: service):
            with self.assertRaises(ClientError):
                await avatar_store.load("avatars/x.webp")


class StorageShapeTests(unittest.TestCase):
    def test_only_webp_is_stored(self) -> None:
        """One format in the bucket, so serving needs no content sniffing."""
        self.assertEqual(avatar_store.CONTENT_TYPE, "image/webp")
        self.assertTrue(avatar_store.key_for("a@b.com").endswith(".webp"))

    def test_the_ceiling_is_far_above_a_compressed_avatar(self) -> None:
        """Generous on purpose: the guard exists to stop abuse, not uploads."""
        self.assertGreaterEqual(avatar_store.MAX_BYTES, 1024 * 1024)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
