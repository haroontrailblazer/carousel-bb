"""Starting and supervising pipeline runs inside a long-lived process.

The CLI could afford to be simple: pop an item, drive it, exit. A web service
cannot. A run started from a browser has to outlive the HTTP request that asked
for it, survive the user closing the tab, be visible while it happens, be
cancellable, and be recoverable when the process dies mid-way.

So a run here is a detached ``asyncio.Task``, held by a strong reference (the
event loop only keeps weak ones, so an unreferenced task can be garbage
collected mid-run). The HTTP handler returns as soon as the session exists.

Deliberately NOT built on ADK's ``/run_sse``: that endpoint cancels the agent
run when the client disconnects, so closing a tab would kill a generation that
has already spent real money.

Cost control lives here too, because this is the only place a run can begin.
Every carousel costs image and reasoning credits, so a bug or an impatient
click must not be able to start ten.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from app import observability
from app.config import settings
from app.runs import cancellation
from app.runs.bus import KIND_ERROR, KIND_TERMINAL
from app.runs.stream import consume_invocation, record_event
from app.services import db
from app.state import (
    K_NEWS_ITEM,
    K_RUN_ID,
    PHASE_DONE,
    PHASE_GENERATE,
    PHASE_REWORK,
)

logger = logging.getLogger(__name__)

#: Where a run came from. Recorded on the run so history can distinguish a
#: typed topic from a scheduled pick.
RunSource = Literal["topic", "url", "queue", "schedule"]

#: Fixed pipeline user id - sessions are addressed by
#: (app_name, user_id, session_id) and every surface must agree.
try:  # pragma: no cover - trivial import wiring
    from fetcher.fetch_news import PIPELINE_USER_ID
except Exception:  # pragma: no cover - env dependent
    PIPELINE_USER_ID = "pipeline"

#: How many carousels may be in flight at once.
#:
#: This pipeline is CPU- and network-heavy (ffmpeg, image generation) on one
#: small instance, so every extra slot buys the ability to work on another
#: story while the first waits - not more throughput on any single one. Three
#: is the default because the long pole in a run is waiting on a model or a
#: reviewer, not local CPU; raise or lower it here rather than in code.
#:
#: Anything that assumed one run per process had to be fixed for this to be
#: safe - image-token accounting is now keyed by run id, and every "is this
#: run being driven?" check reads both task registries.
MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "3"))

#: Ceiling on runs started in a rolling 24 hours. A runaway loop that starts
#: carousels is not a slow bug - it is an invoice.
MAX_RUNS_PER_DAY = int(os.getenv("MAX_RUNS_PER_DAY", "10"))

#: Strong references to in-flight run tasks, keyed by run id.
_run_tasks: dict[str, "asyncio.Task[None]"] = {}

#: Slots claimed by a run that is being set up but has no task yet. Counted as
#: active, so the gap between passing the cap check and registering the task
#: cannot be used by another request to pass the same check.
_reserved: set[str] = set()


class RunRefused(Exception):
    """A run was not started, for a reason the caller should show the user.

    Carries a machine-readable ``code`` so the API can branch on it and the UI
    can render it without matching on message text.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StartedRun:
    run_id: str
    news_id: str
    title: str


def active_run_ids() -> set[str]:
    """Runs currently being driven by this process, from BOTH registries.

    A run is driven from two places and the second is the expensive one. A
    fresh or resumed invocation lives in ``_run_tasks`` here; the rework that
    follows a rejection lives in ``app.review.resume._resume_tasks``. When a
    run pauses for review its ``_drive_run`` task completes and is popped, so
    reading only this module's registry made the process look idle while a
    resumed leg was regenerating images and publishing to Instagram.

    Everything that asks "is this run being driven?" flows through here -
    ``_check_limits``, ``resume_interrupted_run``'s re-entry guard, the
    console's ``is_live`` - so counting only half of it let a second run start
    on top of a rework, and let two drivers touch one ADK session.
    """
    active = {rid for rid, task in _run_tasks.items() if not task.done()}
    active |= set(_reserved)

    # Deferred: app.review.resume imports app.services.db, and importing it at
    # module scope would close an import cycle through app.runs.service.
    try:
        from app.review.resume import active_resume_run_ids

        active |= active_resume_run_ids()
    except Exception as exc:  # pragma: no cover - import wiring
        logger.debug("Could not read in-flight resume tasks: %s", exc)
    return active


