"""Carousel Factory orchestrator - the re-entrant phase state machine.

``CarouselOrchestrator`` is a custom :class:`google.adk.agents.BaseAgent`
implementing the pipeline pinned in docs/CONTRACTS.md::

    (missing) -> generate -> qa -> review -> publish -> done
                              ^        |
                              +- rework<+  (rejected verdicts / failed QA)

Design points (verified against the installed google-adk 2.7.0 source):

* **Re-entrancy.** The human-review pause ends the invocation; the resume is a
  NEW invocation. Everything the machine needs therefore lives in session
  state under the ``K_*`` keys from :mod:`app.state` - ``_run_async_impl``
  reads ``ctx.session.state`` on entry and routes purely on it (e.g. a fresh
  ``K_VERDICT`` in the ``review`` phase means "continue past review").
* **State writes.** A custom BaseAgent commits state by yielding an
  :class:`~google.adk.events.Event` whose ``actions.state_delta`` carries the
  change; the Runner appends each yielded event to the session
  (``BaseSessionService.append_event`` applies the delta) before this
  generator is resumed, so subsequent reads see the committed value.
* **Children.** Sub-agents are driven with ``child.run_async(ctx)`` (2.7.0
  copies the context per child via ``_create_invocation_context``) and their
  events are re-yielded so ``adk web`` streams the whole pipeline. They are
  declared in ``sub_agents`` so the agent graph renders.
* **The pause.** When the Review Dispatcher's ``await_human_review``
  LongRunningFunctionTool fires, the model-response event carries
  ``long_running_tool_ids``; like the shipped ``SequentialAgent`` this
  orchestrator checks ``ctx.should_pause_invocation(event)`` on every child
  event and returns immediately when it trips - the invocation ends paused
  with ``K_PHASE`` still ``review``.
* **Progress.** One concise text event (author = orchestrator name, e.g.
  ``[phase] generate -> qa``) is emitted at every phase transition for
  realtime visibility in ``adk web``.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, AsyncGenerator, ClassVar, Optional, Sequence, Type, TypeVar

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel
from typing_extensions import override

from app import observability
from app.agents.publisher import K_PUBLISH_RESULT
from app.config import settings
from app.schemas import CarouselPlan, NewsItem, QAReport, ReworkPlan, Verdict
from app.services import db
from app.state import (
    K_REVIEW_NOTICE_FAILED,
    AGENT_CTA,
    AGENT_FEEDBACK_ROUTER,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_LEARNER,
    AGENT_PHRASING,
    AGENT_PLANNER,
    AGENT_PUBLISHER,
    AGENT_REVIEW_DISPATCHER,
    AGENT_STITCH_VERIFY,
    AGENT_TEMPLATE_DESIGN,
    K_NEWS_ITEM,
    K_PHASE,
    K_PLAN,
    K_QA_REPORT,
    K_QA_ROUND,
    AGENT_RESEARCH,
    K_RECENT_FEEDBACK,
    K_REVIEW_ROUND,
    K_REWORK_FEEDBACK,
    K_REWORK_PLAN,
    K_REWORK_ROUND,
    K_RUN_ID,
    K_TOKEN_USAGE,
    K_VERDICT,
    PHASE_DONE,
    PHASE_GENERATE,
    PHASE_PUBLISH,
    PHASE_QA,
    PHASE_REVIEW,
    PHASE_REWORK,
    REWORKABLE_AGENTS,
    get_model,
)

logger = logging.getLogger(__name__)

#: The orchestrator's agent name (root of the tree rendered by ``adk web``).
ORCHESTRATOR_NAME = "carousel_orchestrator"

#: Canonical execution order of the generate-phase agents. Rework re-runs use
#: the same order for whatever subset is targeted.
GENERATE_ORDER: tuple[str, ...] = (
    AGENT_RESEARCH,
    AGENT_PLANNER,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_PHRASING,
    AGENT_TEMPLATE_DESIGN,
    AGENT_CTA,
)

#: Data-dependency map for rework targeting: re-running a key agent forces the
#: listed dependents to re-run too, because their inputs changed.
#: - research refreshes the fact base -> the planner (and thus everything
#:   downstream) must re-plan on the corrected facts.
#: - planner re-plans everything -> full regenerate (per docs/CONTRACTS.md).
#: - phrasing rewrites the copy -> template_design must re-render the slides.
_REWORK_DEPENDENTS: dict[str, tuple[str, ...]] = {
    AGENT_RESEARCH: (AGENT_PLANNER,),
    AGENT_PLANNER: (
        AGENT_FIRST_PAGE_VISUAL,
        AGENT_PHRASING,
        AGENT_TEMPLATE_DESIGN,
        AGENT_CTA,
    ),
    AGENT_PHRASING: (AGENT_TEMPLATE_DESIGN,),
}

#: Safety bound on phase-machine iterations within one invocation. The real
#: cycle count is bounded by ``settings.max_rework_rounds``; this only guards
#: against a state-corruption bug looping forever.
_MAX_PHASE_STEPS = 64

_RECENT_FEEDBACK_LIMIT = 20
_MAX_RECENT_NOTES = 12
_MAX_NOTE_CHARS = 200

M = TypeVar("M", bound=BaseModel)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _expand_rework_targets(targets: Sequence[str]) -> list[str]:
    """Expand rework targets with their data-dependent downstream agents.

    Unknown names are dropped (only ``REWORKABLE_AGENTS`` may re-run); the
    result is ordered by the canonical pipeline order so, e.g., a re-planned
    carousel regenerates cover, copy, slides and CTA in the right sequence.

    Args:
        targets: Raw target names (typically ``ReworkPlan.targets``).

    Returns:
        The expanded, pipeline-ordered target list (may be empty when no
        input target was valid).
    """
    seen: set[str] = set()
    stack = [t for t in targets if t in REWORKABLE_AGENTS]
    while stack:
        target = stack.pop()
        if target in seen:
            continue
        seen.add(target)
        stack.extend(_REWORK_DEPENDENTS.get(target, ()))
    return [name for name in GENERATE_ORDER if name in seen]


def _format_recent_feedback(records: Sequence[Any]) -> str:
    """Render recent FeedbackRecords as distilled one-line notes.

    The result is stored under ``K_RECENT_FEEDBACK`` and injected into the
    planner/phrasing instructions via ``{recent_feedback_notes?}`` state
    templating, so braces are neutralised and lines kept short.

    Args:
        records: ``FeedbackRecord``-shaped objects, newest first.

    Returns:
        A newline-joined bullet list, or ``""`` when nothing usable exists.
    """
    lines: list[str] = []
    for record in list(records)[:_MAX_RECENT_NOTES]:
        try:
            feedback = re.sub(r"\s+", " ", str(record.feedback or "")).strip()
            if not feedback:
                continue
            if len(feedback) > _MAX_NOTE_CHARS:
                feedback = feedback[: _MAX_NOTE_CHARS - 3].rstrip() + "..."
            title = str(getattr(record, "news_title", "") or "").strip() or "untitled"
            line = f"- ({record.verdict}) {title}: {feedback}"
            targets = list(getattr(record, "targets", []) or [])
            if targets:
                line += f" [targets: {', '.join(str(t) for t in targets)}]"
            lines.append(line.replace("{", "(").replace("}", ")"))
        except Exception:  # one malformed record must not kill the notes
            logger.warning("Skipping malformed feedback record.", exc_info=True)
    return "\n".join(lines)


def _merge_token_usage(state: Any, holder: dict[str, Any]) -> dict[str, Any]:
    """Fold pending LLM + image token counts into the ``K_TOKEN_USAGE`` total.

    Zeroes the pending counters so every count is committed exactly once.
    The image counts come from a per-run accumulator
    (``observability.pop_image_usage``), drained by run id so that several
    carousels in flight at once cannot take each other's image tokens.

    Returns:
        A state delta - ``{K_TOKEN_USAGE: totals}`` - or ``{}`` when nothing
        new was counted since the last merge.
    """
    tokens: dict[str, int] = holder.get("tokens") or {}
    pending = dict(tokens)
    for key in tokens:
        tokens[key] = 0
    run_id = str(state.get(K_RUN_ID) or "")
    for key, value in observability.pop_image_usage(run_id).items():
        pending[key] = pending.get(key, 0) + value
    if not any(pending.values()):
        return {}
    totals = dict(state.get(K_TOKEN_USAGE) or {})
    for key, value in pending.items():
        if value:
            totals[key] = int(totals.get(key) or 0) + int(value)
    return {K_TOKEN_USAGE: totals}


def _format_token_totals(totals: dict[str, Any]) -> str:
    """Render the run's cumulative token counts as one summary clause."""
    if not totals:
        return "no token usage recorded"
    text = (
        f"tokens in {int(totals.get('prompt_tokens') or 0):,} / "
        f"out {int(totals.get('output_tokens') or 0):,} / "
        f"total {int(totals.get('total_tokens') or 0):,} "
        f"over {int(totals.get('llm_calls') or 0)} LLM call(s)"
    )
    if totals.get("image_calls"):
        text += (
            f" + {int(totals.get('image_total_tokens') or 0):,} image tokens "
            f"over {int(totals.get('image_calls') or 0)} image call(s)"
        )
    return text


