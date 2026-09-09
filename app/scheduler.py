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

from app.services import db, instagram_accounts, instagram_oauth

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

#: Advisory-lock id for the Instagram token refresh. Its own id, not the fetch
#: one: the two jobs are unrelated and must never block each other.
_IG_REFRESH_LOCK_ID = 0x0CA1_0F02

#: When to renew Instagram tokens. Daily, in the small hours - a token has a
#: fortnight of slack before it matters, so this never needs to be prompt.
IG_REFRESH_CRON = "17 4 * * *"

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

        # Deferred import: the fetcher pulls in feedparser and the agent
        # stack. The scheduler module itself must stay cheap to import.
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


async def refresh_instagram_tokens() -> dict:
    """Renew every long-lived Instagram token that is close to expiring.

    Meta's long-lived tokens last 60 days and can be extended at any point
    after their first 24 hours - but ONLY while still valid. A lapsed token
    has no recovery call; the account has to be connected again by hand. So
    this runs daily with a fortnight of slack, and a failure has two weeks of
    further attempts before it becomes somebody's problem.

    Each account is refreshed independently: one failing token must not stop
    the others, because they are unrelated credentials that merely happen to
    be renewed by the same job.

    Returns:
        ``{"refreshed": n, "failed": n}`` - for the log and for tests.
    """
    due = instagram_accounts.due_for_refresh()
    if not due:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0
    for account in due:
        try:
            token, expires_in = await asyncio.to_thread(
                instagram_oauth.refresh_long_lived, account.token
            )
            await instagram_accounts.record_refreshed(account.id, token, expires_in)
            refreshed += 1
            logger.info(
                "Refreshed the Instagram token for %s (%d days were left).",
                account.handle,
                account.expires_in_days or 0,
            )
        except Exception as exc:  # noqa: BLE001 - network, Meta, or storage
            failed += 1
            logger.error(
                "Could not refresh the Instagram token for %s: %s. It expires "
                "in %s days; reconnect the account if this keeps failing.",
                account.handle,
                exc,
                account.expires_in_days,
            )
    return {"refreshed": refreshed, "failed": failed}


async def _refresh_instagram_tokens_locked() -> None:
    """The scheduled entry point, behind an advisory lock."""
    pool = None
    try:
        pool = await db.get_pool()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instagram refresh skipped; no database: %s", exc)
        return
    async with pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _IG_REFRESH_LOCK_ID)
        if not got:
            logger.info("Another instance is refreshing Instagram tokens; skipping.")
            return
        try:
            await refresh_instagram_tokens()
        finally:
            try:
                await conn.execute(
                    "SELECT pg_advisory_unlock($1)", _IG_REFRESH_LOCK_ID
                )
            except Exception:  # noqa: BLE001
                logger.debug("Could not release the Instagram refresh lock.")


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
    fetching = bool(schedule.get("enabled", True))
    if not fetching:
        # NOT an early return any more. Instagram tokens expire on their own
        # schedule and have nothing to do with whether the newsroom polls its
        # feeds; returning here left them to lapse on any console with
        # fetching switched off.
        logger.info("Scheduled fetching is disabled in app_config.")

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

    if fetching:
        scheduler.add_job(
            run_fetch_once, trigger, id="fetch_news", replace_existing=True
        )
    scheduler.add_job(
        _refresh_instagram_tokens_locked,
        CronTrigger.from_crontab(IG_REFRESH_CRON),
        id="refresh_instagram_tokens",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started: fetching %s, Instagram tokens on %r.",
        f"on {schedule['fetch_cron']!r}" if fetching else "off",
        IG_REFRESH_CRON,
    )
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
        # Only the fetch job. remove_all_jobs() used to be right when fetching
        # was the only thing scheduled; it now also deletes the Instagram
        # token refresh, which would quietly let every connected account lapse
        # sixty days after somebody turned fetching off.
        try:
            _scheduler.remove_job("fetch_news")
        except Exception:  # noqa: BLE001 - already absent
            pass
        logger.info("Scheduled fetching disabled; fetch job removed.")
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
