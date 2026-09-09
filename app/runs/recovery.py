"""Dealing with runs that a restart interrupted.

A run paused at review is already safe: the verdict, the session and the
pending function call are all in Postgres, so any surface can resume it later.
A run in the middle of ``generate``, ``qa``, ``rework`` or ``publish`` is not -
it lives in an asyncio task, and a redeploy kills it.

Rather than leave those rows saying "running" forever, the process marks them
``interrupted`` at boot and the console offers a Resume button. That is honest
about what happened and gives the user the one action that actually helps.

**This is only correct at a single instance.** The reasoning is: if a run is in
an active phase and this process has just started, no process can still be
driving it, because there is only ever one. Add a second instance and that
becomes false - instance B would boot and mark instance A's live runs as
interrupted, and a user clicking Resume would then run the same phase twice
concurrently. If this ever needs to scale out, the fix is a lease
(``runs.owner_id`` plus a heartbeat) so a run is only reclaimed once its owner
has demonstrably stopped renewing it.
"""

from __future__ import annotations

import logging

from app.runs.bus import KIND_TERMINAL
from app.runs.stream import record_event
from app.services import db

logger = logging.getLogger(__name__)


async def reconcile_on_startup() -> list[str]:
    """Mark every run stuck in an active phase as interrupted.

    Safe to call on every boot: a run already marked interrupted, cancelled or
    failed is skipped by the query, so this converges rather than rewriting
    history each time.

    Returns:
        The run ids that were reclaimed (empty when there was nothing to do).
    """
    try:
        candidates = await db.interrupted_run_candidates()
    except Exception as exc:
        # A database that is not reachable at boot is a problem, but it is not
        # this function's problem to solve - the health check will surface it.
        logger.warning("Startup reconcile skipped: %s", exc)
        return []

    if not candidates:
        logger.info("Startup reconcile: no interrupted runs.")
        return []

    reclaimed: list[str] = []
    for run in candidates:
        run_id = str(run.get("run_id") or "")
        phase = str(run.get("phase") or "")
        if not run_id:
            continue
        try:
            await db.set_run_status(run_id, db.RUN_STATUS_INTERRUPTED)
            seq = await db.max_run_seq(run_id)
            await record_event(
                run_id,
                seq + 1,
                KIND_TERMINAL,
                text=(
                    f"Interrupted during '{phase}' - the service restarted "
                    "while this run was working. It can be resumed from the "
                    "start of that phase."
                ),
                data={"status": db.RUN_STATUS_INTERRUPTED, "phase": phase},
            )
            reclaimed.append(run_id)
        except Exception as exc:
            logger.warning("Could not reclaim run %s: %s", run_id, exc)

    logger.warning(
        "Startup reconcile: marked %d run(s) interrupted: %s",
        len(reclaimed),
        ", ".join(reclaimed),
    )
    return reclaimed


async def release_stuck_queue_items() -> int:
    """Return news items abandoned mid-run to the queue.

    ``next_queued_news`` flips a row to ``processing`` when a run claims it. If
    that run then dies, the item is stranded: never carouselled, never retried,
    and invisible in the queue.

    An item is only freed when NO run still references it with a live status.
    Call this AFTER :func:`reconcile_on_startup`, which is what demotes a
    killed run out of ``running`` - the ordering is what makes the check
    meaningful. Freeing an item whose run is genuinely still going would let a
    second run pick up the same story and produce a duplicate carousel.

    Returns:
        How many items were returned to ``queued``.
    """
    try:
        pool = await db.get_pool()
        rows = await pool.fetch(
            """
            UPDATE news_queue SET status = $1
            WHERE status = $2
              AND NOT EXISTS (
                  SELECT 1 FROM runs r
                  WHERE r.news_id = news_queue.id
                    AND r.status = ANY($3::text[])
              )
            RETURNING id
            """,
            db.STATUS_QUEUED,
            db.STATUS_PROCESSING,
            [db.RUN_STATUS_RUNNING, db.RUN_STATUS_AWAITING_REVIEW],
        )
    except Exception as exc:
        logger.warning("Could not release stuck queue items: %s", exc)
        return 0

    if rows:
        logger.warning(
            "Startup reconcile: returned %d abandoned news item(s) to the queue.",
            len(rows),
        )
    return len(rows)


__all__ = ["reconcile_on_startup", "release_stuck_queue_items"]
