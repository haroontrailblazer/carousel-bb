from __future__ import annotations

import unittest

from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.schemas import CopySet
from app.agents.template_design import _body_display_numbers
from app.tools.brand_layout import (
    ACCENT_GREEN,
    BODY_FONT_SIZE,
    BODY_MIN_FONT_SIZE,
    HEADLINE_FONT_SIZE,
    HEADLINE_MIN_FONT_SIZE,
    INK,
    PAPER,
    RAIL_DIVIDER_Y,
    SLIDE_NUMBER_LEFT,
    SLIDE_NUMBER_TOP,
    TEXT_PANEL_BOTTOM,
    _fit_typography_layout,
    anchor_dominant_visual_to_divider,
    apply_cta_brand_rail,
    apply_slide_typography,
)
from app.tools import brand_identity


class BrandLayoutTests(unittest.TestCase):
    def test_accent_green_uses_current_brand_value(self) -> None:
        self.assertEqual(ACCENT_GREEN, (143, 184, 50))

    def test_typography_uses_preferred_sizes_when_copy_is_compact(self) -> None:
        layout = _fit_typography_layout(
            "A SHORT HEADLINE",
            ["One short supporting line."],
            904,
        )
        self.assertEqual(layout.headline_size, HEADLINE_FONT_SIZE)
        self.assertEqual(layout.body_size, BODY_FONT_SIZE)

    def test_typography_scales_within_readable_limits_instead_of_failing(self) -> None:
        headline = (
            "THIS POWERFUL MODEL LOOKS SMART UNTIL REAL PRODUCTION REALITY "
            "PUSHES BACK"
        )
        body = [
            "Benchmarks reward the cleanest possible operating conditions.",
            "Production adds latency, ambiguity, failures, and changing context.",
            "That gap is where impressive autonomous agent demos break.",
        ]
        layout = _fit_typography_layout(headline, body, 904)
        self.assertTrue(
            layout.headline_size < HEADLINE_FONT_SIZE
            or layout.body_size < BODY_FONT_SIZE
        )
        self.assertGreaterEqual(layout.headline_size, HEADLINE_MIN_FONT_SIZE)
        self.assertGreaterEqual(layout.body_size, BODY_MIN_FONT_SIZE)
        self.assertLessEqual(layout.total_height, 620 - 140)

        image = Image.new("RGB", (1080, 1350), PAPER)
        rendered = apply_slide_typography(image, headline, body)
        self.assertEqual(rendered.size, (1080, 1350))

    def test_body_counter_starts_at_one_and_ignores_cover_index(self) -> None:
        copy = CopySet.model_validate(
            {
                "slides": [
                    {"index": 4, "lines": ["Third body slide"]},
                    {"index": 2, "lines": ["First body slide"]},
                    {"index": 3, "lines": ["Second body slide"]},
                ],
                "caption": "",
            }
        )
        self.assertEqual(_body_display_numbers(copy.slides), {2: 1, 3: 2, 4: 3})

    def test_cta_rail_leaves_top_counter_zone_unnumbered(self) -> None:
        image = Image.new("RGB", (1080, 1350), INK)
        # The rails draw the RUN's account now, so one has to be in context;
        # see tests/test_brand_rail_account.py for why there is no default.
        with brand_identity.use(
            brand_identity.BrandIdentity(handle="@acme", favicon_png=b"")
        ):
            rendered = apply_cta_brand_rail(image, "@acme")
        number_zone = rendered.crop(
            (
                SLIDE_NUMBER_LEFT,
                SLIDE_NUMBER_TOP,
                SLIDE_NUMBER_LEFT + 72,
                SLIDE_NUMBER_TOP + 48,
            )
        )
        self.assertEqual(number_zone.getcolors(maxcolors=2), [(72 * 48, INK)])

    def test_typography_paints_one_coherent_top_background(self) -> None:
        image = Image.new("RGB", (1080, 1350), PAPER)
        ImageDraw.Draw(image).rectangle((0, 132, 1080, 620), fill=(22, 24, 17))
        rendered = apply_slide_typography(
            image,
            "A MOTHER-SON TEAM MADE IT",
            ["A short supporting line."],
        )
        self.assertEqual(rendered.getpixel((20, 20)), PAPER)
        self.assertEqual(rendered.getpixel((20, 300)), PAPER)

    def test_dominant_visual_is_bottom_aligned_without_stretching(self) -> None:
        image = Image.new("RGB", (1080, 1350), PAPER)
        ImageDraw.Draw(image).rectangle(
            (0, TEXT_PANEL_BOTTOM, 1079, RAIL_DIVIDER_Y - 70),
            fill=(30, 30, 30),
        )
        before_rows = sum(
            image.getpixel((540, y)) == (30, 30, 30)
            for y in range(TEXT_PANEL_BOTTOM, RAIL_DIVIDER_Y)
        )
        rendered = anchor_dominant_visual_to_divider(image)
        self.assertLess(sum(rendered.getpixel((540, RAIL_DIVIDER_Y - 1))), 200)
        after_rows = sum(
            rendered.getpixel((540, y)) == (30, 30, 30)
            for y in range(TEXT_PANEL_BOTTOM, RAIL_DIVIDER_Y)
        )
        self.assertEqual(after_rows, before_rows)

    def test_slide_copy_rejects_alternate_script_parentheticals(self) -> None:
        with self.assertRaises(ValidationError):
            CopySet.model_validate(
                {
                    "slides": [
                        {
                            "index": 4,
                            "lines": ["Xin Yumeng (\u4fe1\u96e8\u840c) directed it."],
                        }
                    ],
                    "caption": "",
                }
            )


if __name__ == "__main__":
    unittest.main()
