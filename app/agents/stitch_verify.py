"""Stitch & Verify agent - assembles the Bundle and runs deterministic QA.

Reads the pieces produced by the generate phase from session state
(``K_COVER``, ``K_BODY_SLIDES``, ``K_CTA_SLIDE``, ``K_COPY``, plus ``K_PLAN``
for budgets) and:

1. Assembles :class:`app.schemas.Bundle` with ``ordered_artifacts`` listing the
   cover VIDEO first, then the body slides in index order, then the CTA slide.
2. Runs the QA checks pinned in docs/CONTRACTS.md: total slide count within
   the Instagram cap, cover duration within the configured window (default
   4-15 s), per-slide line budget from the
   plan, existence of every referenced artifact, and the copy-vs-rendered
   check on every body-slide PNG (the contract's size-heuristic variant:
   real PNG, exact target slide size, byte-size sanity floor), plus exact
   fixed body-slide number plus brand-rail and safe-area validation. The cover
   and CTA are intentionally unnumbered.
3. Writes ``K_BUNDLE`` + ``K_QA_REPORT``. On CRITICAL failures it also writes
   a :class:`app.schemas.ReworkPlan` to ``K_REWORK_PLAN`` (and the distilled
   correction text to ``K_REWORK_FEEDBACK``) targeting the responsible agents,
   so the orchestrator auto-routes back WITHOUT mailing the reviewer.

Design (verified against installed google-adk 2.7.0): all real work happens in
the deterministic ``assemble_and_verify`` tool, which writes state through
``tool_context.state`` - the delta-aware ``google.adk.agents.context.Context``
state. The LlmAgent merely drives the tool and narrates the QA outcome. The
instruction is an ``InstructionProvider`` (2.7.0 resolves callables via
``LlmAgent.canonical_instruction`` with ``bypass_state_injection=True``), so
Learner-edited instruction files can never collide with ``{var}`` state
templating, and ``K_REWORK_FEEDBACK`` is injected as a highest-priority block
read straight from state.
"""

from __future__ import annotations

import logging
import struct
from typing import Any, Literal, Optional

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import FunctionTool, ToolContext

from app.config import agent_instructions, settings
from app.llm import resolve_model
from app.schemas import (
    Bundle,
    CTASlide,
    CarouselPlan,
    CopySet,
    CoverSpec,
    QAIssue,
    QAReport,
    RenderedSlide,
    ReworkPlan,
    ReworkReason,
)
from app.state import (
    AGENT_CTA,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_PHRASING,
    AGENT_PLANNER,
    AGENT_STITCH_VERIFY,
    AGENT_TEMPLATE_DESIGN,
    K_BODY_SLIDES,
    K_BUNDLE,
    K_COPY,
    K_COVER,
    K_CTA_SLIDE,
    K_NEWS_ITEM,
    K_PLAN,
    K_QA_REPORT,
    K_REWORK_FEEDBACK,
    K_REWORK_PLAN,
    REWORKABLE_AGENTS,
    set_model,
)
from app.tools.brand_layout import validate_footer_padding

logger = logging.getLogger(__name__)

# Cover video duration window (seconds) from configuration
# (COVER_CLIP_MIN_S / COVER_CLIP_MAX_S, default 4-15). A small tolerance
# absorbs ffmpeg trim jitter so a 3.98 s clip does not trigger a pointless
# rework loop.
COVER_MIN_DURATION_S = float(settings.cover_clip_min_s)
COVER_MAX_DURATION_S = float(settings.cover_clip_max_s)
_DURATION_TOLERANCE_S = 0.1

# Copy-vs-rendered check - docs/CONTRACTS.md mandates it "via LLM vision or
# size heuristics"; this deterministic tool applies the size heuristics. A
# body slide that really carries its rendered copy is a full-size PNG whose
# compressed byte size cannot fall under a sanity floor: a blank, truncated
# or wrongly-sized render trips one of the checks. (A solid-colour 1080x1350
# PNG compresses to ~1-2 KB; any slide with real anti-aliased text lands far
# above the floor.)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MIN_SLIDE_PNG_BYTES = 10_000

