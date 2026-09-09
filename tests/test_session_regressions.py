"""Defects found by reviewing the fixes themselves.

Every entry here is a bug introduced or missed by an earlier repair in this
same codebase, which is the whole reason the review happened. They are grouped
together so it stays obvious that a fix is not finished until something has
gone looking for what it broke.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.runs import cancellation
from app.runs import service as service_mod
from app.runs.bus import KIND_ERROR, KIND_TERMINAL
from app.runs.stream import _lifecycle_frames
from app.services import db

RUN_ID = "run-session-regressions"


class TaskRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """A finished task must retract only its OWN registration.

    ``_run_tasks`` is keyed by run id, and one run can legitimately have a new
    task registered while the previous is still unwinding - a cancelled run
    resumed straight away, or a timeout whose handler is still writing the
    ending. An unconditional ``pop(run_id)`` in the old callback deletes the
    NEW task's entry: asyncio keeps only a weak reference, so the running
    coroutine can be collected mid-run, and Stop can no longer find it.
    """

    async def asyncTearDown(self) -> None:
        for task in list(service_mod._run_tasks.values()):
            task.cancel()
        service_mod._run_tasks.clear()

    async def test_an_old_task_does_not_evict_its_replacement(self) -> None:
        async def forever() -> None:
            await asyncio.Event().wait()

        old = asyncio.get_running_loop().create_task(asyncio.sleep(0))
        service_mod._run_tasks[RUN_ID] = old
        old.add_done_callback(service_mod._forget_task(RUN_ID, old))

        # A replacement is registered before the old one's callback fires.
        new = asyncio.get_running_loop().create_task(forever())
        service_mod._run_tasks[RUN_ID] = new
        new.add_done_callback(service_mod._forget_task(RUN_ID, new))

        await asyncio.sleep(0)  # let the old task finish and fire its callback
        await asyncio.sleep(0)

        self.assertIs(
            service_mod._run_tasks.get(RUN_ID),
            new,
            "the finished task's callback deleted the live task's entry, "
            "dropping the only strong reference to it and hiding it from Stop",
        )
        self.assertIn(RUN_ID, service_mod.active_run_ids())


class EveryCancellationRaisesTheFlagTests(unittest.IsolatedAsyncioTestCase):
    """Giving up on a run must stop its publish, however it was given up on.

    ``cancel_run`` raises the stop flag, which is what keeps a worker thread
    from finishing an Instagram publish nobody wants any more. The two OTHER
    ways a run is abandoned - the wall-clock timeout and the shutdown drain -
    cancel the task without raising it, so the thread carried on and the post
    could still go live.
    """

    async def asyncSetUp(self) -> None:
        cancellation.clear(RUN_ID)

    async def asyncTearDown(self) -> None:
        cancellation.clear(RUN_ID)

    async def _drive(self, consume):
        patches = [
            patch.object(service_mod, "consume_invocation", consume),
            patch.object(db, "set_run_status", AsyncMock(return_value=None)),
            patch.object(db, "max_run_seq", AsyncMock(return_value=0)),
            patch.object(db, "get_run", AsyncMock(return_value={"status": "running"})),
            patch.object(service_mod, "record_event", AsyncMock(return_value=None)),
            patch.object(service_mod, "_heartbeat", AsyncMock(return_value=None)),
            patch.object(service_mod, "RUN_TIMEOUT_S", 0.01),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        runner = type("R", (), {"close": AsyncMock(return_value=None)})()
        return await service_mod._drive_run(RUN_ID, runner, object())

    async def test_a_timed_out_run_is_flagged_so_its_publish_stops(self) -> None:
        async def hangs(*_a, **_k):
            await asyncio.sleep(30)

        await self._drive(hangs)
        self.assertTrue(
            cancellation.is_requested(RUN_ID),
            "the run was abandoned on a timeout without raising the stop "
            "flag, so a publish already inside asyncio.to_thread keeps going "
            "and the carousel can appear hours after the run was given up on",
        )

    async def test_a_cancelled_run_is_flagged_too(self) -> None:
        async def hangs(*_a, **_k):
            await asyncio.sleep(30)

        patches = [
            patch.object(service_mod, "consume_invocation", hangs),
            patch.object(db, "set_run_status", AsyncMock(return_value=None)),
            patch.object(db, "max_run_seq", AsyncMock(return_value=0)),
            patch.object(service_mod, "record_event", AsyncMock(return_value=None)),
            patch.object(service_mod, "_heartbeat", AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        runner = type("R", (), {"close": AsyncMock(return_value=None)})()
        task = asyncio.get_running_loop().create_task(
            service_mod._drive_run(RUN_ID, runner, object())
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(
            cancellation.is_requested(RUN_ID),
            "the shutdown drain cancels tasks without raising the flag, so a "
            "publish in a worker thread outlives the process's decision to "
            "stop",
        )


class ClaimedStoryIsTheStoryClosedTests(unittest.IsolatedAsyncioTestCase):
    """Claiming a queue row and closing one must name the SAME row.

    Pasting a link claims the matching newsroom card, but ``fetch_url_item``
    mints its own synthetic ``url-<hash>`` id and that is what went into
    ``runs.news_id``. ``_close_news_item`` closes the row named there, so it
    named a row that does not exist - the story stayed 'processing' forever,
    exactly as if the claim had never happened, and the startup sweep later
    returned it to the newsroom for someone to pay for again.
    """

    async def test_a_url_run_records_the_claimed_rows_id(self) -> None:
        created: dict = {}

        async def fake_create_run(run_id, news_id):
            created["news_id"] = news_id

        async def fake_session(**_kwargs):
            return None

        runner = type(
            "R",
            (),
            {"session_service": type("S", (), {"create_session": staticmethod(fake_session)})()},
        )()

        patches = [
            patch.object(
                service_mod,
                "fetch_url_item",
                AsyncMock(return_value={"id": "url-deadbeef", "title": "t"}),
            ),
            patch.object(
                db, "claim_news_by_url_hash", AsyncMock(return_value="queue-row-42")
            ),
            patch.object(db, "create_run", fake_create_run),
            patch.object(db, "set_run_meta", AsyncMock(return_value=None)),
            patch.object(db, "set_run_status", AsyncMock(return_value=None)),
            patch.object(db, "count_runs_since", AsyncMock(return_value=0)),
            patch("app.agent.build_runner", lambda: runner),
            patch.object(service_mod, "spawn_run", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.addCleanup(service_mod._reserved.clear)

        await service_mod.start_run(source="url", url="https://example.com/a")

        self.assertEqual(
            created.get("news_id"),
            "queue-row-42",
            "the run recorded the synthetic url-<hash> id instead of the "
            "queue row it just claimed, so nothing ever closes that row",
        )


class LifecycleMergeDoesNotDuplicateTests(unittest.IsolatedAsyncioTestCase):
    """Merge the events ADK cannot have - and only those.

    ``run_events`` holds two different things under KIND_ERROR: endings
    synthesised outside any invocation (which ADK never sees) and ordinary
    agent errors recorded by ``consume_invocation`` (which ADK has too).
    Merging on the kind alone printed every agent error twice, once from each
    source. The author is what separates them: nothing synthesised from
    outside an invocation has an agent to name.
    """

    async def test_an_authored_error_is_not_merged_back_in(self) -> None:
        rows = [
            {
                "seq": 1,
                "kind": KIND_ERROR,
                "author": "template_design",
                "text": "the render failed",
                "data": {},
                "created_at": "2026-08-27T10:00:00+00:00",
            },
            {
                "seq": 2,
                "kind": KIND_ERROR,
                "author": "",
                "text": "Run failed: image API returned 500",
                "data": {},
                "created_at": "2026-08-27T10:00:05+00:00",
            },
            {
                "seq": 3,
                "kind": KIND_TERMINAL,
                "author": "",
                "text": "Run stopped (interrupted).",
                "data": {},
                "created_at": "2026-08-27T10:00:06+00:00",
            },
        ]
        with patch.object(db, "load_run_events", AsyncMock(return_value=rows)):
            frames = await _lifecycle_frames(RUN_ID, 2000)

        texts = [f["text"] for f in frames]
        self.assertNotIn(
            "the render failed",
            texts,
            "an agent error that ADK already recorded was merged in a second "
            "time, so the trace shows the same failure twice",
        )
        self.assertIn("Run failed: image API returned 500", texts)
        self.assertIn("Run stopped (interrupted).", texts)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AmbiguousPublishIsNotRetriedTests(unittest.TestCase):
    """A publish whose reply was lost must never be retried automatically.

    Every other failure in ``publish_carousel`` happens before anything is
    public, so retrying is free. The POST to ``media_publish`` is different:
    once it leaves, a read timeout says nothing about whether Instagram
    accepted it. The publisher agent is instructed to retry once on anything
    that "looks transient (a timeout or temporary network problem)" - which is
    precisely this case - so an ambiguous publish became a second identical
    carousel on a real account, unattended.
    """

    def test_a_lost_reply_raises_its_own_type_carrying_the_container_id(self) -> None:
        import httpx

        from app.tools import instagram_tools

        calls = {"n": 0}

        def fake_request(client, method, path, params=None, data=None):
            calls["n"] += 1
            if path.endswith("/media_publish"):
                raise httpx.ReadTimeout("reply never arrived")
            if "/media" in path and method == "POST":
                return {"id": "container-1"}
            return {"status_code": "FINISHED"}

        # settings is a frozen dataclass, so swap the module's reference to it
        # rather than trying to assign a field.
        fake_settings = type(
            "S",
            (),
            {
                "ig_user_id": "ig-1",
                "ig_access_token": "tok",
                "max_carousel_slides": 10,
            },
        )()
        with patch.object(instagram_tools, "_graph_request", fake_request), \
             patch.object(instagram_tools, "settings", fake_settings):
            with self.assertRaises(instagram_tools.PublishUncertain) as caught:
                instagram_tools.publish_carousel(
                    {"caption": "c"},
                    ["https://x/a.mp4", "https://x/b.png"],
                )

        self.assertTrue(
            caught.exception.creation_id,
            "the container id is the only handle an operator has for "
            "reconciling against the account",
        )
        self.assertIn("MAY already be live", str(caught.exception))

    def test_the_publisher_marks_it_not_retryable(self) -> None:
        import inspect

        from app.agents import publisher

        source = inspect.getsource(publisher.publish_approved_carousel)
        self.assertIn(
            '"retryable": False',
            source,
            "the result does not tell the agent to stop, so its instruction "
            "to retry transient failures applies and posts twice",
        )
        self.assertIn(
            "retryable",
            publisher.DEFAULT_INSTRUCTION,
            "the instruction never mentions the flag, so the model has no "
            "reason to honour it",
        )


class IncompleteDeckFailsQATests(unittest.TestCase):
    """A deck with a hole in it is the wrong deck, not a deck with a note.

    ``passed = not critical``, so only critical issues route a run back to
    rework. A dropped or out-of-order body slide, a rendered count that
    disagrees with the plan, and a copy set that disagrees with what was
    rendered were all "major" - which meant an incomplete carousel went
    straight to a human as though it were finished. The per-slide checks
    cannot catch any of them: every slide that DID render is individually
    valid.
    """

    def test_completeness_failures_are_critical(self) -> None:
        import inspect
        import re

        from app.agents import stitch_verify

        source = inspect.getsource(stitch_verify)
        for marker in (
            "are not contiguous",
            "match the plan (",
            "body slides were rendered",
        ):
            index = source.find(marker)
            self.assertNotEqual(index, -1, f"check for {marker!r} disappeared")
            window = source[max(0, index - 900) : index]
            severity = re.findall(r'severity="(\w+)"', window)
            self.assertEqual(
                severity[-1] if severity else "",
                "critical",
                f"the {marker!r} check is not critical, so QA passes and the "
                "incomplete deck is mailed to a reviewer as finished",
            )


class OneLessonPerVerdictTests(unittest.IsolatedAsyncioTestCase):
    """Retrying a rework must not multiply the lesson it learned.

    The Learner runs on every entry into the rework phase, and a retried run
    enters it again with the same verdict still in state. A real run finished
    with four rows for one rejection. Theme detection already skips same-run
    duplicates, so no false rule was distilled - but only the newest dozen
    rows reach K_RECENT_FEEDBACK, which the planner and phrasing read as "what
    reviewers keep asking for", so one retried complaint evicted three other
    runs' lessons.
    """

    async def test_an_identical_lesson_is_not_stored_again(self) -> None:
        from app.agents import learner as learner_mod

        text = "[first visual] the cover has no clip for the news"
        stored: list = []

        class _Service:
            async def recent_feedback(self, limit=20):
                return [
                    type(
                        "R",
                        (),
                        {
                            "run_id": RUN_ID,
                            "verdict": "rejected",
                            "feedback": text,
                            "targets": ["first_page_visual"],
                            "news_title": "t",
                        },
                    )()
                ]

            async def store_feedback(self, record):
                stored.append(record)

        ctx = type(
            "Ctx",
            (),
            {
                "state": {
                    "run_id": RUN_ID,
                    "review_verdict": {"status": "rejected", "feedback": text},
                    "news_item": {"title": "t"},
                }
            },
        )()

        with patch.object(
            learner_mod, "_resolve_memory_service", lambda _c: _Service()
        ), patch.object(learner_mod, "_append_learned_rule", lambda *a: (False, "")):
            await learner_mod.store_feedback_and_distill(ctx)

        self.assertEqual(
            stored,
            [],
            "the same lesson was stored a second time; every retry of a stuck "
            "run adds another copy and pushes other runs' lessons out of the "
            "twelve notes the planner actually reads",
        )


class TransactionPoolerIsSafeTests(unittest.TestCase):
    """Switching to the transaction pooler must be a URL change, nothing else.

    The session-mode pooler on 5432 caps this project at 15 clients, which is
    what starved ``save_pending_review`` and stranded a run. Transaction mode
    on 6543 lifts that ceiling - but it hands the connection to someone else
    between statements, so a prepared statement is executed on a backend that
    never saw it and every query dies with "prepared statement
    _asyncpg_stmt_N does not exist". Both pools keep such a cache, so both
    have to be told not to.
    """

    def test_the_statement_cache_is_off_for_a_transaction_pooler(self) -> None:
        from app.services import db as db_mod

        self.assertEqual(
            db_mod._statement_cache_size(
                "postgresql+asyncpg://u:p@aws-0.pooler.supabase.com:6543/postgres"
            ),
            0,
            "asyncpg would prepare statements behind a transaction pooler, "
            "so every query fails once the port is changed",
        )

    def test_the_cache_stays_on_for_session_mode(self) -> None:
        from app.services import db as db_mod

        self.assertGreater(
            db_mod._statement_cache_size(
                "postgresql+asyncpg://u:p@aws-0.pooler.supabase.com:5432/postgres"
            ),
            0,
            "session mode keeps one backend per client, so giving up "
            "prepared statements there costs planning time for nothing",
        )

    def test_an_explicit_override_wins(self) -> None:
        import os

        from app.services import db as db_mod

        with patch.dict(os.environ, {"DB_STATEMENT_CACHE": "0"}):
            self.assertEqual(db_mod._statement_cache_size(), 0)
