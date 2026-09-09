"""An unconfigured cover overlay is a state, not a fault.

The setting used to default to "STRANGE-COVER (1).png" in the repository
root - a file not in git and no longer on disk. So the default could only
resolve to a missing file, and the loader logged "template not found" for a
path nobody had ever configured. That reads as a broken install.

The trap on the way to fixing it: ``Path("")`` is ``Path(".")``, and the
current directory EXISTS. An empty-string default would sail past every
``.exists()`` guard and hand a directory to ``Image.open``.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config
from app.tools import media_tools


def _overlay(value):
    """settings is a frozen dataclass, so swap the whole object."""
    return patch.object(
        media_tools, "settings", SimpleNamespace(cover_overlay_template=value)
    )


class ResolutionTests(unittest.TestCase):
    def test_unset_is_none_not_the_current_directory(self) -> None:
        with patch.dict(config.os.environ, {"COVER_OVERLAY_TEMPLATE": ""}, clear=False):
            self.assertIsNone(config._cover_overlay_template())

    def test_whitespace_only_is_also_none(self) -> None:
        with patch.dict(config.os.environ, {"COVER_OVERLAY_TEMPLATE": "   "}, clear=False):
            self.assertIsNone(config._cover_overlay_template())

    def test_a_relative_path_resolves_against_the_repo_root(self) -> None:
        with patch.dict(
            config.os.environ, {"COVER_OVERLAY_TEMPLATE": "brand/cover.png"}, clear=False
        ):
            resolved = config._cover_overlay_template()
        assert resolved is not None
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved, config.PROJECT_ROOT / "brand" / "cover.png")

    def test_an_absolute_path_is_left_alone(self) -> None:
        absolute = str(Path.cwd() / "somewhere" / "cover.png")
        with patch.dict(
            config.os.environ, {"COVER_OVERLAY_TEMPLATE": absolute}, clear=False
        ):
            self.assertEqual(config._cover_overlay_template(), Path(absolute))


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        media_tools._TEMPLATE_WARNED = False

    def test_no_template_configured_returns_none_without_warning(self) -> None:
        """Silence is correct here - nothing is misconfigured."""
        with _overlay(None):
            with self.assertNoLogs(media_tools.__name__, level="WARNING"):
                self.assertIsNone(media_tools._load_scrubbed_template())

    def test_a_configured_but_missing_template_does_warn(self) -> None:
        """This one IS a misconfiguration: someone named a file that is absent."""
        missing = config.PROJECT_ROOT / "definitely-not-here-9f2a.png"
        with _overlay(missing):
            with self.assertLogs(media_tools.__name__, level="WARNING") as caught:
                self.assertIsNone(media_tools._load_scrubbed_template())
        self.assertIn("definitely-not-here-9f2a.png", "\n".join(caught.output))

    def test_the_current_directory_is_never_opened_as_an_image(self) -> None:
        """The Path("") == Path(".") trap, pinned."""
        with _overlay(Path(".")):
            self.assertIsNone(media_tools._load_scrubbed_template())


if __name__ == "__main__":
    unittest.main()
