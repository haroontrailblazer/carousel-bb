"""The live-event path: classification, fan-out, and sequence continuity.

Three properties matter here, and each has cost a real project dearly
somewhere:

* The console must learn a run's structure from the state delta, not by
  regex-parsing log prose. A test that pins this stops someone from
  "improving" an orchestrator log message and silently breaking the UI.
* A browser that stops reading must not be able to stall the pipeline feeding
  it, and must be told when it has missed something rather than shown a trace
  with a hole in it.
* A resumed run must continue its sequence numbering. Restarting at 1 would
  collide with the first leg's events, and because run_events is keyed on
  (run_id, seq) with ON CONFLICT DO NOTHING, the collision would be silent -
  the rework timeline would simply never appear.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.runs import bus as bus_mod
from app.runs import stream as stream_mod
from app.runs.bus import KIND_ERROR, KIND_PHASE, KIND_PROGRESS, KIND_TOOL, RunBus, RunEvent

RUN_ID = "run-stream-test"


def _part(text=None, call=None, response=None):
    return SimpleNamespace(
        text=text,
        function_call=SimpleNamespace(name=call) if call else None,
        function_response=SimpleNamespace(name=response) if response else None,
    )


def _event(author="carousel_orchestrator", parts=(), delta=None,
           error=None, long_running=None):
    return SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=list(parts)) if parts else None,
        actions=SimpleNamespace(state_delta=dict(delta or {})),
        error_message=error,
        long_running_tool_ids=long_running,
    )


class ClassifyEventTests(unittest.TestCase):
    def test_phase_comes_from_the_state_delta_not_the_log_text(self) -> None:
        event = _event(
            parts=[_part(text="[phase] generate -> qa")],
            delta={"phase": "qa"},
        )
        kind, data = stream_mod.classify_event(event)
        self.assertEqual(kind, KIND_PHASE)
        self.assertEqual(data["phase"], "qa")

    def test_prose_alone_is_not_treated_as_a_phase_change(self) -> None:
        """The text says 'phase' but no delta carries one - it is just text."""
        event = _event(parts=[_part(text="[phase] generate -> qa")])
        kind, data = stream_mod.classify_event(event)
        self.assertEqual(kind, KIND_PROGRESS)
        self.assertNotIn("phase", data)

    def test_rework_and_review_rounds_are_forwarded(self) -> None:
        event = _event(delta={"rework_round": 2, "review_round": 1})
        _kind, data = stream_mod.classify_event(event)
        self.assertEqual(data["rework_round"], 2)
        self.assertEqual(data["review_round"], 1)

    def test_bulk_pipeline_state_is_not_pushed_to_the_browser(self) -> None:
        """The bundle and plan are fetched, not streamed - they are huge."""
        event = _event(delta={"bundle": {"x": "y" * 5000}, "carousel_plan": {}})
        _kind, data = stream_mod.classify_event(event)
        self.assertEqual(data, {})

    def test_tool_calls_and_responses_are_named(self) -> None:
        event = _event(
            author="research",
            parts=[_part(call="search_web"), _part(response="save_research_brief")],
        )
        kind, data = stream_mod.classify_event(event)
        self.assertEqual(kind, KIND_TOOL)
        self.assertEqual(data["tool_calls"], ["search_web"])
        self.assertEqual(data["tool_responses"], ["save_research_brief"])

    def test_errors_win_over_everything_else(self) -> None:
        event = _event(delta={"phase": "qa"}, error="model refused")
        kind, data = stream_mod.classify_event(event)
        self.assertEqual(kind, KIND_ERROR)
        self.assertEqual(data["error"], "model refused")

    def test_a_review_pause_is_flagged_not_reported_as_an_error(self) -> None:
        event = _event(author="review_dispatcher", long_running=["call-1"])
        kind, data = stream_mod.classify_event(event)
        self.assertEqual(kind, KIND_PROGRESS)
        self.assertTrue(data["awaiting_review"])


class SummarizeEventTests(unittest.TestCase):
    def test_tool_calls_render_with_direction_arrows(self) -> None:
        event = _event(author="research", parts=[_part(call="search_web")])
        self.assertEqual(stream_mod.summarize_event(event), "[research] -> tool search_web")

    def test_an_event_with_nothing_to_say_says_nothing(self) -> None:
        self.assertEqual(stream_mod.summarize_event(_event()), "")


class RunBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribers_receive_published_events(self) -> None:
        bus = RunBus()
        async with bus.subscribe(RUN_ID) as queue:
            await bus.publish(RunEvent(run_id=RUN_ID, seq=1, kind=KIND_PROGRESS, text="hi"))
            received = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(received.text, "hi")

    async def test_unsubscribes_even_when_the_reader_is_cancelled(self) -> None:
        """A closed browser tab is the normal case, not an error path."""
        bus = RunBus()
        started = asyncio.Event()

        async def reader():
            async with bus.subscribe(RUN_ID) as queue:
                started.set()
                await queue.get()  # never arrives

        task = asyncio.create_task(reader())
        await started.wait()
        self.assertEqual(bus.subscriber_count(RUN_ID), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            bus.subscriber_count(RUN_ID), 0, "a cancelled reader leaked its queue"
        )

    async def test_events_for_other_runs_are_not_delivered(self) -> None:
        bus = RunBus()
        async with bus.subscribe(RUN_ID) as queue:
            await bus.publish(RunEvent(run_id="some-other-run", seq=1, kind=KIND_PROGRESS))
            self.assertTrue(queue.empty())

    async def test_publishing_to_nobody_is_harmless(self) -> None:
        bus = RunBus()
        await bus.publish(RunEvent(run_id=RUN_ID, seq=1, kind=KIND_PROGRESS))

    async def test_a_stalled_subscriber_cannot_block_the_pipeline(self) -> None:
        """The run must keep going even if a tab never reads a single event."""
        bus = RunBus()
        async with bus.subscribe(RUN_ID) as queue:
            for seq in range(bus_mod.QUEUE_MAXSIZE + 20):
                await asyncio.wait_for(
                    bus.publish(RunEvent(run_id=RUN_ID, seq=seq, kind=KIND_PROGRESS)),
                    timeout=1,
                )
            drained = []
            while not queue.empty():
                drained.append(queue.get_nowait())

        kinds = [e.kind for e in drained]
        self.assertIn(bus_mod.KIND_GAP, kinds, "a lagging client was not told it missed events")
        # The newest events survive; the oldest are what got dropped.
        self.assertEqual(drained[-1].seq, bus_mod.QUEUE_MAXSIZE + 19)


class ConsumeInvocationTests(unittest.IsolatedAsyncioTestCase):
    """The shared loop used by both a fresh run and a review resume."""

    def _runner(self, events):
        class FakeRunner:
            def run_async(self, **_kwargs):
                async def gen():
                    for e in events:
                        yield e
                return gen()
        return FakeRunner()

    async def _consume(self, events, seq_start=None, max_seq=0):
        recorded: list[tuple] = []

        async def fake_append(run_id, seq, kind, author="", text="", data=None):
            recorded.append((seq, kind, text))

        with patch.object(stream_mod.db, "append_run_event", fake_append), patch.object(
            stream_mod.db, "max_run_seq", lambda _rid: _async(max_seq)
        ):
            result = await stream_mod.consume_invocation(
                self._runner(events),
                run_id=RUN_ID,
                session_id=RUN_ID,
                user_id="pipeline",
                new_message=None,
                seq_start=seq_start,
            )
        return result, recorded

    async def test_a_resumed_leg_continues_the_sequence(self) -> None:
        """Restarting at 1 would collide with leg one and vanish silently."""
        events = [
            _event(parts=[_part(text="[phase] review -> rework")], delta={"phase": "rework"}),
            _event(parts=[_part(text="reworking")]),
        ]
        result, recorded = await self._consume(events, max_seq=41)
        self.assertEqual([seq for seq, _k, _t in recorded], [42, 43])
        self.assertEqual(result["last_seq"], 43)
        self.assertEqual(result["phase"], "rework")

    async def test_a_review_pause_is_reported_to_the_caller(self) -> None:
        events = [_event(author="review_dispatcher", long_running=["call-1"],
                         parts=[_part(text="waiting for a human")])]
        result, _ = await self._consume(events)
        self.assertTrue(result["paused"])

    async def test_empty_events_are_not_recorded_as_blank_timeline_rows(self) -> None:
        result, recorded = await self._consume([_event(), _event(parts=[_part(text="real")])])
        self.assertEqual(len(recorded), 1)
        self.assertEqual(result["events"], 2, "both events were still consumed")

    async def test_a_database_hiccup_does_not_kill_the_run(self) -> None:
        async def boom(*_a, **_k):
            raise ConnectionError("pooler dropped it")

        events = [_event(parts=[_part(text="still going")])]
        with patch.object(stream_mod.db, "append_run_event", boom), patch.object(
            stream_mod.db, "max_run_seq", lambda _rid: _async(0)
        ):
            result = await stream_mod.consume_invocation(
                self._runner(events),
                run_id=RUN_ID, session_id=RUN_ID, user_id="pipeline",
                new_message=None,
            )
        self.assertEqual(result["events"], 1)


def _async(value):
    async def coro():
        return value
    return coro()


if __name__ == "__main__":
    unittest.main()
