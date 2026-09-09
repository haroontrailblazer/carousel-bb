"""The single-winner guarantee on a human verdict.

With both Telegram and the web console able to decide the same run, "exactly
one decision wins" stops being theoretical. A losing caller that still resumes
the pipeline publishes the carousel to Instagram twice, so this is the property
worth pinning down in a test rather than in a comment.

The atomicity itself belongs to Postgres (a single DELETE ... RETURNING
serialises two concurrent statements). What these tests own is the half that
can go wrong in Python: that the decision path *asks* for the row atomically
instead of doing a check-then-act, and that the caller which loses the race
resumes nothing.

These target app.review.verdict rather than an HTTP handler on purpose - that
module is the single implementation both surfaces share, so testing it covers
the web console's verdict endpoint as well as the Telegram pages.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.review import verdict as verdict_mod
from app.services import db

RUN_ID = "run-abc123def456"
SESSION_ID = RUN_ID
CALL_ID = "call-xyz789"


class _SerialisingPool:
    """A stand-in pool that models how Postgres serialises the claiming DELETE.

    fetchrow hands the pending row to the FIRST caller of the
    DELETE ... RETURNING and None to every later one, which is what the real
    statement does under concurrency. The await inside is load-bearing: it
    forces a scheduler switch, so two gathered callers genuinely interleave
    rather than each running to completion in turn.
    """

    def __init__(self) -> None:
        self.rows_remaining = 1
        self.delete_calls = 0
        self.inserted_verdicts: list[dict] = []

    async def fetchrow(self, query: str, *args):
        if "DELETE FROM pending_reviews" in query:
            self.delete_calls += 1
            await asyncio.sleep(0)  # yield: let the racing caller run
            if self.rows_remaining <= 0:
                return None
            self.rows_remaining -= 1
            return {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "function_call_id": CALL_ID,
            }
        if "INSERT INTO feedback" in query:
            self.inserted_verdicts.append(
                {"run_id": args[0], "verdict": args[1], "source": args[6]}
            )
            return {"id": len(self.inserted_verdicts)}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query: str, *args) -> None:
        raise AssertionError(
            "the decision path must not issue a bare DELETE - that is the "
            f"check-then-act race this test exists to prevent: {query}"
        )


class ClaimPendingReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_uses_delete_returning_not_select_then_delete(self) -> None:
        pool = _SerialisingPool()
        with patch.object(db, "get_pool", return_value=pool):
            claimed = await db.claim_pending_review(RUN_ID)

        self.assertEqual(pool.delete_calls, 1)
        self.assertEqual(claimed["session_id"], SESSION_ID)
        self.assertEqual(claimed["function_call_id"], CALL_ID)

    async def test_second_claim_gets_nothing(self) -> None:
        pool = _SerialisingPool()
        with patch.object(db, "get_pool", return_value=pool):
            first = await db.claim_pending_review(RUN_ID)
            second = await db.claim_pending_review(RUN_ID)

        self.assertIsNotNone(first)
        self.assertIsNone(second)


class ConcurrentVerdictTests(unittest.IsolatedAsyncioTestCase):
    """Two reviewers, one run, the same instant."""

    async def _decide_twice(self, pool):
        spawned: list[tuple] = []
        with patch.object(db, "get_pool", return_value=pool), patch.object(
            verdict_mod, "spawn_resume", lambda *a: spawned.append(a)
        ):
            outcomes = await asyncio.gather(
                verdict_mod.submit_verdict(RUN_ID, "approved", ""),
                verdict_mod.submit_verdict(RUN_ID, "approved", ""),
            )
        return outcomes, spawned

    async def test_exactly_one_of_two_concurrent_approvals_wins(self) -> None:
        pool = _SerialisingPool()
        outcomes, spawned = await self._decide_twice(pool)

        self.assertEqual(
            sorted(o.result for o in outcomes), ["accepted", "not_pending"]
        )
        # The part that actually costs money if it regresses.
        self.assertEqual(
            len(spawned), 1, "a losing caller must not resume the pipeline"
        )
        self.assertEqual(len(pool.inserted_verdicts), 1)

    async def test_telegram_and_web_racing_still_yields_one_winner(self) -> None:
        """The real-world shape: one run decided from both surfaces at once."""
        pool = _SerialisingPool()
        spawned: list[tuple] = []
        with patch.object(db, "get_pool", return_value=pool), patch.object(
            verdict_mod, "spawn_resume", lambda *a: spawned.append(a)
        ):
            outcomes = await asyncio.gather(
                verdict_mod.submit_verdict(
                    RUN_ID, "approved", "", source="telegram"
                ),
                verdict_mod.submit_verdict(
                    RUN_ID, "approved", "", source="web", reviewer="a@b.co"
                ),
            )

        self.assertEqual(
            sorted(o.result for o in outcomes), ["accepted", "not_pending"]
        )
        self.assertEqual(len(spawned), 1)
        # Whoever won, the verdict row records which surface decided.
        self.assertIn(pool.inserted_verdicts[0]["source"], ("telegram", "web"))

    async def test_the_loser_is_not_an_error(self) -> None:
        """Losing the race is an ordinary outcome, not a failure to report."""
        pool = _SerialisingPool()
        outcomes, _ = await self._decide_twice(pool)
        loser = next(o for o in outcomes if not o.ok)
        self.assertEqual(loser.result, "not_pending")
        self.assertEqual(loser.detail, "")


class ValidationDoesNotConsumeTests(unittest.IsolatedAsyncioTestCase):
    """Validation runs before the claim, so a bad form keeps the run alive."""

    async def test_reject_without_feedback_leaves_the_review_pending(self) -> None:
        pool = _SerialisingPool()
        with patch.object(db, "get_pool", return_value=pool):
            outcome = await verdict_mod.submit_verdict(RUN_ID, "rejected", "   ")

        self.assertEqual(outcome.result, "feedback_required")
        self.assertEqual(
            pool.delete_calls,
            0,
            "an invalid submission must not consume the pending review - the "
            "reviewer has to be able to try again",
        )

    async def test_unknown_verdict_is_refused_before_claiming(self) -> None:
        pool = _SerialisingPool()
        with patch.object(db, "get_pool", return_value=pool):
            outcome = await verdict_mod.submit_verdict(RUN_ID, "maybe", "")

        self.assertEqual(outcome.result, "invalid_status")
        self.assertEqual(pool.delete_calls, 0)

    async def test_approve_tolerates_messy_casing_and_whitespace(self) -> None:
        pool = _SerialisingPool()
        with patch.object(db, "get_pool", return_value=pool), patch.object(
            verdict_mod, "spawn_resume", lambda *a: None
        ):
            outcome = await verdict_mod.submit_verdict(RUN_ID, "  Approved ", "")

        self.assertEqual(outcome.result, "accepted")
        self.assertEqual(outcome.status, "approved")


class DatabaseFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_database_is_reported_not_raised(self) -> None:
        """A dead database must not blow up out of the handler mid-decision."""

        class DeadPool:
            async def fetchrow(self, *a):
                raise ConnectionError("no route to host")

        with patch.object(db, "get_pool", return_value=DeadPool()):
            outcome = await verdict_mod.submit_verdict(RUN_ID, "approved", "")

        self.assertEqual(outcome.result, "db_error")
        self.assertIn("DATABASE_URL", outcome.detail)


if __name__ == "__main__":
    unittest.main()
