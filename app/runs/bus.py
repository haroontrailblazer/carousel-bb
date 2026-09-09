"""In-process fan-out of live run events to connected browsers.

A run is driven by exactly one asyncio task, and zero or more browsers may be
watching it. This is the pipe between them: the task publishes, each open SSE
response subscribes.

The bus carries only LIVE events. History comes from the ``run_events`` table,
which is what makes a page reload, a dropped connection, and a browser opened
halfway through a run all behave the same - the endpoint replays persisted rows
up to the client's cursor, then switches to the bus. Nothing here needs to
remember anything, so a slow reader can never grow memory without bound.

Single-process by design. Two web instances would each have their own bus and
each see only their own runs, which is one of several reasons this service must
run at ``numInstances: 1``. The escape hatch, if that ever changes, is Postgres
LISTEN/NOTIFY in place of these queues.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

#: How many events a single slow subscriber may fall behind before it starts
#: losing them. Generous enough that a browser on a bad connection catches up,
#: small enough that a forgotten tab cannot pin megabytes per run.
QUEUE_MAXSIZE = 256

#: Event kinds. ``gap`` is synthetic - see ``publish``.
KIND_PHASE = "phase"
KIND_PROGRESS = "progress"
KIND_TOOL = "tool"
KIND_ERROR = "error"
KIND_TERMINAL = "terminal"
KIND_GAP = "gap"


@dataclass(frozen=True)
class RunEvent:
    """One item on a run's timeline.

    ``seq`` is assigned by the single task driving the run and is monotonic
    across the whole run, including across a review pause - it doubles as the
    SSE event id, so a reconnecting client can say exactly what it already has.
    """

    run_id: str
    seq: int
    kind: str
    author: str = ""
    text: str = ""
    data: dict = field(default_factory=dict)
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        # `id` is what the browser dedupes on. `seq` is a POSITION and the SSE
        # endpoint renumbers it onto the end of whatever history it replayed,
        # so two frames can share one seq (and one frame can appear under two)
        # depending on which source the client last read. The identity below
        # comes from the run_events counter, which is the same value the
        # merged history uses for these rows - so a live frame and its
        # persisted twin are recognisably the same thing.
        return {**asdict(self), "id": f"evt:{self.seq}"}


class RunBus:
    """Per-run publish/subscribe over asyncio queues."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    @contextlib.asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[asyncio.Queue]:
        """Subscribe to a run for the duration of the block.

        A context manager rather than an add/remove pair because the caller is
        an SSE response that can be cancelled at any await point - a client
        closing its tab is the normal case, not an error. Without guaranteed
        removal, every disconnect would leak a queue that the run task keeps
        filling forever.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            yield queue
        finally:
            listeners = self._subscribers.get(run_id)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    del self._subscribers[run_id]

    async def publish(self, event: RunEvent) -> None:
        """Deliver an event to every current subscriber of its run.

        Never blocks and never raises: publishing happens inside the pipeline's
        own event loop, so a browser that has stopped reading must not be able
        to stall the run that is feeding it.

        A full queue drops its OLDEST events and pushes a ``gap`` marker, so the
        client learns it missed something and can re-fetch from the database
        instead of silently rendering an incomplete trace. Dropping the newest
        instead would be worse: the newest events are the ones the viewer is
        actually waiting for.

        Which is why room is made for BOTH items before either is pushed. An
        earlier version freed a single slot and then pushed two - the marker
        and the event - so the second ``put_nowait`` raised ``QueueFull`` and
        was swallowed by the handler below. The subscriber received the gap
        marker and lost the very event the marker was warning it about, which
        is exactly the behaviour this docstring promises not to have.
        """
        for queue in list(self._subscribers.get(event.run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                gap = RunEvent(
                    run_id=event.run_id,
                    seq=event.seq,
                    kind=KIND_GAP,
                    text="Some events were dropped; reload to see the full trace.",
                )
                try:
                    # Two slots for two items. maxsize is 256, so this cannot
                    # exhaust the queue; the guard is for a pathological one.
                    for _ in range(2):
                        if queue.qsize() + 2 <= QUEUE_MAXSIZE:
                            break
                        try:
                            queue.get_nowait()  # discard the oldest
                        except asyncio.QueueEmpty:  # pragma: no cover
                            break
                    queue.put_nowait(gap)
                    queue.put_nowait(event)
                except Exception:  # pragma: no cover - the reader is gone
                    logger.debug(
                        "Dropped event %s for run %s: subscriber not draining.",
                        event.seq,
                        event.run_id,
                    )

    def subscriber_count(self, run_id: str) -> int:
        """How many browsers are currently watching a run."""
        return len(self._subscribers.get(run_id, ()))

    def watched_runs(self) -> set[str]:
        """Runs with at least one live subscriber."""
        return set(self._subscribers)


#: The process-wide bus. One per process, matching the one-instance constraint.
BUS = RunBus()


__all__ = [
    "BUS",
    "KIND_ERROR",
    "KIND_GAP",
    "KIND_PHASE",
    "KIND_PROGRESS",
    "KIND_TERMINAL",
    "KIND_TOOL",
    "QUEUE_MAXSIZE",
    "RunBus",
    "RunEvent",
]