def _claim_slot(run_id: str) -> None:
    """Count the actives and take a slot, with no await in between.

    This is the whole point of the function. Counting in one statement and
    registering the task several awaits later is a check-then-act race: a run
    only became countable at ``spawn_run``, and between the check and there sit
    a session write, three DB writes and - for a pasted URL - a thirty-second
    HTTP fetch. Every request that arrived inside that window passed the check,
    so N simultaneous clicks started N runs however small the cap was.

    A reservation closes it because the event loop cannot interleave anything
    between the count and the ``add`` below: no await, no suspension point.

    Raises:
        RunRefused: when every slot is taken.
    """
    active = active_run_ids()
    if len(active) >= MAX_CONCURRENT_RUNS:
        raise RunRefused(
            "too_many_active_runs",
            f"{len(active)} carousels are already being made "
            f"(limit {MAX_CONCURRENT_RUNS}). Wait for one to reach review, or "
            "raise MAX_CONCURRENT_RUNS.",
        )
    _reserved.add(str(run_id))


def _release_slot(run_id: str) -> None:
    """Hand the reservation back.

    Called once the real task holds the slot instead (its id is already in
    ``_run_tasks``, so the union in ``active_run_ids`` is unchanged), or when
    the work never started and the slot should go back in the pool.
    """
    _reserved.discard(str(run_id))


async def _check_limits(run_id: str, *, new_run: bool = True) -> None:
    """Refuse work that would exceed the concurrency or daily cap.

    On success a slot is RESERVED for ``run_id``; the caller must release it
    (see ``_release_slot``) once the driving task exists or the attempt is
    abandoned.

    Args:
        run_id: The run about to be driven.
        new_run: True when this would START a carousel. The daily cap exists
            so a bug or an impatient click cannot run up an invoice, which is
            a statement about how many carousels get MADE - so it applies only
            here. Finishing a run already in flight (approving it, resuming
            it, re-running its rework) spends nothing new: those images and
            that reasoning are already paid for, and refusing them left ready
            carousels un-approvable and stopped tasks un-resumable for the
            rest of the day.

    The concurrency cap applies either way, because it is about what this one
    small instance can do at once, not about spend.
    """
    _claim_slot(run_id)
    if new_run and MAX_RUNS_PER_DAY > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        try:
            started = await db.count_runs_since(since)
        except Exception as exc:
            # Refusing every run because the counter is unreachable would be
            # worse than briefly missing the cap.
            logger.warning("Could not check the daily run cap: %s", exc)
            return
        if started >= MAX_RUNS_PER_DAY:
            _release_slot(run_id)
            raise RunRefused(
                "daily_limit_reached",
                f"{started} runs have already started in the last 24 hours "
                f"(limit {MAX_RUNS_PER_DAY}). Raise MAX_RUNS_PER_DAY to allow more.",
            )


async def fetch_url_item(url: str) -> dict:
    """Turn a pasted article URL into a NewsItem payload.

    Reuses the fetcher's extraction helpers rather than reimplementing them, so
    a URL typed into the console produces the same shape as an RSS pick and the
    downstream agents cannot tell the difference.

    Raises:
        RunRefused: when the page cannot be fetched or has no usable text.
    """
    import requests
    from fetcher.fetch_news import (
        MAX_BODY_CHARS,
        _html_to_text,
        _media_urls_from_html,
    )
    from app.schemas import NewsItem

    clean = (url or "").strip()
    if not clean.lower().startswith(("http://", "https://")):
        raise RunRefused("invalid_url", "Enter a full http(s) URL.")

    try:
        resp = await asyncio.to_thread(
            requests.get,
            clean,
            timeout=30,
            headers={"User-Agent": "carousel-factory/1.0"},
        )
        resp.raise_for_status()
    except Exception as exc:
        raise RunRefused(
            "url_unreachable", f"Could not fetch that URL: {exc}"
        ) from exc

    markup = resp.text
    text = _html_to_text(markup)
    if len(text.strip()) < 200:
        raise RunRefused(
            "url_has_no_text",
            "That page has almost no readable text - it may be a paywall, a "
            "login screen, or a JavaScript-rendered app. Paste the article "
            "text as a topic instead.",
        )

    title = ""
    lowered = markup.lower()
    if "<title" in lowered:
        start = lowered.index("<title")
        start = markup.index(">", start) + 1
        end = lowered.index("</title>", start)
        title = _html_to_text(markup[start:end]).strip()

    item = NewsItem(
        id=f"url-{db.url_hash(clean)[:16]}",
        title=(title or text.strip().splitlines()[0])[:150],
        summary=text.strip()[:2000],
        body=text[:MAX_BODY_CHARS],
        source_name="web",
        source_url=clean,
        media_urls=_media_urls_from_html(markup),
        tags=["url"],
    )
    return item.model_dump(mode="json")


