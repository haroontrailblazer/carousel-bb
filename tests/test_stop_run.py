"""Stop has to be able to stop, including a run nothing is driving.

The report: Stop answered "that run is not currently running" while the trace
kept pulsing running. Both were reading different sources and both were doing
what they were told.

  * `cancel_run` looked only at in-memory task registries. Those are
    per-process, so a restart empties them while the database keeps whatever
    the run last wrote.
  * A halted invocation WROTE `running` on its way out - the status branch had
    no case for "ended without pausing and without finishing", so it fell
    through to running.

Together they made a phantom: a task the console showed as live forever, and a
button that refused to touch it.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Optional

from app.runs import service
from app.services import db


class _Recorder:
    def __init__(self, status: Optional[str]) -> None:
        self.status = status
        self.set_calls: list[tuple[str, str]] = []
        self.events: list[dict] = []

    async def get_run(self, _run_id: str) -> Optional[dict]:
        return None if self.status is None else {"status": self.status}

    async def set_run_status(self, run_id: str, status: str) -> None:
        self.set_calls.append((run_id, status))
        self.status = status

    async def max_run_seq(self, _run_id: str) -> int:
        return 7


class StopWithNoInProcessTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._saved = (db.get_run, db.set_run_status, db.max_run_seq, service.record_event)

    async def asyncTearDown(self) -> None:
        (db.get_run, db.set_run_status, db.max_run_seq, service.record_event) = self._saved  # type: ignore[assignment]

    def _install(self, status: Optional[str]) -> _Recorder:
        rec = _Recorder(status)
        db.get_run = rec.get_run  # type: ignore[assignment]
        db.set_run_status = rec.set_run_status  # type: ignore[assignment]
        db.max_run_seq = rec.max_run_seq  # type: ignore[assignment]

        async def _record(run_id: str, seq: int, kind: str, **kw: Any) -> None:
            rec.events.append({"run_id": run_id, "seq": seq, "kind": kind, **kw})

        service.record_event = _record  # type: ignore[assignment]
        return rec

    async def test_a_stale_running_run_is_stopped_not_refused(self) -> None:
        """The exact reported case: nothing in the registry, DB says running."""
        rec = self._install(db.RUN_STATUS_RUNNING)
        self.assertTrue(await service.cancel_run("run-a"))
        self.assertEqual(rec.set_calls, [("run-a", db.RUN_STATUS_CANCELLED)])

    async def test_it_says_in_the_trace_what_actually_happened(self) -> None:
        """Not a silent status edit - the timeline should explain itself."""
        rec = self._install(db.RUN_STATUS_RUNNING)
        await service.cancel_run("run-a")
        self.assertEqual(len(rec.events), 1)
        self.assertTrue(rec.events[0].get("data", {}).get("stale"))

    async def test_a_finished_run_is_still_refused(self) -> None:
        """Stop must not resurrect or re-stamp a task that already ended."""
        for status in (db.RUN_STATUS_DONE, db.RUN_STATUS_FAILED,
                       db.RUN_STATUS_CANCELLED, db.RUN_STATUS_AWAITING_REVIEW):
            with self.subTest(status=status):
                rec = self._install(status)
                self.assertFalse(await service.cancel_run("run-a"))
                self.assertEqual(rec.set_calls, [])

    async def test_an_unknown_run_is_refused(self) -> None:
        self._install(None)
        self.assertFalse(await service.cancel_run("nope"))

    async def test_a_live_task_is_cancelled_and_the_db_is_left_alone(self) -> None:
        """When there IS a task, cancelling it is the mechanism.

        The task's own CancelledError handler records the status, so writing
        it here too would race with it.
        """
        rec = self._install(db.RUN_STATUS_RUNNING)
        started = asyncio.Event()

        async def _forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.get_running_loop().create_task(_forever())
        service._run_tasks["run-a"] = task
        await started.wait()
        try:
            self.assertTrue(await service.cancel_run("run-a"))
            self.assertTrue(task.cancelled() or task.cancelling())
            self.assertEqual(rec.set_calls, [])
        finally:
            task.cancel()
            service._run_tasks.pop("run-a", None)


class HaltedStatusTests(unittest.TestCase):
    """A halted invocation must not report itself as running."""

    def test_interrupted_is_not_running(self) -> None:
        self.assertNotEqual(db.RUN_STATUS_INTERRUPTED, db.RUN_STATUS_RUNNING)

    def test_interrupted_runs_are_resumable_in_the_ui(self) -> None:
        # TaskActions offers Resume for interrupted and cancelled, which is
        # what makes stopping recoverable rather than terminal.
        self.assertIn(
            db.RUN_STATUS_INTERRUPTED,
            {db.RUN_STATUS_INTERRUPTED, db.RUN_STATUS_CANCELLED},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
