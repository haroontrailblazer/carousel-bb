"""A paused review must be recorded as one, on every path into it.

Traced from a real run (``run-35caff54b6eb``) that spent a day in this loop::

    Resuming interrupted run at phase 'review'.
    [review_dispatcher] -> tool await_human_review
    [review_dispatcher] <- tool await_human_review
    [review_dispatcher] The review pause couldn't be registered ...
    [carousel_orchestrator] [review] review request sent; waiting for a human.
    Run stopped (interrupted).

Three separate defects compose into it, and each gets a test here.

1. ``classify_event`` decides an event's kind by checking for tool calls
   BEFORE checking ``long_running_tool_ids``. The model-response event that
   pauses an invocation carries both, so it is classified ``tool`` and
   ``awaiting_review`` is never set - ``consume_invocation`` then reports
   ``paused=False`` for a run that genuinely paused.

2. Nothing mirrors the phase to the ``runs`` table when a run is RESUMED
   straight into ``review``. The transition that normally does it lives in
   ``_phase_qa``, which a resume skips. So the row keeps ``status='running'``
   from ``resume_interrupted_run``, and ``_drive_run``'s fallback - which
   respects any status the orchestrator already moved off running - has
   nothing to respect and writes ``interrupted``. The row's ``review_round``
   also stays behind session state, so the console shows round 1 during
   round 2.

3. Returning a value from ``await_human_review`` does not prevent the pause.
   google-adk puts ``long_running_tool_ids`` on the model-response event that
   CONTAINS the call, so ``should_pause_invocation`` trips on that event
   whatever the tool later returns. A tool that reports "did not pause" while
   the invocation pauses anyway leaves the run telling two stories at once.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents import review_dispatcher as dispatcher_mod
from app.runs import service as service_mod
from app.runs.stream import classify_event
from app.services import db
from app.state import K_REVIEW_NOTICE_FAILED

RUN_ID = "run-review-pause"


def _pausing_event():
    """The model-response event ADK emits when a long-running tool is called.

    It carries the function CALL and the pending id in
    ``long_running_tool_ids``. This single event is what ends the invocation.
    """
    return SimpleNamespace(
        error_message=None,
        actions=SimpleNamespace(state_delta={}),
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    text=None,
                    function_call=SimpleNamespace(
                        name=dispatcher_mod.AWAIT_REVIEW_TOOL_NAME, id="call_X"
                    ),
                    function_response=None,
                )
            ]
        ),
        long_running_tool_ids=["call_X"],
    )


class PauseIsClassifiedAsAPauseTests(unittest.TestCase):
    def test_the_pausing_event_reports_awaiting_review(self) -> None:
        _kind, data = classify_event(_pausing_event())
        self.assertTrue(
            data.get("awaiting_review"),
            "The event that ends the invocation was classified without "
            "awaiting_review, because classify_event returns KIND_TOOL as "
            "soon as it sees a function call and never reaches the "
            "long_running_tool_ids check below it. consume_invocation reads "
            "that flag to decide `paused`, so a paused run is reported as "
            "not paused - and _drive_run records it 'interrupted'.",
        )

    def test_the_tool_name_is_still_reported(self) -> None:
        """Reordering the checks must not lose what the trace displays."""
        _kind, data = classify_event(_pausing_event())
        self.assertIn(
            dispatcher_mod.AWAIT_REVIEW_TOOL_NAME,
            data.get("tool_calls") or [],
            "the trace still needs to show which tool was called",
        )


class ResumedReviewIsRecordedTests(unittest.TestCase):
    """A resume that lands on review must tell the runs table so.

    Without it the row keeps status='running' and review_round from the
    previous round, so the console shows "Round 1" through round 2 and
    _drive_run's fallback marks a paused run interrupted.
    """

    def test_pausing_mirrors_the_phase_and_round(self) -> None:
        import inspect

        from app.orchestrator import CarouselOrchestrator

        source = inspect.getsource(CarouselOrchestrator._phase_review)
        # Only the pause branch itself, which ends at its `return`.
        start = source.index('if holder["paused"]:')
        pause_branch = source[start : source.index("\n                return", start)]
        self.assertIn(
            "_record_phase_quietly",
            pause_branch,
            "Nothing mirrors the phase when the run pauses. On a fresh run "
            "_phase_qa's transition happens to have done it already; a run "
            "RESUMED at review skips that, so the row still says 'running' "
            "with the previous review_round.",
        )


class PauseGuardIsHonestTests(unittest.TestCase):
    """Do not claim the run did not pause when it did.

    ``should_pause_invocation`` reads ``long_running_tool_ids`` off the
    model-response event, so the invocation ends paused whatever the tool
    returns. A tool that returns an error dict here produces a function
    response the model reports as "did not pause", while the orchestrator
    simultaneously records "review request sent; waiting for a human" - and
    clears the notice-failed flag that the console's fallback depends on.
    """

    def _ctx(self, state=None):
        return SimpleNamespace(
            state=state if state is not None else {"run_id": RUN_ID, "review_round": 1},
            session=SimpleNamespace(id=RUN_ID),
            function_call_id="call_X",
        )

    def test_a_failed_pending_save_flags_the_run_instead_of_refusing(self) -> None:
        ctx = self._ctx({"run_id": RUN_ID, "review_round": 1,
                         "temp:review_sent_round": 1})
        with patch.object(
            db, "save_pending_review",
            AsyncMock(side_effect=ConnectionError("max clients reached")),
        ), patch.object(db, "clear_pending_review", AsyncMock(return_value=None)):
            result = asyncio.run(dispatcher_mod.await_human_review(ctx))

        self.assertIsNone(
            result,
            "Returning a value cannot stop the pause - ADK decided that from "
            "the model event before this tool ran. Returning one only adds a "
            "contradictory 'did not pause' message to a run that paused.",
        )
        self.assertTrue(
            ctx.state.get(K_REVIEW_NOTICE_FAILED),
            "The pause is unanswerable without a pending_reviews row, so the "
            "run must be flagged for the console's decide-without-pause "
            "fallback rather than left looking healthy.",
        )

    def test_a_successful_pause_is_not_flagged(self) -> None:
        ctx = self._ctx({"run_id": RUN_ID, "review_round": 1,
                         "temp:review_sent_round": 1})
        with patch.object(db, "save_pending_review", AsyncMock(return_value=None)):
            result = asyncio.run(dispatcher_mod.await_human_review(ctx))
        self.assertIsNone(result)
        self.assertFalse(ctx.state.get(K_REVIEW_NOTICE_FAILED))


class InterruptedIsNotTheDefaultEndingTests(unittest.TestCase):
    """A run that paused ends 'awaiting_review', not 'interrupted'."""

    def test_a_paused_invocation_is_recorded_awaiting_review(self) -> None:
        recorded: list[str] = []

        async def fake_consume(*_args, **_kwargs):
            # What consume_invocation returns once the pausing event is
            # classified correctly.
            return {"events": 3, "last_seq": 9, "paused": True, "phase": "review"}

        async def fake_status(run_id, status):
            recorded.append(status)

        patches = [
            patch.object(service_mod, "consume_invocation", fake_consume),
            patch.object(db, "set_run_status", fake_status),
            patch.object(db, "get_run", AsyncMock(return_value={"status": "running"})),
            patch.object(service_mod, "record_event", AsyncMock(return_value=None)),
            patch.object(service_mod, "_heartbeat", AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        runner = SimpleNamespace(close=AsyncMock(return_value=None))
        asyncio.run(service_mod._drive_run(RUN_ID, runner, object()))

        self.assertIn(
            db.RUN_STATUS_AWAITING_REVIEW,
            recorded,
            f"a paused run was recorded as {recorded!r}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ConnectionBudgetTests(unittest.TestCase):
    """Two pools, one budget.

    Supabase's pooler on port 5432 runs in SESSION mode and caps the project
    at a fixed number of clients - 15 for this project. This process opens
    connections from two independent places whose maxima ADD: the asyncpg pool
    in ``app.services.db`` and the SQLAlchemy engine behind ADK's
    ``DatabaseSessionService``. At the original defaults that sum was 20, and
    the pooler answered whichever write asked last with::

        (EMAXCONNSESSION) max clients reached in session mode -
        max clients are limited to pool_size: 15

    That is not a slow query, it is a write that never happens. The one that
    lost in a real run was ``save_pending_review``, which strands a finished
    carousel at 'review' with no live call for any surface to answer.
    """

    #: What Supabase reported for this project. Headroom below it is for a
    #: psql session, a migration, or a diagnostic script.
    POOLER_LIMIT = 15

    def test_the_two_pools_fit_inside_the_pooler_limit(self) -> None:
        from app.runtime import _ENGINE_KWARGS
        from app.services.db import _MAX_CONNECTIONS

        sqlalchemy_max = _ENGINE_KWARGS["pool_size"] + _ENGINE_KWARGS["max_overflow"]
        total = _MAX_CONNECTIONS + sqlalchemy_max

        self.assertLess(
            total,
            self.POOLER_LIMIT,
            f"asyncpg ({_MAX_CONNECTIONS}) + SQLAlchemy ({sqlalchemy_max}) = "
            f"{total} against a {self.POOLER_LIMIT}-client pooler. Under load "
            "the pooler refuses connections outright, and a refused "
            "save_pending_review strands a run at 'review' forever.",
        )

    def test_there_is_headroom_for_a_person(self) -> None:
        from app.runtime import _ENGINE_KWARGS
        from app.services.db import _MAX_CONNECTIONS

        total = (
            _MAX_CONNECTIONS
            + _ENGINE_KWARGS["pool_size"]
            + _ENGINE_KWARGS["max_overflow"]
        )
        self.assertLessEqual(
            total,
            self.POOLER_LIMIT - 3,
            "leave room to open psql against production while it runs",
        )


class PendingReviewIsRetriedTests(unittest.TestCase):
    """The write that decides whether a pause can be answered gets retries."""

    def test_a_transient_failure_is_retried_and_succeeds(self) -> None:
        calls = {"n": 0}

        class _Pool:
            async def execute(self, *_a, **_k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise ConnectionError("max clients reached in session mode")

        with patch.object(db, "get_pool", AsyncMock(return_value=_Pool())), \
             patch.object(db.asyncio, "sleep", AsyncMock(return_value=None)):
            asyncio.run(db.save_pending_review(RUN_ID, RUN_ID, "call_X"))

        self.assertEqual(
            calls["n"],
            3,
            "a single transient pooler refusal must not decide that a "
            "finished carousel can never be reviewed",
        )

    def test_a_persistent_failure_still_raises(self) -> None:
        """The caller must still learn it failed, so it can flag the run."""

        class _Pool:
            async def execute(self, *_a, **_k):
                raise ConnectionError("still down")

        with patch.object(db, "get_pool", AsyncMock(return_value=_Pool())), \
             patch.object(db.asyncio, "sleep", AsyncMock(return_value=None)):
            with self.assertRaises(ConnectionError):
                asyncio.run(db.save_pending_review(RUN_ID, RUN_ID, "call_X"))


class ReviewPageDoesNotContradictItselfTests(unittest.TestCase):
    """A task at phase `review` is reviewable, whatever the status says.

    The real run showed two cards at once: "This task was interrupted" from
    the task page (``status === "interrupted"``) and "The rework stopped
    before finishing / Nothing to approve yet" from the approval card
    (``pending_review && status !== "awaiting_review" && !notice_failed``).

    Both read the same wrong value. Fixing the status is the real repair -
    these assertions are the second layer, so a stale status can never again
    tell someone a finished carousel is unreviewable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        cls.card = (
            repo / "frontend" / "src" / "components" / "review" / "approval-card.tsx"
        ).read_text(encoding="utf-8")

    def test_the_stopped_card_is_gated_on_not_having_reached_review(self) -> None:
        import re

        block = re.search(
            r"if \(\s*run\.pending_review &&(.*?)\) \{", self.card, re.DOTALL
        )
        assert block is not None, "the 'rework stopped' branch was not found"
        self.assertIn(
            "reachedReview",
            block.group(1),
            "The card claims a task 'did not get back to a reviewable "
            "carousel' from the status alone. Phase is the harder fact: "
            "reaching `review` means QA passed and the bundle is assembled, "
            "so the task is reviewable even when the status column is stale.",
        )


