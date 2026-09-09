"""Stop, for the work that ``task.cancel()`` cannot reach.

Cancelling an asyncio task raises ``CancelledError`` at the task's next await.
That is enough for almost everything the pipeline does - but not for the parts
that hand a blocking call to a worker thread with ``asyncio.to_thread``. Those
threads are not interruptible: the event loop stops waiting for them, the work
carries on regardless, and its side effects land whether or not anyone still
wants them.

For ffmpeg that is wasted CPU on a bounded timeout, which is tolerable. For the
Instagram publish it is not: ``instagram_tools.publish_carousel`` uploads the
slides, then polls until the container is ready, then posts. Cancel it halfway
and the post can still go live - minutes after the console said "Stopping", on
a carousel someone deliberately stopped.

So Stop also raises a flag here, and the irreversible steps check it at the
last point where not-doing-it is still an option. A flag is the right shape:
it costs nothing to check, it cannot deadlock, and code that never checks it is
no worse off than it was.

Deliberately independent of ``app.runs.service`` so that tools and agents can
import it without pulling in the runner, the DB layer or the agent tree.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_requested: set[str] = set()


class RunCancelled(Exception):
    """Raised when a run was stopped and the caller must not continue.

    Carries the run id so a handler can say which carousel it abandoned.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} was stopped before this step ran.")
        self.run_id = run_id


def request(run_id: str) -> None:
    """Record that a human asked for this run to stop.

    Thread-safe, because the flag is READ from worker threads (the Instagram
    client polls from one) and SET from the event loop.
    """
    if not run_id:
        return
    with _lock:
        _requested.add(str(run_id))
    logger.info("Cancellation requested for run %s.", run_id)


def is_requested(run_id: str) -> bool:
    """True when Stop has been pressed for this run and not yet cleared."""
    if not run_id:
        return False
    with _lock:
        return str(run_id) in _requested


def clear(run_id: str) -> None:
    """Forget a cancellation, so the run can be resumed or re-run later.

    Called when a run starts or restarts. Without this a stopped run would
    refuse to publish for the rest of the process's life, which turns a
    deliberate Stop into a permanent one.
    """
    if not run_id:
        return
    with _lock:
        _requested.discard(str(run_id))


def raise_if_cancelled(run_id: str) -> None:
    """Abort here if Stop was pressed.

    Call immediately before anything that cannot be undone - the moment where
    checking is still cheaper than the consequence of not checking.

    Raises:
        RunCancelled: when a stop is pending for this run.
    """
    if is_requested(run_id):
        raise RunCancelled(str(run_id))


def pending() -> set[str]:
    """Runs with an unhonoured stop request (diagnostics only)."""
    with _lock:
        return set(_requested)


__all__ = [
    "RunCancelled",
    "clear",
    "is_requested",
    "pending",
    "raise_if_cancelled",
    "request",
]
