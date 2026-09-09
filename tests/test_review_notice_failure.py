"""A carousel that is ready must be decidable even if nobody was notified.

The stall this pins: the rework finished, QA passed, the bundle was assembled
- and then `send_review_request` failed. The orchestrator halted at 'review',
the run stayed recorded as `running` forever, and the console showed a live
task with a finished carousel it could not act on.

Two things were wrong, and only one of them was cosmetic:

  * the STATUS. Nothing was running. "awaiting_review" is what was true: the
    work is done and a human is needed.
  * the VERDICT path. It required a pending_reviews row to claim, and there
    was none - the dispatcher never reached `await_human_review`. Any row
    still lying about belonged to an earlier round whose function call had
    already been answered, so claiming it would have resumed against a dead
    call id.
"""

from __future__ import annotations

import unittest
from typing import Any, Optional

from app.services import db
from app.state import K_PHASE, K_REVIEW_NOTICE_FAILED, PHASE_REVIEW


class _Row(dict):
    def __getitem__(self, key: str) -> Any:  # asyncpg rows index by name
        return super().__getitem__(key)


class _FakePool:
    def __init__(self, state: Optional[dict] = None) -> None:
        self.state = state
        self.updates: list[tuple[str, tuple]] = []

    async def fetchrow(self, _sql: str, *_args: Any) -> Optional[_Row]:
        return None if self.state is None else _Row(state=self.state)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.updates.append((sql, args))
        return "run-a"


class HaltedDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._saved = db.get_pool

    async def asyncTearDown(self) -> None:
        db.get_pool = self._saved  # type: ignore[assignment]

    def _use(self, state: Optional[dict]) -> _FakePool:
        pool = _FakePool(state)

        async def _pool() -> _FakePool:
            return pool

        db.get_pool = _pool  # type: ignore[assignment]
        return pool

    async def test_flag_plus_review_phase_means_halted(self) -> None:
        from app.review.verdict import _halted_awaiting_review

        self._use({K_REVIEW_NOTICE_FAILED: True, K_PHASE: PHASE_REVIEW})
        self.assertTrue(await _halted_awaiting_review("run-a"))

    async def test_the_flag_alone_is_not_enough(self) -> None:
        """A stale flag from an earlier round must not hijack a live pause."""
        from app.review.verdict import _halted_awaiting_review

        self._use({K_REVIEW_NOTICE_FAILED: True, K_PHASE: "rework"})
        self.assertFalse(await _halted_awaiting_review("run-a"))

    async def test_a_normal_review_pause_is_not_halted(self) -> None:
        from app.review.verdict import _halted_awaiting_review

        self._use({K_PHASE: PHASE_REVIEW})
        self.assertFalse(await _halted_awaiting_review("run-a"))

    async def test_a_missing_session_is_not_halted(self) -> None:
        from app.review.verdict import _halted_awaiting_review

        self._use(None)
        self.assertFalse(await _halted_awaiting_review("run-a"))


class SessionVerdictWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = _FakePool({})
        self._saved = db.get_pool

        async def _pool() -> _FakePool:
            return self.pool

        db.get_pool = _pool  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        db.get_pool = self._saved  # type: ignore[assignment]

    async def test_it_writes_the_key_the_orchestrator_reads(self) -> None:
        """Must be `review_verdict` - the orchestrator routes on that key."""
        from app.state import K_VERDICT

        ok = await db.set_session_verdict(
            "run-a", "app", "pipeline", {"status": "approved", "feedback": ""}
        )
        self.assertTrue(ok)
        sql, args = self.pool.updates[0]
        self.assertIn("{" + K_VERDICT + "}", sql)
        self.assertIn("jsonb_set", sql)  # never a wholesale overwrite
        self.assertIn("approved", args[3])


class StatusHonestyTests(unittest.TestCase):
    def test_awaiting_review_is_what_a_ready_carousel_reports(self) -> None:
        """Not running (nothing is) and not failed (the work is done)."""
        self.assertEqual(db.RUN_STATUS_AWAITING_REVIEW, "awaiting_review")
        self.assertNotEqual(db.RUN_STATUS_AWAITING_REVIEW, db.RUN_STATUS_RUNNING)
        self.assertNotEqual(db.RUN_STATUS_AWAITING_REVIEW, db.RUN_STATUS_FAILED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