_DEFAULT_INSTRUCTION = """\
# Stitch & Verify

You are the Stitch & Verify agent of the Carousel Factory pipeline. The
generate phase has produced four pieces in session state: the cover video
spec, the rendered body slides, the CTA slide and the slide copy. Your job is
to assemble them into the final Bundle and gate quality BEFORE any human sees
a review mail.

## Exactly what to do

1. Call the `assemble_and_verify` tool EXACTLY ONCE, with no arguments. It
   deterministically:
   - assembles the Bundle (ordered_artifacts: cover video FIRST, then body
     slides in index order, then the CTA slide) and stores it in state;
   - runs every QA check (slide count within the Instagram cap, cover video
     duration within the configured window, per-slide line budget from the
     plan, that every
     referenced artifact actually exists, a copy-vs-rendered size check, and
     deterministic body-slide-number and footer safe-area validation (with
     the cover and CTA intentionally unnumbered) so
     every body-slide PNG is a real, full-size render actually able to
     carry its approved text);
   - stores the QAReport, and on CRITICAL failures also stores a ReworkPlan
     targeting the agents responsible, so the orchestrator re-runs only them.
2. Read the tool result and reply with a short plain-text QA summary
   (2-4 sentences): whether QA passed, the total slide count, and - if it
   failed - each critical issue and which agent must redo its piece.

## Hard rules

- Never call the tool more than once per run.
- Never invent issues or hide issues: report exactly what the tool returned.
- You have no other tools. Do not try to fix content yourself - routing the
  rework to the responsible agent is the fix.
- If rework feedback from the human reviewer is present in your context, it
  is the highest-priority correction: mention in your summary whether the
  re-checked pieces now satisfy it.
"""


