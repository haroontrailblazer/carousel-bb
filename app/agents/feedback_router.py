"""Feedback Router agent - maps human review feedback to rework targets.

A schema-only ``LlmAgent`` (no tools, per the ADK guidance for structured
output) on ``settings.utility_model``. It reads the reviewer's verdict from
session state, decides which pipeline agents must re-run, and emits a strict
:class:`app.schemas.ReworkPlan` that ADK writes into
``state[K_REWORK_PLAN]`` via ``output_key``.

Targets are restricted to :data:`app.state.REWORKABLE_AGENTS`. Because the
model can still misspell or invent targets, an ``after_agent_callback``
deterministically sanitizes the stored plan: it normalizes aliases, drops
unknown targets, falls back to keyword-derived targets (and finally the
planner) when nothing valid remains, and guarantees one reason per target.

The LLM instruction is loaded from ``skills/agents/feedback_router.md`` (the
Learner agent may append "Learned rules" there); the identical default text
below is used as fallback and written to that file when it is missing.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types
from pydantic import ValidationError

from app.config import agent_instructions, settings
from app.llm import resolve_model
from app.schemas import ReworkPlan, ReworkReason, Verdict
from app.state import (
    AGENT_CTA,
    AGENT_FEEDBACK_ROUTER,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_PHRASING,
    AGENT_PLANNER,
    AGENT_RESEARCH,
    AGENT_TEMPLATE_DESIGN,
    K_REWORK_FEEDBACK,
    K_REWORK_PLAN,
    K_VERDICT,
    REWORKABLE_AGENTS,
    get_model,
    set_model,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic target normalization / derivation (used by the sanitizer and
# re-used by the Learner agent for consistent feedback-to-target mapping).
# ---------------------------------------------------------------------------

#: Common non-canonical labels the LLM (or a human) might use for a target.
_ALIAS_TO_TARGET: dict[str, str] = {
    # first_page_visual
    "cover": AGENT_FIRST_PAGE_VISUAL,
    "cover_video": AGENT_FIRST_PAGE_VISUAL,
    "video": AGENT_FIRST_PAGE_VISUAL,
    "visual": AGENT_FIRST_PAGE_VISUAL,
    "visuals": AGENT_FIRST_PAGE_VISUAL,
    "first_visual": AGENT_FIRST_PAGE_VISUAL,
    "first_page": AGENT_FIRST_PAGE_VISUAL,
    "first_slide": AGENT_FIRST_PAGE_VISUAL,
    "poster": AGENT_FIRST_PAGE_VISUAL,
    "clip": AGENT_FIRST_PAGE_VISUAL,
    # phrasing
    "text": AGENT_PHRASING,
    "texts": AGENT_PHRASING,
    "copy": AGENT_PHRASING,
    "copywriting": AGENT_PHRASING,
    "wording": AGENT_PHRASING,
    "words": AGENT_PHRASING,
    "caption": AGENT_PHRASING,
    "content_phrasing": AGENT_PHRASING,
    # template_design
    "design": AGENT_TEMPLATE_DESIGN,
    "designs": AGENT_TEMPLATE_DESIGN,
    "layout": AGENT_TEMPLATE_DESIGN,
    "template": AGENT_TEMPLATE_DESIGN,
    "templates": AGENT_TEMPLATE_DESIGN,
    "slide_design": AGENT_TEMPLATE_DESIGN,
    "body_slides": AGENT_TEMPLATE_DESIGN,
    # cta
    "call_to_action": AGENT_CTA,
    "cta_slide": AGENT_CTA,
    "last_slide": AGENT_CTA,
    # research
    "facts": AGENT_RESEARCH,
    "fact_check": AGENT_RESEARCH,
    "accuracy": AGENT_RESEARCH,
    "research_agent": AGENT_RESEARCH,
    # planner
    "plan": AGENT_PLANNER,
    "planning": AGENT_PLANNER,
    "structure": AGENT_PLANNER,
    "editorial": AGENT_PLANNER,
    "editorial_planner": AGENT_PLANNER,
    "classification": AGENT_PLANNER,
}

#: Keyword heuristics over raw feedback text, used only as a fallback when the
#: LLM produced no valid target. Checked in order; every matching target is
#: collected.
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        AGENT_RESEARCH,
        (
            "wrong fact",
            "facts are wrong",
            "factually",
            "factual",
            "inaccurate",
            "incorrect",
            "outdated",
            "not true",
            "fact check",
            "fact-check",
            "wrong number",
            "wrong price",
            "wrong date",
            "made up",
            "misleading",
        ),
    ),
    (
        AGENT_FIRST_PAGE_VISUAL,
        (
            "first visual",
            "first page",
            "first slide",
            "cover",
            "video",
            "clip",
            "poster",
            "footage",
            "opening",
        ),
    ),
    (
        AGENT_PHRASING,
        (
            "text",
            "wording",
            "word",
            "copy",
            "caption",
            "phras",
            "typo",
            "grammar",
            "tone",
            "sentence",
            "line",
        ),
    ),
    (
        AGENT_TEMPLATE_DESIGN,
        (
            "design",
            "layout",
            "template",
            "font",
            "color",
            "colour",
            "background",
            "render",
            "style of the slide",
        ),
    ),
    (
        AGENT_CTA,
        (
            "cta",
            "call to action",
            "call-to-action",
            "last slide",
            "follow",
            "redirect",
            "link",
        ),
    ),
    (
        AGENT_PLANNER,
        (
            "structure",
            "slide count",
            "too many slide",
            "too few slide",
            "number of slide",
            "order",
            "story",
            "narrative",
            "hook",
            "points",
            "prose",
            "classif",
            "plan",
        ),
    ),
)


def _normalize_target(raw: str) -> Optional[str]:
    """Map a raw target label to a canonical REWORKABLE_AGENTS name.

    Args:
        raw: Target string as produced by the LLM (any casing/spacing).

    Returns:
        The canonical agent name, or ``None`` when it cannot be mapped.
    """
    token = re.sub(r"[\s\-]+", "_", str(raw).strip().lower())
    if token in REWORKABLE_AGENTS:
        return token
    return _ALIAS_TO_TARGET.get(token)


def derive_targets_from_feedback(feedback: str) -> list[str]:
    """Derive rework targets from raw feedback text via keyword heuristics.

    This is the deterministic fallback used when the router LLM produced no
    valid target; the Learner agent re-uses it so feedback records get
    consistent target labels.

    Args:
        feedback: The reviewer's free-text feedback.

    Returns:
        Canonical target names (subset of ``REWORKABLE_AGENTS``), possibly
        empty when nothing matches.
    """
    text = (feedback or "").lower()
    if not text.strip():
        return []
    targets: list[str] = []
    for target, keywords in _KEYWORD_RULES:
        if target in targets:
            continue
        if any(keyword in text for keyword in keywords):
            targets.append(target)
    return targets


def _sanitize_rework_plan(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """After-agent callback: force ``state[K_REWORK_PLAN]`` into a valid plan.

    Guarantees (regardless of what the LLM emitted):

    - every target is a canonical ``REWORKABLE_AGENTS`` name (aliases are
      normalized, unknowns dropped, duplicates removed, order preserved);
    - at least one target exists - keyword-derived from the feedback text,
      with the planner as the final catch-all (planner implies a full
      regenerate of dependents);
    - ``reasons`` holds exactly one entry per chosen target;
    - ``feedback`` carries the reviewer's text verbatim.

    Args:
        callback_context: ADK callback context (mutable session state).

    Returns:
        ``None`` - no extra content event; the state delta is still emitted.
    """
    state = callback_context.state
    verdict = get_model(state, K_VERDICT, Verdict)
    verdict_feedback = verdict.feedback if verdict is not None else ""
    rework_feedback = str(state.get(K_REWORK_FEEDBACK) or "")
    effective_feedback = (rework_feedback or verdict_feedback or "").strip()

    raw_plan = state.get(K_REWORK_PLAN)
    try:
        plan = (
            ReworkPlan.model_validate(raw_plan)
            if raw_plan is not None
            else ReworkPlan()
        )
    except ValidationError:
        logger.warning(
            "Feedback router produced an invalid ReworkPlan (%r); rebuilding "
            "from feedback text.",
            raw_plan,
        )
        plan = ReworkPlan()

    # The reviewer's own words win, always. This used to fill the field only
    # when the model left it empty, so a router that summarised instead of
    # copying got the last word - and its summary is what the re-run agents
    # read as their highest-priority instruction, because _phase_rework
    # prefers plan.feedback over verdict.feedback when writing
    # K_REWORK_FEEDBACK.
    #
    # "the price is wrong, the model is $20/M not $200/M" became "pricing
    # figure incorrect", so research re-ran without the correct value, emitted
    # the same wrong number, passed QA (which does not check facts), and the
    # identical carousel went back to the reviewer - round after round, to the
    # cap. Both this module's docstring and the dispatcher's hard rules promise
    # feedback is carried verbatim; this is where that promise is kept.
    #
    # The model's text is still used to ROUTE (targets below), and is kept as
    # the feedback only when there is no reviewer text to prefer.
    plan.feedback = effective_feedback or plan.feedback

    # A human who NAMED an agent outranks the model that guessed one.
    #
    # The console lets a reviewer point - pick /cta, or click the cover on the
    # review screen - and those choices arrive on the verdict. When they are
    # present the router's opinion is discarded rather than merged: the whole
    # value of pointing is that it is exact, and a plan that quietly added
    # `planner` alongside the CTA would re-run the entire carousel to fix one
    # slide. Anything unrecognised still falls through to the normal path, so
    # a bad target cannot leave the plan empty.
    human_targets: list[str] = []
    for raw_target in verdict.targets if verdict is not None else []:
        target = _normalize_target(str(raw_target))
        if target is not None and target not in human_targets:
            human_targets.append(target)

    # Normalize targets, preserving order and dropping duplicates/unknowns.
    normalized: list[str] = list(human_targets)
    if not normalized:
        for raw_target in plan.targets:
            target = _normalize_target(str(raw_target))
            if target is not None and target not in normalized:
                normalized.append(target)

    if not normalized:
        normalized = derive_targets_from_feedback(
            plan.feedback or effective_feedback
        )
    if not normalized:
        # Unclassifiable feedback: re-plan from the top (planner re-run
        # implies dependents re-run, so nothing criticised can survive).
        normalized = [AGENT_PLANNER]

    # One reason per target, in target order: keep the LLM's reason when it
    # maps cleanly, otherwise fall back to the feedback text itself.
    by_target: dict[str, str] = {}
    for entry in plan.reasons or []:
        key = _normalize_target(str(entry.target))
        if key in normalized and key not in by_target and entry.reason:
            by_target[key] = str(entry.reason)
    fallback_reason = (
        plan.feedback
        or effective_feedback
        or "Reviewer rejected the carousel; rework required."
    )

    plan.targets = normalized
    plan.reasons = [
        ReworkReason(target=target, reason=by_target.get(target, fallback_reason))
        for target in normalized
    ]
    set_model(state, K_REWORK_PLAN, plan)
    logger.info("Rework plan sanitized: targets=%s", normalized)
    return None


# ---------------------------------------------------------------------------
# Instruction (fallback default; canonical copy lives in
# skills/agents/feedback_router.md so the Learner can evolve it).
# NOTE: only "{review_verdict?}" and "{rework_feedback?}" may appear as
# {identifier} placeholders - ADK's instruction templating substitutes any
# bare {state_key} and raises on unknown non-optional keys.
# ---------------------------------------------------------------------------
DEFAULT_INSTRUCTION = """\
# Feedback Router

