"""Template Design agent - renders the carousel BODY slides as PNG artifacts.

Builds an :class:`~google.adk.agents.LlmAgent` (utility model) whose single
tool, :func:`render_body_slides`, reads the approved ``CarouselPlan`` /
``CopySet`` from session state, renders one 1080x1350 PNG per body slide with
:func:`app.tools.image_gen.generate_slide_image` (copy passed VERBATIM), saves
each PNG through ``tool_context.save_artifact`` and writes the resulting
``list[RenderedSlide]`` to ``state[K_BODY_SLIDES]``.

The template reference image is discovered from the "Body slide template"
section of ``skills/design-skill.md`` (markdown image, backticked path, or a
bare ``*.png/*.jpg/*.webp`` token). Until the designer delivers a real
template, no reference is found and the renderer falls back to the full style
prompt built from the same skill file.
"""

from __future__ import annotations

import asyncio
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from PIL import Image

from app.config import agent_instructions, load_skill, settings
from app.llm import resolve_model
from app.schemas import (
    CarouselPlan,
    CopySet,
    CoverSpec,
    NewsItem,
    RenderedSlide,
    ResearchBrief,
    SlideCopy,
    SlidePlan,
)
from app.state import (
    AGENT_TEMPLATE_DESIGN,
    K_BODY_SLIDES,
    K_COVER,
    K_COPY,
    K_NEWS_ITEM,
    K_PLAN,
    K_RESEARCH,
    K_RUN_ID,
    get_model,
)
from app.tools import image_gen

logger = logging.getLogger(__name__)

# Patterns that recognise a template image reference inside design-skill.md:
# markdown image ![alt](path), a `backticked/path.png`, or a bare token.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_BACKTICK_IMAGE_RE = re.compile(r"`([^`]+\.(?:png|jpe?g|webp))`", re.IGNORECASE)
_BARE_IMAGE_RE = re.compile(r"([\w\-./\\]+\.(?:png|jpe?g|webp))\b", re.IGNORECASE)

_DEFAULT_INSTRUCTION = """\
# Template Design agent

You render the BODY slides of an Instagram carousel - every slide between the
cover and the final CTA - as 1080x1350 (4:5) PNG images that follow the design
system in skills/design-skill.md (ink/paper rhythm, one lime-accent element,
content-aware layout archetype, deterministic body-only slide-number tag, and swipe-cue
arrow). Generate the lower visual directly for its exact 2:1 panel so it meets
the divider naturally without stretching or cropping. Keep all important
subjects fully visible in that frame.

## How to work

1. Call the `render_body_slides` tool exactly once (pass no arguments on a
   normal run). The tool reads the approved plan and copy from session state,
   renders one PNG per body slide, saves each PNG as an artifact and records
   the rendered slide list in state. You never retype, rewrite, summarise or
   "improve" the copy yourself - the tool passes the approved copy to the
   image renderer VERBATIM, and a downstream QA agent rejects slides whose
   rendered text drifts from the approved copy.
2. If the tool returns status "error", read its message. If some slides were
   rendered before the failure, retry ONCE with `indices` set to only the
   failed slide indices from the message; otherwise retry once with no
   arguments. If it fails again, report the error plainly and stop.
3. On success, answer with one short line per rendered slide in the form
   "slide <index> -> <artifact filename>", plus which template was used.

## Rework feedback (highest priority when present)

{rework_feedback?}

If reviewer feedback appears directly above this line, it is your
HIGHEST-PRIORITY instruction and overrides everything else:
- If the feedback names specific slides (for example "slide 3 looks cramped"),
  call `render_body_slides` with `indices` set to just those carousel slide
  numbers so only they are re-rendered; the other slides are kept.
- If the feedback is about the copy or the plan, the upstream agents have
  already updated state - re-render ALL body slides (no arguments) so every
  slide reflects the corrected copy.
- If the feedback is general ("design feels off", "too busy"), re-render ALL
  body slides and mention in your reply that a template/design change may need
  the designer's attention in skills/design-skill.md.

## Distilled feedback from past runs

{recent_feedback_notes?}

Apply any relevant distilled rules above when deciding what to re-render and
what to flag in your reply.
"""


def _ensure_default_instruction_file() -> None:
    """Write the fallback instruction to ``skills/agents/template_design.md`` if absent.

    The file is the editable source of truth for this agent's instruction, so
    it is only created when missing - an existing file is never overwritten.
    (The Learner routes template_design rules to ``skills/design-skill.md``,
    but seeding this file too guarantees any rule ever appended here lands in
    a file already carrying the full default instruction, never a bare stub
    that would shadow it.)
    """
    path = settings.skills_dir / "agents" / f"{AGENT_TEMPLATE_DESIGN}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_INSTRUCTION, encoding="utf-8")