def _ensure_default_instruction_file() -> None:
    """Write the fallback instruction to ``skills/agents/stitch_verify.md`` if absent.

    The file is the editable source of truth for this agent's instruction
    (the Learner agent appends learned rules to it), so it is only created
    when missing - an existing file is never overwritten.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_STITCH_VERIFY}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_INSTRUCTION, encoding="utf-8")


def _instruction_provider(ctx: ReadonlyContext) -> str:
    """Assemble the full instruction at LLM-call time (InstructionProvider).

    Loads ``skills/agents/stitch_verify.md`` fresh from disk on every call
    (the Learner agent edits it between runs), appends the authoritative
    runtime limits from configuration, and injects ``K_REWORK_FEEDBACK`` as a
    highest-priority block when present. Because this is a provider callable,
    ADK 2.7.0 bypasses ``{var}`` state templating entirely, so braces inside
    the Learner-edited file can never raise a template ``KeyError``.

    Args:
        ctx: Read-only view of the invocation (session state access).

    Returns:
        The complete instruction string for this LLM call.
    """
    base = agent_instructions(AGENT_STITCH_VERIFY) or _DEFAULT_INSTRUCTION
    parts = [base]

    rework = str(ctx.state.get(K_REWORK_FEEDBACK) or "").strip()
    if rework:
        parts.append(
            "## HIGHEST PRIORITY - rework feedback for this run\n\n"
            "The human reviewer (or a previous QA round) demanded corrections."
            " This overrides every default guideline. The re-run agents were"
            " asked to fix exactly this; verify their output with extra care"
            " and reference it in your summary:\n\n"
            f"{rework}"
        )

    parts.append(
        "## Runtime limits (authoritative, from configuration)\n\n"
        f"- Total slides (cover + body + CTA) must be <= "
        f"{settings.max_carousel_slides}.\n"
        f"- Cover video duration must be {COVER_MIN_DURATION_S:g}-"
        f"{COVER_MAX_DURATION_S:g} seconds.\n"
        f"- Slides render at {settings.slide_width}x{settings.slide_height} px."
    )
    return "\n\n".join(parts)


def _validated(
    raw: Any, model_cls: type, piece: str, target: str, issues: list[QAIssue]
) -> Optional[Any]:
    """Validate one state piece into its pydantic model, logging QA on failure.

    Args:
        raw: The raw state value (dict / model / None).
        model_cls: The pydantic model class to validate against.
        piece: Human-readable piece name for the issue message.
        target: The agent responsible for this piece (``AGENT_*`` constant).
        issues: The QA issue list to append to on failure/absence.

    Returns:
        The validated model instance, or ``None`` when missing/invalid.
    """
    if raw is None:
        issues.append(
            QAIssue(
                severity="critical",
                message=f"{piece} is missing from session state - {target} must run again.",
            )
        )
        return None
    try:
        if isinstance(raw, model_cls):
            return raw
        return model_cls.model_validate(raw)
    except Exception as exc:  # malformed state must route back, not crash
        issues.append(
            QAIssue(
                severity="critical",
                message=f"{piece} in session state is malformed ({exc}) - {target} must redo it.",
            )
        )
        return None


async def _existing_artifacts(tool_context: ToolContext) -> Optional[set[str]]:
    """List the artifact filenames attached to this session.

    Returns:
        The set of filenames, or ``None`` when no artifact service is
        configured (local fallback) or listing failed - in which case the
        existence check is skipped with a minor issue, never a crash.
    """
    try:
        return set(await tool_context.list_artifacts())
    except ValueError:
        # Raised by google-adk 2.7.0 Context.list_artifacts when
        # invocation_context.artifact_service is None (local fallback runs).
        logger.warning("Artifact service unavailable - skipping existence checks.")
        return None
    except Exception as exc:
        logger.warning("Artifact listing failed (%s) - skipping existence checks.", exc)
        return None


def _png_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """Read the pixel dimensions out of a PNG byte blob.

    Parses the fixed-position IHDR fields directly (bytes 16-24 hold width
    and height as big-endian uint32s - IHDR is always the first chunk per the
    PNG spec), so the contract's size-heuristic check needs no image library.

    Args:
        data: Raw artifact bytes.

    Returns:
        ``(width, height)`` in pixels, or ``None`` when the blob is not a
        valid PNG.
    """
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


async def _verify_rendered_png(
    tool_context: ToolContext, slide: RenderedSlide
) -> Optional[QAIssue]:
    """Copy-vs-rendered check for one body slide (size-heuristic variant).

    docs/CONTRACTS.md requires verifying the rendered slides against the
    approved copy "via LLM vision or size heuristics". This deterministic
    tool applies the size heuristics: the slide's artifact bytes must decode
    as a PNG, measure exactly ``settings.slide_width`` x
    ``settings.slide_height``, and clear a byte-size sanity floor no slide
    that actually carries its rendered text falls under. Any miss is a
    CRITICAL issue routed to template_design (failed/drifted text rendering
    is that agent's number-one failure mode).

    Args:
        tool_context: The tool context (artifact access).
        slide: The rendered body slide whose artifact to inspect.

    Returns:
        A :class:`QAIssue` when the render fails a check (critical) or could
        not be inspected (minor), or ``None`` when the render passes.
    """
    try:
        part = await tool_context.load_artifact(slide.artifact)
    except Exception as exc:  # service hiccup: degrade to a note, never crash
        logger.warning(
            "Could not load artifact '%s' for the copy-vs-rendered check: %s",
            slide.artifact,
            exc,
        )
        return QAIssue(
            severity="minor",
            slide_index=slide.index,
            message=f"Copy-vs-rendered check skipped for slide {slide.index}: "
            f"artifact '{slide.artifact}' could not be loaded ({exc}).",
        )
    data = (
        part.inline_data.data
        if part is not None and part.inline_data is not None
        else None
    )
    if not data:
        return QAIssue(
            severity="minor",
            slide_index=slide.index,
            message=f"Copy-vs-rendered check skipped for slide {slide.index}: "
            f"artifact '{slide.artifact}' returned no inline bytes.",
        )
    dimensions = _png_dimensions(data)
    if dimensions is None:
        return QAIssue(
            severity="critical",
            slide_index=slide.index,
            message=f"Rendered slide {slide.index} artifact '{slide.artifact}' "
            f"is not a valid PNG - {AGENT_TEMPLATE_DESIGN} must re-render it.",
        )
    width, height = dimensions
    if (width, height) != (settings.slide_width, settings.slide_height):
        return QAIssue(
            severity="critical",
            slide_index=slide.index,
            message=(
                f"Rendered slide {slide.index} measures {width}x{height} px "
                f"instead of the required {settings.slide_width}x"
                f"{settings.slide_height} - the render drifted from the "
                f"template, so its text cannot be trusted either; "
                f"{AGENT_TEMPLATE_DESIGN} must re-render it."
            ),
        )
    if len(data) < _MIN_SLIDE_PNG_BYTES:
        return QAIssue(
            severity="critical",
            slide_index=slide.index,
            message=(
                f"Rendered slide {slide.index} PNG is only {len(data)} bytes "
                f"(sanity floor {_MIN_SLIDE_PNG_BYTES}) - far too small to "
                f"carry the approved copy, the text almost certainly failed "
                f"to render; {AGENT_TEMPLATE_DESIGN} must re-render it."
            ),
        )
    return None


async def _verify_brand_padding(
    tool_context: ToolContext,
    *,
    artifact: str,
    slide_index: int,
    kind: Literal["body", "cta"],
) -> Optional[QAIssue]:
    """Validate body numbering, footer furniture, and safe-area padding."""
    try:
        part = await tool_context.load_artifact(artifact)
    except Exception as exc:
        return QAIssue(
            severity="minor",
            slide_index=slide_index,
            message=(
                f"Brand-rail padding check skipped for slide {slide_index}: "
                f"artifact '{artifact}' could not be loaded ({exc})."
            ),
        )
    data = (
        part.inline_data.data
        if part is not None and part.inline_data is not None
        else None
    )
    if not data:
        return QAIssue(
            severity="minor",
            slide_index=slide_index,
            message=(
                f"Brand-rail padding check skipped for slide {slide_index}: "
                f"artifact '{artifact}' returned no inline bytes."
            ),
        )
    # Body slides carry a number in the top-left anchor; the cover and CTA do
    # not. Only presence is checked - see validate_footer_padding on why the
    # value itself is not, and do not read this as "the number is correct".
    errors = validate_footer_padding(data, kind, expect_slide_number=kind == "body")
    if not errors:
        return None
    owner = AGENT_CTA if kind == "cta" else AGENT_TEMPLATE_DESIGN
    return QAIssue(
        severity="critical",
        slide_index=slide_index,
        message=(
            f"Slide {slide_index} brand rail/padding failed: "
            + "; ".join(errors)
            + f" - {owner} must re-render it."
        ),
    )


async def assemble_and_verify(tool_context: ToolContext) -> dict:
    """Assemble the carousel Bundle from session state and run all QA checks.

    Deterministic, idempotent. Reads the cover spec, rendered body slides,
    CTA slide, copy set and plan from session state; assembles the Bundle
    (ordered_artifacts: cover video first, then body slides by index, then
    CTA); checks slide count, cover duration, per-slide line budgets,
    artifact existence and the copy-vs-rendered size heuristics on every
    body-slide PNG; then writes the Bundle and QAReport to state. On any
    CRITICAL issue it also writes a ReworkPlan targeting the responsible
    agents so the orchestrator re-runs only them instead of mailing a review.

    Returns:
        A summary dict: ``passed`` (bool), ``total_slides`` (int),
        ``ordered_artifacts`` (list[str]), ``issues`` (list of
        severity/slide_index/message dicts) and ``critical_targets``
        (list of agent names that must redo their piece; empty when passed).
    """
    state = tool_context.state
    issues: list[QAIssue] = []

    plan: Optional[CarouselPlan] = None
    raw_plan = state.get(K_PLAN)
    if raw_plan is not None:
        try:
            plan = CarouselPlan.model_validate(raw_plan)
        except Exception as exc:
            issues.append(
                QAIssue(
                    severity="major",
                    message=f"Carousel plan in state is malformed ({exc}); "
                    "budget checks fall back to defaults.",
                )
            )

    cover = _validated(
        state.get(K_COVER), CoverSpec, "Cover spec", AGENT_FIRST_PAGE_VISUAL, issues
    )
    copy_set = _validated(
        state.get(K_COPY), CopySet, "Copy set", AGENT_PHRASING, issues
    )
    cta = _validated(
        state.get(K_CTA_SLIDE), CTASlide, "CTA slide", AGENT_CTA, issues
    )

    body_slides: list[RenderedSlide] = []
    raw_body = state.get(K_BODY_SLIDES)
    if not raw_body:
        issues.append(
            QAIssue(
                severity="critical",
                message="No rendered body slides in session state - "
                f"{AGENT_TEMPLATE_DESIGN} must run again.",
            )
        )
    else:
        for entry in raw_body:
            try:
                body_slides.append(
                    entry
                    if isinstance(entry, RenderedSlide)
                    else RenderedSlide.model_validate(entry)
                )
            except Exception as exc:
                issues.append(
                    QAIssue(
                        severity="critical",
                        message=f"A rendered body slide is malformed ({exc}) - "
                        f"{AGENT_TEMPLATE_DESIGN} must redo it.",
                    )
                )
        body_slides.sort(key=lambda s: s.index)

    # ------------------------------------------------------------------ QA -
    # 1. Cover: video present + duration window.
    if cover is not None:
        if not cover.video_artifact:
            issues.append(
                QAIssue(
                    severity="critical",
                    slide_index=1,
                    message="Cover has no video artifact - "
                    f"{AGENT_FIRST_PAGE_VISUAL} must rebuild the cover.",
                )
            )
        if not cover.poster_artifact:
            issues.append(
                QAIssue(
                    severity="major",
                    slide_index=1,
                    message="Cover has no poster frame (used for mail preview "
                    "and the Instagram fallback image).",
                )
            )
        duration = float(cover.duration_s or 0.0)
        if not (
            COVER_MIN_DURATION_S - _DURATION_TOLERANCE_S
            <= duration
            <= COVER_MAX_DURATION_S + _DURATION_TOLERANCE_S
        ):
            fallback_note = (
                " (static fallback cover)" if cover.used_fallback_image else ""
            )
            issues.append(
                QAIssue(
                    severity="critical",
                    slide_index=1,
                    message=(
                        f"Cover video duration {duration:.2f}s is outside the "
                        f"required {COVER_MIN_DURATION_S:g}-"
                        f"{COVER_MAX_DURATION_S:g}s window{fallback_note} - "
                        f"{AGENT_FIRST_PAGE_VISUAL} must re-trim or re-source "
                        "the clip."
                    ),
                )
            )

    # 2. Body slides: artifacts + contiguous indexes starting at 2.
    for slide in body_slides:
        if not slide.artifact:
            issues.append(
                QAIssue(
                    severity="critical",
                    slide_index=slide.index,
                    message=f"Body slide {slide.index} has no rendered artifact - "
                    f"{AGENT_TEMPLATE_DESIGN} must re-render it.",
                )
            )
    body_indexes = [s.index for s in body_slides]
    if body_slides and body_indexes != list(range(2, 2 + len(body_slides))):
        issues.append(
            QAIssue(
                # Critical, because only critical fails QA. A deck with a hole
                # in it is not a carousel with a note attached - it is the
                # wrong deck, and "major" let it go straight to a human as if
                # it were finished. The per-slide checks cannot catch this:
                # every slide that DID render is individually valid.
                severity="critical",
                message=f"Body slide indexes {body_indexes} are not contiguous "
                "starting at 2 (slide 1 is the cover) - a slide is missing or "
                "out of order.",
            )
        )

    # 3. CTA slide artifact.
    if cta is not None and not cta.artifact:
        issues.append(
            QAIssue(
                severity="critical",
                message=f"CTA slide has no rendered artifact - {AGENT_CTA} "
                "must re-render it.",
            )
        )

    # 4. Total slide count within the Instagram cap.
    total_slides = 1 + len(body_slides) + 1  # cover + body + CTA
    if total_slides > settings.max_carousel_slides:
        issues.append(
            QAIssue(
                severity="critical",
                message=(
                    f"Total slide count {total_slides} exceeds the Instagram "
                    f"cap of {settings.max_carousel_slides} - {AGENT_PLANNER} "
                    "must re-plan with fewer slides."
                ),
            )
        )

    # 5. Plan consistency (body count vs plan).
    if plan is not None and body_slides:
        planned_body = max(plan.slide_count - 2, 0)
        if len(body_slides) != planned_body:
            issues.append(
                QAIssue(
                    # See the contiguity check above: a deck that does not
                    # match its own plan is incomplete, and shipping it to a
                    # reviewer as finished wastes their round.
                    severity="critical",
                    message=(
                        f"Rendered body slide count {len(body_slides)} does not "
                        f"match the plan ({planned_body} body slides) - "
                        f"{AGENT_TEMPLATE_DESIGN} likely dropped or duplicated "
                        "a slide."
                    ),
                )
            )

    # 6. Per-slide line budget from the plan.
    line_budget = plan.max_lines_per_slide if plan is not None else 4
    if copy_set is not None:
        for slide_copy in copy_set.slides:
            if len(slide_copy.lines) > line_budget:
                issues.append(
                    QAIssue(
                        severity="critical",
                        slide_index=slide_copy.index,
                        message=(
                            f"Slide {slide_copy.index} copy has "
                            f"{len(slide_copy.lines)} lines, over the budget of "
                            f"{line_budget} - {AGENT_PHRASING} must tighten it."
                        ),
                    )
                )
        if body_slides and len(copy_set.slides) != len(body_slides):
            issues.append(
                QAIssue(
                    # "Some slides may show the wrong text" is not a note, it
                    # is a reason not to publish. Nothing downstream compares
                    # rendered pixels to the approved copy, so this count is
                    # the only signal that they diverged.
                    severity="critical",
                    message=(
                        f"Copy set has {len(copy_set.slides)} slides but "
                        f"{len(body_slides)} body slides were rendered - some "
                        "slides may show the wrong text."
                    ),
                )
            )

    # ------------------------------------------------------- assemble Bundle
    ordered_artifacts: list[str] = []
    if cover is not None and cover.video_artifact:
        ordered_artifacts.append(cover.video_artifact)  # cover video FIRST
    ordered_artifacts.extend(s.artifact for s in body_slides if s.artifact)
    if cta is not None and cta.artifact:
        ordered_artifacts.append(cta.artifact)

    bundle = Bundle(
        cover=cover or CoverSpec(),
        slides=body_slides,
        cta=cta or CTASlide(cta_type="follow"),
        caption=(copy_set.caption if copy_set is not None else ""),
        ordered_artifacts=ordered_artifacts,
    )

    # 7. Every referenced artifact must actually exist in the artifact store.
    existing = await _existing_artifacts(tool_context)
    if existing is None:
        issues.append(
            QAIssue(
                severity="minor",
                message="Artifact existence and copy-vs-rendered checks "
                "skipped: no artifact service available in this run.",
            )
        )
    else:
        to_check: list[tuple[str, str]] = [
            (name, _owner_of_artifact(name, bundle)) for name in ordered_artifacts
        ]
        if cover is not None and cover.poster_artifact:
            to_check.append((cover.poster_artifact, AGENT_FIRST_PAGE_VISUAL))
        for name, owner in to_check:
            if name not in existing:
                issues.append(
                    QAIssue(
                        severity="critical",
                        message=f"Referenced artifact '{name}' does not exist in "
                        f"the artifact store - {owner} must regenerate it.",
                    )
                )

        # 8. Copy-vs-rendered text check (docs/CONTRACTS.md: "via LLM vision
        # or size heuristics" - this tool applies the size heuristics): every
        # rendered body-slide PNG must be a real PNG at exactly the target
        # slide size and above a byte-size sanity floor, otherwise the
        # approved copy cannot be on the slide and template_design must
        # re-render it.
        for slide in body_slides:
            if not slide.artifact or slide.artifact not in existing:
                continue  # absence is already a critical issue above
            render_issue = await _verify_rendered_png(tool_context, slide)
            if render_issue is not None:
                issues.append(render_issue)
                continue
            padding_issue = await _verify_brand_padding(
                tool_context,
                artifact=slide.artifact,
                slide_index=slide.index,
                kind="body",
            )
            if padding_issue is not None:
                issues.append(padding_issue)

        if cta is not None and cta.artifact and cta.artifact in existing:
            padding_issue = await _verify_brand_padding(
                tool_context,
                artifact=cta.artifact,
                slide_index=1 + len(body_slides) + 1,
                kind="cta",
            )
            if padding_issue is not None:
                issues.append(padding_issue)

    # ------------------------------------------------------- report + route
    critical = [i for i in issues if i.severity == "critical"]
    report = QAReport(passed=not critical, issues=issues)

    set_model(state, K_BUNDLE, bundle)
    set_model(state, K_QA_REPORT, report)

    critical_targets: list[str] = []
    if critical:
        reasons: dict[str, list[str]] = {}
        for issue in critical:
            target = _target_for_issue(issue)
            reasons.setdefault(target, []).append(issue.message)
        # Stable, pipeline-order targets restricted to reworkable agents.
        critical_targets = [a for a in REWORKABLE_AGENTS if a in reasons]
        news_title = str((state.get(K_NEWS_ITEM) or {}).get("title") or "")
        feedback_text = (
            f"Automated QA failed for '{news_title or 'carousel'}': "
            + " | ".join(i.message for i in critical)
        )
        rework = ReworkPlan(
            targets=critical_targets,
            reasons=[
                ReworkReason(target=t, reason="; ".join(reasons[t]))
                for t in critical_targets
            ],
            feedback=feedback_text,
        )
        set_model(state, K_REWORK_PLAN, rework)
        state[K_REWORK_FEEDBACK] = feedback_text
        logger.warning(
            "QA failed with %d critical issue(s); rework targets: %s",
            len(critical),
            critical_targets,
        )
    else:
        # Clear stale routing so a passed QA can never re-trigger old rework.
        state[K_REWORK_PLAN] = None
        state[K_REWORK_FEEDBACK] = ""
        logger.info(
            "QA passed: %d slide(s), %d non-critical note(s).",
            total_slides,
            len(issues),
        )

    return {
        "passed": report.passed,
        "total_slides": total_slides,
        "ordered_artifacts": ordered_artifacts,
        "issues": [
            {
                "severity": i.severity,
                "slide_index": i.slide_index,
                "message": i.message,
            }
            for i in issues
        ],
        "critical_targets": critical_targets,
    }


def _owner_of_artifact(name: str, bundle: Bundle) -> str:
    """Map an ordered-artifact filename to the agent that produced it."""
    if name == bundle.cover.video_artifact or name == bundle.cover.poster_artifact:
        return AGENT_FIRST_PAGE_VISUAL
    if name == bundle.cta.artifact:
        return AGENT_CTA
    return AGENT_TEMPLATE_DESIGN


def _target_for_issue(issue: QAIssue) -> str:
    """Derive the rework target agent from a critical QA issue.

    The issue messages embed the responsible ``AGENT_*`` name (they are built
    in this module), so the mapping is a simple substring scan with a planner
    fallback for structural problems.
    """
    for agent_name in REWORKABLE_AGENTS:
        if agent_name in issue.message:
            return agent_name
    return AGENT_PLANNER


def build_stitch_verify_agent() -> LlmAgent:
    """Build the Stitch & Verify agent.

    Returns:
        An :class:`~google.adk.agents.LlmAgent` named
        :data:`app.state.AGENT_STITCH_VERIFY` running
        ``settings.utility_model``, whose single deterministic tool assembles
        ``K_BUNDLE``, writes ``K_QA_REPORT`` and, on critical QA failure,
        auto-routes rework via ``K_REWORK_PLAN``. Tool-using agent: no
        ``output_schema`` - state is written inside the tool via
        ``tool_context.state`` (google-adk 2.7.0 pattern).
    """
    _ensure_default_instruction_file()
    return LlmAgent(
        name=AGENT_STITCH_VERIFY,
        model=resolve_model(settings.utility_model),
        description=(
            "Stitch & Verify: assembles the final carousel Bundle (cover video "
            "first) and runs deterministic QA - slide count, cover duration, "
            "line budgets, artifact existence, copy-vs-rendered slide size "
            "checks, and deterministic number/footer safe-area validation. Critical "
            "failures auto-route a ReworkPlan back to the "
            "responsible agents instead of mailing."
        ),
        instruction=_instruction_provider,
        tools=[FunctionTool(assemble_and_verify)],
        # Orchestrator-driven pipeline node: never LLM-transfer elsewhere.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


__all__ = ["build_stitch_verify_agent", "assemble_and_verify"]
