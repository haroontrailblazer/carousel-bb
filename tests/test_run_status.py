"""The phase a run stops in does not always imply that it succeeded."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Optional

from app.services import db


class _FakePool:
    """Captures the parameters of the UPDATE without touching Postgres."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, _sql: str, *args: Any) -> None:
        self.calls.append(args)


class UpdateRunPhaseStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = _FakePool()
        self._saved = db.get_pool

        async def _pool() -> _FakePool:
            return self.pool

        db.get_pool = _pool  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        db.get_pool = self._saved  # type: ignore[assignment]

    def _status(self, index: int = 0) -> Optional[str]:
        # (run_id, phase, status[, review_round])
        return self.pool.calls[index][2]

    async def test_phase_still_derives_the_status_by_default(self) -> None:
        await db.update_run_phase("run-a", "review")
        self.assertEqual(self._status(), db.RUN_STATUS_AWAITING_REVIEW)

        self.pool.calls.clear()
        await db.update_run_phase("run-a", "done")
        self.assertEqual(self._status(), db.RUN_STATUS_DONE)

        self.pool.calls.clear()
        await db.update_run_phase("run-a", "generate")
        self.assertEqual(self._status(), db.RUN_STATUS_RUNNING)

    async def test_explicit_status_overrides_the_phase(self) -> None:
        """The rework hard stop: DONE phase, FAILED run.

        DONE is what terminates the orchestrator loop, so the hard stop has to
        use it - but a run that exhausted its rework rounds without producing
        a carousel is not finished, it gave up. Deriving the status from the
        phase reported it as published and offered no Re-run.
        """
        await db.update_run_phase("run-a", "done", status=db.RUN_STATUS_FAILED)
        self.assertEqual(self._status(), db.RUN_STATUS_FAILED)

    async def test_override_survives_the_review_round_variant(self) -> None:
        """Both SQL branches must honour it, not just the short one."""
        await db.update_run_phase(
            "run-a", "done", review_round=2, status=db.RUN_STATUS_FAILED
        )
        self.assertEqual(self._status(), db.RUN_STATUS_FAILED)
        self.assertEqual(self.pool.calls[0][3], 2)

    async def test_hard_stop_reports_the_phase_it_died_in_not_done(self) -> None:
        """The console renders runs.phase, so it must not claim Publishing.

        A run that exhausts its rework rounds never reached publish and never
        reached review. Recording the DONE phase lit every step of the phase
        rail and labelled the task "Done" in the list.
        """
        await db.update_run_phase(
            "run-a", "rework", review_round=0, status=db.RUN_STATUS_FAILED
        )
        run_id, phase, status, review_round = self.pool.calls[0]
        self.assertEqual(phase, "rework")
        self.assertEqual(status, db.RUN_STATUS_FAILED)
        self.assertNotEqual(phase, "done")

    async def test_a_failed_status_is_one_the_ui_offers_a_rerun_for(self) -> None:
        """Guards the contract the task list's action buttons rely on."""
        self.assertIn(
            db.RUN_STATUS_FAILED,
            {db.RUN_STATUS_FAILED, db.RUN_STATUS_CANCELLED},
        )
        self.assertNotEqual(db.RUN_STATUS_FAILED, db.RUN_STATUS_DONE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class StoppedIsDecidedInOnePlaceTests(unittest.TestCase):
    """"Did this task stop?" is one function, not one copy per screen.

    ``failed || cancelled || interrupted`` was written out inline in three
    different components. Nothing was broken by that on the day - the three
    copies agreed - but the failure mode is the one duplication always has:
    a fourth stopped status, or a rename, lands in one copy and the other two
    keep answering the old question, silently and only on the screens nobody
    reopened.

    So this test is not about style. It is the drift alarm: the set is defined
    once, in ``isStopped``, and the console asks it rather than re-deriving it.

    A note on the near-miss it must NOT accept: ``!isLive`` is not the same
    question. A task awaiting review is not live either, and reading one for
    the other is exactly how "Nothing to approve yet" ended up on a carousel
    that was ready to approve.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from pathlib import Path

        cls.src = Path(__file__).resolve().parent.parent / "frontend" / "src"

    def test_the_set_is_defined_once_and_names_all_three(self) -> None:
        pipeline = (self.src / "lib" / "pipeline.ts").read_text(encoding="utf-8")
        self.assertIn(
            "export function isStopped(", pipeline,
            "The one definition of 'this task stopped' is gone. Every screen "
            "that asks the question is now asking a function that no longer "
            "exists, or has quietly grown its own copy again.",
        )
        body = pipeline[pipeline.index("export function isStopped(") :]
        body = body[: body.index("\n}")]
        for status in ("failed", "cancelled", "interrupted"):
            self.assertIn(
                f'"{status}"', body,
                f"isStopped no longer counts `{status}` as stopped. If that is "
                "deliberate, every caller wants re-reading: the review card "
                "withholds Approve on the strength of this answer.",
            )

    def test_no_screen_re_derives_it_inline(self) -> None:
        import re

        STOPPED = {"failed", "cancelled", "interrupted"}
        ALL = "running|awaiting_review|done|failed|cancelled|interrupted"
        # Two shapes, because those are the two ways it was actually written:
        # a chain of `===` comparisons, and a literal array with `.includes`.
        chain = re.compile(rf'(?:[\w.]+\s*===\s*"(?:{ALL})"\s*(?:\|\|\s*)?)+')
        array = re.compile(rf'\[(?:\s*"(?:{ALL})"\s*,?\s*)+\]\s*\.includes')
        quoted = re.compile(rf'"({ALL})"')

        offenders: list[str] = []
        for path in sorted(self.src.rglob("*.ts*")):
            # The definition itself, and the union type it is derived from.
            if path.name in ("pipeline.ts", "types.ts"):
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in (chain, array):
                for match in pattern.finditer(text):
                    # EXACTLY the stopped set, and nothing else. A deliberate
                    # subset is a different question, not a stale copy - the
                    # task actions ask two of them (`interrupted | cancelled |
                    # awaiting_review` is "carry on from here"; `failed |
                    # cancelled` is "give the failing part another go"), and
                    # neither wants folding into isStopped.
                    if set(quoted.findall(match.group(0))) == STOPPED:
                        offenders.append(
                            f"{path.relative_to(self.src)}: "
                            f"{' '.join(match.group(0).split())}"
                        )

        self.assertEqual(
            [], offenders,
            "These re-derive the stopped set instead of calling isStopped() "
            "from @/lib/pipeline. Import it - a copy here is a copy that will "
            "not be updated when the set changes:\n  " + "\n  ".join(offenders),
        )
