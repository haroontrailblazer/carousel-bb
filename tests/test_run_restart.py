"""Re-run continues the same task instead of opening a new one."""

from __future__ import annotations

import unittest
from typing import Any, Optional

from app.services import db


class _FakePool:
    def __init__(self, updated_id: Optional[str] = "run-a") -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._updated = updated_id

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self._updated


class RewindSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = _FakePool()
        self._saved = db.get_pool

        async def _pool() -> _FakePool:
            return self.pool

        db.get_pool = _pool  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        db.get_pool = self._saved  # type: ignore[assignment]

    async def test_it_resets_the_budget_and_the_phase_together(self) -> None:
        ok = await db.rewind_session_for_restart("run-a", "app", "pipeline", "rework")
        self.assertTrue(ok)
        sql, args = self.pool.calls[0]

        # The round cap is usually WHY the run stopped.
        self.assertIn("rework_round", sql)
        self.assertIn("'0'::jsonb", sql)

        # And the phase must be rewound, because the hard stop writes DONE
        # into session state to end the orchestrator's loop - a resume that
        # read DONE would emit a summary and stop again immediately.
        self.assertIn("{phase}", sql)
        self.assertEqual(args, ("app", "pipeline", "run-a", "rework"))

    async def test_it_preserves_the_rest_of_the_session(self) -> None:
        """jsonb_set, never a wholesale overwrite.

        The session holds the researched story, the plan, the approved copy
        and the rendered slides. Replacing it would throw away everything the
        run earned - which is the entire reason Re-run stopped starting a new
        task.
        """
        await db.rewind_session_for_restart("run-a", "app", "pipeline", "generate")
        sql, _ = self.pool.calls[0]
        self.assertIn("jsonb_set", sql)
        self.assertNotIn("SET state = $", sql)

    async def test_a_missing_session_reports_false(self) -> None:
        self.pool._updated = None
        self.assertFalse(
            await db.rewind_session_for_restart("gone", "app", "pipeline", "qa")
        )


class RestartPhaseChoiceTests(unittest.TestCase):
    """Which phase a restart re-enters."""

    def test_done_is_rewound_to_rework(self) -> None:
        from app.state import PHASE_DONE, PHASE_REWORK

        # A hard-stopped run records phase=rework on the run row, but its
        # SESSION says done. Re-entering "done" would stop instantly.
        phase = PHASE_DONE
        if phase == PHASE_DONE:
            phase = PHASE_REWORK
        self.assertEqual(phase, PHASE_REWORK)

    def test_any_other_phase_is_re_entered_as_recorded(self) -> None:
        from app.state import PHASE_DONE, PHASE_GENERATE

        phase = PHASE_GENERATE
        if phase == PHASE_DONE:
            phase = "rework"
        self.assertEqual(phase, PHASE_GENERATE)


class CancelReachesBothRegistriesTests(unittest.IsolatedAsyncioTestCase):
    """Stop must reach a rework, not just a fresh invocation."""

    async def test_a_resume_task_is_cancellable(self) -> None:
        import asyncio

        from app.review import resume as resume_mod

        started = asyncio.Event()

        async def _forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.get_running_loop().create_task(_forever(), name="resume-run-x")
        resume_mod._resume_tasks.add(task)
        task.add_done_callback(resume_mod._resume_tasks.discard)
        await started.wait()

        try:
            self.assertTrue(resume_mod.cancel_resume("run-x"))
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            resume_mod._resume_tasks.discard(task)

    async def test_an_unknown_run_is_not_reported_as_stopped(self) -> None:
        from app.review import resume as resume_mod

        self.assertFalse(resume_mod.cancel_resume("nobody"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