You are the Feedback Router of the Carousel Factory - an automated pipeline
that turns AI/product news into Instagram carousels. A human reviewer just
REJECTED the current carousel (or asked for changes). Your only job is to
translate their feedback into a precise rework plan: exactly which pipeline
agents must re-run, and why.

Your reply is parsed as strict JSON matching the ReworkPlan schema with the
keys "targets", "reasons" and "feedback". Output the JSON object only - no
commentary, no markdown.

## Input - the human verdict

The reviewer's verdict (a dict with status, feedback, reviewer, decided_at):

{review_verdict?}

Rework feedback for this round (when non-empty, this exact text is the
complaint you must route - it is the highest-priority input):

{rework_feedback?}

## Allowed targets - use these EXACT strings and NOTHING else

- research - the fact base: wrong/outdated/unverified facts, numbers, dates,
  prices or claims; missing context the carousel should have covered.
- planner - the editorial plan: carousel structure, slide count, slide order,
  narrative/story arc, points-vs-prose classification, the hook idea.
- first_page_visual - the cover: the first visual, cover video/clip, poster
  frame, source footage choice.
- phrasing - all wording: slide texts, copy, captions, tone, typos, grammar,
  line length.
- template_design - the rendered body slides: design, layout, template,
  fonts, colors, backgrounds.