async def start_run(
    *,
    source: RunSource,
    topic: str = "",
    url: str = "",
    news: Optional[dict] = None,
    requested_by: str = "",
) -> StartedRun:
    """Create a run, seed its session, and start driving it in the background.

    Returns as soon as the session exists - the pipeline itself runs for
    minutes afterwards and is watched over the event stream.

    Args:
        source: How this run was triggered.
        topic: Free text, for ``source="topic"``. The orchestrator synthesises
            a NewsItem from it (see ``_phase_init``).
        url: Article URL, for ``source="url"``.
        news: A ready NewsItem payload, for ``source="queue"``/``"schedule"``.
        requested_by: Email of the person who asked, for the audit trail.

    Raises:
        RunRefused: cap exceeded, or the input could not be turned into a run.
    """
    # The id is minted BEFORE the cap check so the slot can be reserved under
    # it. Everything after the check - a session write, three DB writes, and
    # for a pasted URL a thirty-second fetch - happens while this run already
    # counts as active, so a second click cannot pass the same check.
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    await _check_limits(run_id)

    try:
        if source == "url":
            news = await fetch_url_item(url)
            # Pasting a link and clicking its newsroom card are the same act,
            # so the paste claims the card too. Without this the story stayed
            # queued and someone could pick it later, paying twice for one
            # carousel. Best effort: most pasted links are not in the queue.
            try:
                claimed = await db.claim_news_by_url_hash(db.url_hash(url.strip()))
            except Exception as exc:  # pragma: no cover - bookkeeping only
                logger.debug("Could not claim a queued item for %s: %s", url, exc)
                claimed = None
            if claimed:
                # Record the QUEUE ROW's id, not the synthetic "url-<hash>"
                # one minted by fetch_url_item. _close_news_item closes the row
                # named by runs.news_id, so recording the synthetic id claimed
                # a story and then had nothing to close - leaving it stuck at
                # 'processing' exactly as if it had never been claimed.
                news = dict(news or {})
                news["id"] = str(claimed)
        elif source == "topic":
            cleaned = (topic or "").strip()
            if len(cleaned) < 3:
                raise RunRefused("empty_topic", "Type a topic to generate a carousel.")
            news = None  # the orchestrator synthesises the NewsItem from the message
        elif news is None:
            raise RunRefused("no_input", "Nothing to run.")

        news_id = str((news or {}).get("id") or run_id)
        title = str(
            (news or {}).get("title") or (topic or "").strip()[:150] or "Untitled"
        )

        state: dict[str, Any] = {K_RUN_ID: run_id}
        if news is not None:
            state[K_NEWS_ITEM] = news

        from app.agent import build_runner

        runner = build_runner()
        await runner.session_service.create_session(
            app_name=settings.app_name,
            user_id=PIPELINE_USER_ID,
            session_id=run_id,
            state=state,
        )
        await db.create_run(run_id, news_id)
        await db.set_run_meta(
            run_id, title=title, source=source, requested_by=requested_by
        )
        await db.set_run_status(run_id, db.RUN_STATUS_RUNNING)

        first_message = (
            topic.strip()
            if source == "topic"
            else f"Create an Instagram carousel for the queued news item: {title}"
        )
        spawn_run(run_id, runner=runner, first_message=first_message)
    except BaseException:
        # Nothing is driving this run, so its slot must go back in the pool -
        # otherwise a refused or failed start silently shrinks the cap for the
        # rest of the process's life.
        _release_slot(run_id)
        raise

    # The task holds the slot now; its id is in _run_tasks, which
    # active_run_ids already counts.
    _release_slot(run_id)
    logger.info("Started run %s (%s) for %r.", run_id, source, title)
    return StartedRun(run_id=run_id, news_id=news_id, title=title)


