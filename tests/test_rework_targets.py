"""Pointing at an agent must actually reach that agent.

The console lets a reviewer name who should fix something - pick the CTA from
the composer, or click the cover on the review screen. The value of pointing
is that it is EXACT: if the router's guess can still overrule the person who
pointed, the feature is indistinguishable from not having it.

These tests pin the two halves of that promise:

* the named targets survive every hop between the browser and the pipeline
  (verdict payload -> function response -> Verdict -> rework plan), and
* the sanitizer treats them as final rather than merging them with whatever
  the LLM guessed - a plan that quietly added ``planner`` alongside the CTA
  would re-run the whole carousel to fix one slide.

They also pin the OTHER direction: a verdict that names nobody must route
exactly as it always did, because that is every Telegram verdict.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.agents.feedback_router import _sanitize_rework_plan
from app.agents.review_dispatcher import _verdict_from_payload
from app.review.resume import build_resume_content
from app.schemas import ReworkPlan, ReworkReason, Verdict
from app.state import (
    AGENT_CTA,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_PLANNER,
    K_REWORK_PLAN,
    K_VERDICT,
    REWORKABLE_AGENTS,
)


def _sanitize(verdict: Verdict, plan: ReworkPlan) -> ReworkPlan:
    """Run the after-agent callback over a state dict and read the plan back."""
    state = {
        K_VERDICT: verdict.model_dump(mode="json"),
        K_REWORK_PLAN: plan.model_dump(mode="json"),
    }
    context = MagicMock()
    context.state = state
    _sanitize_rework_plan(context)
    return ReworkPlan.model_validate(state[K_REWORK_PLAN])


class TargetsSurviveTheWire(unittest.TestCase):
    """Browser -> function response -> Verdict, without losing the choice."""

    def test_named_targets_ride_on_the_function_response(self) -> None:
        content = build_resume_content("call-1", "rejected", "shorter", ["cta"])
        payload = content.parts[0].function_response.response
        self.assertEqual(payload["targets"], ["cta"])

    def test_no_targets_means_no_key_at_all(self) -> None:
        """A Telegram verdict must look exactly as it always did."""
        content = build_resume_content("call-1", "rejected", "shorter")
        payload = content.parts[0].function_response.response
        self.assertNotIn("targets", payload)

    def test_the_dispatcher_lifts_them_into_the_verdict(self) -> None:
        verdict = _verdict_from_payload(
            {"status": "rejected", "feedback": "shorter", "targets": ["cta"]}
        )
        self.assertEqual(verdict.targets, ["cta"])

    def test_a_payload_without_targets_yields_an_empty_list(self) -> None:
        verdict = _verdict_from_payload({"status": "rejected", "feedback": "x"})
        self.assertEqual(verdict.targets, [])

    def test_a_malformed_targets_field_is_ignored_not_fatal(self) -> None:
        verdict = _verdict_from_payload(
            {"status": "rejected", "feedback": "x", "targets": "cta"}
        )
        self.assertEqual(verdict.targets, [])


class TheHumanOutranksTheRouter(unittest.TestCase):
    """The sanitizer is the deterministic authority; pointing wins there."""

    def test_a_named_target_replaces_the_models_guess(self) -> None:
        plan = _sanitize(
            Verdict(status="rejected", feedback="shorter", targets=["cta"]),
            # The router guessed something else entirely.
            ReworkPlan(
                targets=[AGENT_PLANNER],
                reasons=[ReworkReason(target=AGENT_PLANNER, reason="replan")],
                feedback="shorter",
            ),
        )
        self.assertEqual(plan.targets, [AGENT_CTA])

    def test_it_replaces_rather_than_merges(self) -> None:
        """Adding planner beside the CTA would re-run the whole carousel."""
        plan = _sanitize(
            Verdict(status="rejected", feedback="shorter", targets=["cta"]),
            ReworkPlan(
                targets=[AGENT_PLANNER, AGENT_CTA],
                reasons=[ReworkReason(target=AGENT_PLANNER, reason="replan")],
                feedback="shorter",
            ),
        )
        self.assertEqual(plan.targets, [AGENT_CTA])

    def test_aliases_are_accepted_so_the_ui_can_speak_plainly(self) -> None:
        """"cover" is what a person clicks; first_page_visual is who fixes it."""
        plan = _sanitize(
            Verdict(status="rejected", feedback="bolder", targets=["cover"]),
            ReworkPlan(targets=[], reasons=[], feedback="bolder"),
        )
        self.assertEqual(plan.targets, [AGENT_FIRST_PAGE_VISUAL])

    def test_every_reworkable_agent_can_be_named(self) -> None:
        for agent in REWORKABLE_AGENTS:
            with self.subTest(agent=agent):
                plan = _sanitize(
                    Verdict(status="rejected", feedback="fix it", targets=[agent]),
                    ReworkPlan(targets=[AGENT_PLANNER], reasons=[], feedback="fix it"),
                )
                self.assertEqual(plan.targets, [agent])

    def test_an_unknown_target_falls_through_instead_of_emptying_the_plan(self) -> None:
        """A bad name must never leave the rework with nothing to run."""
        plan = _sanitize(
            Verdict(status="rejected", feedback="the cta is too long", targets=["nope"]),
            ReworkPlan(targets=[], reasons=[], feedback="the cta is too long"),
        )
        self.assertTrue(plan.targets)
        for target in plan.targets:
            self.assertIn(target, REWORKABLE_AGENTS)

    def test_naming_nobody_leaves_the_router_in_charge(self) -> None:
        plan = _sanitize(
            Verdict(status="rejected", feedback="shorter"),
            ReworkPlan(
                targets=[AGENT_CTA],
                reasons=[ReworkReason(target=AGENT_CTA, reason="too long")],
                feedback="shorter",
            ),
        )
        self.assertEqual(plan.targets, [AGENT_CTA])

    def test_one_reason_per_target_is_still_guaranteed(self) -> None:
        plan = _sanitize(
            Verdict(status="rejected", feedback="bolder", targets=["cover", "cta"]),
            ReworkPlan(targets=[], reasons=[], feedback="bolder"),
        )
        self.assertEqual(len(plan.reasons), len(plan.targets))
        self.assertEqual(
            [reason.target for reason in plan.reasons], plan.targets
        )

    def test_the_reviewers_words_are_still_carried_verbatim(self) -> None:
        plan = _sanitize(
            Verdict(
                status="rejected",
                feedback="the price is $20/M not $200/M",
                targets=["research"],
            ),
            ReworkPlan(targets=[], reasons=[], feedback="pricing figure incorrect"),
        )
        self.assertEqual(plan.feedback, "the price is $20/M not $200/M")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