def _user_text(content: Optional[types.Content]) -> str:
    """Extract the plain text of the invocation's user content (or ``""``)."""
    if content is None or not content.parts:
        return ""
    return "\n".join(part.text for part in content.parts if part.text).strip()


def _safe_model(state: Any, key: str, model_cls: Type[M]) -> Optional[M]:
    """``get_model`` that returns None (with a log) on malformed state."""
    try:
        return get_model(state, key, model_cls)
    except Exception:
        logger.warning("State key '%s' holds a malformed value.", key, exc_info=True)
        return None


class CarouselOrchestrator(BaseAgent):
    """Root agent of the Carousel Factory: a re-entrant phase state machine.

    Construct it with the eleven pipeline agents in ``sub_agents`` (see
    ``app.agent.build_root_agent``); children are looked up by their
    ``app.state`` names at runtime, so ordering inside ``sub_agents`` only
    affects how ``adk web`` draws the graph.

    Phases (state key ``K_PHASE``):

    * *(missing)* - init: stamp ``K_RUN_ID`` / round counters, inject recent
      reviewer feedback from the memory service, synthesize an ad-hoc
      ``NewsItem`` from the user message when the fetcher did not seed one.
    * ``generate`` - planner → first_page_visual → phrasing →
      template_design → cta.
    * ``qa`` - stitch_verify assembles the Bundle + QAReport; critical issues
      auto-route to ``rework`` (no mail), otherwise → ``review``.
    * ``review`` - review_dispatcher mails the reviewers and pauses on
      ``await_human_review``; on resume the recorded ``K_VERDICT`` routes to
      ``publish`` (approved) or ``rework`` (rejected).
    * ``rework`` - learner + feedback_router (human rejections only), then
      re-run ONLY the targeted agents (planner implies its dependents);
      increments ``K_REWORK_ROUND`` and hard-stops at
      ``settings.max_rework_rounds``; → ``qa``.
    * ``publish`` - learner (store optional approval feedback) → publisher;
      → ``done`` on success.
    * ``done`` - final summary event, stop.
    """

    NAME: ClassVar[str] = ORCHESTRATOR_NAME

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------
    def _child(self, name: str) -> BaseAgent:
        """Return the sub-agent registered under an ``app.state`` name.

        Raises:
            ValueError: When the agent tree was built without that child.
        """
        agent = self.find_sub_agent(name)
        if agent is None:
            raise ValueError(
                f"CarouselOrchestrator '{self.name}' has no sub-agent named "
                f"'{name}'. Build the tree via app.agent.build_root_agent()."
            )
        return agent

    def _progress(
        self,
        ctx: InvocationContext,
        text: str,
        state_delta: Optional[dict[str, Any]] = None,
        holder: Optional[dict[str, Any]] = None,
    ) -> Event:
        """Build a concise orchestrator progress event.

        Args:
            ctx: The current invocation context.
            text: Short human-readable progress line (e.g. ``[phase] qa ->
                review``).
            state_delta: Optional state changes committed with the event.
            holder: When given, pending token counts are folded into the
                delta under ``K_TOKEN_USAGE`` (see ``_merge_token_usage``).

        Returns:
            The event to yield (the Runner appends it, applying the delta).
        """
        delta = dict(state_delta or {})
        if holder is not None:
            delta.update(_merge_token_usage(ctx.session.state, holder))
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=delta),
        )

    def _transition(
        self,
        ctx: InvocationContext,
        old: str,
        new: str,
        extra_delta: Optional[dict[str, Any]] = None,
        note: str = "",
        holder: Optional[dict[str, Any]] = None,
    ) -> Event:
        """Build the phase-transition event (sets ``K_PHASE`` = *new*)."""
        delta: dict[str, Any] = {K_PHASE: new}
        if extra_delta:
            delta.update(extra_delta)
        text = f"[phase] {old} -> {new}"
        if note:
            text += f" ({note})"
        return self._progress(ctx, text, delta, holder=holder)

    async def _record_phase_quietly(
        self, state: Any, phase: str, status: Optional[str] = None
    ) -> None:
        """Best-effort mirror of the phase into the ``runs`` table.

        ``status`` overrides the one derived from the phase, for the one case
        where they disagree: the rework hard stop ends in the ``done`` phase
        because that is what terminates the loop, but it is a failure.
        """
        run_id = str(state.get(K_RUN_ID) or "")
        if not run_id:
            return
        try:
            review_round = int(state.get(K_REVIEW_ROUND) or 0)
            await db.update_run_phase(
                run_id, phase, review_round=review_round, status=status
            )
        except Exception as exc:  # DB may be absent in local runs - never fatal
            logger.debug("runs-table phase update skipped (%s).", exc)

    async def _recent_feedback_notes(self, ctx: InvocationContext) -> str:
        """Load distilled recent reviewer feedback from the memory service.

        Best-effort: any error (no DB, no ``recent_feedback`` on the wired
        memory service such as ``InMemoryMemoryService``) yields ``""``.
        """
        service = ctx.memory_service
        recent = getattr(service, "recent_feedback", None)
        if not callable(recent):
            return ""
        try:
            records = await recent(limit=_RECENT_FEEDBACK_LIMIT)
        except Exception as exc:
            logger.warning(
                "recent_feedback unavailable (%s); continuing without notes.", exc
            )
            return ""
        return _format_recent_feedback(records)

    async def _drive(
        self,
        child: BaseAgent,
        ctx: InvocationContext,
        holder: dict[str, bool],
    ) -> AsyncGenerator[Event, None]:
        """Run one child agent, re-yielding its events.

        Sets ``holder["paused"]`` when a yielded event pauses the invocation
        (the Review Dispatcher's long-running ``await_human_review`` call) -
        the caller must then stop driving further agents and return.

        Args:
            child: The sub-agent to run.
            ctx: The orchestrator's invocation context (2.7.0 re-scopes it to
                the child inside ``run_async``).
            holder: Mutable flag holder shared with the phase loop.

        Yields:
            Every event the child produces.
        """
        logger.info("[%s] running child agent '%s'", self.name, child.name)
        async with Aclosing(child.run_async(ctx)) as agen:
            async for event in agen:
                usage = getattr(event, "usage_metadata", None)
                if usage is not None:
                    tokens = holder["tokens"]
                    tokens["prompt_tokens"] += int(usage.prompt_token_count or 0)
                    tokens["output_tokens"] += int(
                        usage.candidates_token_count or 0
                    )
                    tokens["total_tokens"] += int(usage.total_token_count or 0)
                    tokens["llm_calls"] += 1
                if ctx.should_pause_invocation(event):
                    holder["paused"] = True
                yield event

    # ------------------------------------------------------------------
    # Phase handlers (each an async generator of events)
    # ------------------------------------------------------------------
    async def _phase_init(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """Init: stamp run identity + counters, seed context, -> generate."""
        run_id = str(state.get(K_RUN_ID) or "") or f"run-{uuid.uuid4().hex[:12]}"
        delta: dict[str, Any] = {
            K_RUN_ID: run_id,
            K_REWORK_ROUND: 0,
            K_QA_ROUND: 0,
            K_REVIEW_ROUND: 0,
        }

        news_raw = state.get(K_NEWS_ITEM)
        news_id = ""
        if news_raw is None:
            # Ad-hoc run (e.g. typed into `adk web`): synthesize a NewsItem
            # from the user's message so the pipeline has a subject.
            text = _user_text(ctx.user_content)
            if not text:
                holder["halted"] = True
                yield self._progress(
                    ctx,
                    "[init] no news item found: seed session state key "
                    f"'{K_NEWS_ITEM}' (fetcher) or send the news text as the "
                    "message. Halting.",
                )
                return
            title = text.splitlines()[0].strip()[:150] or "Untitled update"
            news = NewsItem(id=run_id, title=title, body=text, source_name="adhoc")
            delta[K_NEWS_ITEM] = news.model_dump(mode="json")
            news_id = news.id
        else:
            news_id = str((news_raw or {}).get("id") or "") if isinstance(
                news_raw, dict
            ) else ""

        notes = await self._recent_feedback_notes(ctx)
        if notes or state.get(K_RECENT_FEEDBACK) is None:
            delta[K_RECENT_FEEDBACK] = notes

        delta[K_PHASE] = PHASE_GENERATE
        yield self._progress(
            ctx, f"[phase] init -> {PHASE_GENERATE} (run {run_id})", delta
        )

        # Best-effort run bookkeeping (idempotent upsert; FK/db errors ignored).
        try:
            await db.create_run(run_id, news_id or run_id)
        except Exception as exc:
            logger.debug("runs-table create skipped (%s).", exc)
        await self._record_phase_quietly(state, PHASE_GENERATE)

    async def _name_run_quietly(self, state: Any) -> None:
        """Give the task the name the PLANNER chose, once it has one.

        A topic run is created titled with whatever the person typed, because
        at that moment there is no story yet - so the sidebar showed "the new
        viral ai news" where a headline belongs. The planner's ``hook_title``
        is the carousel's actual cover line, which is the best short name this
        pipeline ever produces for the thing it is making.

        Deliberately best-effort and silent about failure, like the phase
        mirror above it: a title is presentation, and nothing downstream reads
        it to decide what to do. ``db.set_auto_title`` refuses when a human has
        renamed the task, so a rework re-running the planner cannot take a
        chosen name away.
        """
        run_id = str(state.get(K_RUN_ID) or "")
        if not run_id:
            return
        plan = _safe_model(state, K_PLAN, CarouselPlan)
        title = (getattr(plan, "hook_title", "") or "").strip() if plan else ""
        if not title:
            return
        try:
            if await db.set_auto_title(run_id, title):
                logger.info("Run %s named by the planner: %r", run_id, title)
        except Exception as exc:  # DB may be absent in local runs - never fatal
            logger.debug("runs-table title update skipped (%s).", exc)

    async def _phase_generate(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """Generate: run all six content agents in pipeline order, -> qa."""
        for name in GENERATE_ORDER:
            async for event in self._drive(self._child(name), ctx, holder):
                yield event
            if holder["paused"]:
                return
        # The plan exists by now, so the task can stop being called by the
        # words that were typed to start it.
        await self._name_run_quietly(state)
        yield self._transition(ctx, PHASE_GENERATE, PHASE_QA, holder=holder)
        await self._record_phase_quietly(state, PHASE_QA)

    async def _phase_qa(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """QA: stitch_verify; auto-route criticals to rework, else -> review."""
        async for event in self._drive(
            self._child(AGENT_STITCH_VERIFY), ctx, holder
        ):
            yield event
        if holder["paused"]:
            return

        report = _safe_model(state, K_QA_REPORT, QAReport)
        if report is None:
            holder["halted"] = True
            yield self._progress(
                ctx,
                "[qa] stitch_verify produced no QA report - halting; phase "
                "stays 'qa', re-run the pipeline to retry.",
            )
            return

        if report.passed:
            issue_count = len(report.issues)
            yield self._transition(
                ctx,
                PHASE_QA,
                PHASE_REVIEW,
                extra_delta={K_VERDICT: None},  # each review round starts clean
                note=f"QA passed, {issue_count} non-critical note(s)",
                holder=holder,
            )
            await self._record_phase_quietly(state, PHASE_REVIEW)
            return

        plan = _safe_model(state, K_REWORK_PLAN, ReworkPlan)
        targets = plan.targets if plan is not None else []
        yield self._transition(
            ctx,
            PHASE_QA,
            PHASE_REWORK,
            note="QA failed - auto rework, no review mail; targets: "
            + (", ".join(targets) or "(router default)"),
            holder=holder,
        )
        await self._record_phase_quietly(state, PHASE_REWORK)

    async def _phase_review(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """Review: dispatch mail + pause, or route a recorded verdict.

        Re-entrancy: on the resumed invocation ``K_PHASE`` is still
        ``review`` and the dispatcher records ``K_VERDICT`` from the pending
        function response. If a fresh verdict is ALREADY in state (recorded
        by an earlier invocation that stopped before routing), it is routed
        directly without re-running the dispatcher.
        """
        verdict = _safe_model(state, K_VERDICT, Verdict)
        if verdict is None:
            async for event in self._drive(
                self._child(AGENT_REVIEW_DISPATCHER), ctx, holder
            ):
                yield event
            if holder["paused"]:
                # Mail sent; invocation ends here. K_PHASE stays "review" so
                # the resume re-enters this handler.
                #
                # The flag is READ, not written. await_human_review has just
                # decided whether this pause can actually be answered - it
                # cannot be, if the reviewers were never told or the pending
                # call could not be persisted - and hard-coding False here
                # overwrote that verdict with an optimistic one, hiding a
                # stranded run behind a healthy-looking status.
                unanswerable = bool(state.get(K_REVIEW_NOTICE_FAILED))
                # Carried on THIS event's delta, which is the only way it
                # reaches the database.
                #
                # await_human_review sets the flag on tool_context.state, and
                # that write is visible here because ADK state writes mutate
                # the live session dict. But a long-running tool returning a
                # falsy result builds NO function-response event
                # (flows/llm_flows/functions.py), so its state_delta never
                # becomes an event and append_event never persists it. The
                # value existed only in this process's memory - which is
                # exactly where the console cannot see it, because
                # _halted_awaiting_review reads sessions.state out of
                # Postgres.
                #
                # Both directions matter. True is what routes a stranded run
                # to the decide-without-pause fallback; False is what clears a
                # True persisted by an earlier halt, so a run whose re-notice
                # succeeded stops offering that fallback while live Approve
                # buttons are in someone's Telegram.
                yield self._progress(
                    ctx,
                    "[review] the carousel is ready but the pause could not "
                    "be registered - decide it in the console."
                    if unanswerable
                    else "[review] review request sent; waiting for a human.",
                    {K_REVIEW_NOTICE_FAILED: unanswerable},
                )
                # Mirror the phase even though it did not CHANGE.
                #
                # A fresh run reached review through _phase_qa's transition,
                # which recorded it. A run RESUMED at review skips that
                # entirely, so the row kept status='running' from
                # resume_interrupted_run - and _drive_run's fallback, which
                # exists to respect a status the orchestrator already moved
                # off running, found nothing to respect and wrote
                # 'interrupted' over a run that was waiting for a human. The
                # console then offered Resume, which re-entered review, paused,
                # and was recorded interrupted again.
                #
                # This also carries K_REVIEW_ROUND across, so the console stops
                # showing round 1 while the pipeline is on round 2.
                await self._record_phase_quietly(
                    state, PHASE_REVIEW, status=db.RUN_STATUS_AWAITING_REVIEW
                )
                logger.info(
                    "[%s] paused for human review (run %s, answerable=%s).",
                    self.name,
                    state.get(K_RUN_ID),
                    not unanswerable,
                )
                return
            verdict = _safe_model(state, K_VERDICT, Verdict)
            if verdict is None:
                # The carousel is FINISHED and reviewable - QA passed and the
                # bundle is assembled. The only thing that failed is telling
                # the reviewer about it. So the honest status is "a human is
                # needed", not "running" (nothing is) and not "failed" (the
                # work is done and waiting).
                #
                # Recording it as running left the console showing a live task
                # forever, with no way to act on a carousel that was ready.
                holder["halted"] = True
                yield self._progress(
                    ctx,
                    "[review] the carousel is ready but the review "
                    "notification could not be sent - halting at 'review'. "
                    "Decide it in the console, or resume to retry the notice.",
                    {K_REVIEW_NOTICE_FAILED: True},
                )
                await self._record_phase_quietly(
                    state, PHASE_REVIEW, status=db.RUN_STATUS_AWAITING_REVIEW
                )
                return

        if verdict.status == "approved":
            yield self._transition(
                ctx,
                PHASE_REVIEW,
                PHASE_PUBLISH,
                note="approved by human reviewer",
                holder=holder,
            )
            await self._record_phase_quietly(state, PHASE_PUBLISH)
        else:
            summary = (verdict.feedback or "").strip()
            if len(summary) > 140:
                summary = summary[:137].rstrip() + "..."
            yield self._transition(
                ctx,
                PHASE_REVIEW,
                PHASE_REWORK,
                note=f"rejected: {summary}" if summary else "rejected",
                holder=holder,
            )
            await self._record_phase_quietly(state, PHASE_REWORK)

    async def _phase_rework(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """Rework: learn + route (human rejections), re-run targets, -> qa.

        Two kinds of rework arrive here and they are budgeted separately.
        A human rejection spends ``K_REWORK_ROUND`` against
        ``max_rework_rounds``; stitch_verify's automatic critical-QA rework
        spends ``K_QA_ROUND`` against ``max_qa_rounds``. They shared one
        counter once, which meant a run whose first render kept failing could
        exhaust the budget before the reviewer had seen anything - their first
        rejection then hit the hard stop on entry, was never routed, and (with
        K_VERDICT cleared by the stop delta) was discarded outright. The
        reviewer's real allowance varied run to run depending on how flaky the
        renders happened to be.
        """
        verdict = _safe_model(state, K_VERDICT, Verdict)
        human_driven = verdict is not None and verdict.status == "rejected"
        if human_driven:
            counter_key, cap = K_REWORK_ROUND, settings.max_rework_rounds
            kind, cap_note = "rework", "rework round cap"
        else:
            counter_key, cap = K_QA_ROUND, settings.max_qa_rounds
            kind, cap_note = "QA retry", "automatic QA retry cap"

        rounds_done = int(state.get(counter_key) or 0)
        if rounds_done >= cap:
            yield self._transition(
                ctx,
                PHASE_REWORK,
                PHASE_DONE,
                extra_delta={K_REWORK_PLAN: None, K_VERDICT: None},
                note=(
                    f"HARD STOP: {cap_note} of {cap} reached without "
                    "approval - manual intervention required"
                ),
                holder=holder,
            )
            # Session state moves to DONE above because that is what stops the
            # loop - but the runs table is what the console DISPLAYS, and this
            # run did not finish, it gave up in rework. Mirroring DONE there
            # claimed two things that never happened: it lit every step of the
            # phase rail including Publishing, and it recorded the run as
            # published in the task list. What is true is "failed, while
            # reworking", so that is what is stored.
            #
            # Recovery is unaffected: interrupted_run_candidates skips runs
            # whose status is already failed, so leaving an active phase name
            # here cannot make a dead run look resumable.
            await self._record_phase_quietly(
                state, PHASE_REWORK, status=db.RUN_STATUS_FAILED
            )
            return

        if human_driven:
            # Human-driven rework: store the lesson, then map feedback to the
            # exact agents that must re-run (contract order: learner first).
            async for event in self._drive(self._child(AGENT_LEARNER), ctx, holder):
                yield event
            if holder["paused"]:
                return
            async for event in self._drive(
                self._child(AGENT_FEEDBACK_ROUTER), ctx, holder
            ):
                yield event
            if holder["paused"]:
                return
        # QA auto-rework arrives here with K_REWORK_PLAN already written by
        # stitch_verify and no verdict - learner/router are skipped.

        plan = _safe_model(state, K_REWORK_PLAN, ReworkPlan)
        targets = _expand_rework_targets(plan.targets if plan is not None else [])
        if not targets:
            # No usable plan: re-plan from the top (planner implies a full
            # regenerate of its dependents, so nothing criticised survives).
            targets = _expand_rework_targets([AGENT_PLANNER])

        feedback = (
            (plan.feedback.strip() if plan is not None and plan.feedback else "")
            or (verdict.feedback.strip() if verdict is not None else "")
            or str(state.get(K_REWORK_FEEDBACK) or "").strip()
        )
        next_round = rounds_done + 1

        # Commit the correction context BEFORE the re-runs: the targeted
        # agents read K_REWORK_FEEDBACK as their highest-priority instruction.
        yield self._progress(
            ctx,
            f"[rework] {kind} {next_round}/{cap}: "
            f"re-running {', '.join(targets)}",
            {K_REWORK_FEEDBACK: feedback, counter_key: next_round},
            holder=holder,
        )

        for name in targets:
            async for event in self._drive(self._child(name), ctx, holder):
                yield event
            if holder["paused"]:
                return

        # The plan and verdict are consumed; K_REWORK_FEEDBACK stays set so
        # stitch_verify re-checks against it (it clears the key on QA pass).
        yield self._transition(
            ctx,
            PHASE_REWORK,
            PHASE_QA,
            extra_delta={K_REWORK_PLAN: None, K_VERDICT: None},
            note=f"round {next_round} pieces regenerated - re-verifying",
            holder=holder,
        )
        await self._record_phase_quietly(state, PHASE_QA)

    async def _phase_publish(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """Publish: learner (optional approval feedback) -> publisher, -> done."""
        async for event in self._drive(self._child(AGENT_LEARNER), ctx, holder):
            yield event
        if holder["paused"]:
            return
        async for event in self._drive(self._child(AGENT_PUBLISHER), ctx, holder):
            yield event
        if holder["paused"]:
            return

        result = state.get(K_PUBLISH_RESULT)
        if isinstance(result, dict) and result.get("media_id"):
            permalink = str(result.get("permalink") or "")
            yield self._transition(
                ctx,
                PHASE_PUBLISH,
                PHASE_DONE,
                extra_delta={K_VERDICT: None, K_REWORK_FEEDBACK: ""},
                note=f"published: {permalink or result.get('media_id')}",
                holder=holder,
            )
            await self._record_phase_quietly(state, PHASE_DONE)
            return

        message = (
            str(result.get("message") or "unknown error")
            if isinstance(result, dict)
            else "no publish result recorded"
        )
        holder["halted"] = True
        yield self._progress(
            ctx,
            f"[publish] publish failed ({message}) - halting; phase stays "
            "'publish', re-run the pipeline to retry (the publisher is "
            "idempotent and will not double-post).",
        )

    async def _phase_done(
        self, ctx: InvocationContext, state: Any, holder: dict[str, bool]
    ) -> AsyncGenerator[Event, None]:
        """Done: emit the final run summary and stop."""
        holder["stopped"] = True
        run_id = str(state.get(K_RUN_ID) or "?")
        review_rounds = int(state.get(K_REVIEW_ROUND) or 0)
        rework_rounds = int(state.get(K_REWORK_ROUND) or 0)
        qa_rounds = int(state.get(K_QA_ROUND) or 0)
        result = state.get(K_PUBLISH_RESULT)
        if isinstance(result, dict) and result.get("media_id"):
            outcome = f"published ({result.get('permalink') or result.get('media_id')})"
        else:
            outcome = "not published"
        tokens_delta = _merge_token_usage(state, holder)
        totals = tokens_delta.get(K_TOKEN_USAGE) or dict(
            state.get(K_TOKEN_USAGE) or {}
        )
        yield self._progress(
            ctx,
            f"[done] run {run_id}: {outcome}; review mails: {review_rounds}; "
            f"rework rounds: {rework_rounds}; QA retries: {qa_rounds}; "
            f"{_format_token_totals(totals)}.",
            tokens_delta,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        """Run the phase machine until it pauses, halts, or completes.

        Phases can chain within one invocation (e.g. a resumed rejection runs
        rework -> qa -> review and pauses again on the round-2 mail); the loop
        re-reads ``K_PHASE`` from session state after every handler, so a
        fresh invocation entering at any phase routes identically.
        """
        state = ctx.session.state
        handlers = {
            PHASE_GENERATE: self._phase_generate,
            PHASE_QA: self._phase_qa,
            PHASE_REVIEW: self._phase_review,
            PHASE_REWORK: self._phase_rework,
            PHASE_PUBLISH: self._phase_publish,
            PHASE_DONE: self._phase_done,
        }
        holder: dict[str, Any] = {
            "paused": False,
            "halted": False,
            "stopped": False,
            # Pending (not yet state-committed) token counts for this
            # invocation; _drive accumulates, _merge_token_usage commits.
            "tokens": {
                "prompt_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "llm_calls": 0,
            },
        }

        for _ in range(_MAX_PHASE_STEPS):
            phase = str(state.get(K_PHASE) or "")
            if not phase:
                handler = self._phase_init
            elif phase in handlers:
                handler = handlers[phase]
            else:
                yield self._progress(
                    ctx,
                    f"[error] unknown phase '{phase}' in session state - "
                    "halting; fix or clear the 'phase' key to recover.",
                )
                return

            async for event in handler(ctx, state, holder):
                yield event
            if holder["paused"] or holder["halted"] or holder["stopped"]:
                # Commit token counts gathered since the last transition (e.g.
                # the Review Dispatcher's call before the review pause) so the
                # run total survives the invocation boundary. Content-free
                # state-delta events are ordinary in ADK and do not disturb
                # the paused long-running call.
                tokens_delta = _merge_token_usage(state, holder)
                if tokens_delta:
                    yield Event(
                        invocation_id=ctx.invocation_id,
                        author=self.name,
                        branch=ctx.branch,
                        actions=EventActions(state_delta=tokens_delta),
                    )
                return

        yield self._progress(
            ctx,
            f"[error] phase-machine step budget ({_MAX_PHASE_STEPS}) exceeded "
            "- stopping this invocation; state is preserved, re-run to "
            "continue.",
        )


__all__ = [
    "GENERATE_ORDER",
    "ORCHESTRATOR_NAME",
    "CarouselOrchestrator",
]