def _forget_task(run_id: str, task: "asyncio.Task[None]"):
    """Retract a task's registration - but only if it is still the one there.

    ``_run_tasks`` is keyed by run id, and a run can legitimately have a NEW
    task registered while the previous one is still unwinding: a cancelled run
    that is resumed straight away, or a timeout whose handler is still writing
    the ending. An unconditional ``pop(run_id)`` in the old task's callback
    then deletes the NEW task's entry - which drops the only strong reference
    to a running coroutine (asyncio keeps just a weak one, so it can be
    collected mid-run) and leaves Stop unable to find it.
    """

    def _done(finished: "asyncio.Task[None]") -> None:
        if _run_tasks.get(run_id) is finished:
            _run_tasks.pop(run_id, None)

    return _done


def spawn_run(run_id: str, *, runner: Any, first_message: str) -> "asyncio.Task[None]":
    """Drive a run in the background, holding a strong reference to the task."""
    from google.genai import types

    message = types.Content(role="user", parts=[types.Part(text=first_message)])
    task = asyncio.get_running_loop().create_task(
        _drive_run(run_id, runner, message), name=f"run-{run_id}"
    )
    _run_tasks[run_id] = task
    task.add_done_callback(_forget_task(run_id, task))
    return task


#: How often a running task says "still here". Must stay well below the idle
#: threshold recovery uses (db.interrupted_run_candidates), or a healthy run
#: gets reclaimed while it is working.
HEARTBEAT_INTERVAL_S = float(os.getenv("RUN_HEARTBEAT_S", "30"))

#: Hard wall-clock ceiling on one invocation. Nothing else bounds it: neither
#: app.llm.resolve_model nor the ADK/LiteLLM call it builds sets a request
#: timeout, so a hung provider call simply never returns.
#:
#: Left unbounded, three facts compose into a dead service. The invocation
#: waits forever; _heartbeat keeps touching the run row, so the 180 s idle
#: threshold in db.interrupted_run_candidates never trips and startup recovery
#: cannot reclaim it; and MAX_CONCURRENT_RUNS is 1, so every later run is
#: refused behind it. The console shows a task Running with a pulsing trace and
#: no carousel can be made again until a human notices and presses Stop.
#:
#: Two hours is far above a real run (minutes, including ffmpeg and image
#: generation) and well under "nobody is coming". The resume path has always
#: had its own ceiling in RESUME_TIMEOUT_S; this is the same idea for the path
#: that actually starts carousels.
RUN_TIMEOUT_S = float(os.getenv("RUN_TIMEOUT_S", "7200"))


async def _heartbeat(run_id: str, deadline: float = RUN_TIMEOUT_S) -> None:
    """Touch the run row on a timer until cancelled or out of time.

    Runs alongside the pipeline rather than inside it, so a phase that does
    fifteen minutes of ffmpeg and image generation without a single database
    write still looks alive to startup recovery.

    It stops after ``deadline`` seconds because a heartbeat is an assertion,
    not a fact: it says "a task is driving this run", and past the run cap that
    claim is no longer one this process can make. Beating forever meant a
    stalled run stayed invisible to every automatic recovery path there is.
    """
    elapsed = 0.0
    while elapsed < deadline:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            elapsed += HEARTBEAT_INTERVAL_S
            await db.touch_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A missed heartbeat is not worth killing a run over; the next one
            # is thirty seconds away.
            logger.debug("Heartbeat failed for run %s: %s", run_id, exc)
    logger.warning(
        "Heartbeat for run %s stopped after %.0f s; it will no longer vouch "
        "for this run's liveness, so startup recovery can reclaim it.",
        run_id,
        deadline,
    )


