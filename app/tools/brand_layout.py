"""Deterministic footer furniture for generated slides.

Image models create the editorial content, but the brand favicon, handle,
swipe arrow, and their padding are composited here so every carousel uses
identical geometry and exact text.

The GEOMETRY is fixed; the MARKS are not. Which handle and which profile
picture get drawn belongs to whichever Instagram account the run is targeting,
and comes from ``app.tools.brand_identity``. This module used to load a single
favicon checked into the repository, which was right while the console
published to one account and silently wrong the moment a second was connected.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont, ImageStat

from app.tools import brand_identity


SlideKind = Literal["body", "cta"]

SLIDE_WIDTH = 1080
SLIDE_HEIGHT = 1350
SAFE_LEFT = 88
SAFE_RIGHT = 88
SAFE_TOP = 76
SAFE_BOTTOM = 76

SLIDE_NUMBER_LEFT = SAFE_LEFT
SLIDE_NUMBER_TOP = SAFE_TOP
SLIDE_NUMBER_FONT_SIZE = 32

# Shared editorial headline contract. The deterministic cover compositor uses
# these values directly, while the body-slide image prompt names the same size
# and type treatment so slide 1 no longer looks like a different template.
HEADLINE_FONT_SIZE = 76
HEADLINE_MIN_FONT_SIZE = 60
HEADLINE_MAX_LINES = 3
HEADLINE_STYLE = "condensed bold grotesk"
BODY_FONT_SIZE = 36
BODY_MIN_FONT_SIZE = 30
TEXT_PANEL_TOP = 140
TEXT_PANEL_BOTTOM = 620
TEXT_CONTENT_LEFT = SAFE_LEFT
TEXT_CONTENT_RIGHT = SLIDE_WIDTH - SAFE_RIGHT

RAIL_DIVIDER_Y = 1160
RAIL_FILL_TOP = RAIL_DIVIDER_Y
RAIL_CENTER_Y = 1232
RAIL_RIGHT = SLIDE_WIDTH - SAFE_RIGHT

BODY_FAVICON_SIZE = 56
BODY_FAVICON_LEFT = SAFE_LEFT
BODY_HANDLE_LEFT = 160
BODY_ARROW_LEFT = 944

CTA_FAVICON_SIZE = BODY_FAVICON_SIZE
CTA_FAVICON_LEFT = SAFE_LEFT
CTA_HANDLE_LEFT = BODY_HANDLE_LEFT

INK = (22, 24, 17)
PAPER = (247, 247, 245)
ACCENT_GREEN = (143, 184, 50)
WARM_WHITE = (232, 228, 214)
TEXT_DARK = (26, 26, 24)
MUTED_DARK = (113, 122, 95)
MUTED_LIGHT = (185, 197, 170)


@dataclass(frozen=True)
class _TypographyLayout:
    """One measured, readable typography layout that fits the top panel."""

    head_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    body_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    headline_lines: list[str]
    wrapped_body: list[list[str]]
    headline_size: int
    body_size: int
    head_line_height: int
    body_line_height: int
    head_gap: int
    body_gap: int
    thought_gap: int
    section_gap: int
    total_height: int

# Windows paths first (the dev machine), then the Linux equivalents installed
# by the Dockerfile. Without the Linux entries every lookup falls through to
# ImageFont.load_default() - a tiny bitmap face - and slide typography silently
# collapses in a container instead of failing loudly. DejaVu ships with
# fonts-dejavu-core; Liberation is metrically compatible with Arial.
_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
_BOLD_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/seguisb.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
_HEADLINE_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/bahnschrift.ttf"),
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    # Condensed first - closest to Bahnschrift/Impact for headline weight.
    Path("/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a stable UI font available on Windows, with a Pillow fallback."""
    candidates = _BOLD_FONT_CANDIDATES if bold else _FONT_CANDIDATES
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def headline_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the shared condensed headline face used by the cover system."""
    for path in _HEADLINE_FONT_CANDIDATES:
        if not path.exists():
            continue
        try:
            font = ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
        if "bahnschrift" in path.name.lower():
            try:
                axes = font.get_variation_axes()
                values: list[float] = []
                for axis in axes:
                    name = axis.get("name", b"")
                    if isinstance(name, bytes):
                        name = name.decode("ascii", errors="ignore")
                    lowered = str(name).lower()
                    if "weight" in lowered or lowered == "wght":
                        values.append(min(float(axis["maximum"]), 700.0))
                    elif "width" in lowered or lowered == "wdth":
                        values.append(max(float(axis["minimum"]), 75.0))
                    else:
                        values.append(float(axis["default"]))
                if values:
                    font.set_variation_by_axes(values)
            except OSError:
                pass
        return font
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _line_width(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    text: str,
) -> float:
    """Measure one line using the same font instance used for drawing."""
    return float(font.getlength(text))


def _balanced_wrap(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap a short headline into balanced lines without shrinking its font."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    if _line_width(font, normalized) <= max_width:
        return [normalized]
    words = normalized.split(" ")
    if any(_line_width(font, word) > max_width for word in words):
        raise ValueError("headline contains a word wider than the fixed text area")
    best_overall: tuple[float, list[str]] | None = None
    for line_count in range(2, min(max_lines, len(words)) + 1):
        best: tuple[float, list[str]] | None = None
        for cuts in combinations(range(1, len(words)), line_count - 1):
            boundaries = (0, *cuts, len(words))
            lines = [
                " ".join(words[boundaries[i] : boundaries[i + 1]])
                for i in range(line_count)
            ]
            widths = [_line_width(font, line) for line in lines]
            score = max(widths) + (max(widths) - min(widths)) * 0.12
            if best is None or score < best[0]:
                best = (score, lines)
        if best is None:
            continue
        best_overall = best
        if all(_line_width(font, line) <= max_width for line in best[1]):
            return best[1]
    if best_overall is None or any(
        _line_width(font, line) > max_width for line in best_overall[1]
    ):
        raise ValueError(
            f"headline does not fit at the fixed {HEADLINE_FONT_SIZE}px size "
            f"within {HEADLINE_MAX_LINES} lines"
        )
    return best_overall[1]


def _greedy_wrap(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Wrap body copy at a fixed size while preserving every word verbatim."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    words = normalized.split(" ")
    if any(_line_width(font, word) > max_width for word in words):
        raise ValueError("body copy contains a word wider than the fixed text area")
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _line_width(font, candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _headline_highlight(headline: str) -> str:
    """Choose one stable emphasis phrase without changing the approved copy."""
    words = " ".join(str(headline or "").split()).split(" ")
    if not words:
        return ""
    return " ".join(words[-2:]) if len(words) >= 2 else words[0]


def _fit_typography_layout(
    headline_text: str,
    clean_body: list[str],
    max_width: int,
) -> _TypographyLayout:
    """Choose the largest balanced type pair that fits without changing copy."""
    head_sizes = range(HEADLINE_FONT_SIZE, HEADLINE_MIN_FONT_SIZE - 1, -2)
    body_sizes = range(BODY_FONT_SIZE, BODY_MIN_FONT_SIZE - 1, -1)
    candidates = [
        (head_size, body_size)
        for head_size in head_sizes
        for body_size in body_sizes
    ]
    head_span = max(HEADLINE_FONT_SIZE - HEADLINE_MIN_FONT_SIZE, 1)
    body_span = max(BODY_FONT_SIZE - BODY_MIN_FONT_SIZE, 1)
    candidates.sort(
        key=lambda sizes: (
            max(
                (HEADLINE_FONT_SIZE - sizes[0]) / head_span,
                (BODY_FONT_SIZE - sizes[1]) / body_span,
            ),
            (HEADLINE_FONT_SIZE - sizes[0]) / head_span
            + (BODY_FONT_SIZE - sizes[1]) / body_span,
        )
    )

    for headline_size, body_size in candidates:
        head_font = headline_font(headline_size)
        body_font = _font(body_size)
        try:
            headline_lines = _balanced_wrap(
                headline_text,
                head_font,
                max_width,
                HEADLINE_MAX_LINES,
            )
            wrapped_body = [
                _greedy_wrap(line, body_font, max_width) for line in clean_body
            ]
        except ValueError:
            continue

        head_ascent, head_descent = head_font.getmetrics()
        body_ascent, body_descent = body_font.getmetrics()
        head_line_height = head_ascent + head_descent
        body_line_height = body_ascent + body_descent
        head_gap = max(4, round(head_line_height * 0.06))
        body_gap = max(5, round(body_line_height * 0.14))
        thought_gap = 10
        headline_height = (
            len(headline_lines) * head_line_height
            + max(0, len(headline_lines) - 1) * head_gap
        )
        body_height = sum(
            len(lines) * body_line_height + max(0, len(lines) - 1) * body_gap
            for lines in wrapped_body
        ) + max(0, len(wrapped_body) - 1) * thought_gap
        section_gap = 28 if wrapped_body else 0
        total_height = headline_height + section_gap + body_height
        if TEXT_PANEL_TOP + total_height <= TEXT_PANEL_BOTTOM:
            return _TypographyLayout(
                head_font=head_font,
                body_font=body_font,
                headline_lines=headline_lines,
                wrapped_body=wrapped_body,
                headline_size=headline_size,
                body_size=body_size,
                head_line_height=head_line_height,
                body_line_height=body_line_height,
                head_gap=head_gap,
                body_gap=body_gap,
                thought_gap=thought_gap,
                section_gap=section_gap,
                total_height=total_height,
            )

    raise ValueError(
        "approved slide copy does not fit the typography reservation even at "
        f"the readable minimums ({HEADLINE_MIN_FONT_SIZE}px headline and "
        f"{BODY_MIN_FONT_SIZE}px body); shorten the copy upstream"
    )


def apply_slide_typography(
    image: Image.Image,
    headline: str,
    body_lines: list[str],
    *,
    uppercase_headline: bool = False,
    theme: Literal["auto", "paper", "ink"] = "auto",
) -> Image.Image:
    """Composite readable Baskaran Builds typography over a text-free visual.

    The preferred 76px/36px sizes are used whenever they fit. Longer approved
    copy steps down proportionally within the explicit 60px/30px readable
    limits, preserving every word and the shared type treatment.
    """
    result = image.convert("RGB")
    if theme == "paper":
        background, text_color = PAPER, TEXT_DARK
    elif theme == "ink":
        background, text_color = INK, WARM_WHITE
    else:
        background, text_color, _divider = _visual_field_colors(result)
    headline_text = " ".join(str(headline or "").split())
    if uppercase_headline:
        headline_text = headline_text.upper()
    clean_body = [" ".join(str(line).split()) for line in body_lines if str(line).strip()]

    max_width = TEXT_CONTENT_RIGHT - TEXT_CONTENT_LEFT
    layout = _fit_typography_layout(headline_text, clean_body, max_width)
    head_font = layout.head_font
    body_font = layout.body_font
    headline_lines = layout.headline_lines
    wrapped_body = layout.wrapped_body

    draw = ImageDraw.Draw(result)
    draw.rectangle((0, 0, SLIDE_WIDTH, TEXT_PANEL_BOTTOM), fill=background)
    highlight = _headline_highlight(headline_text)
    highlight_start = headline_text.rfind(highlight) if highlight else -1
    highlight_end = highlight_start + len(highlight) if highlight_start >= 0 else -1
    global_index = 0
    y = TEXT_PANEL_TOP
    for line in headline_lines:
        x = float(TEXT_CONTENT_LEFT)
        for char in line:
            color = ACCENT_GREEN if highlight_start <= global_index < highlight_end else text_color
            draw.text((x, y), char, font=head_font, fill=color)
            x += head_font.getlength(char)
            global_index += 1
        global_index += 1
        y += layout.head_line_height + layout.head_gap
    if headline_lines:
        y -= layout.head_gap
    y += layout.section_gap
    for thought_index, lines in enumerate(wrapped_body):
        for line_index, line in enumerate(lines):
            draw.text(
                (TEXT_CONTENT_LEFT, y),
                line,
                font=body_font,
                fill=text_color,
            )
            y += layout.body_line_height
            if line_index < len(lines) - 1:
                y += layout.body_gap
        if thought_index < len(wrapped_body) - 1:
            y += layout.thought_gap
    return result


def draw_slide_number(
    image: Image.Image,
    slide_no: int | str,
    *,
    fill: tuple[int, int, int] = WARM_WHITE,
) -> None:
    """Draw one fixed two-digit number at the shared top-left anchor."""
    try:
        tag = f"{int(slide_no):02d}"
    except (TypeError, ValueError):
        tag = str(slide_no).strip().zfill(2)
    color: tuple[int, ...] = fill
    if image.mode == "RGBA":
        color = (*fill, 255)
    ImageDraw.Draw(image).text(
        (SLIDE_NUMBER_LEFT, SLIDE_NUMBER_TOP),
        tag,
        font=_font(SLIDE_NUMBER_FONT_SIZE, bold=True),
        fill=color,
        anchor="lt",
    )


def _clear_slide_number_zone(
    image: Image.Image,
    fill: tuple[int, int, int],
) -> None:
    """Clear the fixed number reservation before drawing the exact tag."""
    ImageDraw.Draw(image).rectangle(
        (
            SLIDE_NUMBER_LEFT - 4,
            SLIDE_NUMBER_TOP - 4,
            SLIDE_NUMBER_LEFT + 76,
            SLIDE_NUMBER_TOP + 48,
        ),
        fill=fill,
    )


def normalize_accent_green(image: Image.Image) -> Image.Image:
    """Lock every strong lime accent pixel to the one brand green token."""
    result = image.convert("RGB")
    pixels = list(result.getdata())
    normalized: list[tuple[int, int, int]] = []
    for red, green, blue in pixels:
        is_lime = (
            green >= 135
            and red >= 70
            and blue <= 170
            and green >= red * 1.06
            and red >= blue * 1.18
            and green - red >= 20
            and green - blue >= 55
        )
        normalized.append(ACCENT_GREEN if is_lime else (red, green, blue))
    result.putdata(normalized)
    return result


def _is_light_slide(image: Image.Image) -> bool:
    """Classify a finished slide from its deterministic upper field."""
    sample = image.convert("RGB").crop((24, 180, 80, 250))
    r, g, b = ImageStat.Stat(sample).mean[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b >= 145


def _visual_field_colors(image: Image.Image) -> tuple[tuple[int, int, int], ...]:
    """Infer a theme from a generated lower visual before typography exists."""
    sample = image.convert("RGB").crop(
        (0, TEXT_PANEL_BOTTOM, SLIDE_WIDTH, RAIL_DIVIDER_Y)
    )
    r, g, b = ImageStat.Stat(sample).mean[:3]
    if 0.2126 * r + 0.7152 * g + 0.0722 * b >= 145:
        return PAPER, TEXT_DARK, MUTED_DARK
    return INK, WARM_WHITE, MUTED_LIGHT


def _rail_colors(image: Image.Image) -> tuple[tuple[int, int, int], ...]:
    """Return background, text, divider colors for the slide theme."""
    if _is_light_slide(image):
        return PAPER, TEXT_DARK, MUTED_DARK
    return INK, WARM_WHITE, MUTED_LIGHT


def anchor_dominant_visual_to_divider(image: Image.Image) -> Image.Image:
    """Bottom-align a dominant visual without resizing or cropping its content."""
    result = image.convert("RGB")
    visual_top = TEXT_PANEL_BOTTOM
    visual_bottom = RAIL_DIVIDER_Y
    bottom_band = result.crop((0, visual_bottom - 8, SLIDE_WIDTH, visual_bottom))
    mean = ImageStat.Stat(bottom_band).mean[:3]
    background = tuple(round(channel) for channel in mean)
    pixels = result.load()
    dominant_rows: list[int] = []
    sampled_columns = len(range(0, SLIDE_WIDTH, 4))
    for y in range(visual_top, visual_bottom):
        changed = 0
        for x in range(0, SLIDE_WIDTH, 4):
            red, green, blue = pixels[x, y]
            delta = (
                abs(red - background[0])
                + abs(green - background[1])
                + abs(blue - background[2])
            )
            if delta >= 70:
                changed += 1
        if changed / sampled_columns >= 0.32:
            dominant_rows.append(y)
    if not dominant_rows:
        return result
    last_content = max(dominant_rows)
    if last_content >= visual_bottom - 5:
        return result
    source_height = last_content + 1 - visual_top
    if source_height < 180:
        return result
    source = result.crop((0, visual_top, SLIDE_WIDTH, last_content + 1))
    gap = visual_bottom - (last_content + 1)
    ImageDraw.Draw(result).rectangle(
        (0, visual_top, SLIDE_WIDTH, visual_bottom - 1),
        fill=background,
    )
    result.paste(source, (0, visual_top + gap))
    return result


def _draw_round_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    fill: tuple[int, int, int],
    width: int,
) -> None:
    """Draw one line with round caps."""
    draw.line((start, end), fill=fill, width=width)
    radius = width / 2
    for x, y in (start, end):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def _favicon_from_source(size: int) -> Image.Image:
    """The profile mark of the account this run is publishing to.

    Raises:
        brand_identity.NoBrandIdentity: when no account is in context. That is
            deliberate: there is no safe default here, because drawing some
            other account's logo would mis-brand a post that nobody would
            check before it went live.
    """
    return brand_identity.require_favicon(size)


def _draw_handle(
    image: Image.Image,
    handle: str,
    *,
    left: int,
    center_y: int,
    fill: tuple[int, int, int],
) -> None:
    """Draw the account's handle with exact spelling and vertical centering.

    An empty handle is an error rather than a default. The old fallback named
    one specific account, which on a console with several connected is not a
    safe guess - it is a wrong one that ships.
    """
    text = handle.strip()
    if not text:
        raise brand_identity.NoBrandIdentity(
            "The brand rail was asked to draw an empty handle."
        )
    if not text.startswith("@"):
        text = "@" + text
    font = _font(32)
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), text, font=font)
    height = bbox[3] - bbox[1]
    draw.text((left, center_y - height / 2 - bbox[1]), text, font=font, fill=fill)


def _draw_swipe_arrow(image: Image.Image, fill: tuple[int, int, int]) -> None:
    """Draw the icon-only right arrow inside the safe-area boundary."""
    draw = ImageDraw.Draw(image)
    y = RAIL_CENTER_Y
    width = 4
    _draw_round_line(draw, (BODY_ARROW_LEFT, y), (RAIL_RIGHT, y), fill=fill, width=width)
    _draw_round_line(
        draw,
        (RAIL_RIGHT - 18, y - 17),
        (RAIL_RIGHT, y),
        fill=fill,
        width=width,
    )
    _draw_round_line(
        draw,
        (RAIL_RIGHT - 18, y + 17),
        (RAIL_RIGHT, y),
        fill=fill,
        width=width,
    )


def _prepare_rail(image: Image.Image) -> tuple[int, int, int]:
    """Clear the rail below its divider while preserving visuals that meet it."""
    background, text, divider = _rail_colors(image)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, RAIL_FILL_TOP, SLIDE_WIDTH, SLIDE_HEIGHT), fill=background)
    draw.line((SAFE_LEFT, RAIL_DIVIDER_Y, RAIL_RIGHT, RAIL_DIVIDER_Y), fill=divider, width=2)
    return text


def apply_body_brand_rail(
    image: Image.Image,
    handle: str,
    slide_no: int | str | None = None,
) -> Image.Image:
    """Add the official favicon, exact handle, divider, and arrow."""
    result = image.convert("RGB")
    background, _, _ = _rail_colors(result)
    text = _prepare_rail(result)
    if slide_no is not None:
        _clear_slide_number_zone(result, background)
        draw_slide_number(result, slide_no, fill=text)
    favicon_top = round(RAIL_CENTER_Y - BODY_FAVICON_SIZE / 2)
    favicon = _favicon_from_source(BODY_FAVICON_SIZE)
    result.paste(favicon, (BODY_FAVICON_LEFT, favicon_top), favicon)
    _draw_handle(result, handle, left=BODY_HANDLE_LEFT, center_y=RAIL_CENTER_Y, fill=text)
    _draw_swipe_arrow(result, text)
    return result


def apply_cta_brand_rail(
    image: Image.Image,
    handle: str,
) -> Image.Image:
    """Add only the official favicon and handle to the unnumbered CTA rail."""
    result = image.convert("RGB")
    text = _prepare_rail(result)
    favicon_top = round(RAIL_CENTER_Y - CTA_FAVICON_SIZE / 2)
    favicon = _favicon_from_source(CTA_FAVICON_SIZE)
    result.paste(favicon, (CTA_FAVICON_LEFT, favicon_top), favicon)
    _draw_handle(result, handle, left=CTA_HANDLE_LEFT, center_y=RAIL_CENTER_Y, fill=text)
    return result


def validate_footer_padding(
    data: bytes,
    kind: SlideKind,
    expect_slide_number: bool = False,
) -> list[str]:
    """Validate footer furniture, safe-area geometry, and exact native size.

    Args:
        expect_slide_number: Whether a slide number should be drawn in the
            fixed top-left anchor. Deliberately a BOOLEAN, not the number.

            This used to take the expected value, which read as though the
            check compared it - it never did, and could not: the only test
            available here is pixel variance in the anchor box, which says
            that *something* was drawn, not *what*. Passing a number the
            function ignored made the check look stronger than it is, so a
            wrong number on a slide would have been reported as verified.
            Reading which digits are rendered would need OCR; until something
            does, the honest signature is the one that admits the limit.
    """
    errors: list[str] = []
    try:
        with Image.open(BytesIO(data)) as opened:
            image = opened.convert("RGB")
    except Exception as exc:
        return [f"could not decode PNG for padding validation: {exc}"]
    if image.size != (SLIDE_WIDTH, SLIDE_HEIGHT):
        return [f"expected {SLIDE_WIDTH}x{SLIDE_HEIGHT}, got {image.width}x{image.height}"]

    if not (
        BODY_FAVICON_LEFT >= SAFE_LEFT
        and RAIL_RIGHT <= SLIDE_WIDTH - SAFE_RIGHT
        and RAIL_CENTER_Y + BODY_FAVICON_SIZE / 2
        <= SLIDE_HEIGHT - SAFE_BOTTOM
    ):
        errors.append("footer furniture falls outside the 88/76 px safe area")

    divider = image.crop((SAFE_LEFT, RAIL_DIVIDER_Y - 1, RAIL_RIGHT, RAIL_DIVIDER_Y + 2))
    if len(divider.getcolors(maxcolors=100_000) or []) < 2:
        errors.append("footer divider is missing")

    if kind == "body":
        favicon_top = round(RAIL_CENTER_Y - BODY_FAVICON_SIZE / 2)
        favicon = image.crop(
            (
                BODY_FAVICON_LEFT,
                favicon_top,
                BODY_FAVICON_LEFT + BODY_FAVICON_SIZE,
                favicon_top + BODY_FAVICON_SIZE,
            )
        )
        cream_pixels = sum(
            1
            for r, g, b in favicon.getdata()
            if r > 205 and g > 205 and b > 185
        )
        if cream_pixels < 500 or sum(ImageStat.Stat(favicon).var) < 500:
            errors.append("official Baskaran Builds favicon is missing or mispositioned")
    else:
        favicon_top = round(RAIL_CENTER_Y - CTA_FAVICON_SIZE / 2)
        favicon = image.crop(
            (
                CTA_FAVICON_LEFT,
                favicon_top,
                CTA_FAVICON_LEFT + CTA_FAVICON_SIZE,
                favicon_top + CTA_FAVICON_SIZE,
            )
        )
        cream_pixels = sum(
            1
            for r, g, b in favicon.getdata()
            if r > 205 and g > 205 and b > 185
        )
        if cream_pixels < 500 or sum(ImageStat.Stat(favicon).var) < 500:
            errors.append("official Baskaran Builds favicon is missing from the CTA rail")
    if expect_slide_number:
        number = image.crop(
            (
                SLIDE_NUMBER_LEFT,
                SLIDE_NUMBER_TOP,
                SLIDE_NUMBER_LEFT + 72,
                SLIDE_NUMBER_TOP + 48,
            )
        )
        if sum(ImageStat.Stat(number).var) < 35:
            errors.append("slide number is missing from the fixed top-left anchor")
    return errors


__all__ = [
    "ACCENT_GREEN",
    "BODY_FONT_SIZE",
    "BODY_MIN_FONT_SIZE",
    "HEADLINE_FONT_SIZE",
    "HEADLINE_MAX_LINES",
    "HEADLINE_MIN_FONT_SIZE",
    "HEADLINE_STYLE",
    "apply_body_brand_rail",
    "apply_cta_brand_rail",
    "apply_slide_typography",
    "anchor_dominant_visual_to_divider",
    "draw_slide_number",
    "headline_font",
    "normalize_accent_green",
    "validate_footer_padding",
]
