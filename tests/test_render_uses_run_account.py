"""The renderers stamp the RUN's handle, not a process-wide one.

``settings.ig_handle`` was a single global read at import time. Every slide
and every CTA drew it. With several accounts connected that global is no
longer an answer to "whose carousel is this", so the two render entry points
take the handle from the run's brand identity instead.

These tests intercept the rail calls rather than rendering a whole slide: what
is being pinned is WHICH handle reaches the rail, and a full render would cost
an image-API round trip to assert the same thing.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from app.tools import brand_identity, image_gen


class NoGlobalHandleTests(unittest.TestCase):
    def test_there_is_no_settings_handle_left_to_read(self) -> None:
        """The global is gone, not merely unused - so it cannot come back.

        This is what makes the call sites safe without a slow render test: any
        surviving ``settings.ig_handle`` reference now raises rather than
        quietly stamping the wrong brand.
        """
        from app.config import settings

        self.assertFalse(hasattr(settings, "ig_handle"))

    def test_no_module_still_references_the_old_global(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "import app.tools.image_gen, app.agents.cta"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CurrentHandleTests(unittest.TestCase):
    def test_current_handle_comes_from_the_context(self) -> None:
        with brand_identity.use(
            brand_identity.BrandIdentity(handle="@one", favicon_png=b"")
        ):
            self.assertEqual(image_gen._current_handle(), "@one")

    def test_current_handle_refuses_outside_a_run(self) -> None:
        with self.assertRaises(brand_identity.NoBrandIdentity):
            image_gen._current_handle()


class CtaLinkTests(unittest.TestCase):
    """The CTA compares its link text against the account's own handle."""

    def test_the_link_line_is_dropped_when_it_repeats_the_account_handle(
        self,
    ) -> None:
        from app.agents import cta

        with brand_identity.use(
            brand_identity.BrandIdentity(handle="@acme", favicon_png=b"")
        ):
            self.assertEqual(cta._handle_for_run(), "@acme")


if __name__ == "__main__":
    unittest.main()