async def _drive_run(run_id: str, runner: Any, message: Any) -> None:
    """Consume one invocation and record how it ended.

    Never raises: this is a detached task with nobody to catch it, and an
    unhandled exception here would be logged by asyncio and then lost, leaving
    the run row saying "running" forever.
    """
    # Everything this task does from here on is attributed to this run,
    # including image calls made several layers down inside a worker thread.
    observability.bind_run(run_id)
    if cancellation.is_requested(run_id):
        # Stopped while it was still being set up. Clearing the flag here
        # unconditionally threw that away and the run carried on as if nobody
        # had asked - so honour it first, then clear.
        logger.info("Run %s was stopped before it started; not driving it.", run_id)
        cancellation.clear(run_id)
        await _finish_badly(
            run_id, db.RUN_STATUS_CANCELLED, "Stopped before it started."
        )
        return
    # A stop belongs to the leg it stopped. Leaving the flag up would make a
    # deliberate Stop permanent: every later resume would refuse to publish.
    cancellation.clear(run_id)
    beat = asyncio.get_running_loop().create_task(
        _heartbeat(run_id), name=f"heartbeat-{run_id}"
    )
    try:
        result = await asyncio.wait_for(
            consume_invocation(
                runner,
                run_id=run_id,
                session_id=run_id,
                user_id=PIPELINE_USER_ID,
                new_message=message,
            ),
            timeout=RUN_TIMEOUT_S,
        )
        if result["paused"]:
            status = db.RUN_STATUS_AWAITING_REVIEW
        elif result.get("phase") == "done":
            status = db.RUN_STATUS_DONE
        else:
            # The invocation ENDED without pausing and without reaching done -
            # the orchestrator halted mid-phase. Recording RUNNING here was
            # wrong twice over: nothing was running, so the console showed a
            # live task forever with a pulsing trace and a Stop button that
            # answered "not running"; and it overwrote whatever status the
            # halting phase had just decided for itself.
            #
            # So respect a status the orchestrator already moved off running
            # (the review-notice halt sets awaiting_review, because the
            # carousel is ready and a human IS needed), and otherwise call it
            # what it is: stopped part-way, and resumable.
            current = ""
            try:
                row = await db.get_run(run_id)
                current = str((row or {}).get("status") or "")
            except Exception as exc:  # pragma: no cover - status read is advisory
                logger.warning("Could not re-read status for %s: %s", run_id, exc)
            status = (
                current
                if current and current != db.RUN_STATUS_RUNNING
                else db.RUN_STATUS_INTERRUPTED
            )
        await db.set_run_status(run_id, status)
        if status == db.RUN_STATUS_DONE:
            await _close_news_item(run_id)
        await record_event(
            run_id,
            result["last_seq"] + 1,
            KIND_TERMINAL,
            text=(
                "Waiting for your review."
                if result["paused"]
                else "Run finished."
                if status == db.RUN_STATUS_DONE
                else f"Run stopped ({status})."
            ),
            data={"status": status, "paused": result["paused"]},
        )
    except asyncio.TimeoutError:
        logger.error(
            "Run %s exceeded RUN_TIMEOUT_S (%.0f s) and was stopped; a tool "
            "or model call never returned.",
            run_id,
            RUN_TIMEOUT_S,
        )
        # The same flag Stop raises. Abandoning a run because it hung must not
        # leave a worker thread free to finish publishing it - the timeout
        # cancels the task, but a thread inside to_thread never notices, so
        # without this the carousel could still go live hours after the run was
        # given up on.
        cancellation.request(run_id)
        await _finish_badly(
            run_id,
            db.RUN_STATUS_INTERRUPTED,
            f"Stopped after {RUN_TIMEOUT_S / 3600:.0f}h with no progress - a "
            "tool or model call never returned. Resume to pick it up.",
        )
    except asyncio.CancelledError:
        # Reached from Stop (which has already raised the flag) and from the
        # shutdown drain (which has not). Raising it again is free, and it is
        # what stops a half-finished publish completing after the process has
        # decided to give the run up.
        cancellation.request(run_id)
        await _finish_badly(run_id, db.RUN_STATUS_CANCELLED, "Run cancelled.")
        raise
    except Exception as exc:
        logger.exception("Run %s failed.", run_id)
        await _finish_badly(run_id, db.RUN_STATUS_FAILED, f"Run failed: {exc}")
    finally:
        beat.cancel()
        try:
            await runner.close()
        except Exception as exc:  # pragma: no cover - cleanup best effort
            logger.warning("Closing the runner for %s failed: %s", run_id, exc)


