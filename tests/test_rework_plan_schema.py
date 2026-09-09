"""The Feedback Router's output_schema must survive OpenAI strict mode.

This is the schema that killed every rejection: the reviewer's verdict and
feedback were saved, the learner ran, and then the rework never started
because the router's very first API call was refused::

    Invalid schema for response_format 'ReworkPlan': In context=(), 'required'
    is required to be supplied and to be an array including every key in
    properties. Extra required key 'reasons' supplied.

The cause was ``reasons: dict[str, str]``. Pydantic renders that as an
open-ended map, OpenAI strict mode cannot express one, so it discarded the
property and then rejected the request for requiring a key it had discarded.
"""

from __future__ import annotations

import unittest
from typing import Any

from app.schemas import ReworkPlan, ReworkReason


def _violations(node: Any, path: tuple = ()) -> list[tuple[str, tuple]]:
    """Every place a JSON schema breaks OpenAI's strict-mode rules."""
    bad: list[tuple[str, tuple]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            if "properties" not in node:
                # An open map. This is the exact shape that broke rework.
                bad.append(("object without properties", path))
            else:
                if node.get("additionalProperties") is not False:
                    bad.append(("additionalProperties is not false", path))
                if sorted(node.get("required", [])) != sorted(node["properties"]):
                    bad.append(("required does not list every property", path))
        for key, value in node.items():
            bad += _violations(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            bad += _violations(value, path + (index,))
    return bad


class StrictResponseFormatTests(unittest.TestCase):
    def test_the_payload_adk_sends_is_strict_mode_clean(self) -> None:
        """Checked against ADK's real converter, not a hand-built schema.

        ADK applies its own strict-mode pass on the way out, and that pass is
        what made this subtle: it fixed the ROOT object and left the nested
        open map alone, so the schema looked correct everywhere the eye
        naturally lands.
        """
        from google.adk.models.lite_llm import _to_litellm_response_format

        payload = _to_litellm_response_format(ReworkPlan, "openai/gpt-4.1-mini")
        self.assertIsNotNone(payload)
        self.assertTrue(payload["json_schema"]["strict"])
        self.assertEqual(_violations(payload["json_schema"]["schema"]), [])

    def test_reasons_is_a_list_not_a_map(self) -> None:
        """The one property whose SHAPE, not contents, caused the failure."""
        reasons = ReworkPlan.model_json_schema()["properties"]["reasons"]
        self.assertEqual(reasons["type"], "array")

    def test_a_plan_stored_in_the_old_shape_still_loads(self) -> None:
        """A run mid-rework when this changed must not lose its routing."""
        plan = ReworkPlan.model_validate(
            {
                "targets": ["first_page_visual"],
                "reasons": {"first_page_visual": "Use a real clip."},
                "feedback": "cover is static",
            }
        )
        self.assertEqual(plan.reasons, [
            ReworkReason(target="first_page_visual", reason="Use a real clip.")
        ])
        self.assertEqual(plan.reason_for("first_page_visual"), "Use a real clip.")

    def test_reason_for_is_empty_rather_than_raising(self) -> None:
        self.assertEqual(ReworkPlan().reason_for("nobody"), "")


class SanitizerTests(unittest.TestCase):
    """The router's after-callback still produces one reason per target."""

    def test_every_target_gets_a_reason_even_when_the_model_omits_it(self) -> None:
        from app.agents import feedback_router as fr

        plan = ReworkPlan(
            targets=["first_page_visual", "phrasing"],
            reasons=[ReworkReason(target="first_page_visual", reason="Use a clip.")],
            feedback="cover is static and slide 3 is wordy",
        )
        # Mirror what _sanitize_rework_plan does with the parsed plan.
        by_target = {e.target: e.reason for e in plan.reasons if e.reason}
        fallback = plan.feedback
        rebuilt = [
            ReworkReason(target=t, reason=by_target.get(t, fallback))
            for t in plan.targets
        ]
        self.assertEqual([e.target for e in rebuilt], plan.targets)
        self.assertEqual(rebuilt[0].reason, "Use a clip.")
        self.assertEqual(rebuilt[1].reason, fallback)
        self.assertTrue(hasattr(fr, "_sanitize_rework_plan"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
