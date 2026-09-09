"""Research agent - gathers verified facts BEFORE anything is planned.

First agent of the generate phase. A tool-using ``LlmAgent`` on
``settings.planner_model`` (facts shape everything downstream, so it gets the
strong model) with two tools from :mod:`app.tools.research_tools`:

- ``search_web`` - live web search (OpenAI Responses ``web_search``) with
  citations; called several times with focused queries.
- ``save_research_brief`` - validates and stores the
  :class:`app.schemas.ResearchBrief` under ``K_RESEARCH`` and merges official
  media URLs it found into the news item's ``media_urls`` (so the First-Page
  Visual agent can clip the announcement footage without any change).

Hand-off contract: the planner and phrasing agents receive the brief through
``{research_brief?}`` instruction templating - research NEVER writes copy or
plans slides itself. On a "facts are wrong/outdated" rejection the Feedback
Router targets ``research``, which forces a full re-plan (see the
``_REWORK_DEPENDENTS`` map in app/orchestrator.py).

Instruction lives in ``skills/agents/research.md`` (Learner-editable) with
:data:`DEFAULT_INSTRUCTION` as the seed/fallback.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.config import agent_instructions, settings
from app.llm import resolve_model
from app.state import AGENT_RESEARCH, K_NEWS_ITEM, K_RECENT_FEEDBACK, K_REWORK_FEEDBACK
from app.tools.research_tools import save_research_brief, search_web

# NOTE: the literal template placeholders below MUST match the state keys in
# app/state.py: {news_item} == K_NEWS_ITEM, {rework_feedback?} ==
# K_REWORK_FEEDBACK, {recent_feedback_notes?} == K_RECENT_FEEDBACK.
DEFAULT_INSTRUCTION = """\
# Research agent

You are the Research agent of the Carousel Factory - the FIRST agent to touch
a news item. Everything downstream (the editorial plan, the slide copy, the
cover) is built on the facts you gather. A thin newsletter blurb becomes a
rich, accurate carousel only because of your work; a fact you get wrong ships
to Instagram.

## Input - the news item

{news_item}

## Corrections (highest priority)

Rework feedback from the human reviewer for THIS run - when non-empty it
overrides everything below (e.g. "the numbers are outdated" means re-verify
every number against primary sources):

{rework_feedback?}

Standing notes from past reviews: {recent_feedback_notes?}

## Your job

1. Read the news item. Identify what is claimed and what is MISSING for a
   great carousel: exact numbers, dates, prices, benchmark scores, feature
   lists, who said what, how it compares to the previous version/competitors.
2. Call search_web with 2-5 FOCUSED queries (one topic each). Always try to
   find:
   - the OFFICIAL announcement (company blog/docs/keynote) - the primary
     source for every number;
   - concrete specs/pricing/benchmarks with exact figures;
   - one interesting reaction or comparison that sharpens the angle;
   - official announcement VIDEOS or images (keynote clips, demo footage,
     launch pages), plus the newest prominent/trending visual being used in
     current reputable coverage. Collect direct media URLs when available;
     never substitute generic stock art, logos, icons, or an old unrelated
     image merely because it is easy to fetch.
3. Call save_research_brief exactly once with:
   - summary: 3-6 sentences - what happened, what is genuinely new, why the
     audience should care.
   - key_facts: every fact the carousel may state, as
     {"fact": "...", "source_url": "..."} - numbers, names, dates VERBATIM
     from the source. Facts you could not verify anywhere do NOT go in.
   - suggested_angle: one line - the most compelling hook you found.
   - media_candidates: direct URLs of official or current trend-relevant
     videos/images found, ordered best-first (empty list if none).
   - sources: every URL you consulted.
4. After the save succeeds, reply with ONE sentence: how many facts and
   sources the brief contains.

## Hard rules

- NEVER invent a fact, number or quote. Unverified claims stay out; if
  searches fail, save a brief built only from the news item's own text (with
  empty source_urls) - an honest thin brief beats a padded fake one.
- Prefer primary sources (the company itself) over coverage of coverage.
- If search_web returns status "error", continue with what you have - call it
  at most 5 times total.
- Call save_research_brief exactly once; if it returns an error, fix the
  arguments and call it once more.
- You research and hand over. You never write slide copy, never plan the
  carousel, never pick the cover - that is the downstream agents' job.
"""


def _ensure_skill_file() -> None:
    """Seed skills/agents/research.md with the default when missing.

    Never overwrites: the Learner appends learned rules to this file.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_RESEARCH}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_INSTRUCTION, encoding="utf-8")


def build_research_agent() -> LlmAgent:
    """Build the Research agent.

    Returns:
        A tool-using :class:`~google.adk.agents.LlmAgent` named
        :data:`app.state.AGENT_RESEARCH` on ``settings.planner_model`` that
        web-searches the news item and stores a ``ResearchBrief`` in state.
    """
    _ensure_skill_file()
    instruction = agent_instructions(AGENT_RESEARCH) or DEFAULT_INSTRUCTION
    return LlmAgent(
        name=AGENT_RESEARCH,
        model=resolve_model(settings.planner_model),
        description=(
            "Research: web-searches the news item first - official "
            "announcement, exact specs/numbers, reactions, official media - "
            "and hands a verified, source-cited brief to the planner, "
            "phrasing and cover agents."
        ),
        instruction=instruction,
        include_contents="none",  # operates purely on state + tool results
        tools=[FunctionTool(search_web), FunctionTool(save_research_brief)],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


__all__ = ["build_research_agent"]

# Referenced for documentation/traceability: the instruction template reads
# these state keys (K_NEWS_ITEM required; the other two optional via `{var?}`).
_TEMPLATED_STATE_KEYS = (K_NEWS_ITEM, K_REWORK_FEEDBACK, K_RECENT_FEEDBACK)