async def _close_news_item(run_id: str) -> None:
    """Mark a published run's story finished, so nothing picks it up again.

    ``mark_news_done`` had exactly one caller - the CLI fetcher - so a carousel
    started from the console left its story at ``processing`` forever. The
    startup sweep then treats a long-stuck ``processing`` row as abandoned and
    returns it to the newsroom, where someone picks the same story and pays to
    generate it a second time.

    Best effort: a story that cannot be closed is a duplicate risk, not a
    reason to fail a run that has already published.
    """
    try:
        run = await db.get_run(run_id)
        news_id = str((run or {}).get("news_id") or "")
        if news_id and news_id != run_id:
            await db.mark_news_done(news_id)
    except Exception as exc:  # pragma: no cover - bookkeeping only
        logger.warning("Could not close the news item for run %s: %s", run_id, exc)


async def _finish_badly(run_id: str, status: str, text: str) -> None:
    """Record an unhappy ending without letting the recording itself fail.

    Shielded, because this runs INSIDE a cancellation handler. Press Stop
    twice and the second ``task.cancel()`` lands on one of these awaits;
    ``CancelledError`` derives from BaseException, so it sails past the
    ``except Exception`` below and the status write never completes - leaving
    the run recorded 'running' with a trace that stops dead, and needing a
    third Stop to reach the stale-status branch. ``asyncio.shield`` lets the
    write finish; the cancellation is still delivered afterwards.

    Image tokens are folded in here too. A cancelled or failed run never
    reaches another phase boundary, so without this its per-run bucket is
    never drained: the cost is lost from the run's total and the entry stays
    in the accumulator for the life of the process.
    """
    try:
        observability.pop_image_usage(run_id)
    except Exception:  # pragma: no cover - accounting must never block an ending
        pass

    async def _record() -> None:
        await db.set_run_status(run_id, status)
        seq = await db.max_run_seq(run_id)
        await record_event(
            run_id, seq + 1, KIND_ERROR, text=text, data={"status": status}
        )

    try:
        # ONE shield around the whole ending, not one per statement.
        #
        # Shielding each await individually protects only the await itself: a
        # cancellation arriving in the gap BETWEEN two of them still lands, so
        # the status could be written while the terminal event that explains
        # it was not - a run marked cancelled above a trace that stops dead
        # with no reason given. Shielding the coroutine protects the sequence.
        await asyncio.shield(_record())
    except Exception as exc:  # pragma: no cover - shutdown / DB down
        logger.warning("Could not record the end of run %s: %s", run_id, exc)


async def resume_interrupted_run(
    run_id: str, *, requested_by: str = "", slot_held: bool = False
) -> bool:
    """Re-enter an interrupted run at whatever phase it stopped in.

    This works because the orchestrator is a re-entrant phase machine: it reads
    the phase back out of persisted session state and starts that phase again
    from the top. Some work inside the interrupted phase is repeated, but the
    output is correct and the publisher is idempotent, so an interrupted run is
    genuinely recoverable rather than merely restartable.

    Returns:
        True if a resume was started; False if the run is unknown or already
        being driven.
    """
    if run_id in active_run_ids():
        return False
    run = await db.get_run(run_id)
    if run is None:
        return False

    # Not a new carousel: this one's images and reasoning are already spent,
    # so the daily cap does not apply. Only the concurrency cap does.
    #
    # ``slot_held`` means the caller already reserved for this run and is
    # keeping it across work of its own (restart_run rewinds the session
    # first). Re-checking there would be worse than redundant: the slot has to
    # be released to be re-taken, and in that gap another request can claim it
    # - so the refusal would arrive AFTER the session had been rewound.
    if not slot_held:
        await _check_limits(run_id, new_run=False)

    try:
        from app.agent import build_runner
        from google.genai import types

        runner = build_runner()
        message = types.Content(
            role="user",
            parts=[
                types.Part(text="Continue the interrupted run from its current phase.")
            ],
        )
        await db.set_run_status(run_id, db.RUN_STATUS_RUNNING)
        seq = await db.max_run_seq(run_id)
        await record_event(
            run_id, seq + 1, KIND_TERMINAL,
            text=f"Resuming interrupted run at phase '{run.get('phase')}'.",
            data={"resumed_by": requested_by} if requested_by else {},
        )
    except BaseException:
        _release_slot(run_id)
        raise
    task = asyncio.get_running_loop().create_task(
        _drive_run(run_id, runner, message), name=f"resume-run-{run_id}"
    )
    _run_tasks[run_id] = task
    task.add_done_callback(_forget_task(run_id, task))
    _release_slot(run_id)  # the task holds the slot now
    return True



