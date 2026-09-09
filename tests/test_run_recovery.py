"""Recovering runs that a restart killed, without touching live ones.

The pipeline pauses for humans and runs for minutes at a stretch, so a redeploy
mid-generate is routine rather than exceptional. Recovery exists so those runs
end up honestly labelled and resumable instead of sitting at "running" forever.

The dangerous direction is the other one. Reclaiming a run that is actually
alive marks a healthy run interrupted, frees its news item for a second run to
pick up, and produces a duplicate carousel. That is not hypothetical - it
happened during development when this reconcile was run from a second process
while a real run was in flight. Hence the idle guard, and hence these tests.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.runs import recovery
from app.services import db


class _FakeDb:
    """Records what recovery asked the database to do."""

    def __init__(self, candidates):
        self._candidates = candidates
        self.statuses: list[tuple[str, str]] = []
        self.events: list[tuple[str, int, str]] = []
        self.idle_arg = None

    async def interrupted_run_candidates(self, min_idle_seconds: int = 120):
        self.idle_arg = min_idle_seconds
        return list(self._candidates)

    async def set_run_status(self, run_id, status):
        self.statuses.append((run_id, status))

    async def max_run_seq(self, run_id):
        return 7


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def _reconcile(self, candidates):
        fake = _FakeDb(candidates)
        recorded: list[tuple] = []

        async def fake_record(run_id, seq, kind, author="", text="", data=None):
            recorded.append((run_id, seq, kind, text))

        with patch.object(recovery.db, "interrupted_run_candidates",
                          fake.interrupted_run_candidates), \
             patch.object(recovery.db, "set_run_status", fake.set_run_status), \
             patch.object(recovery.db, "max_run_seq", fake.max_run_seq), \
             patch.object(recovery, "record_event", fake_record):
            reclaimed = await recovery.reconcile_on_startup()
        return reclaimed, fake, recorded

    async def test_a_killed_run_is_marked_interrupted_and_says_so(self) -> None:
        reclaimed, fake, recorded = await self._reconcile(
            [{"run_id": "run-a", "phase": "generate"}]
        )
        self.assertEqual(reclaimed, ["run-a"])
        self.assertEqual(fake.statuses, [("run-a", db.RUN_STATUS_INTERRUPTED)])
        # The user is told what happened and that it can be resumed.
        self.assertEqual(recorded[0][1], 8, "the timeline must continue, not restart")
        self.assertIn("generate", recorded[0][3])
        self.assertIn("resumed", recorded[0][3])

    async def test_it_asks_the_database_to_skip_recently_active_runs(self) -> None:
        """The guard that stops a live run being reclaimed by mistake."""
        _reclaimed, fake, _recorded = await self._reconcile([])
        self.assertGreater(
            fake.idle_arg, 0,
            "reconcile must pass an idle threshold, or it can reclaim a live run",
        )

    async def test_nothing_to_do_writes_nothing(self) -> None:
        reclaimed, fake, recorded = await self._reconcile([])
        self.assertEqual(reclaimed, [])
        self.assertEqual(fake.statuses, [])
        self.assertEqual(recorded, [])

    async def test_one_bad_row_does_not_abandon_the_rest(self) -> None:
        calls = {"n": 0}

        async def flaky_status(run_id, status):
            calls["n"] += 1
            if run_id == "run-bad":
                raise ConnectionError("transient")

        async def ok(*_a, **_k):
            return 0

        async def rec(*_a, **_k):
            return None

        with patch.object(recovery.db, "interrupted_run_candidates",
                          lambda min_idle_seconds=120: _async([
                              {"run_id": "run-bad", "phase": "qa"},
                              {"run_id": "run-good", "phase": "generate"},
                          ])), \
             patch.object(recovery.db, "set_run_status", flaky_status), \
             patch.object(recovery.db, "max_run_seq", ok), \
             patch.object(recovery, "record_event", rec):
            reclaimed = await recovery.reconcile_on_startup()

        self.assertEqual(reclaimed, ["run-good"])

    async def test_an_unreachable_database_is_not_fatal_at_boot(self) -> None:
        """Failing to reconcile must not stop the service from starting."""

        async def boom(**_k):
            raise ConnectionError("db down")

        with patch.object(recovery.db, "interrupted_run_candidates", boom):
            self.assertEqual(await recovery.reconcile_on_startup(), [])


class HeartbeatInvariantTests(unittest.TestCase):
    """The guard is only meaningful if live runs actually heartbeat.

    This pairing broke twice in development, both times the same way: recovery
    inferred liveness from runs.updated_at, which only moves on a PHASE
    transition, while template_design spent fifteen minutes inside one phase
    rendering slides. A healthy run looked dead and was reclaimed, freeing its
    news item for a second run to pick up.

    The fix has two halves and they only work together, so the relationship
    between them is asserted rather than left as a comment.
    """

    def test_the_idle_threshold_is_well_above_the_heartbeat_interval(self) -> None:
        import inspect

        from app.runs.service import HEARTBEAT_INTERVAL_S

        default = inspect.signature(
            db.interrupted_run_candidates
        ).parameters["min_idle_seconds"].default

        self.assertGreaterEqual(
            default,
            HEARTBEAT_INTERVAL_S * 3,
            "recovery must tolerate several missed heartbeats before it "
            "declares a run dead, or a slow database will orphan live runs",
        )

    def test_both_run_paths_heartbeat(self) -> None:
        """The CLI drives runs too, and it is subject to the same recovery."""
        from pathlib import Path as _P

        for path in ("app/runs/service.py", "fetcher/fetch_news.py"):
            source = _P(path).read_text(encoding="utf-8")
            self.assertIn(
                "touch_run",
                source,
                f"{path} drives pipeline runs but never heartbeats, so startup "
                "recovery will reclaim its runs while they are still working",
            )


class ReleaseStuckQueueItemsTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_excludes_items_whose_run_is_still_live(self) -> None:
        """Freeing a live run's item would produce a duplicate carousel."""
        captured: dict = {}

        class Pool:
            async def fetch(self, query, *args):
                captured["query"] = query
                captured["args"] = args
                return []

        async def get_pool():
            return Pool()

        with patch.object(recovery.db, "get_pool", get_pool):
            freed = await recovery.release_stuck_queue_items()

        self.assertEqual(freed, 0)
        self.assertIn("NOT EXISTS", captured["query"])
        live = captured["args"][2]
        self.assertIn(db.RUN_STATUS_RUNNING, live)
        self.assertIn(db.RUN_STATUS_AWAITING_REVIEW, live)

    async def test_a_database_error_is_reported_as_zero_not_raised(self) -> None:
        async def boom():
            raise ConnectionError("db down")

        with patch.object(recovery.db, "get_pool", boom):
            self.assertEqual(await recovery.release_stuck_queue_items(), 0)


def _async(value):
    async def coro():
        return value
    return coro()


if __name__ == "__main__":
    unittest.main()
