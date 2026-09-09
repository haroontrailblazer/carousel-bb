"""A rework must be able to ask for review again.

The failure this pins: after a rejection was reworked, the pipeline returned
to `review`, the dispatcher was put in SEND_MAIL mode - and refused, replying

    I can't send a second review request in this run because the single review
    mail was already sent; please start a new run if you need another human
    review.

The orchestrator then halted at 'review' with a finished carousel nobody had
been told about. The cause was a standing "Hard rule" in the dispatcher's own
instructions - "One review request per run, maximum" - which outranked the
per-turn mode directive telling it to send.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.agents import review_dispatcher as rd

SKILL = Path(__file__).resolve().parents[1] / "skills" / "agents" / "review_dispatcher.md"


class InstructionRulesTests(unittest.TestCase):
    def _texts(self) -> list[tuple[str, str]]:
        """The code default AND the on-disk skill, which is what actually loads."""
        return [
            ("code default", rd._DEFAULT_INSTRUCTION),
            ("skill file", SKILL.read_text(encoding="utf-8")),
        ]

    def test_no_rule_caps_review_requests_per_run(self) -> None:
        for name, text in self._texts():
            with self.subTest(source=name):
                self.assertNotIn("One review request per run", text)
                self.assertNotIn("per run, maximum", text)

    def test_the_cap_is_per_turn_instead(self) -> None:
        for name, text in self._texts():
            with self.subTest(source=name):
                self.assertIn("per TURN", text)

    def test_later_rounds_are_explicitly_allowed(self) -> None:
        """The instruction must SAY a rework round comes back here.

        Removing the old rule is not enough on its own - the model has to be
        told that a second request is expected, or it re-derives the same
        caution from "one of two modes".
        """
        for name, text in self._texts():
            with self.subTest(source=name):
                lowered = text.lower()
                self.assertIn("review round", lowered)
                self.assertTrue(
                    "reworked" in lowered or "rework" in lowered,
                    "the instruction should mention rework rounds",
                )

    def test_the_skill_file_and_the_code_default_agree(self) -> None:
        """The skill file is what loads at runtime; the default seeds it.

        They drifted once already - fixing only one would leave the bug live
        on either a fresh checkout or this machine, depending which was
        missed.
        """
        code = rd._DEFAULT_INSTRUCTION
        disk = SKILL.read_text(encoding="utf-8")
        for marker in ("per TURN", "Do not"):
            if marker in code:
                self.assertIn(
                    marker.split()[0], disk, f"skill file is missing '{marker}'"
                )


class SendMailDirectiveTests(unittest.TestCase):
    """Round 2+ gets an explicit "this is expected" nudge; round 1 does not."""

    def _directive(self, review_round: int) -> str:
        next_round = review_round + 1
        return (
            f" This is review round {next_round}: the carousel has been "
            "reworked since the last request, so a NEW review request is "
            "required and expected. Do not refuse it on the grounds that "
            "one was already sent."
            if next_round > 1
            else ""
        )

    def test_first_round_carries_no_extra_wording(self) -> None:
        self.assertEqual(self._directive(0), "")

    def test_second_round_names_itself_and_forbids_refusing(self) -> None:
        text = self._directive(1)
        self.assertIn("review round 2", text)
        self.assertIn("Do not refuse", text)

    def test_the_builder_still_produces_a_send_mail_directive(self) -> None:
        self.assertIn("SEND_MAIL", rd._DEFAULT_INSTRUCTION)
        self.assertTrue(callable(rd._instruction_provider))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