async def restart_run(run_id: str, *, requested_by: str = "") -> bool:
    """Re-run a stopped task IN PLACE, with its rework budget reset.

    Not a new run. Everything the task already earned - the researched story,
    the plan, the approved copy, the rendered slides, the reviewer's feedback,
    the whole transcript - lives in this session, and starting a fresh one
    throws all of it away to redo work that was never the problem. When a
    carousel dies rendering its last slide, what you want is that slide again,
    not another twenty minutes of research.

    Two things are reset before re-entering, and only two:

    * ``rework_round`` to 0, because the round cap is usually WHY the run
      stopped; leaving it exhausted would stop it again immediately.
    * ``phase`` back to where the work stopped, because the hard stop writes
      DONE into session state to end the orchestrator's loop.

    Applies to every origin - chat, newsroom, schedule - since none of them
    changes what a session holds.

    Returns:
        True when a restart was started; False if the run is unknown or
        already being driven.
    """
    if run_id in active_run_ids():
        return False
    run = await db.get_run(run_id)
    if run is None:
        return False

    # Ask permission BEFORE changing anything. The rewind resets the rework
    # budget and moves the recorded phase, and doing it first meant a refused
    # re-run still mutated the task: the restart never happened, but a later
    # Resume would re-enter at the rewound phase instead of where the work
    # actually stopped. resume_interrupted_run checks again, which is fine -
    # the check is cheap and idempotent.
    # Reserved here and HELD across the rewind below, then handed to
    # resume_interrupted_run. Releasing it in between opened a window where
    # another request could take the slot, so the refusal landed after the
    # rework budget had already been reset and the phase moved.
    await _check_limits(run_id, new_run=False)

    from app.review.resume import PIPELINE_USER_ID

    # The phase recorded on the run is where it actually stopped; session
    # state may say DONE because that is how the loop was ended.
    phase = str(run.get("phase") or "") or PHASE_GENERATE
    if phase == PHASE_DONE:
        phase = PHASE_REWORK
    try:
        await db.rewind_session_for_restart(
            run_id, settings.app_name, PIPELINE_USER_ID, phase
        )
    except BaseException:
        _release_slot(run_id)
        raise
    logger.info(
        "Restarting run %s in place at phase '%s' (rework budget reset) for %s.",
        run_id,
        phase,
        requested_by or "unknown",
    )
    try:
        return await resume_interrupted_run(
            run_id, requested_by=requested_by, slot_held=True
        )
    except BaseException:
        _release_slot(run_id)
        raise