def _skill_section(markdown: str, heading_hint: str) -> str:
    """Return the body of the first ``##`` section whose heading contains hint.

    Args:
        markdown: Full markdown text of the skill file.
        heading_hint: Case-insensitive substring to look for in ``##`` headings.

    Returns:
        The section body (text until the next ``##`` heading), or ``""`` when
        no matching section exists.
    """
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    hint = heading_hint.lower()
    for heading, body in sections.items():
        if hint in heading.lower():
            return "\n".join(body)
    return ""


def _discover_template_ref(heading_hint: str) -> str:
    """Find a template reference image path in a design-skill.md section.

    Scans the section of ``skills/design-skill.md`` whose heading contains
    ``heading_hint`` for a markdown image, a backticked image path, or a bare
    image-file token. Returns ``""`` when the section holds no image reference
    (the placeholder state until the designer delivers real templates), which
    makes :mod:`app.tools.image_gen` fall back to its style prompt.

    Args:
        heading_hint: Case-insensitive substring of the target ``##`` heading.

    Returns:
        The template reference (path string) or ``""``.
    """
    section = _skill_section(load_skill("design-skill.md"), heading_hint)
    if not section:
        return ""
    for pattern in (_MD_IMAGE_RE, _BACKTICK_IMAGE_RE, _BARE_IMAGE_RE):
        match = pattern.search(section)
        if match:
            return match.group(1).strip()
    return ""


def _slide_texts(slide: SlideCopy) -> tuple[str, list[str]]:
    """Split one slide's approved copy into (headline, body lines).

    The phrasing agent produces plain lines with no separate headline field,
    so by convention the FIRST non-empty line is the slide headline and the
    remaining lines are the body. All lines stay verbatim; only empty /
    whitespace-only lines are dropped.

    Args:
        slide: The approved copy for one body slide.

    Returns:
        ``(headline, body_lines)``; headline is ``""`` for an all-empty slide.
    """
    lines = [line.strip() for line in slide.lines if line and line.strip()]
    if not lines:
        return "", []
    return lines[0], lines[1:]


def _layout_hint(slide: SlideCopy) -> str:
    """Choose a content-aware visual archetype without changing copy/state."""
    text = " ".join(slide.lines).lower()
    if re.search(r"\b(vs\.?|versus|compare|comparison|before|after|old|new)\b", text):
        return "comparison"
    number_tokens = re.findall(r"\b\d[\d,]*\b", text)
    has_non_year_number = any(
        not (token.isdigit() and 1900 <= int(token) <= 2099)
        for token in number_tokens
    )
    if (
        re.search(r"\d[\d,.]*\s*%|[$€£₹]\s*\d", text)
        or has_non_year_number
    ):
        return "data evidence"
    if re.search(
        r"\b(step|process|workflow|first|then|next|finally|loop|sequence)\b|(?:->|→)",
        text,
    ):
        return "process line"
    if re.search(
        r"\b(code|api|function|command|error|exception|prompt|query|tool|model)\b",
        text,
    ):
        return "dark proof"
    return "editorial explainer" if slide.index % 2 == 0 else "statement pause"


def _body_display_numbers(slides: list[SlideCopy]) -> dict[int, int]:
    """Map carousel-wide body indexes to their visible 01..0N sequence."""
    ordered = sorted(slides, key=lambda slide: slide.index)
    return {
        slide.index: position for position, slide in enumerate(ordered, start=1)
    }


def _visual_context(
    news: NewsItem | None,
    research: ResearchBrief | None,
    plan_slide: SlidePlan | None,
) -> str:
    """Build a compact source-of-truth block for visual generation."""
    sections: list[str] = []
    if news is not None:
        if news.title.strip():
            sections.append(f"Exact news item: {news.title.strip()}")
        if news.summary.strip():
            sections.append(f"News summary: {news.summary.strip()}")
    if research is not None:
        if research.summary.strip():
            sections.append(f"Verified research: {research.summary.strip()}")
        facts = [fact.fact.strip() for fact in research.key_facts if fact.fact.strip()]
        if facts:
            sections.append("Verified facts: " + " | ".join(facts[:6]))
    if plan_slide is not None:
        if plan_slide.purpose.strip():
            sections.append(f"This slide's purpose: {plan_slide.purpose.strip()}")
        points = [point.strip() for point in plan_slide.key_points if point.strip()]
        if points:
            sections.append("This slide's approved points: " + " | ".join(points))
    return "\n".join(sections)[:2800]


