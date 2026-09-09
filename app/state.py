"""Session-state contract: keys, agent names, phases, and typed accessors.

The orchestrator is a state machine over ``session.state`` - after the review
pause the run resumes in a NEW invocation, so everything the pipeline needs to
continue MUST live in state under these keys, never in Python locals.
"""

from __future__ import annotations

from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Agent names - the Feedback Router returns these exact strings as targets.
# ---------------------------------------------------------------------------
AGENT_RESEARCH = "research"
AGENT_PLANNER = "planner"
AGENT_FIRST_PAGE_VISUAL = "first_page_visual"
AGENT_PHRASING = "phrasing"
AGENT_TEMPLATE_DESIGN = "template_design"
AGENT_CTA = "cta"
AGENT_STITCH_VERIFY = "stitch_verify"
AGENT_REVIEW_DISPATCHER = "review_dispatcher"
AGENT_FEEDBACK_ROUTER = "feedback_router"
AGENT_PUBLISHER = "publisher"
AGENT_LEARNER = "learner"

REWORKABLE_AGENTS = [
    AGENT_RESEARCH,
    AGENT_PLANNER,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_PHRASING,
    AGENT_TEMPLATE_DESIGN,
    AGENT_CTA,
]

# ---------------------------------------------------------------------------
# Phases - the orchestrator's state machine (see docs/CONTRACTS.md).
# ---------------------------------------------------------------------------
PHASE_GENERATE = "generate"
PHASE_QA = "qa"
PHASE_REVIEW = "review"
PHASE_REWORK = "rework"
PHASE_PUBLISH = "publish"
PHASE_DONE = "done"

# ---------------------------------------------------------------------------
# State keys.
# ---------------------------------------------------------------------------
K_RUN_ID = "run_id"
K_PHASE = "phase"
K_NEWS_ITEM = "news_item"            # NewsItem dict
K_RESEARCH = "research_brief"        # ResearchBrief dict
K_PLAN = "carousel_plan"             # CarouselPlan dict
K_COVER = "cover"                    # CoverSpec dict
K_COPY = "copy_set"                  # CopySet dict
K_BODY_SLIDES = "body_slides"        # list[RenderedSlide dict]
K_CTA_SLIDE = "cta_slide"            # CTASlide dict
K_BUNDLE = "bundle"                  # Bundle dict
K_QA_REPORT = "qa_report"            # QAReport dict
K_VERDICT = "review_verdict"         # Verdict dict (written on resume)
K_REWORK_PLAN = "rework_plan"        # ReworkPlan dict
K_REWORK_FEEDBACK = "rework_feedback"  # str - injected into re-run agents' context
K_REWORK_ROUND = "rework_round"      # int - human-driven rework rounds
#: Automatic QA-driven rework rounds. Counted SEPARATELY from
#: K_REWORK_ROUND: a machine retrying a bad render is not the reviewer
#: spending one of their chances, and sharing one counter meant a few
#: flaky renders could exhaust the budget before a human saw anything.
K_QA_ROUND = "qa_round"              # int - automatic QA retry rounds
K_REVIEW_ROUND = "review_round"      # int - how many review mails sent
K_REVIEW_NOTICE_FAILED = "review_notice_failed"  # bool - carousel ready,
                                     # but the reviewer could not be told
K_RECENT_FEEDBACK = "recent_feedback_notes"  # str - distilled past feedback, injected for planner/phrasing
K_PUBLISH_RESULT = "publish_result"  # dict - Instagram media_id + permalink
K_TOKEN_USAGE = "token_usage"        # dict - cumulative run token counts (prompt/output/total LLM tokens + image tokens)

M = TypeVar("M", bound=BaseModel)


def get_model(state: Any, key: str, model_cls: Type[M]) -> Optional[M]:
    """Read a pydantic model stored as a dict in session state."""
    raw = state.get(key)
    if raw is None:
        return None
    if isinstance(raw, model_cls):
        return raw
    return model_cls.model_validate(raw)


def set_model(state: Any, key: str, value: BaseModel) -> None:
    """Store a pydantic model as a plain dict (state must stay serializable)."""
    state[key] = value.model_dump(mode="json")