- cta - the call-to-action slide: CTA type, its text, its link, the last
  slide.

These are the only re-runnable agents. Never output any other value.

## Mapping guide

- Complaints about the first visual / cover / video / clip / poster / opening
  image map to first_page_visual. Example: "the first visual is not good"
  gives targets ["first_page_visual"].
- Complaints about texts / wording / copy / captions / typos / tone map to
  phrasing.
- Complaints about slide design / layout / template / fonts / colors /
  backgrounds map to template_design.
- Complaints about the CTA / call to action / last slide / link map to cta.
- Complaints about structure / slide count / slide order / the story /
  points-vs-prose classification / the hook idea map to planner. Note:
  planner re-runs force every dependent agent to re-run too, so pick planner
  only when the plan itself is criticised.
- Complaints that facts/numbers/dates/prices are wrong, outdated or made up -
  or that important known information is missing - map to research. Note:
  research re-runs force a full re-plan, so pick it only for factual issues,
  not for wording (phrasing) or structure (planner) complaints.

## Rules

1. Multiple targets are allowed - include one entry per distinct complaint
   when the feedback names several problems.
2. Keep targets MINIMAL: never include an agent the feedback does not
   criticise. A complaint about only the cover must not re-run phrasing.
3. "reasons" is a LIST with exactly one entry per chosen target, each
   {"target": <one of the targets>, "reason": <correction>}. Each reason is a
   short, concrete, imperative correction for that agent (what to fix, not a
   restatement of the complaint).
