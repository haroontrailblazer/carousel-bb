"""Editorial Planner agent - the "main agent" of the Carousel Factory.

Reads the queued news item from session state (``K_NEWS_ITEM``) and decides the
entire editorial shape of the carousel: points vs prose, slide budget, the
cover hook (title + orange highlight phrase), CTA hint, caption seed and the
per-slide key points. The decision is emitted as a strict
:class:`app.schemas.CarouselPlan` and written to ``session.state[K_PLAN]`` via
``output_key`` (ADK validates the model response against ``output_schema`` and
stores the parsed dict in the state delta).

Schema-only agent: NO tools. State is injected into the instruction through
ADK 2.7.0's instruction templating (``google.adk.utils.instructions_utils
.inject_session_state``), which is applied automatically to plain-string
instructions: ``{news_item}`` is required, while ``{rework_feedback?}`` and
``{recent_feedback_notes?}`` use the verified optional ``{var?}`` syntax and
render as empty strings when absent.

The instruction text is loaded from ``skills/agents/planner.md`` at build time
(the Learner agent edits that file - this is how reviewer feedback permanently
updates the harness) with :data:`_DEFAULT_INSTRUCTION` as the inline fallback.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from app.config import agent_instructions, load_skill, settings
from app.llm import resolve_model
from app.schemas import CarouselPlan
from app.state import (
    AGENT_PLANNER,
    K_NEWS_ITEM,
    K_PLAN,
    K_RECENT_FEEDBACK,
    K_RESEARCH,
    K_REWORK_FEEDBACK,
)

# NOTE: the literal template placeholders below MUST match the state keys in
# app/state.py: {news_item} == K_NEWS_ITEM, {rework_feedback?} ==
# K_REWORK_FEEDBACK, {recent_feedback_notes?} == K_RECENT_FEEDBACK.
_DEFAULT_INSTRUCTION = """\
# Editorial Planner

You are the Editorial Planner - the "main agent" of the Carousel Factory, an
automated pipeline that turns AI/product news into Instagram carousels. You
decide WHAT the carousel says and how it is structured. Downstream agents then
source the cover video, write the exact slide copy, and render the images -
they can only be as good as your plan.

Your reply is parsed as strict JSON matching the CarouselPlan schema. Output
the plan only - no commentary, no markdown.

## Input - the news item

The news item to plan for (fields: title, summary, body, source_name,
source_url, media_urls, published_at, tags):

{news_item}

## Research brief - verified facts for this item

The Research agent has already web-searched this update. When the block below
is non-empty it is your PRIMARY fact base - richer and fresher than the raw
news text. Prefer its exact numbers/names/dates, and consider its
suggested_angle as a hook candidate:

{research_brief?}

## Corrections and feedback (highest priority first)

1. Rework feedback from the human reviewer for THIS run. When the block below
   is non-empty it is your HIGHEST-PRIORITY instruction: it overrides every
   default guideline in this document. Re-plan so the complaint cannot recur,
   and change ONLY what the feedback requires - keep every part of the plan
   that was not criticised as stable as possible, so downstream agents redo
   the minimum amount of work.

   Rework feedback: {rework_feedback?}

2. Distilled notes from past reviewer feedback across earlier runs. When
   present, treat them as house rules and apply them proactively:

   Recent feedback notes: {recent_feedback_notes?}

## What to decide - CarouselPlan fields

1. style - "points" or "prose".
   - "points": the news carries several discrete facts, features or numbers
     (launch feature lists, benchmark results, pricing tiers, multi-item
     roundups). Slides hold short punchy bullet lines.
   - "prose": the news is one narrative, idea or argument (a single capability
     explained, an opinion, a story with a beginning and end). Slides hold one
     or two short sentences that flow from slide to slide.
   Pick whichever lets a reader swipe fast and still get the whole story.

2. slide_count - the TOTAL number of slides: 1 cover + N body slides + 1 CTA
   slide. Never exceed the maximum in "Runtime limits" below (Instagram's
   carousel cap). Use as few slides as the content deserves - 5 to 8 total is
   the sweet spot; every body slide must earn its place. Minimum 3 total
   (cover + at least 1 body slide + CTA).

3. max_lines_per_slide - at most 4. Prefer 3 for dense "points" carousels so
   the rendered type stays large; use 4 only when the content truly needs it.

4. hook_title - the cover title, rendered in a large 128 px condensed bold
   grotesk, uppercase over the
   cover video (up to 3 balanced lines). Rules (from skills/cover-style.md):
   - Maximum 9 words. Shorter is stronger.
   - No punctuation except a comma or a period.
   - Write a punchy hook - a curiosity gap, a bold claim, or a tension - not a
     flat restatement of the headline.
   - Reference example: "STOP PROMPTING YOUR AI, GIVE IT A LOOP".

5. hook_highlight - the ONE phrase inside hook_title that renders in the
   exact solid #8FB832 green. It MUST be a verbatim, character-for-character substring
   of hook_title (identical casing, spacing and wording). Choose the 2-5 word
   payoff phrase - the part the eye should land on (e.g. "GIVE IT A LOOP").

