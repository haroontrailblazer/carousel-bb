"""Slide and CTA image generation via the OpenAI Images API (gpt-image-2).

Exposes plain, type-hinted functions (wrapped in ``FunctionTool`` by the
Template Design and CTA agents):

- :func:`generate_slide_image` - renders one body slide PNG.
- :func:`generate_cta_image` - renders the closing CTA slide PNG.

Rendering contract (see docs/CONTRACTS.md + skills/design-skill.md):

- The image model creates a text-free visual layer. Approved copy is composited
  afterward with Pillow, so headline and body typography are identical across
  every render and remain verbatim.
- A real news-subject reference can be supplied for slides that identify a
  film, person, product, character, or event. It is composited into the lower
  visual zone after generation and is never used as an edit-template input.
- Without a reference, ``images.generate`` is used with a detailed style
  prompt built from skills/design-skill.md.
- The visual slot is exactly 1080x540 (2:1), from y=620 through y=1160.
  gpt-image-2 generation happens at a divisible-by-16 canvas of the same
  exact 2:1 ratio (see ``_GEN_SIZES``), then the complete visual is merged
  into that slot without stretching or cropping before deterministic
  typography and rails are added.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    OpenAI,
    OpenAIError,
)
from PIL import Image, ImageOps

from app import observability
from app.config import load_skill, settings
from app.text_rules import require_no_em_dash, require_readable_text
from app.tools.brand_layout import (
    INK,
    PAPER,
    RAIL_DIVIDER_Y,
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
    TEXT_PANEL_BOTTOM,
    apply_body_brand_rail,
    apply_cta_brand_rail,
    apply_slide_typography,
    normalize_accent_green,
)

logger = logging.getLogger(__name__)

# The lower visual slot is 1080x540 (exact 2:1). gpt-image-2 takes arbitrary
# WIDTHxHEIGHT sizes as long as both axes divide by 16, so every rung below is
# exactly 2:1 and divisible by 16; the result is downscaled proportionally on
# both axes before it is merged into the slide, so which rung was used never
# changes the output.
#
# It is a LADDER rather than one constant because the API also enforces a
# minimum pixel budget that is not part of the published size contract - it is
# described as "current", and it moved. 1088x544 (0.59 MP) rendered every
# carousel until the floor rose above it, and then every body slide and CTA
# failed with:
#
#   Invalid size '1088x544'. Requested resolution is below the current
#   minimum pixel budget.
#
# Hardcoding a bigger number just moves the same breakage into the future. So
# the first rung clears a 1 MP floor (1024x1024 is a documented supported
# size, so the floor cannot be above it), and if the API rejects a rung for
# being too small, _call_images_api climbs to the next one and the process
# remembers it. A rejected size costs nothing: the API refuses it before
# generating, so no image is billed.
_GEN_SIZES: tuple[tuple[int, int], ...] = (
    (1536, 768),    # 1.18 MP
    (2048, 1024),   # 2.10 MP
    (2560, 1280),   # 3.28 MP - the last rung before "experimental" resolutions
)
_gen_size_index: int = 0
_gen_size_lock = threading.Lock()

_VISUAL_WIDTH: int = SLIDE_WIDTH
_VISUAL_HEIGHT: int = RAIL_DIVIDER_Y - TEXT_PANEL_BOTTOM


def _gen_size() -> str:
    """The size string for the rung this process is currently using."""
    with _gen_size_lock:
        width, height = _GEN_SIZES[_gen_size_index]
    return f"{width}x{height}"


def _climb_size_ladder(current: str) -> bool:
    """Step up one rung. False when there is nothing bigger left to try.

    Takes the size that failed so two threads racing on the same rejection
    advance the ladder once between them rather than once each.
    """
    global _gen_size_index
    with _gen_size_lock:
        # Read the index directly rather than calling _gen_size(): the lock is
        # a plain Lock, not an RLock, so taking it again here would deadlock
        # the render thread the moment a size was ever refused.
        width, height = _GEN_SIZES[_gen_size_index]
        if f"{width}x{height}" != current:
            return True  # another caller already climbed; retry on their rung
        if _gen_size_index + 1 >= len(_GEN_SIZES):
            return False
        _gen_size_index += 1
        width, height = _GEN_SIZES[_gen_size_index]
    logger.warning(
        "Image size %s was refused as below the API's minimum pixel budget; "
        "retrying at %dx%d.",
        current,
        width,
        height,
    )
    return True


def _is_size_too_small(exc: Exception) -> bool:
    """True for the 400 that means "this resolution is under the floor".

    Matched on the size-related wording rather than the status code alone: a
    400 can also mean a rejected prompt, and climbing the ladder for that would
    burn every rung on an error more pixels cannot fix.
    """
    if not isinstance(exc, APIStatusError) or exc.status_code != 400:
        return False
    message = str(exc).lower()
    return "size" in message and (
        "pixel budget" in message or "minimum" in message or "too small" in message
    )

# Image generation is slow; give the API a generous but explicit budget.
_REQUEST_TIMEOUT_S: float = 300.0
# Downloading a result URL (rare fallback path) is fast by comparison.
_DOWNLOAD_TIMEOUT_S: float = 60.0
# One retry on transient failure, after a short pause.
_RETRY_DELAY_S: float = 5.0

_client_singleton: Optional[OpenAI] = None

# Inline fallback used only when skills/design-skill.md is missing on disk.
_FALLBACK_STYLE = """
Design system for an exact 2:1 lower visual panel that will be merged into a
1080x1350 editorial social slide. Alternate ink (#161811) and paper (#F7F7F5)
backgrounds. Use warm white (#E8E4D6) or ink (#1A1A18)
text and exactly #8FB832 green for exactly ONE emphasized element. Never use
another green shade, tint, gradient, glow, or color variation for highlights.
Bricolage-style bold grotesk headlines, clean Instrument-style body text,
one dominant explanatory visual composed fully inside the wide panel. Choose
an editorial explainer, data proof, process line, comparison,
dark technical proof, or statement-pause layout based on the content. Never use
a repeated card grid. Keep every important subject fully visible and let the
visual meet the panel edges naturally. CTA visuals use the same family and one
clear action.
""".strip()

_NO_TEXT_RULE = (
    "CRITICAL VISUAL-LAYER RULE: this output is only the standalone 2:1 lower "
    "visual panel, not a full carousel slide. Fill the complete canvas with the "
    "finished visual composition and keep every important subject fully inside "
    "the frame. Do not create a header, text panel, footer, or empty reserved "
    "bands. Render no text, letters, digits, labels, "
    "captions, logos, handles, watermarks, pseudo-text, or writing-like marks "
    "anywhere. Typography is added deterministically after this panel is merged "
    "into the slide. If a chart, interface, sign, poster, or diagram normally contains "
    "writing, remove its labels and use only clear unlabeled shapes. Do not "
    "use lime or any green accent in the visual layer because the typography "
    "layer owns the slide's single #8FB832 emphasis. Generated illustration "
    "uses only paper, ink, warm white, and muted neutral tones. Do not use red, "
    "orange, blue, purple, or unrelated gradients. Real source-image colors "
    "are allowed only in the sourced image composited after generation."
)


def _client() -> OpenAI:
    """Return a lazily created OpenAI client.

    The API key comes from the ``OPENAI_API_KEY`` environment variable, which
    ``app.config`` loads from ``.env`` on import (no key is ever hard-coded).
    SDK auto-retries are disabled so this module's own single-retry policy is
    the only retry in play.
    """
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = OpenAI(timeout=_REQUEST_TIMEOUT_S, max_retries=0)
    return _client_singleton


def _style_prompt() -> str:
    """Load the design system text from skills/design-skill.md (with fallback)."""
    skill = load_skill("design-skill.md").strip()
    return skill if skill else _FALLBACK_STYLE


def _template_file(template_ref: str) -> Optional[Tuple[str, bytes, str]]:
    """Resolve ``template_ref`` to an upload tuple, or ``None`` if unusable.

    Accepts an absolute path or a path relative to the project skills dir /
    workdir. Returns ``(filename, content_bytes, mime_type)`` suitable for the
    OpenAI SDK ``image`` parameter.
    """
    if not template_ref or not template_ref.strip():
        return None
    candidates = [
        Path(template_ref),
        settings.skills_dir / template_ref,
        settings.workdir / template_ref,
    ]
    for candidate in candidates:
        if candidate.is_file():
            mime, _ = mimetypes.guess_type(candidate.name)
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                mime = "image/png"
            return (candidate.name, candidate.read_bytes(), mime)
    logger.warning("Template reference %r not found; using style-prompt fallback.", template_ref)
    return None


def _is_transient(exc: Exception) -> bool:
    """True for failures worth one retry (timeouts, connection drops, 429/5xx)."""
    if isinstance(exc, APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, httpx.TimeoutException) or isinstance(exc, httpx.TransportError):
        return True
    return False


def _call_images_api(prompt: str, template: Optional[Tuple[str, bytes, str]]) -> bytes:
    """Call images.edit (with template) or images.generate, returning PNG bytes.

    Handles the ``b64_json`` response (default for gpt-image models) with a
    URL-download fallback, and performs exactly one retry on transient failure.
    """
    client = _client()
    last_exc: Optional[Exception] = None
    # One retry for transient faults, plus one attempt per remaining ladder
    # rung. A size rejection is not transient - the same request will be
    # refused forever - so it consumes a ladder step instead of the retry.
    attempts = 2 + len(_GEN_SIZES)
    transient_retry_used = False
    for attempt in range(attempts):
        size = _gen_size()
        try:
            if template is not None:
                response = client.images.edit(
                    model=settings.image_model,
                    image=template,
                    prompt=prompt,
                    size=size,
                    n=1,
                    quality="high",
                    output_format="png",
                    timeout=_REQUEST_TIMEOUT_S,
                )
            else:
                response = client.images.generate(
                    model=settings.image_model,
                    prompt=prompt,
                    size=size,
                    n=1,
                    quality="high",
                    output_format="png",
                    timeout=_REQUEST_TIMEOUT_S,
                )
            observability.record_image_usage(
                model=settings.image_model,
                endpoint="images.edit" if template is not None else "images.generate",
                usage=getattr(response, "usage", None),
                prompt=prompt,
            )
            if not response.data:
                raise RuntimeError("OpenAI Images API returned an empty data list.")
            item = response.data[0]
            if item.b64_json:
                return base64.b64decode(item.b64_json)
            if item.url:
                download = httpx.get(item.url, timeout=_DOWNLOAD_TIMEOUT_S)
                download.raise_for_status()
                return download.content
            raise RuntimeError("OpenAI Images API returned neither b64_json nor url.")
        except (OpenAIError, httpx.HTTPError, RuntimeError) as exc:
            last_exc = exc
            if _is_size_too_small(exc) and _climb_size_ladder(size):
                continue
            if not transient_retry_used and _is_transient(exc):
                transient_retry_used = True
                logger.warning(
                    "Transient image API failure (%s); retrying once in %.0fs.",
                    exc,
                    _RETRY_DELAY_S,
                )
                time.sleep(_RETRY_DELAY_S)
                continue
            raise
    raise RuntimeError(f"Image generation failed after retry: {last_exc}")  # pragma: no cover


def _contain_without_crop(
    image: Image.Image,
    target: tuple[int, int],
    background: tuple[int, int, int],
    *,
    bottom_align: bool = True,
) -> Image.Image:
    """Fit the complete image into ``target`` without cropping or distortion."""
    source = image.convert("RGB")
    fitted = ImageOps.contain(source, target, method=Image.Resampling.LANCZOS)
    panel = Image.new("RGB", target, background)
    left = (target[0] - fitted.width) // 2
    top = target[1] - fitted.height if bottom_align else (target[1] - fitted.height) // 2
    panel.paste(fitted, (left, top))
    return panel


def _finalize(
    png_bytes: bytes,
    out_path: str,
    *,
    theme: str = "paper",
) -> str:
    """Merge an exact-aspect generated visual into the full carousel slide."""
    background = INK if theme == "ink" else PAPER
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(png_bytes)) as img:
        visual = _contain_without_crop(
            img,
            (_VISUAL_WIDTH, _VISUAL_HEIGHT),
            background,
        )
    slide = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), background)
    slide.paste(visual, (0, TEXT_PANEL_BOTTOM))
    slide.save(destination, format="PNG")
    return str(destination)


def _composite_subject_reference(
    image: Image.Image,
    visual_reference: str,
) -> Image.Image:
    """Merge the complete sourced image into the visual slot without cropping."""
    result = image.convert("RGB")
    reference = _template_file(visual_reference) if visual_reference else None
    if reference is None:
        return result
    _filename, data, _mime = reference
    with Image.open(BytesIO(data)) as source:
        fitted = ImageOps.contain(
            source.convert("RGB"),
            (_VISUAL_WIDTH, _VISUAL_HEIGHT),
            method=Image.Resampling.LANCZOS,
        )
    left = (SLIDE_WIDTH - fitted.width) // 2
    top = RAIL_DIVIDER_Y - fitted.height
    result.paste(fitted, (left, top))
    return result


def _meaning_block(headline: str, lines: list[str]) -> str:
    """Describe the approved message as visual context, never as image text."""
    body = " | ".join(lines) if lines else "No supporting body copy."
    return (
        "MESSAGE TO EXPLAIN VISUALLY ONLY. Do not render these words: "
        f"headline={headline!r}; supporting ideas={body!r}."
    )


def generate_slide_image(
    template_ref: str,
    copy_lines: list[str],
    headline: str,
    slide_no: int,
    out_path: str,
    layout_hint: str = "editorial explainer",
    visual_context: str = "",
    visual_reference: str = "",
) -> str:
    """Render one body slide (1080x1350 PNG) with gpt-image-2.

    Args:
        template_ref: Path to the template reference image. When the file
            exists the images.edit endpoint reproduces its text-free visual
            layout; when empty/missing, images.generate is used with the full
            style prompt from skills/design-skill.md.
        copy_lines: The approved body copy lines (rendered verbatim, one
            thought per line, in order).
        headline: The slide headline (rendered verbatim, uppercase condensed
            per the design system).
        slide_no: 1-based number within the body-slide sequence, shown as a
            small quiet number tag. The first body slide is "01"; the cover
            and CTA are unnumbered.
        out_path: Destination PNG path; parent directories are created.
        layout_hint: Content-aware archetype chosen by the template agent.
        visual_context: Research-grounded description of the exact news subject,
            the slide purpose, and verified facts relevant to its visual.
        visual_reference: Optional local image containing the real news subject.
            Used as a factual identity reference, not as a text/layout source.

    Returns:
        The absolute/normalized path of the written PNG as a string.
    """
    require_no_em_dash([headline, *copy_lines], "body slide copy")
    require_readable_text([headline, *copy_lines], "body slide copy")
    tag = f"{slide_no:02d}"
    allowed = [
        _NO_TEXT_RULE,
        "",
        f'Use the "{layout_hint}" layout archetype from the design system.',
        _meaning_block(headline, copy_lines),
        (
            "Generate the complete explanatory visual for an exact 2:1 panel. "
            "Compose directly for that wide frame so no element needs to be "
            "stretched or cropped later. Use the full panel edge-to-edge, keep "
            "important subjects fully visible, and let the composition naturally "
            "meet its bottom edge. The runtime merges this panel exactly into "
            "slide coordinates x=0..1080 and y=620..1160."
        ),
    ]
    if visual_context.strip():
        allowed.insert(
            2,
            (
                "VISUAL GROUNDING SOURCE OF TRUTH:\n"
                f"{visual_context.strip()}\n"
                "Every visible subject must correspond to this exact news item. "
                "Do not substitute a different film, person, character, product, "
                "interface, country, studio, or event."
            ),
        )
    text_spec = "\n".join(allowed)
    template = _template_file(template_ref)
    if template is not None:
        prompt = (
            "The attached image is a layout reference. Preserve its visual "
            "composition and spacing, but remove all existing typography and "
            "footer furniture so the output is a clean text-free layer.\n\n"
            + text_spec
        )
        input_image = template
    else:
        reference_note = (
            "A sourced subject image will be composited into the lower visual "
            "zone after generation. Create only a restrained brand-consistent "
            "frame and do not invent or depict a replacement subject.\n\n"
            if visual_reference.strip()
            else ""
        )
        prompt = (
            "Design a single Instagram carousel body slide, portrait 4:5.\n\n"
            f"{_style_prompt()}\n\n{reference_note}{text_spec}"
        )
        input_image = None
    png = _call_images_api(prompt, input_image)
    result = _finalize(png, out_path, theme="paper")
    with Image.open(result) as rendered:
        grounded = _composite_subject_reference(rendered, visual_reference)
        normalized = normalize_accent_green(grounded)
        typeset = apply_slide_typography(
            normalized,
            headline,
            copy_lines,
            theme="paper",
        )
        branded = apply_body_brand_rail(typeset, settings.ig_handle, slide_no)
    branded.save(result, format="PNG")
    logger.info("Rendered body slide %s -> %s", tag, result)
    return result


def generate_cta_image(
    cta_type: str,
    headline: str,
    lines: list[str],
    link_text: str,
    template_ref: str,
    out_path: str,
) -> str:
    """Render the closing CTA slide (1080x1350 PNG) with gpt-image-2.

    Args:
        cta_type: One of ``"follow"``, ``"comment"``, ``"redirect"`` - picks
            the CTA variant per skills/design-skill.md.
        headline: The big centered CTA headline (rendered verbatim, e.g.
            "FOLLOW FOR MORE").
        lines: Supporting lines (question, emphasis line, etc.), rendered
            verbatim in order.
        link_text: A redirect destination or configured handle. The handle is
            composited deterministically in the final brand rail; a distinct
            redirect URL remains part of the generated CTA content.
        template_ref: Path to the CTA template reference image; falls back to
            the style prompt when empty/missing.
        out_path: Destination PNG path; parent directories are created.

    Returns:
        The absolute/normalized path of the written PNG as a string.
    """
    require_no_em_dash([headline, *lines, link_text], "CTA image copy")
    require_readable_text([headline, *lines, link_text], "CTA image copy")
    variant_hints = {
        "follow": "Follow CTA: make the value promise and action unmistakable.",
        "comment": "Comment CTA: a question line with a bold 'drop a comment' emphasis.",
        "redirect": "Redirect CTA: point to the full breakdown, with a short link line.",
    }
    hint = variant_hints.get(cta_type, variant_hints["follow"])
    all_lines = [line for line in lines if line and line.strip()]
    normalized_handle = settings.ig_handle.strip().lstrip("@").lower()
    normalized_link = link_text.strip().lstrip("@").lower()
    render_lines = list(all_lines)
    if link_text and normalized_link != normalized_handle:
        render_lines.append(link_text.strip())
    text_parts = [
        _NO_TEXT_RULE,
        "",
        f"CTA variant: {cta_type}. {hint}",
        _meaning_block(headline, render_lines),
    ]
    text_parts.append(
        "Generate the CTA visual as a complete exact 2:1 panel. Compose directly "
        "for that wide frame, use the full canvas, and keep every important "
        "subject fully visible so nothing needs cropping or stretching. The "
        "runtime merges it into y=620..1160 and adds all typography and footer "
        "furniture afterward."
    )
    text_spec = "\n".join(text_parts)

    template = _template_file(template_ref)
    if template is not None:
        prompt = (
            "The attached image is a CTA layout reference. Preserve its visual "
            "composition, but remove all existing typography and footer "
            "furniture so the output is a clean text-free layer.\n\n" + text_spec
        )
    else:
        prompt = (
            "Design the final call-to-action slide of an Instagram carousel, "
            "portrait 4:5.\n\n"
            f"{_style_prompt()}\n\n{text_spec}"
        )
    png = _call_images_api(prompt, template)
    result = _finalize(png, out_path, theme="ink")
    with Image.open(result) as rendered:
        normalized = normalize_accent_green(rendered)
        typeset = apply_slide_typography(
            normalized,
            headline,
            render_lines,
            theme="ink",
        )
        branded = apply_cta_brand_rail(
            typeset,
            settings.ig_handle,
        )
    branded.save(result, format="PNG")
    logger.info("Rendered CTA slide (%s) -> %s", cta_type, result)
    return result
