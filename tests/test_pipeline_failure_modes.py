"""Failure modes the pipeline must survive, pinned as tests.

Every test here was written against a defect that was reproduced first. They
assert the CORRECT behaviour, so a red test is a bug that is still open and a
green one is a bug that has been fixed - not the other way round.

The five properties, and why each is worth a test rather than a comment:

* **A slow browser must not lose the event it is waiting for.** ``RunBus``
  promises in its own docstring that a full queue drops its OLDEST event,
  because "the newest events are the ones the viewer is actually waiting for".
  The implementation frees one slot and then pushes two items into it, so the
  newest is the one that is actually lost.
* **Deciding a verdict must never raise.** ``submit_verdict`` documents
  "Never raises for an expected condition". The notice-failed path re-enters
  the run through ``resume_interrupted_run``, which enforces the run caps and
  raises ``RunRefused`` - straight through the API as a 500, after the verdict
  has already been written to session state.
* **Finishing a run that has already been paid for must not be refused by the
  cap that limits STARTING runs.** The daily cap exists so a runaway loop
  cannot generate ten carousels; applying it to a resume means a finished
  carousel cannot be approved.
* **A review resume must record its timeline.** ``app.runs.stream`` states
  that both a fresh run and a review resume go through ``consume_invocation``,
  precisely so that approving from Telegram streams the rework and publish
  into an open browser tab. ``resume_pipeline`` drives its own loop instead.
* **Every action the console offers must exist on the server.** The task list
  renders a Delete button that issues ``DELETE /api/runs/{id}``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.review import resume as resume_mod
from app.review import verdict as verdict_mod
from app.runs.bus import KIND_GAP, KIND_PROGRESS, QUEUE_MAXSIZE, RunBus, RunEvent
from app.runs import service as service_mod
from app.services import db
from web_api import routes_runs
from web_api.auth import Identity
from web_api.deps import current_identity

RUN_ID = "run-failure-modes"


# ---------------------------------------------------------------------------
# The live event bus under backpressure
# ---------------------------------------------------------------------------
class BusBackpressureTests(unittest.TestCase):
    """A subscriber that has fallen behind must still get the newest event."""

    def test_a_full_queue_drops_the_oldest_event_not_the_newest(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            bus = RunBus()
            async with bus.subscribe(RUN_ID) as queue:
                # A browser that has stopped reading: fill its queue exactly.
                for seq in range(QUEUE_MAXSIZE):
                    await bus.publish(
                        RunEvent(
                            run_id=RUN_ID,
                            seq=seq,
                            kind=KIND_PROGRESS,
                            text=f"old-{seq}",
                        )
                    )
                self.assertTrue(queue.full())

                # The event the viewer is actually waiting for arrives now.
                await bus.publish(
                    RunEvent(
                        run_id=RUN_ID,
                        seq=9_999,
                        kind=KIND_PROGRESS,
                        text="the-newest-event",
                    )
                )

                delivered = []
                while not queue.empty():
                    delivered.append(queue.get_nowait())
                texts = [event.text for event in delivered]
                kinds = [event.kind for event in delivered]
                return "the-newest-event" in texts, KIND_GAP in kinds

        newest_delivered, gap_marked = asyncio.run(scenario())

        # The gap marker is right and is already delivered.
        self.assertTrue(gap_marked, "a client that missed events must be told")
        # ...but it must not cost the client the event it was waiting for.
        self.assertTrue(
            newest_delivered,
            "RunBus.publish freed one slot and then pushed TWO items (the gap "
            "marker and the event), so the second put_nowait raised QueueFull "
            "and was swallowed by the bare `except Exception`. The newest "
            "event - the one the docstring promises to protect - is dropped.",
        )


# ---------------------------------------------------------------------------
# Deciding a verdict when the run caps are exhausted
# ---------------------------------------------------------------------------
def _notice_failed_run(daily_count: int = 999):
    """Patches for a run parked at review because the notice never sent."""
    return (
        patch.object(
            verdict_mod, "_halted_awaiting_review", AsyncMock(return_value=True)
        ),
        patch.object(db, "claim_pending_review", AsyncMock(return_value=None)),
        patch.object(db, "set_session_verdict", AsyncMock(return_value=True)),
        patch.object(db, "record_verdict", AsyncMock(return_value=None)),
        patch.object(
            db,
            "get_run",
            AsyncMock(return_value={"run_id": RUN_ID, "phase": "review"}),
        ),
        patch.object(db, "count_runs_since", AsyncMock(return_value=daily_count)),
        # The decision now proceeds all the way into re-entering the run, so
        # everything that path touches has to be stubbed for the test to be
        # about the verdict rather than about the database being absent.
        patch.object(db, "set_run_status", AsyncMock(return_value=None)),
        patch.object(db, "max_run_seq", AsyncMock(return_value=0)),
        patch.object(service_mod, "record_event", AsyncMock(return_value=None)),
        patch("app.agent.build_runner", lambda: object()),
        patch.object(service_mod, "_drive_run", AsyncMock(return_value=None)),
    )


class VerdictNeverRaisesTests(unittest.TestCase):
    """``submit_verdict`` documents that it never raises. Hold it to that."""

    def test_approving_a_notice_failed_run_at_the_daily_cap_returns_an_outcome(
        self,
    ) -> None:
        patches = _notice_failed_run(daily_count=999)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        try:
            outcome = asyncio.run(
                verdict_mod.submit_verdict(
                    RUN_ID, "approved", "", reviewer="a@b.co", source="web"
                )
            )
        except Exception as exc:  # noqa: BLE001 - that is the defect
            self.fail(
                "submit_verdict raised "
                f"{type(exc).__name__}({exc}) instead of returning a "
                "VerdictOutcome. _decide_without_pause calls "
                "resume_interrupted_run, which calls _check_limits and raises "
                "RunRefused; nothing between there and the HTTP handler "
                "catches it. The verdict has ALREADY been written to session "
                "state by then, so the run is left decided-but-not-re-entered."
            )

        self.assertIsNotNone(outcome)

    def test_approving_a_notice_failed_run_while_busy_returns_an_outcome(self) -> None:
        patches = _notice_failed_run(daily_count=0)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        busy = patch.object(
            service_mod,
            "active_run_ids",
            lambda: {f"run-other-{i}" for i in range(service_mod.MAX_CONCURRENT_RUNS)},
        )
        busy.start()
        self.addCleanup(busy.stop)

        try:
            outcome = asyncio.run(
                verdict_mod.submit_verdict(
                    RUN_ID, "approved", "", reviewer="a@b.co", source="web"
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "submit_verdict raised "
                f"{type(exc).__name__}({exc}) because another run was in "
                "flight. Approving a finished carousel must not depend on "
                "whether an unrelated run happens to be working."
            )
        self.assertIsNotNone(outcome)


class VerdictEndpointTests(unittest.TestCase):
    """The same defect, seen the way the reviewer sees it."""

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(routes_runs.router, prefix="/api")
        app.dependency_overrides[current_identity] = lambda: Identity(
            email="a@b.co", subject="a@b.co", role="reviewer"
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_the_verdict_endpoint_never_answers_500(self) -> None:
        patches = list(_notice_failed_run(daily_count=999))
        patches.append(
            patch.object(routes_runs, "_apply_cover_choice", AsyncMock(return_value=None))
        )
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        response = self._client().post(
            f"/api/runs/{RUN_ID}/verdict", json={"status": "approved", "feedback": ""}
        )

        self.assertNotEqual(
            response.status_code,
            500,
            "Approving a ready carousel answered 500 Internal Server Error. "
            "An exhausted run cap is an expected condition and belongs in the "
            "409 family with a code the console can render.",
        )


# ---------------------------------------------------------------------------
# Finishing a run must not be blocked by the cap on STARTING runs
# ---------------------------------------------------------------------------
class ResumeIsNotAStartTests(unittest.TestCase):
    """The daily cap limits new carousels, not the completion of old ones."""

    def test_the_daily_cap_does_not_block_resuming_an_existing_run(self) -> None:
        patches = [
            patch.object(
                db,
                "get_run",
                AsyncMock(return_value={"run_id": RUN_ID, "phase": "review"}),
            ),
            patch.object(db, "count_runs_since", AsyncMock(return_value=999)),
            patch.object(db, "set_run_status", AsyncMock(return_value=None)),
            patch.object(db, "max_run_seq", AsyncMock(return_value=0)),
            patch.object(service_mod, "record_event", AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        async def scenario() -> None:
            with patch("app.agent.build_runner", lambda: object()), patch.object(
                service_mod, "_drive_run", AsyncMock(return_value=None)
            ):
                await service_mod.resume_interrupted_run(RUN_ID, requested_by="a@b.co")

        try:
            asyncio.run(scenario())
        except service_mod.RunRefused as exc:
            self.fail(
                f"resume_interrupted_run refused with {exc.code!r}. The run's "
                "images and reasoning are already paid for; MAX_RUNS_PER_DAY "
                "exists to stop new carousels being STARTED, and applying it "
                "here means a finished carousel cannot be approved, a stopped "
                "one cannot be resumed, and Re-run is dead for the rest of "
                "the day."
            )


# ---------------------------------------------------------------------------
# A review resume must produce a timeline
# ---------------------------------------------------------------------------
class ResumeRecordsItsTimelineTests(unittest.TestCase):
    """Approving from Telegram must stream into an open browser tab.

    ``app/runs/stream.py`` opens with: "One function does the consuming -
    consume_invocation - and BOTH a fresh run and a review resume go through
    it. That is the point. If the resume path kept its own loop, approving
    from Telegram would advance the pipeline while an open browser tab showed
    nothing."
    """

    def test_resume_pipeline_consumes_through_the_recorded_stream(self) -> None:
        source = inspect.getsource(resume_mod.resume_pipeline)
        self.assertTrue(
            "consume_invocation" in source,
            "resume_pipeline drives `runner.run_async` in its own loop, so "
            "record_event is never called for the resumed leg: nothing is "
            "published to the run bus (no live SSE for the whole rework and "
            "publish), no run_events rows are written, and no terminal event "
            "fires - so the console's onEnd never runs and the task list and "
            "sidebar are not refreshed when the carousel goes live.",
        )

    def test_a_resumed_leg_heartbeats_like_a_driven_run(self) -> None:
        source = inspect.getsource(resume_mod.resume_pipeline).lower()
        self.assertTrue(
            "heartbeat" in source,
            "resume_pipeline runs for minutes (rework, then the Instagram "
            "publish) without touching the run row. _drive_run starts a "
            "30s heartbeat for exactly this reason - db.interrupted_run_"
            "candidates uses a 180s idle threshold and its docstring requires "
            "the heartbeat to keep it honest.",
        )


class WhyItDiedIsVisibleTests(unittest.TestCase):
    """A failed task must be able to say what killed it.

    Six timeline entries exist ONLY in ``run_events``, because ``record_event``
    synthesises them outside any ADK invocation: the terminal line in
    ``_drive_run``, ``_finish_badly``'s "Run failed: {exc}" and "Run
    cancelled.", ``resume_interrupted_run``'s "Resuming interrupted run at
    phase ...", ``cancel_run``'s stale-stop notice, and recovery's
    "Interrupted during '<phase>' - the service restarted".

    ``load_trace`` returns ADK's transcript EXCLUSIVELY whenever it has at
    least one row, and every console-started run has ADK rows - so for every
    console run those six lines are unreachable. ``/runs/{id}/trace`` is the
    console's only history source, and ``run-detail.tsx`` opens a failed task
    on the trace tab precisely "because that is where the explanation is".
    """

    def test_a_lifecycle_event_survives_alongside_the_adk_transcript(self) -> None:
        adk_rows = [
            {
                "seq": 1,
                "created_at": "2026-08-26T10:00:00+00:00",
                "event_data": {
                    "author": "template_design",
                    "content": {"parts": [{"text": "rendering slide 3"}]},
                },
            }
        ]
        # What _finish_badly wrote, and the only record of why the run died.
        run_event_rows = [
            {
                "seq": 2,
                "kind": "error",
                "author": "",
                "text": "Run failed: image API returned 500",
                "data": {"status": "failed"},
                "created_at": "2026-08-26T10:00:05+00:00",
            }
        ]

        with patch.object(db, "load_adk_events", AsyncMock(return_value=adk_rows)), \
             patch.object(db, "load_run_events", AsyncMock(return_value=run_event_rows)):
            from app.runs.stream import load_trace

            frames = asyncio.run(load_trace(RUN_ID))

        rendered = " ".join(str(frame.get("text") or "") for frame in frames)
        self.assertIn(
            "image API returned 500",
            rendered,
            "load_trace short-circuits on `if rows: return [...]`, so the "
            "run_events table is read only when ADK has NOTHING. A console "
            "run always has ADK rows, so the console shows a red 'Failed' "
            "chip above a trace that just stops mid-agent - while the "
            "exception text sits in run_events, served by no endpoint. The "
            "same silence hides Stopped, Cancelled, the 'service restarted' "
            "recovery note, and the 'Resuming at phase X' marker that "
            "separates two legs of one task.",
        )


class TheReasonSurvivesRoutingTests(unittest.TestCase):
    """What the reviewer actually said must reach the agents that re-run.

    ``feedback_router``'s own module docstring and its instruction rule both
    promise the reviewer's text is used "verbatim, never paraphrased". The
    sanitizer computes the authoritative text into ``effective_feedback`` and
    then only uses it when the model left the field blank::

        if not plan.feedback:
            plan.feedback = effective_feedback

    So a router LLM that summarises instead of copying wins, and
    ``_phase_rework`` prefers ``plan.feedback`` over ``verdict.feedback``
    when building ``K_REWORK_FEEDBACK`` - the one string the re-run agents
    read as their highest-priority instruction. The same paraphrase is also
    what ``derive_targets_from_feedback`` keyword-matches on, so a summary
    that drops the tell-tale words misroutes as well.
    """

    def test_the_reviewers_words_win_over_the_routers_paraphrase(self) -> None:
        from app.agents.feedback_router import _sanitize_rework_plan
        from app.state import K_REWORK_FEEDBACK, K_REWORK_PLAN, K_VERDICT

        verbatim = "the price is wrong, the model is $20/M not $200/M"

        class _Ctx:
            state = {
                K_VERDICT: {"status": "rejected", "feedback": verbatim},
                K_REWORK_FEEDBACK: "",
                # What a small utility model actually tends to emit.
                K_REWORK_PLAN: {
                    "targets": ["research"],
                    "reasons": [],
                    "feedback": "pricing figure incorrect",
                },
            }

        ctx = _Ctx()
        _sanitize_rework_plan(ctx)
        routed = str((ctx.state.get(K_REWORK_PLAN) or {}).get("feedback") or "")

        self.assertIn(
            "$20/M",
            routed,
            "The router's paraphrase replaced the reviewer's text, so "
            "research re-runs told only 'pricing figure incorrect' - without "
            "the correct value. It re-emits the same wrong number, QA passes "
            "(it does not check facts), and the identical carousel is mailed "
            "again, round after round, to the cap. effective_feedback is "
            "already computed one line above; it needs to overwrite rather "
            "than fill-if-empty.",
        )


class ReworkBudgetsAreNotSharedTests(unittest.TestCase):
    """Machine retries must not spend the human's five chances.

    ``K_REWORK_ROUND`` is a single counter incremented in ``_phase_rework``
    (orchestrator.py:690) and checked against ``max_rework_rounds``
    (orchestrator.py:623). Both routes into that phase increment it: a human
    rejection, and stitch_verify's automatic critical-QA rework - which needs
    no human at all.
    """

    def test_qa_retries_and_human_rejections_do_not_share_one_counter(self) -> None:
        from app.config import settings
        from app.orchestrator import CarouselOrchestrator
        from app.state import K_QA_ROUND, K_REWORK_ROUND

        self.assertNotEqual(
            K_REWORK_ROUND,
            K_QA_ROUND,
            "the two kinds of rework must be counted in different state keys",
        )
        self.assertTrue(
            settings.max_qa_rounds > 0 and settings.max_rework_rounds > 0,
            "each budget needs its own cap",
        )

        source = inspect.getsource(CarouselOrchestrator._phase_rework)
        for key in ("K_REWORK_ROUND", "K_QA_ROUND"):
            self.assertIn(key, source, f"_phase_rework must budget {key}")
        self.assertIn(
            "human_driven",
            source,
            "Both routes into _phase_rework must not spend the same counter. "
            "Sharing one meant automatic QA "
            "retries (a missing or undersized artifact, retried until it "
            "renders) exhaust the budget before the reviewer has seen "
            "anything. Their first rejection then hits the hard stop on "
            "entry: it is never routed, the learner never stores it, "
            "K_VERDICT is cleared by the stop delta so the feedback is lost "
            "outright, and the run is recorded FAILED with 'rework round cap "
            "of 5 reached without approval'. The human's allowance is "
            "5 minus however many times QA happened to fail, which varies "
            "run to run. Count them separately.",
        )


class RestartIsAtomicTests(unittest.TestCase):
    """A refused Re-run must leave the task exactly as it found it."""

    def test_a_refused_rerun_does_not_rewind_the_session(self) -> None:
        rewound: list[str] = []

        async def fake_rewind(run_id, app_name, user_id, phase):
            rewound.append(phase)
            return True

        patches = [
            patch.object(
                db,
                "get_run",
                AsyncMock(return_value={"run_id": RUN_ID, "phase": "done"}),
            ),
            patch.object(db, "rewind_session_for_restart", fake_rewind),
            # The concurrency cap is what refuses a re-run now: the daily cap
            # deliberately no longer applies to work already paid for. Fill
            # every slot, whatever the cap happens to be set to.
            patch.object(
                service_mod,
                "active_run_ids",
                lambda: {f"run-other-{i}" for i in range(service_mod.MAX_CONCURRENT_RUNS)},
            ),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        async def scenario() -> None:
            await service_mod.restart_run(RUN_ID, requested_by="a@b.co")

        with self.assertRaises(service_mod.RunRefused):
            asyncio.run(scenario())

        self.assertEqual(
            rewound,
            [],
            "restart_run rewound the session (rework_round back to 0, phase "
            f"moved to {rewound!r}) and only THEN called "
            "resume_interrupted_run, which refused. The re-run never "
            "happened, but the task's rework budget and recorded phase were "
            "changed anyway - so a later Resume re-enters at the rewound "
            "phase rather than where the work actually stopped. Check the "
            "caps before mutating anything.",
        )


# ---------------------------------------------------------------------------
# A hung agent must not brick the pipeline
# ---------------------------------------------------------------------------
class RunHasAWallClockCapTests(unittest.TestCase):
    """One hung LLM call must not stop every future carousel.

    ``resume_pipeline`` already bounds a resumed leg with
    ``asyncio.wait_for(..., timeout=RESUME_TIMEOUT_S)``. The primary path has
    no equivalent, and the three facts below compose into a dead service:

    * ``_drive_run`` awaits ``consume_invocation`` with no cap, and neither
      ``app.llm.resolve_model`` nor the ADK/LiteLLM call it builds is given a
      request timeout;
    * ``_heartbeat`` keeps touching the run row every 30 s regardless, so the
      180 s idle threshold in ``db.interrupted_run_candidates`` never trips
      and startup recovery cannot reclaim it;
    * the concurrency cap counts the hung run as active, so it holds a slot
      against every new run for as long as it hangs.

    Net effect: the console shows a task Running forever with a pulsing trace,
    and no carousel can be made again until a human notices and clicks Stop.
    """

    def test_the_primary_run_path_is_bounded_like_the_resume_path(self) -> None:
        drive = inspect.getsource(service_mod._drive_run)
        consume = inspect.getsource(service_mod)
        bounded = "wait_for" in drive or "RUN_TIMEOUT" in consume
        self.assertTrue(
            bounded,
            "_drive_run awaits the whole invocation with no wall-clock cap, "
            "while resume_pipeline bounds its leg with RESUME_TIMEOUT_S. A "
            "hung tool or LLM call therefore pins the single concurrency slot "
            "forever: the heartbeat keeps the run looking alive to startup "
            "recovery, and _check_limits refuses every new run behind it.",
        )

    def test_the_heartbeat_cannot_outlive_the_work_it_vouches_for(self) -> None:
        self.assertLess(
            service_mod.HEARTBEAT_INTERVAL_S,
            180,
            "the heartbeat must stay well under the recovery idle threshold",
        )
        source = inspect.getsource(service_mod._heartbeat)
        self.assertTrue(
            "deadline" in source or "max" in source.lower(),
            "_heartbeat touches the run row unconditionally and forever, so "
            "it vouches for a task that may have stopped making progress "
            "hours ago. It needs an upper bound after which it stops "
            "asserting liveness, or a stalled run is invisible to every "
            "automatic recovery path the service has.",
        )


# ---------------------------------------------------------------------------
# Every action the console offers must exist on the server
# ---------------------------------------------------------------------------
class ConsoleActionsAreRoutedTests(unittest.TestCase):
    """The task list renders buttons; each one must reach a real endpoint."""

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(routes_runs.router, prefix="/api")
        app.dependency_overrides[current_identity] = lambda: Identity(
            email="a@b.co", subject="a@b.co", role="reviewer"
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_deleting_a_task_is_a_route(self) -> None:
        with patch.object(
            db,
            "get_run",
            AsyncMock(return_value={"run_id": RUN_ID, "status": "done", "phase": "done"}),
        ), patch.object(
            db, "delete_run", AsyncMock(return_value={"deleted": True})
        ):
            response = self._client().delete(f"/api/runs/{RUN_ID}")

        self.assertNotIn(
            response.status_code,
            (404, 405),
            "frontend/src/components/run/task-actions.tsx issues "
            "DELETE /api/runs/{run_id} from the Delete button on the task "
            "list, the trace page and the review page, and "
            "db.delete_run exists - but no route connects them, so every "
            "confirmed delete fails.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ImageCostIsAttributedPerRunTests(unittest.TestCase):
    """With several carousels in flight, each pays for its own images.

    Image tools run outside any ADK model call, so their token usage cannot
    ride on an event's ``usage_metadata`` the way every other count does. It
    goes into a module-level accumulator instead - which was a single shared
    bucket, drained by whichever run reached a phase transition first. That was
    exact only while one run existed per process, an assumption
    ``MAX_CONCURRENT_RUNS`` no longer makes.
    """

    def setUp(self) -> None:
        from app import observability

        self.obs = observability

    def test_two_runs_do_not_drain_each_others_image_tokens(self) -> None:
        class _Usage:
            def __init__(self, n: int) -> None:
                self.input_tokens = n
                self.output_tokens = n
                self.total_tokens = n * 2

        async def scenario() -> tuple[dict, dict]:
            async def work(run_id: str, tokens: int) -> None:
                # Each run's task gets its own context, which is what carries
                # the run id down to the image tool.
                self.obs.bind_run(run_id)
                await asyncio.sleep(0)
                self.obs.record_image_usage(
                    "gpt-image-2", "images.generate", _Usage(tokens)
                )

            await asyncio.gather(work("run-a", 100), work("run-b", 7))
            return self.obs.pop_image_usage("run-a"), self.obs.pop_image_usage("run-b")

        a, b = asyncio.run(scenario())

        self.assertEqual(
            (a["image_input_tokens"], a["image_calls"]),
            (100, 1),
            "run-a's image tokens were taken by another run: the accumulator "
            "is a process-global bucket, so whichever run reaches a phase "
            "transition first drains everyone's counts into its own total.",
        )
        self.assertEqual(
            (b["image_input_tokens"], b["image_calls"]),
            (7, 1),
            "run-b reported no image cost because another run drained it",
        )

    def test_draining_a_run_twice_does_not_double_count(self) -> None:
        self.obs.bind_run("run-c")
        self.obs.pop_image_usage("run-c")  # start clean
        first = self.obs.pop_image_usage("run-c")
        self.assertEqual(first["image_calls"], 0)


class ConcurrentRunsAreCountedTests(unittest.TestCase):
    """The cap must be enforced against every driver, not just fresh runs."""

    def test_the_cap_is_a_positive_number_of_slots(self) -> None:
        self.assertGreaterEqual(
            service_mod.MAX_CONCURRENT_RUNS,
            1,
            "a cap of zero would refuse every run, including resumes",
        )

    def test_the_driving_task_binds_its_run_before_working(self) -> None:
        """Without this, image cost lands on whichever run bound last."""
        for name, source in (
            ("_drive_run", inspect.getsource(service_mod._drive_run)),
            ("resume_pipeline", inspect.getsource(resume_mod.resume_pipeline)),
        ):
            self.assertIn(
                "bind_run",
                source,
                f"{name} drives a run without binding it, so every image call "
                "it makes is attributed to whatever ran before it.",
            )


class ConcurrencySlotIsReservedTests(unittest.TestCase):
    """The cap must hold against simultaneous clicks, not just sequential ones.

    A run only became countable at ``spawn_run``. Between ``_check_limits`` and
    there sat a session write, three DB writes and - for a pasted URL - a
    thirty-second HTTP fetch. Every request that arrived inside that window
    passed the same check, so N simultaneous starts began N runs however small
    the cap was. A reservation closes it: the count and the claim happen with
    no await between them, so the event loop cannot interleave anything.
    """

    def test_simultaneous_starts_cannot_exceed_the_cap(self) -> None:
        cap = service_mod.MAX_CONCURRENT_RUNS
        attempts = cap + 3
        started: list[str] = []

        async def slow_session(**_kwargs):
            # Stands in for create_session + the DB writes: any await here is
            # enough to let another start_run interleave.
            await asyncio.sleep(0)

        class _Runner:
            session_service = type("S", (), {"create_session": staticmethod(slow_session)})()

        def fake_spawn(run_id, *, runner, first_message):
            started.append(run_id)
            task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
            service_mod._run_tasks[run_id] = task
            task.add_done_callback(lambda _t: service_mod._run_tasks.pop(run_id, None))
            return task

        patches = [
            patch("app.agent.build_runner", lambda: _Runner()),
            patch.object(service_mod, "spawn_run", fake_spawn),
            patch.object(db, "create_run", AsyncMock(return_value=None)),
            patch.object(db, "set_run_meta", AsyncMock(return_value=None)),
            patch.object(db, "set_run_status", AsyncMock(return_value=None)),
            patch.object(db, "count_runs_since", AsyncMock(return_value=0)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        async def scenario():
            results = await asyncio.gather(
                *(
                    service_mod.start_run(source="topic", topic=f"story number {i}")
                    for i in range(attempts)
                ),
                return_exceptions=True,
            )
            for task in list(service_mod._run_tasks.values()):
                task.cancel()
            service_mod._run_tasks.clear()
            service_mod._reserved.clear()
            return results

        results = asyncio.run(scenario())
        refused = [r for r in results if isinstance(r, service_mod.RunRefused)]

        self.assertLessEqual(
            len(started),
            cap,
            f"{len(started)} runs started at once against a cap of {cap}. "
            "_check_limits counted the actives and then let the caller await "
            "several times before the run became countable, so every "
            "simultaneous request passed the same check.",
        )
        self.assertEqual(
            len(refused),
            attempts - cap,
            "the requests over the cap must be refused, not silently dropped",
        )

    def test_a_refused_start_hands_its_slot_back(self) -> None:
        """Otherwise the cap shrinks by one on every refusal, permanently."""
        service_mod._reserved.clear()

        async def scenario():
            with patch.object(db, "count_runs_since", AsyncMock(return_value=10**6)):
                try:
                    await service_mod.start_run(source="topic", topic="a story")
                except service_mod.RunRefused:
                    pass
            return set(service_mod._reserved)

        leftover = asyncio.run(scenario())
        self.assertEqual(
            leftover,
            set(),
            f"a refused start left {leftover} reserved forever, so the number "
            "of usable slots drops with every refusal until none are left.",
        )
