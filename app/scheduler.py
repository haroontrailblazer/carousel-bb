"""The in-process scheduler that keeps the news queue topped up.

**It fetches. It does not run.** Polling RSS and YouTube costs nothing; turning
a story into a carousel costs image and reasoning credits, so that decision
stays with a person. The scheduler's whole job is to make sure there is always
something worth choosing from when someone opens the console.

Deliberately in-process rather than a separate Render cron service. One web
service is the whole deployment, and a cron job would be a second one - which
also means a second process that could reclaim this one's runs at startup (see
app/runs/recovery.py on why exactly one instance may exist).

The cadence lives in the ``app_config`` table, not in an environment variable,
because ``settings`` is a frozen dataclass read once at import: changing an env
var needs a redeploy, whereas changing a row does not.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.services import db

logger = logging.getLogger(__name__)

#: Config key holding the schedule.
CONFIG_KEY = "schedule"

#: Default: top the queue up hourly. Cheap - it is a handful of HTTP GETs.
DEFAULT_SCHEDULE: dict = {
    "enabled": True,
    "fetch_cron": "0 * * * *",
}

#: Advisory-lock id for the fetch job. Cheap insurance: even though this design
#: assumes one instance, a second one starting by accident would otherwise
#: double-fetch and race on the queue's unique url_hash.
_FETCH_LOCK_ID = 0x0CA1_0F01

_scheduler: Any = None

#: How many fetches are in flight right now.
#:
#: A counter rather than a flag: a manual "check now" can overlap the cron
#: tick, and a flag would be cleared by whichever finished first while the
#: other was still working - so the console would stop showing "checking"
#: while a check was still running.
_fetch_in_flight = 0


def fetch_in_progress() -> bool:
    """Whether a feed check is running right now, from any trigger.

    Exposed for the console: the newsroom shows a live dot while the sources
    are being polled. Note this is NOT ``scheduler_state()["running"]``, which
    says whether the timer itself is alive - a very different question.
    """
    return _fetch_in_flight > 0


async def load_schedule() -> dict:
    """The current schedule, falling back to the default."""
    try:
        stored = await db.get_config(CONFIG_KEY)
    except Exception as exc:
        logger.warning("Could not read the schedule config (%s); using defaults.", exc)
        return dict(DEFAULT_SCHEDULE)
    if not isinstance(stored, dict):
        return dict(DEFAULT_SCHEDULE)
    return {**DEFAULT_SCHEDULE, **stored}


async def save_schedule(schedule: dict) -> dict:
    """Persist a new schedule and apply it without a restart."""
    merged = {**DEFAULT_SCHEDULE, **(schedule or {})}
    await db.set_config(CONFIG_KEY, merged)
    await reschedule()
    return merged


async def run_fetch_once() -> dict:
    """Poll the configured sources into ``news_queue``.

    Returns a summary rather than raising: this runs on a timer with nobody
    watching, and a feed being down for an hour is not an incident.
    """
    global _fetch_in_flight
    lock_held = False
    _fetch_in_flight += 1
    try:
        pool = await db.get_pool()
        lock_held = bool(
            await pool.fetchval("SELECT pg_try_advisory_lock($1)", _FETCH_LOCK_ID)
        )
        if not lock_held:
            logger.info("Another process is already fetching; skipping this tick.")
            return {"skipped": "locked"}

        # Deferred import: the fetcher pulls in feedparser, Gmail auth and the
        # agent stack. The scheduler module itself must stay cheap to import.
        #
        # Two calls, not one: fetch_all() only POLLS the sources and returns
        # payloads - enqueue_items() is what writes them to news_queue and
        # dedupes. Calling fetch_all alone would poll every hour and quietly
        # discard everything it found.
        from fetcher.fetch_news import enqueue_items, fetch_all

        payloads = await asyncio.to_thread(fetch_all)
        enqueued, skipped = await enqueue_items(payloads)
        summary = {"fetched": len(payloads), "enqueued": enqueued, "duplicates": skipped}
        logger.info(
            "Scheduled fetch: %d fetched, %d new, %d duplicate(s).",
            len(payloads),
            enqueued,
            skipped,
        )
        return summary
    except Exception as exc:
        logger.exception("Scheduled fetch failed: %s", exc)
        return {"error": str(exc)}
    finally:
        _fetch_in_flight -= 1
        if lock_held:
            try:
                pool = await db.get_pool()
                await pool.fetchval("SELECT pg_advisory_unlock($1)", _FETCH_LOCK_ID)
            except Exception:  # pragma: no cover - best effort
                logger.debug("Could not release the fetch advisory lock.")


async def start_scheduler() -> Optional[Any]:
    """Start the scheduler, or return ``None`` if it cannot run.

    Missing APScheduler or a disabled schedule are both normal, non-fatal
    states: the console works fine without automatic fetching, and someone
    running locally usually does not want it.
    """
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning(
            "APScheduler is not installed; automatic news fetching is off. "
            "Add apscheduler to requirements.txt to enable it."
        )
        return None

    schedule = await load_schedule()
    if not schedule.get("enabled", True):
        logger.info("Scheduled fetching is disabled in app_config.")
        return None

    scheduler = AsyncIOScheduler(
        job_defaults={
            # coalesce: after a redeploy, run the missed tick ONCE rather than
            # once per hour of downtime.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        }
    )
    try:
        trigger = CronTrigger.from_crontab(str(schedule["fetch_cron"]))
    except Exception as exc:
        logger.error(
            "Invalid fetch_cron %r (%s); falling back to %r.",
            schedule.get("fetch_cron"),
            exc,
            DEFAULT_SCHEDULE["fetch_cron"],
        )
        trigger = CronTrigger.from_crontab(DEFAULT_SCHEDULE["fetch_cron"])

    scheduler.add_job(run_fetch_once, trigger, id="fetch_news", replace_existing=True)
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler started: fetching on %r.", schedule["fetch_cron"])
    return scheduler


async def reschedule() -> None:
    """Apply a changed schedule to the running scheduler."""
    global _scheduler
    if _scheduler is None:
        await start_scheduler()
        return
    from apscheduler.triggers.cron import CronTrigger

    schedule = await load_schedule()
    if not schedule.get("enabled", True):
        _scheduler.remove_all_jobs()
        logger.info("Scheduled fetching disabled; jobs removed.")
        return
    _scheduler.add_job(
        run_fetch_once,
        CronTrigger.from_crontab(str(schedule["fetch_cron"])),
        id="fetch_news",
        replace_existing=True,
    )
    logger.info("Scheduler updated: fetching on %r.", schedule["fetch_cron"])


def scheduler_state() -> dict:
    """Whether the scheduler is live, and when it next fires.

    Worth exposing rather than inferring from config: "enabled: true" in a
    table says what was ASKED for, not what is actually running. If APScheduler
    is missing or the job failed to schedule, the config still reads enabled
    and nothing ever fetches.
    """
    if _scheduler is None:
        return {"running": False, "next_run": None}
    job = _scheduler.get_job("fetch_news")
    next_run = getattr(job, "next_run_time", None) if job else None
    return {
        "running": bool(getattr(_scheduler, "running", False)),
        "next_run": next_run.isoformat() if next_run else None,
    }


def shutdown_scheduler() -> None:
    """Stop the scheduler without waiting for a running job."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # pragma: no cover - shutdown best effort
            pass
        _scheduler = None


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_SCHEDULE",
    "fetch_in_progress",
    "load_schedule",
    "reschedule",
    "run_fetch_once",
    "save_schedule",
    "scheduler_state",
    "shutdown_scheduler",
    "start_scheduler",
]