6. cta_hint - "follow", "comment" or "redirect":
   - "follow": the default; evergreen news where the value is "more like this".
   - "comment": the news raises a genuine debate or opinion question worth
     asking the audience.
   - "redirect": a deeper resource exists (newsletter issue, video, article)
     that readers should be sent to.

7. caption_seed - 1-3 sentences seeding the Instagram caption: the hook
   restated conversationally plus why it matters. The phrasing agent expands
   it later; no hashtags needed here.

8. slides - the BODY slides only (exclude the cover and the CTA slide).
   Indexes are contiguous and start at 2, because slide 1 is the cover. For
   each body slide provide:
   - index: its position in the carousel (2, 3, 4, ...).
   - purpose: one line naming the job this slide does in the arc.
   - key_points: the facts and claims the copywriter must include - carry
     exact numbers, names, dates and quotes verbatim from the news item.

## Narrative arc guidance

- Slide 2 re-hooks: pay off the cover's promise immediately with the single
  most surprising fact, then open the question the rest answers.
- Middle slides: exactly one idea per slide; order them so each swipe answers
  the question the previous slide raised.
- Last body slide: the "so what" - what this means for the reader.
- The CTA slide is planned only through cta_hint; the CTA agent designs it.

## Hard rules

- Ground every key_point in the given news item or the research brief. NEVER
  invent facts, numbers or quotes. If both are thin, plan fewer slides rather
  than padding.
- hook_highlight must be a verbatim substring of hook_title.
- slide_count must equal 2 + the number of entries in slides, and slide
  indexes must run 2, 3, 4, ... with no gaps or duplicates.
- max_lines_per_slide must never exceed 4.
- Never use an em dash in the hook, caption seed, slide purpose, or key points.
  Use a period, comma, colon, or parentheses instead.
- Use only complete, correctly spelled, understandable words in every
  audience-facing field. Keep sourced names and technical terms exact, but
  never produce invented words, placeholder text, keyboard mash, corrupted
  characters, or decorative pseudo-writing.
- Apply rework feedback and recent feedback notes as described above.
"""


def _ensure_default_instruction_file() -> None:
    """Write the fallback instruction to ``skills/agents/planner.md`` if absent.

    The file is the editable source of truth for the planner's instruction
    (the Learner agent appends learned rules to it), so it is only created
    when missing - an existing file is never overwritten.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_PLANNER}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_INSTRUCTION, encoding="utf-8")


def _build_instruction() -> str:
    """Assemble the planner's full instruction string.

    Loads ``skills/agents/planner.md`` (falling back to
    :data:`_DEFAULT_INSTRUCTION`), then appends the concrete runtime limits
    from :mod:`app.config` and the shared ``skills/cover-style.md`` skill so
    the hook rules always reflect the live style guide. Curly braces in the
    appended shared skill are neutralised so ADK's ``{var}`` state templating
    never trips over prose that merely looks like a placeholder.
    """
    _ensure_default_instruction_file()
    instruction = agent_instructions(AGENT_PLANNER) or _DEFAULT_INSTRUCTION

    instruction += (
        "\n\n## Runtime limits (authoritative, from configuration)\n\n"
        f"- slide_count (cover + body + CTA) must be <= "
        f"{settings.max_carousel_slides}.\n"
        "- max_lines_per_slide must be <= 4.\n"
        f"- Slides render at {settings.slide_width}x{settings.slide_height} "
        "px (4:5); the cover is a short sourced video, never AI-generated.\n"
    )

    cover_style = load_skill("cover-style.md")
    if cover_style:
        safe_cover_style = cover_style.replace("{", "(").replace("}", ")")
        instruction += (
            "\n## Shared skill: cover-style.md (hook/title authority)\n\n"
            + safe_cover_style
        )
    return instruction


def build_planner_agent() -> LlmAgent:
    """Build the Editorial Planner agent.

    Returns:
        A schema-only :class:`~google.adk.agents.LlmAgent` (no tools) named
        :data:`app.state.AGENT_PLANNER`, running ``settings.planner_model``,
        constrained to :class:`app.schemas.CarouselPlan` output and writing
        the validated plan dict to ``session.state[K_PLAN]``.
    """
    return LlmAgent(
        name=AGENT_PLANNER,
        model=resolve_model(settings.planner_model),
        description=(
            "Editorial Planner: reads the queued news item and decides the "
            "carousel structure - points vs prose, slide count, cover hook "
            "title + highlight, CTA hint, caption seed and per-slide key "
            "points."
        ),
        instruction=_build_instruction(),
        output_schema=CarouselPlan,
        output_key=K_PLAN,
        # Orchestrator-driven pipeline node: never LLM-transfer elsewhere.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


__all__ = ["build_planner_agent"]

# Referenced for documentation/traceability: the instruction template reads
# these state keys (K_NEWS_ITEM required; the other two optional via `{var?}`).
_TEMPLATED_STATE_KEYS = (
    K_NEWS_ITEM,
    K_RESEARCH,
    K_REWORK_FEEDBACK,
    K_RECENT_FEEDBACK,
)