def _needs_subject_reference(plan_slide: SlidePlan | None) -> bool:
    """True when the slide must visibly identify the exact news subject."""
    if plan_slide is None:
        return False
    purpose = plan_slide.purpose.lower()
    return bool(
        re.search(
            r"\b(introduce|identify|identity|subject|meet)\b|"
            r"\b(show|explain) what\b|\b(who|what) (is|are)\b",
            purpose,
        )
    )


async def _cover_subject_reference(
    tool_context: ToolContext,
    cover: CoverSpec | None,
    out_dir: Path,
) -> str:
    """Extract the sourced top media zone from the current cover poster."""
    if cover is None or not cover.poster_artifact.strip():
        return ""
    try:
        part = await tool_context.load_artifact(cover.poster_artifact)
    except Exception as exc:
        logger.warning("Could not load cover poster for subject grounding: %s", exc)
        return ""
    data = (
        part.inline_data.data
        if part is not None and part.inline_data is not None
        else None
    )
    if not data:
        logger.warning("Cover poster has no inline image bytes for subject grounding.")
        return ""
    try:
        with Image.open(BytesIO(data)) as source:
            rgb = source.convert("RGB")
            crop_bottom = max(1, round(rgb.height * 0.62))
            subject = rgb.crop((0, 0, rgb.width, crop_bottom))
            path = out_dir / "news-subject-reference.png"
            subject.save(path, format="PNG")
    except Exception as exc:
        logger.warning("Could not extract cover subject reference: %s", exc)
        return ""
    return str(path)


def _run_workdir(state: Any) -> Path:
    """Return (and create) the slide output directory for the current run."""
    run_id = str(state.get(K_RUN_ID) or "run")
    out_dir = settings.workdir / run_id / "slides"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