async def cancel_run(run_id: str) -> bool:
    """Stop whatever this process is running for ``run_id``.

    TWO task registries, because a run is driven from two places: a fresh or
    resumed invocation lives in ``_run_tasks`` here, while the rework that
    follows a rejection lives in ``app.review.resume._resume_tasks``. Checking
    only the first made Stop silently do nothing during a rework - the button
    reported success and the agents kept running.

    Cancelling a resume also has to FINISH the job here. A ``_run_tasks``
    cancellation lands in ``_drive_run``'s handler, which calls
    ``_finish_badly`` and records both the status and a terminal event.
    ``resume_pipeline`` has no such handler - it restores the pending review
    and re-raises - so returning as soon as ``cancel_resume`` said yes left
    ``runs.status`` on 'running' with no terminal event: the timeline simply
    stopped, the console kept a pulsing live task, and because Stop is only
    offered for 'running' the only way out was to press it again.

    Returns False when nothing was running, which the API turns into a 409.
    """
    from app.review.resume import cancel_resume

    # Raise the flag FIRST, before anything is cancelled. Work already inside
    # a worker thread cannot be interrupted - the Instagram publish is the one
    # that matters - so the only way to stop it is for it to ask, and it can
    # only ask about a flag that is already set.
    cancellation.request(run_id)

    task = _run_tasks.get(run_id)
    stopped = False
    if task is None and run_id in _reserved:
        # Reserved but not yet driven: start_run has taken a slot and is still
        # setting the run up (a session write, three DB writes, and for a
        # pasted URL a thirty-second fetch). There is no task to cancel, but
        # the flag above is now raised and _drive_run checks it before doing
        # any work - so Stop is honoured rather than answering "that run is
        # not currently running" about a run the console is showing.
        logger.info("Stop requested for %s while it was still starting.", run_id)
        return True
    if task is not None and not task.done():
        # Cancel once. A second task.cancel() re-raises CancelledError inside
        # the handler that is busy recording the ending, which is how a
        # double-press used to leave the run stuck on 'running'. The flag
        # above is already set, so a repeat press is not lost - it is just not
        # a second interruption.
        if not task.cancelling():
            task.cancel()
        # _drive_run's CancelledError handler records the ending itself.
        stopped = True
    elif cancel_resume(run_id):
        # Nothing else will write the ending for a resume leg, so do it here.
        stopped = True
        await _finish_badly(
            run_id,
            db.RUN_STATUS_CANCELLED,
            "Stopped while reworking. The verdict can be submitted again.",
        )
    if stopped:
        return True

    # Nothing in either registry - but the run may still be RECORDED as
    # running. Both registries are per-process and in-memory, so a restart
    # empties them while the database keeps whatever the run last wrote. That
    # left a phantom: a task pulsing "running" in the console that Stop
    # refused to touch, answering "that run is not currently running" while
    # the page insisted it was.
    #
    # This deployment is single-instance by design (see the run bus and the
    # startup reconcile, which rely on the same fact), so if this process is
    # not driving the run, nothing is. The status is stale, and the honest
    # thing Stop can do is say so.
    run = await db.get_run(run_id)
    if run is None:
        return False
    if str(run.get("status") or "") != db.RUN_STATUS_RUNNING:
        return False

    await db.set_run_status(run_id, db.RUN_STATUS_CANCELLED)
    try:
        seq = await db.max_run_seq(run_id)
        await record_event(
            run_id,
            seq + 1,
            KIND_TERMINAL,
            text=(
                "Stopped. No agent was still running for this task - the "
                "service had restarted since it started - so its status was "
                "corrected rather than a process being cancelled."
            ),
            data={"status": db.RUN_STATUS_CANCELLED, "stale": True},
        )
    except Exception as exc:  # pragma: no cover - the status change is the point
        logger.warning("Could not record the stop event for %s: %s", run_id, exc)
    logger.info("Stopped stale run %s (no in-process task).", run_id)
    return True


async def drain_run_tasks(timeout: float = 10.0) -> None:
    """Give in-flight runs a moment at shutdown, then cancel them.

    Not a drain to completion, deliberately: a generate phase runs for minutes
    and no platform grace period covers that, so waiting only guarantees being
    SIGKILLed mid-write. Cancelling marks the run cancelled and leaves it
    re-enterable, which is a better end state than being killed silently.
    """
    tasks = [t for t in _run_tasks.values() if not t.done()]
    if not tasks:
        return
    logger.info(
        "Shutdown: waiting up to %.0f s for %d in-flight run(s).", timeout, len(tasks)
    )
    _, pending = await asyncio.wait(set(tasks), timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "MAX_CONCURRENT_RUNS",
    "MAX_RUNS_PER_DAY",
    "RUN_TIMEOUT_S",
    "PIPELINE_USER_ID",
    "RunRefused",
    "RunSource",
    "StartedRun",
    "active_run_ids",
    "cancel_run",
    "drain_run_tasks",
    "fetch_url_item",
    "resume_interrupted_run",
    "spawn_run",
    "start_run",
]