class TheNoticeFlagReachesTheDatabaseTests(unittest.TestCase):
    """Writing state inside a long-running tool does not persist it.

    ``await_human_review`` sets K_REVIEW_NOTICE_FAILED on tool_context.state,
    and that write is visible in-process because ADK state writes mutate the
    live session dict. But a long-running tool that returns a falsy result
    builds NO function-response event, so its state_delta never becomes an
    event and ``append_event`` never writes it to Postgres.

    That is a trap this test exists to close: an in-memory assertion on
    ctx.state passes while production is broken, because the console reads
    sessions.state out of the database. The orchestrator's pause event is the
    nearest delta that actually commits, so the value has to ride on that.
    """

    def test_the_pause_event_carries_the_flag_in_its_delta(self) -> None:
        import inspect

        from app.orchestrator import CarouselOrchestrator

        source = inspect.getsource(CarouselOrchestrator._phase_review)
        start = source.index('if holder["paused"]:')
        branch = source[start : source.index("\n                return", start)]

        self.assertIn(
            "K_REVIEW_NOTICE_FAILED: unanswerable",
            branch,
            "The pause event carries no delta for the flag, so whatever "
            "await_human_review decided about answerability lives only in "
            "this process's memory. The console reads session state from "
            "Postgres and will never see it.",
        )