async def render_body_slides(
    tool_context: ToolContext,
    indices: Optional[list[int]] = None,
) -> dict:
    """Render the carousel body slides as PNG artifacts from the approved copy.

    Reads the approved plan and copy from session state, renders one
    1080x1350 PNG per body slide with the design-system template (the approved
    copy is passed to the renderer VERBATIM - never edited), saves each PNG as
    a session artifact, and writes the rendered slide list to state.

    Args:
        indices: Optional list of carousel slide numbers (matching the copy
            slide indices, where slide 1 is the cover so body slides start
            at 2) to re-render selectively during rework. Omit to render ALL
            body slides.

    Returns:
        On success: {"status": "ok", "template_used": str, "count": int,
        "rendered": [{"index": int, "artifact": str}, ...]} plus an optional
        "warning". On failure: {"status": "error", "message": str} with any
        slides rendered before the failure already saved into state.
    """
    state = tool_context.state
    copy_set = get_model(state, K_COPY, CopySet)
    if copy_set is None or not copy_set.slides:
        return {
            "status": "error",
            "message": (
                f"No approved copy found in state['{K_COPY}'] - the phrasing "
                "agent must run before body slides can be rendered."
            ),
        }
    plan = get_model(state, K_PLAN, CarouselPlan)
    news = get_model(state, K_NEWS_ITEM, NewsItem)
    research = get_model(state, K_RESEARCH, ResearchBrief)
    cover = get_model(state, K_COVER, CoverSpec)

    all_slides = sorted(copy_set.slides, key=lambda s: s.index)
    # Planner/copy indexes remain carousel-wide (cover=1, body starts at 2),
    # but the visible counter belongs only to the body sequence. Keep those
    # concerns separate so the first inside slide is always 01 and the final
    # inside slide is the number of body slides, even during a partial rerender.
    display_numbers = _body_display_numbers(all_slides)
    slides = all_slides
    wanted = {int(i) for i in indices} if indices else None
    if wanted is not None:
        slides = [s for s in slides if s.index in wanted]
        if not slides:
            return {
                "status": "error",
                "message": (
                    f"No copy slides match indices {sorted(wanted)}; available "
                    f"indices are {[s.index for s in copy_set.slides]}."
                ),
            }

    warning = ""
    if plan is not None and wanted is None and len(copy_set.slides) != len(plan.slides):
        warning = (
            f"Copy has {len(copy_set.slides)} body slides but the plan expects "
            f"{len(plan.slides)}; rendering the copy as the source of truth."
        )

    template_ref = _discover_template_ref("Body slide template")
    out_dir = _run_workdir(state)
    plan_slides = {slide.index: slide for slide in plan.slides} if plan is not None else {}
    needs_reference = any(
        _needs_subject_reference(plan_slides.get(slide.index)) for slide in slides
    )
    subject_reference = (
        await _cover_subject_reference(tool_context, cover, out_dir)
        if needs_reference
        else ""
    )

    # Partial re-renders merge into the existing rendered list; full renders
    # replace it (dropping stale entries for slides no longer in the copy).
    existing: dict[int, dict] = {}
    if wanted is not None:
        for entry in state.get(K_BODY_SLIDES) or []:
            model = RenderedSlide.model_validate(entry)
            existing[model.index] = model.model_dump(mode="json")

    rendered: list[dict] = []
    for slide in slides:
        headline, body_lines = _slide_texts(slide)
        if not headline:
            return {
                "status": "error",
                "message": f"Copy slide {slide.index} has no non-empty lines.",
            }
        filename = f"slide_{slide.index:02d}.png"
        out_path = out_dir / filename
        try:
            # generate_slide_image blocks on a slow image API call - keep the
            # event loop free by running it in a worker thread.
            layout_hint = _layout_hint(slide)
            plan_slide = plan_slides.get(slide.index)
            written = await asyncio.to_thread(
                image_gen.generate_slide_image,
                template_ref,
                body_lines,
                headline,
                display_numbers[slide.index],
                str(out_path),
                layout_hint,
                _visual_context(news, research, plan_slide),
                subject_reference if _needs_subject_reference(plan_slide) else "",
            )
            png_bytes = Path(written).read_bytes()
            await tool_context.save_artifact(
                filename,
                types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the LLM
            logger.exception("Body slide %s failed to render.", slide.index)
            # Persist whatever succeeded so a retry can target just the rest.
            if rendered or existing:
                merged = {**existing, **{r["index"]: r for r in rendered}}
                state[K_BODY_SLIDES] = [merged[i] for i in sorted(merged)]
            done = [r["index"] for r in rendered]
            return {
                "status": "error",
                "message": (
                    f"Rendering slide {slide.index} failed: {exc}. Slides "
                    f"{done or 'none'} rendered before the failure are saved; "
                    f"retry with indices covering slide {slide.index} onward."
                ),
            }
        rendered.append(
            RenderedSlide(
                index=slide.index,
                artifact=filename,
                template_used=template_ref,
            ).model_dump(mode="json")
        )
        logger.info("Body slide %02d rendered -> artifact %s", slide.index, filename)

    merged = {**existing, **{r["index"]: r for r in rendered}}
    state[K_BODY_SLIDES] = [merged[i] for i in sorted(merged)]

    result: dict = {
        "status": "ok",
        "template_used": template_ref or "style-prompt fallback (no template image yet)",
        "count": len(rendered),
        "rendered": [{"index": r["index"], "artifact": r["artifact"]} for r in rendered],
    }
    if warning:
        result["warning"] = warning
    return result


def build_template_design_agent() -> LlmAgent:
    """Build the Template Design agent.

    Returns:
        An ``LlmAgent`` named ``AGENT_TEMPLATE_DESIGN`` on the utility model
        with the single ``render_body_slides`` tool. State is written by the
        tool (``K_BODY_SLIDES``); the agent itself carries no output_schema.
    """
    _ensure_default_instruction_file()
    instruction = agent_instructions(AGENT_TEMPLATE_DESIGN) or _DEFAULT_INSTRUCTION
    return LlmAgent(
        name=AGENT_TEMPLATE_DESIGN,
        model=resolve_model(settings.utility_model),
        description=(
            "Renders the carousel body slides as 1080x1350 PNG artifacts from "
            "the approved copy, using the design-system template."
        ),
        instruction=instruction,
        tools=[FunctionTool(render_body_slides)],
        # Orchestrator-driven pipeline node: never LLM-transfer elsewhere.
        # google-adk 2.7 picks SingleFlow only when BOTH flags are set (see
        # LlmAgent._llm_flow); without them this agent gets AutoFlow, which
        # hands the model a transfer_to_agent tool naming every peer in the
        # tree. CarouselOrchestrator._drive re-yields child events without
        # checking their author, so a transfer would look like a finished
        # stage and the phase machine would move to QA with K_BODY_SLIDES
        # unwritten or holding the previous round's renders.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
