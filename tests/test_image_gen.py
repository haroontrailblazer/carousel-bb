from __future__ import annotations

import base64
import tempfile
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIStatusError
from PIL import Image, ImageChops, ImageDraw

from app.tools import image_gen
from app.tools.brand_layout import PAPER, RAIL_DIVIDER_Y, TEXT_PANEL_BOTTOM


def _png_bytes(image: Image.Image) -> bytes:
    payload = BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _size_error(size: str) -> APIStatusError:
    """The exact 400 the API returns for an under-budget resolution."""
    return APIStatusError(
        f"Invalid size '{size}'. Requested resolution is below the current "
        f"minimum pixel budget.",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://api.openai.com/v1/images")
        ),
        body=None,
    )


def _ok_response() -> SimpleNamespace:
    return SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(b"PNG").decode(), url=None)],
        usage=None,
    )


@contextmanager
def _fake_client(images: object):
    """Swap in a stub client - no network, no billed generation."""
    saved = image_gen._client_singleton
    image_gen._client_singleton = SimpleNamespace(images=images)
    try:
        yield
    finally:
        image_gen._client_singleton = saved


class GenerationSizeTests(unittest.TestCase):
    """The size ladder that keeps renders alive when the API's floor moves."""

    def setUp(self) -> None:
        self._saved = image_gen._gen_size_index
        image_gen._gen_size_index = 0

    def tearDown(self) -> None:
        image_gen._gen_size_index = self._saved

    def test_every_rung_matches_the_visual_slot_and_the_api_contract(self) -> None:
        for width, height in image_gen._GEN_SIZES:
            with self.subTest(size=f"{width}x{height}"):
                # Exact 2:1, or the merge would letterbox the visual slot.
                self.assertEqual(width / height, 2.0)
                # gpt-image-2 requires both axes divisible by 16.
                self.assertEqual(width % 16, 0)
                self.assertEqual(height % 16, 0)
                # Above 1024x1024, a documented supported size, so no rung can
                # sit under the minimum pixel budget that broke 1088x544.
                self.assertGreater(width * height, 1024 * 1024)

    def test_rungs_increase(self) -> None:
        areas = [w * h for w, h in image_gen._GEN_SIZES]
        self.assertEqual(areas, sorted(areas))
        self.assertEqual(len(set(areas)), len(areas))

    def test_a_size_rejection_climbs_the_ladder_and_is_remembered(self) -> None:
        """The real failure: the first rung is refused, the next one works."""
        seen: list[str] = []

        class _Images:
            def generate(self, **kwargs):
                size = kwargs["size"]
                seen.append(size)
                if size == "1536x768":
                    raise _size_error(size)
                return _ok_response()

        with _fake_client(_Images()):
            payload = image_gen._call_images_api("prompt", None)

        self.assertEqual(payload, b"PNG")
        self.assertEqual(seen, ["1536x768", "2048x1024"])
        # Remembered, so the next slide does not pay for the refusal again.
        self.assertEqual(image_gen._gen_size(), "2048x1024")
        with _fake_client(_Images()):
            image_gen._call_images_api("prompt", None)
        self.assertEqual(seen[2:], ["2048x1024"])

    def test_ladder_is_exhausted_rather_than_looping(self) -> None:
        calls: list[str] = []

        class _Images:
            def generate(self, **kwargs):
                calls.append(kwargs["size"])
                raise _size_error(kwargs["size"])

        with _fake_client(_Images()):
            with self.assertRaises(APIStatusError):
                image_gen._call_images_api("prompt", None)
        self.assertEqual(calls, ["1536x768", "2048x1024", "2560x1280"])

    def test_a_prompt_rejection_does_not_burn_the_ladder(self) -> None:
        """A 400 that more pixels cannot fix must not climb."""
        calls: list[str] = []

        class _Images:
            def generate(self, **kwargs):
                calls.append(kwargs["size"])
                raise APIStatusError(
                    "Your request was rejected by the safety system.",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://api.openai.com")
                    ),
                    body=None,
                )

        with _fake_client(_Images()):
            with self.assertRaises(APIStatusError):
                image_gen._call_images_api("prompt", None)
        self.assertEqual(calls, ["1536x768"])
        self.assertEqual(image_gen._gen_size(), "1536x768")


class VisualMergeTests(unittest.TestCase):
    def test_generated_visual_is_merged_into_exact_lower_slot(self) -> None:
        visual = Image.new("RGB", (1536, 768), (20, 30, 40))
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "slide.png"
            image_gen._finalize(_png_bytes(visual), str(output), theme="paper")
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (1080, 1350))
                self.assertEqual(rendered.getpixel((540, TEXT_PANEL_BOTTOM - 1)), PAPER)
                self.assertEqual(rendered.getpixel((540, TEXT_PANEL_BOTTOM)), (20, 30, 40))
                self.assertEqual(rendered.getpixel((540, RAIL_DIVIDER_Y - 1)), (20, 30, 40))
                self.assertEqual(rendered.getpixel((540, RAIL_DIVIDER_Y)), PAPER)

    def test_unexpected_aspect_is_contained_without_crop_or_stretch(self) -> None:
        visual = Image.new("RGB", (100, 200), (70, 80, 90))
        ImageDraw.Draw(visual).rectangle((0, 0, 99, 199), outline=(220, 30, 30), width=4)
        panel = image_gen._contain_without_crop(visual, (1080, 540), PAPER)
        background = Image.new("RGB", panel.size, PAPER)
        self.assertEqual(ImageChops.difference(panel, background).getbbox(), (405, 0, 675, 540))
        self.assertGreater(panel.getpixel((405, 0))[0], 180)
        self.assertGreater(panel.getpixel((674, 539))[0], 180)
        self.assertEqual(panel.getpixel((404, 270)), PAPER)
        self.assertEqual(panel.getpixel((675, 270)), PAPER)

    def test_sourced_subject_is_contained_and_bottom_aligned(self) -> None:
        source = Image.new("RGB", (100, 200), (40, 50, 60))
        ImageDraw.Draw(source).rectangle((0, 0, 99, 199), outline=(210, 40, 40), width=4)
        base = Image.new("RGB", (1080, 1350), PAPER)
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "subject.png"
            source.save(source_path)
            merged = image_gen._composite_subject_reference(base, str(source_path))

        self.assertGreater(merged.getpixel((405, TEXT_PANEL_BOTTOM))[0], 180)
        self.assertGreater(merged.getpixel((674, RAIL_DIVIDER_Y - 1))[0], 180)
        self.assertEqual(merged.getpixel((404, RAIL_DIVIDER_Y - 1)), PAPER)
        self.assertEqual(merged.getpixel((675, RAIL_DIVIDER_Y - 1)), PAPER)


if __name__ == "__main__":
    unittest.main()