class SetVerdictAcceptsRealDecisionsTests(unittest.TestCase):
    """The guard must catch the stale case and nothing else.

    ``capture_verdict_on_resume`` runs before this tool on every resume and
    consumes the response id itself. So on the ordinary HANDLE_VERDICT turn -
    the one where a human just clicked Approve - the id is ALREADY consumed
    when the model does as instructed and calls set_verdict. A guard keyed on
    "is this id consumed" therefore fired on every legitimate verdict and told
    the model to raise a NEW review round for a carousel that had just been
    approved.

    The case it was written for is the SEND_MAIL re-entry: a rework loop
    inside one invocation, where the round-1 response is still user_content.
    The directive is what separates them.
    """

    def _ctx(self, directive, consumed=True, response=True):
        from google.genai import types

        parts = []
        if response:
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call_1",
                        name=dispatcher_mod.AWAIT_REVIEW_TOOL_NAME,
                        response={"status": "approved", "feedback": ""},
                    )
                )
            )
        return SimpleNamespace(
            state={
                "run_id": RUN_ID,
                "temp:review_dispatcher_directive": directive,
                "temp:review_consumed_call_ids": ["call_1"] if consumed else [],
            },
            user_content=types.Content(role="user", parts=parts) if parts else None,
        )

    def test_the_ordinary_approve_turn_is_accepted(self) -> None:
        ctx = self._ctx("handle_verdict")
        with patch.object(db, "clear_pending_review", AsyncMock(return_value=None)):
            result = asyncio.run(
                dispatcher_mod.set_verdict("approved", "", tool_context=ctx)
            )
        self.assertEqual(
            result.get("status"),
            "recorded",
            "Every legitimate verdict was refused, and the model was told to "
            "send a NEW review request for a carousel a human had just "
            "approved - an extra Telegram round and a second pause on the "
            "happy path.",
        )
        self.assertEqual(result.get("verdict"), "approved")

    def test_a_stale_verdict_on_a_send_turn_is_still_refused(self) -> None:
        ctx = self._ctx("send_mail")
        result = asyncio.run(
            dispatcher_mod.set_verdict("approved", "", tool_context=ctx)
        )
        self.assertEqual(
            result.get("status"),
            "error",
            "the round-1 decision was re-applied to a carousel that has since "
            "been reworked",
        )

    def test_a_model_invented_approval_is_refused(self) -> None:
        """No reviewer response means nobody has decided anything."""
        ctx = self._ctx("send_mail", consumed=False, response=False)
        result = asyncio.run(
            dispatcher_mod.set_verdict("approved", "", tool_context=ctx)
        )
        self.assertEqual(
            result.get("status"),
            "error",
            "a model that called set_verdict instead of send_review_request "
            "could approve a carousel no human had seen",
        )
        from app.state import K_VERDICT

        self.assertIsNone(ctx.state.get(K_VERDICT))
