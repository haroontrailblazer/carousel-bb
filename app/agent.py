"""Root agent + runner wiring for the Carousel Factory.

Exposes:

* ``root_agent`` - the module-level :class:`app.orchestrator.CarouselOrchestrator`
  instance that ``adk web`` / ``adk run`` discover (they import
  ``app.agent`` and read ``root_agent``), with all eleven pipeline agents as
  ``sub_agents`` so the agent graph renders.
* ``build_root_agent()`` - builds a FRESH agent tree. Agent builders re-read
  their ``skills/agents/<name>.md`` instruction files, so a fresh tree picks
  up rules the Learner agent appended since the last build.
* ``build_runner()`` - a :class:`google.adk.runners.Runner` wired to the
  production services (``DatabaseSessionService`` on
  ``settings.database_url``, ``SupabaseArtifactService`` on Supabase S3
  storage, ``PostgresMemoryService``) with graceful in-memory fallbacks
  (``InMemorySessionService`` / ``InMemoryArtifactService`` /
  ``InMemoryMemoryService`` - names verified against installed google-adk
  2.7.0) whenever the environment is not configured, so ``adk web`` works out
  of the box.

Runner constructor verified against installed google-adk 2.7.0
(``google/adk/runners.py``): all-keyword ``Runner(*, app=None, app_name=None,
agent=None, node=None, plugins=None, artifact_service=None, session_service,
memory_service=None, credential_service=None, plugin_close_timeout=5.0,
auto_create_session=False)`` - ``session_service`` is required; passing
``agent`` requires ``app_name``.
"""

from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents import BaseAgent
from google.adk.runners import Runner

from app.agents.cta import build_cta_agent
from app.agents.feedback_router import build_feedback_router_agent
from app.agents.first_page_visual import build_first_page_visual_agent
from app.agents.learner import build_learner_agent
from app.agents.phrasing import build_phrasing_agent
from app.agents.planner import build_planner_agent
from app.agents.publisher import build_publisher_agent
from app.agents.research import build_research_agent
from app.agents.review_dispatcher import build_review_dispatcher_agent
from app.agents.stitch_verify import build_stitch_verify_agent
from app.agents.template_design import build_template_design_agent
from app import runtime
from app.config import settings
from app.observability import init_observability
from app.orchestrator import ORCHESTRATOR_NAME, CarouselOrchestrator

logger = logging.getLogger(__name__)

# Instrument BEFORE any model call so every process importing this module
# (`adk web`, the fetcher, the review API) traces from the first request.
init_observability()

_ORCHESTRATOR_DESCRIPTION = (
    "Carousel Factory orchestrator: turns one queued AI/product news item "
    "into a human-reviewed Instagram carousel. Re-entrant phase machine: "
    "generate (plan, cover video, copy, slides, CTA) -> qa (stitch + verify) "
    "-> review (mail + pause for the human verdict) -> publish or rework -> "
    "done."
)


def build_sub_agents() -> list[BaseAgent]:
    """Build fresh instances of all eleven pipeline agents.

    Order matters only for how ``adk web`` draws the graph: generation
    pipeline first, then QA, the review loop, and the terminal agents.

    Returns:
        New, parentless agent instances (each named by its ``app.state``
        constant), ready to be attached to one orchestrator.
    """
    return [
        build_research_agent(),
        build_planner_agent(),
        build_first_page_visual_agent(),
        build_phrasing_agent(),
        build_template_design_agent(),
        build_cta_agent(),
        build_stitch_verify_agent(),
        build_review_dispatcher_agent(),
        build_feedback_router_agent(),
        build_publisher_agent(),
        build_learner_agent(),
    ]


def build_root_agent() -> CarouselOrchestrator:
    """Build a fresh orchestrator with a fresh sub-agent tree.

    A new tree re-reads every ``skills/agents/<name>.md`` instruction file,
    so Learner-appended rules take effect on the next build without a code
    change.

    Returns:
        The configured :class:`CarouselOrchestrator` root agent.
    """
    return CarouselOrchestrator(
        name=ORCHESTRATOR_NAME,
        description=_ORCHESTRATOR_DESCRIPTION,
        sub_agents=build_sub_agents(),
    )


#: Module-level root agent for ``adk web`` / ``adk run`` discovery.
root_agent: CarouselOrchestrator = build_root_agent()


# ---------------------------------------------------------------------------
# Service wiring (production services with graceful in-memory fallbacks)
# ---------------------------------------------------------------------------
# The three services moved to app/runtime.py, where they are built ONCE and
# shared. Building them per runner meant a new SQLAlchemy engine and connection
# pool for every run and every resume - fine for a one-shot CLI, a connection
# leak in a long-lived web process. The agent tree is deliberately still built
# fresh per runner (see build_root_agent), because that is what lets the
# Learner's edits to skills/agents/*.md take effect without a redeploy.


def build_runner(agent: Optional[BaseAgent] = None) -> Runner:
    """Build a Runner wired to the configured (or fallback) services.

    Used by the fetcher to start pipeline runs and by the review API to
    resume paused ones - both must share the same ``settings.app_name`` and
    session database so they address the same sessions.

    Args:
        agent: Optional pre-built root agent. Defaults to a FRESH tree from
            :func:`build_root_agent` so Learner-updated instruction files are
            picked up per runner.

    Returns:
        A ready :class:`google.adk.runners.Runner`.
    """
    return Runner(
        app_name=settings.app_name,
        agent=agent if agent is not None else build_root_agent(),
        session_service=runtime.session_service(),
        artifact_service=runtime.artifact_service(),
        memory_service=runtime.memory_service(),
    )


__all__ = [
    "build_root_agent",
    "build_runner",
    "build_sub_agents",
    "root_agent",
]
