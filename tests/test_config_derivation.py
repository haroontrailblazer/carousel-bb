"""Settings that are derived rather than repeated.

``SUPABASE_S3_ENDPOINT`` was always ``SUPABASE_URL`` with a fixed suffix -
Supabase publishes exactly one storage endpoint per project. Asking for both
meant two places to change on a project move, and a silent mismatch if only
one of them was updated: the console would authenticate against one project
and read slides from another.

An explicit value still wins, because a self-hosted or proxied deployment can
legitimately put storage somewhere else.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import config


class S3EndpointDerivationTests(unittest.TestCase):
    def test_it_is_derived_from_the_supabase_url(self) -> None:
        with patch.dict(
            config.os.environ,
            {"SUPABASE_URL": "https://abc123.supabase.co", "SUPABASE_S3_ENDPOINT": ""},
            clear=False,
        ):
            self.assertEqual(
                config._s3_endpoint(), "https://abc123.supabase.co/storage/v1/s3"
            )

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        with patch.dict(
            config.os.environ,
            {"SUPABASE_URL": "https://abc123.supabase.co/", "SUPABASE_S3_ENDPOINT": ""},
            clear=False,
        ):
            self.assertEqual(
                config._s3_endpoint(), "https://abc123.supabase.co/storage/v1/s3"
            )

    def test_an_explicit_endpoint_still_wins(self) -> None:
        """Self-hosted and proxied deployments put storage elsewhere."""
        with patch.dict(
            config.os.environ,
            {
                "SUPABASE_URL": "https://abc123.supabase.co",
                "SUPABASE_S3_ENDPOINT": "https://storage.example.com/s3",
            },
            clear=False,
        ):
            self.assertEqual(config._s3_endpoint(), "https://storage.example.com/s3")

    def test_no_supabase_url_yields_no_endpoint(self) -> None:
        """Empty rather than a nonsense '/storage/v1/s3' with no host.

        app/runtime.py:123 treats an empty endpoint as "storage not
        configured"; a bare path would read as configured and fail later.
        """
        with patch.dict(
            config.os.environ,
            {"SUPABASE_URL": "", "SUPABASE_S3_ENDPOINT": ""},
            clear=False,
        ):
            self.assertEqual(config._s3_endpoint(), "")

    def test_surrounding_whitespace_is_ignored(self) -> None:
        with patch.dict(
            config.os.environ,
            {"SUPABASE_URL": "  https://abc123.supabase.co  ", "SUPABASE_S3_ENDPOINT": "  "},
            clear=False,
        ):
            self.assertEqual(
                config._s3_endpoint(), "https://abc123.supabase.co/storage/v1/s3"
            )


class LiveSettingsTests(unittest.TestCase):
    def test_the_running_config_resolves_a_storage_endpoint(self) -> None:
        """Whatever the local .env says, storage must still be addressable."""
        if not config.settings.supabase_url:
            self.skipTest("no SUPABASE_URL configured in this environment")
        self.assertTrue(config.settings.s3_endpoint.endswith("/storage/v1/s3"))
        self.assertTrue(config.settings.s3_endpoint.startswith("http"))


if __name__ == "__main__":
    unittest.main()