4. "feedback" must carry the reviewer's feedback text verbatim.
5. If the feedback is empty or too vague to classify, choose ["planner"] and
   explain in its reason that the carousel must be rethought from the plan.

## Output shape (example values, not a template to copy)

{"targets": ["first_page_visual", "phrasing"], "reasons": [{"target": "first_page_visual", "reason": "Pick a more dynamic source clip for the cover."}, {"target": "phrasing", "reason": "Shorten slide 3 to two punchy lines."}], "feedback": "first visual is boring and slide 3 is too wordy"}
"""


def _ensure_skill_file() -> None:
    """Write the default instruction to skills/agents/feedback_router.md.

    Only when the file is missing - the Learner agent appends "Learned rules"
    to this file, and those must never be overwritten.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_FEEDBACK_ROUTER}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_INSTRUCTION, encoding="utf-8")


def build_feedback_router_agent() -> LlmAgent:
    """Build the Feedback Router agent.

    Returns:
        A schema-only ``LlmAgent`` named ``AGENT_FEEDBACK_ROUTER`` on
        ``settings.utility_model`` that writes a sanitized ``ReworkPlan``
        into ``state[K_REWORK_PLAN]``.
    """
    _ensure_skill_file()
    instruction = agent_instructions(AGENT_FEEDBACK_ROUTER) or DEFAULT_INSTRUCTION
    return LlmAgent(
        name=AGENT_FEEDBACK_ROUTER,
        model=resolve_model(settings.utility_model),
        description=(
            "Maps human review feedback to the exact pipeline agents that "
            "must re-run (rework plan)."
        ),
        instruction=instruction,
        include_contents="none",  # routes purely on state, not chat history
        output_schema=ReworkPlan,
        output_key=K_REWORK_PLAN,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        after_agent_callback=_sanitize_rework_plan,
    )
