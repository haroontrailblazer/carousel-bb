"""The console's run API: start, watch, review, recover.

Two design notes that shape most of this file.

**Session state is the source of truth for content; Postgres for lifecycle.**
The plan, the bundle and the QA report live in the ADK session because that is
what the pipeline resumes from. The ``runs`` table mirrors phase and status so
the history screen can list and filter without loading a session per row.

**Streaming must never pass through anything that buffers.** The SSE endpoint
returns a generator and the auth layer is raw ASGI, deliberately - a
``BaseHTTPMiddleware`` anywhere in the chain would hold the whole response and
the live trace would appear only once the run had already finished.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import runtime
from app.config import settings
from app.review.verdict import REJECT_QUESTION, submit_verdict
from app.runs.bus import BUS
from app.runs.stream import load_trace, load_trace_with_summary
from app.runs.service import (
    RunRefused,
    active_run_ids,
    cancel_run,
    restart_run,
    resume_interrupted_run,
    start_run,
)
from app.services import db, instagram_accounts
from app.state import (
    K_REVIEW_NOTICE_FAILED,
    AGENT_CTA,
    AGENT_FEEDBACK_ROUTER,
    AGENT_FIRST_PAGE_VISUAL,
    AGENT_LEARNER,
    AGENT_PHRASING,
    AGENT_PLANNER,
    AGENT_PUBLISHER,
    AGENT_RESEARCH,
    AGENT_REVIEW_DISPATCHER,
    AGENT_STITCH_VERIFY,
    AGENT_TEMPLATE_DESIGN,
    K_BODY_SLIDES,
    K_BUNDLE,
    K_COVER,
    K_CTA_SLIDE,
    K_NEWS_ITEM,
    K_PLAN,
    K_PHASE,
    K_PUBLISH_RESULT,
    K_QA_REPORT,
    K_REVIEW_ROUND,
    K_REWORK_ROUND,
    K_TOKEN_USAGE,
    K_VERDICT,
    PHASE_DONE,
    PHASE_GENERATE,
    PHASE_PUBLISH,
    PHASE_QA,
    PHASE_REVIEW,
    PHASE_REWORK,
    REWORKABLE_AGENTS,
)
from web_api.auth import Identity
from web_api.deps import current_identity

logger = logging.getLogger(__name__)

router = APIRouter()

#: Fixed pipeline user id; sessions are addressed by
#: (app_name, user_id, session_id).
try:  # pragma: no cover - trivial import wiring
    from fetcher.fetch_news import PIPELINE_USER_ID
except Exception:  # pragma: no cover - env dependent
    PIPELINE_USER_ID = "pipeline"

#: How long a signed artifact URL lasts in the browser.
#:
#: Much shorter than the 24h default used for publishing - Instagram's fetcher
#: needs a long window, a page being looked at right now does not, and a URL
#: that leaks out of a screenshot should stop working the same hour.
ARTIFACT_URL_TTL_S = 3600

#: Heartbeat interval for the SSE stream. Render's proxy closes idle
#: connections, and this pipeline routinely produces no events for minutes at a
#: time while an image model works, so a silent stream is normal and must be
#: kept alive explicitly.
SSE_KEEPALIVE_S = 15.0

#: A no-op SSE comment. Sent first to establish the response before any
#: slow work, and again as the keepalive.
_SSE_COMMENT = ": connected" + chr(10) + chr(10)
_SSE_KEEPALIVE = ": keepalive" + chr(10) + chr(10)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class StartRunRequest(BaseModel):
    source: str = Field("topic", pattern="^(topic|url|queue)$")
    topic: str = ""
    url: str = ""
    news_id: str = ""
    #: Which connected Instagram account to generate and publish for. Empty
    #: selects the default. Chosen BEFORE the run because the account's handle
    #: and profile picture are composited into the slide artwork.
    account_id: str = ""


class VerdictRequest(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    feedback: str = ""
    #: Which cover to publish when the run produced both a clip and a still.
    cover: Optional[str] = Field(None, pattern="^(video|image)$")
    #: Agents the reviewer POINTED AT, from ``state.REWORKABLE_AGENTS`` (or any
    #: alias the feedback router understands, such as "cover" or "design").
    #:
    #: Left empty, routing works exactly as it always has: the router LLM reads
    #: the feedback and decides. Named, they are honoured exactly - the console
    #: lets a reviewer pick an agent or click the slide they are unhappy with,
    #: and being overruled by a model would defeat the point of pointing.
    #:
    #: Capped at the number of reworkable agents, since naming more than all of
    #: them is either a mistake or an attempt to make the list unbounded.
    targets: list[str] = Field(default_factory=list, max_length=6)


class EnqueueRequest(BaseModel):
    url: str


class RenameRunRequest(BaseModel):
    #: Capped rather than unbounded: this is rendered in a sidebar row, sent
    #: in Telegram notifications and stored per run. An empty string is
    #: allowed and means "drop my name, use the generated one again".
    title: str = Field("", max_length=200)


# ---------------------------------------------------------------------------
# Session reading
# ---------------------------------------------------------------------------
async def _session_state(run_id: str) -> dict:
    """Read a run's ADK session state in a single query.

    ``DatabaseSessionService.get_session`` is the supported way to do this and
    it is far too slow here. It loads the event transcript unless told not to
    (measured: 58 SECONDS for a run with 34 events), and even with
    ``num_recent_events=0`` it still costs about three seconds, because it also
    reads app-level and user-level state and merges them.

    Every one of those is a separate round trip, and from here a round trip to
    Supabase is 0.5-1.5 s. The console only needs the run's own state - the
    bundle, the QA report, the verdict - which is one row and one column.

    Falls back to the supported API if the direct read fails for any reason,
    so an ADK schema change degrades performance rather than breaking the page.
    """
    try:
        pool = await db.get_pool()
        row = await pool.fetchrow(
            "SELECT state FROM public.sessions "
            "WHERE app_name = $1 AND user_id = $2 AND id = $3",
            settings.app_name,
            PIPELINE_USER_ID,
            run_id,
        )
        if row is not None:
            state = row["state"]
            if isinstance(state, str):
                state = json.loads(state)
            return dict(state or {})
        return {}
    except Exception as exc:
        logger.warning(
            "Direct session-state read failed for %s (%s); falling back to "
            "the ADK session service.",
            run_id,
            exc,
        )

    from google.adk.sessions.base_session_service import GetSessionConfig

    try:
        session = await runtime.session_service().get_session(
            app_name=settings.app_name,
            user_id=PIPELINE_USER_ID,
            session_id=run_id,
            config=GetSessionConfig(num_recent_events=0),
        )
    except Exception as exc:
        logger.warning("Could not load session for run %s: %s", run_id, exc)
        return {}
    return dict(session.state) if session is not None else {}


async def _trace_length(run_id: str) -> int:
    """How many timeline frames a run has, matching what the stream replays.

    Read from ADK's transcript so the client's cursor lines up with the
    history the events endpoint will send.
    """
    try:
        count = await db.count_adk_events(settings.app_name, PIPELINE_USER_ID, run_id)
        if count:
            return count
    except Exception:
        pass
    try:
        return await db.max_run_seq(run_id)
    except Exception:
        return 0


def _news_summary(state: dict) -> dict:
    news = state.get(K_NEWS_ITEM) or {}
    return {
        "title": news.get("title", ""),
        "summary": news.get("summary", ""),
        "source_name": news.get("source_name", ""),
        "source_url": news.get("source_url", ""),
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    payload: StartRunRequest, identity: Identity = Depends(current_identity)
) -> dict:
    """Start a run and return immediately.

    202, not 200: the pipeline runs for minutes after this responds. The client
    gets a run id and watches the event stream.
    """
    news: Optional[dict] = None
    if payload.source == "queue":
        if not payload.news_id:
            raise HTTPException(400, {"code": "no_news_id", "message": "Pick an item."})
        item = await db.next_queued_news_by_id(payload.news_id)
        if item is None:
            raise HTTPException(
                404,
                {
                    "code": "queue_item_gone",
                    "message": "That item is no longer queued - it may already "
                               "have been picked up.",
                },
            )
        news = item

    try:
        started = await start_run(
            source=payload.source,  # type: ignore[arg-type]
            topic=payload.topic,
            url=payload.url,
            news=news,
            requested_by=identity.email,
            account_id=payload.account_id,
        )
    except RunRefused as exc:
        # Picking a story from the newsroom CLAIMED it (queued -> processing)
        # above, before the run was known to be startable. A refusal here would
        # otherwise strand it: gone from the newsroom, attached to no run, and
        # recoverable only by the startup sweep. Put it back so the next click
        # can have it.
        await _return_to_queue(payload)
        # 409 for "not now" (every slot is busy, the cap is reached); 400 for
        # "not like that" (bad URL, empty topic). The SPA renders them
        # differently, so the distinction has to reach it.
        code = 409 if exc.code in ("too_many_active_runs", "daily_limit_reached") else 400
        raise HTTPException(code, {"code": exc.code, "message": exc.detail}) from exc
    except BaseException:
        await _return_to_queue(payload)
        raise

    return {
        "run_id": started.run_id,
        "news_id": started.news_id,
        "title": started.title,
        "phase": PHASE_GENERATE,
        "status": db.RUN_STATUS_RUNNING,
    }


async def _return_to_queue(payload: StartRunRequest) -> None:
    """Undo the newsroom claim when the run never started."""
    if payload.source != "queue" or not payload.news_id:
        return
    try:
        await db.release_news_claim(payload.news_id)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Could not return %s to the queue: %s", payload.news_id, exc)


@router.get("/runs")
async def list_runs(
    limit: int = Query(25, ge=1, le=100),
    before: Optional[str] = None,
    phase: Optional[str] = None,
    run_status: Optional[str] = Query(None, alias="status"),
    _identity: Identity = Depends(current_identity),
) -> dict:
    """Run history, newest first.

    Everyone signed in sees every run - this is a shared workspace, and the
    Telegram channel already broadcasts to the whole team anyway.
    """
    rows = await db.list_runs(
        limit=limit, before=before, phase=phase, status=run_status
    )
    live = active_run_ids()
    for row in rows:
        row["is_live"] = row["run_id"] in live
    return {
        "items": rows,
        "next_cursor": rows[-1]["created_at"] if len(rows) == limit else None,
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, _identity: Identity = Depends(current_identity)
) -> dict:
    """Everything the run detail screen needs, in one request."""
    # Four independent lookups, each a round trip to a database ~600 ms away.
    # Sequentially that is the whole page's latency; concurrently it is one
    # round trip. return_exceptions so one failing lookup degrades a field
    # rather than the response.
    run, state, pending, last_seq = await asyncio.gather(
        db.get_run(run_id),
        _session_state(run_id),
        db.load_pending_review(run_id),
        _trace_length(run_id),
        return_exceptions=True,
    )
    if isinstance(run, BaseException) or run is None:
        raise HTTPException(404, {"code": "no_such_run", "message": "Unknown run."})
    if isinstance(state, BaseException):
        logger.warning("Session state unavailable for %s: %s", run_id, state)
        state = {}
    if isinstance(pending, BaseException):
        pending = None
    if isinstance(last_seq, BaseException):
        last_seq = 0
    bundle = state.get(K_BUNDLE) or {}
    qa = state.get(K_QA_REPORT) or {}
    verdict = state.get(K_VERDICT) or {}
    publish = state.get(K_PUBLISH_RESULT) or {}

    return {
        **run,
        "is_live": run_id in active_run_ids(),
        "news": _news_summary(state),
        "phase_state": state.get(K_PHASE, run.get("phase")),
        "rework_round": state.get(K_REWORK_ROUND, 0),
        "review_round": state.get(K_REVIEW_ROUND, run.get("review_round", 0)),
        "caption": bundle.get("caption", ""),
        "slide_count": len(bundle.get("slides") or []),
        "qa": {"passed": qa.get("passed"), "issues": qa.get("issues", [])},
        "verdict": verdict or None,
        "publish": {
            "media_id": publish.get("media_id"),
            "permalink": publish.get("permalink"),
            "error": publish.get("message") if publish.get("status") == "error" else None,
        },
        "token_usage": state.get(K_TOKEN_USAGE, {}),
        "last_seq": last_seq,
        # The authoritative "is a decision still wanted?" flag. NOT monotonic:
        # a failed resume restores the pending row, so a run can go pending ->
        # not pending -> pending again. Any UI that reads this once will get
        # stuck showing the wrong thing.
        "pending_review": pending is not None,
        # The carousel is ready but the reviewer could not be told. The console
        # can still decide it - this only changes what the page SAYS, so the
        # reviewer knows Telegram never got the message.
        "notice_failed": bool(state.get(K_REVIEW_NOTICE_FAILED)),
    }


@router.get("/runs/{run_id}/artifacts")
async def run_artifacts(
    run_id: str, _identity: Identity = Depends(current_identity)
) -> dict:
    """Freshly signed URLs for every piece of the carousel.

    Signed on every request rather than stored: these expire, and a run
    reviewed the morning after would otherwise show a grid of broken images
    with no explanation.
    """
    # Independent round trips, so overlap them: one to Postgres for the
    # bundle, one to object storage for the version map.
    state, versions = await asyncio.gather(
        _session_state(run_id),
        runtime.artifact_service().latest_versions_async(
            settings.app_name, PIPELINE_USER_ID, run_id
        ),
        return_exceptions=True,
    )
    if isinstance(state, BaseException):
        state = {}
    if isinstance(versions, BaseException):
        logger.warning("Could not list artifact versions for %s: %s", run_id, versions)
        versions = {}

    bundle = state.get(K_BUNDLE) or {}
    cover = bundle.get("cover") or state.get(K_COVER) or {}
    slides = bundle.get("slides") or state.get(K_BODY_SLIDES) or []
    cta = bundle.get("cta") or state.get(K_CTA_SLIDE) or {}
    if not bundle and not cover and not slides and not cta:
        raise HTTPException(
            404,
            {
                "code": "no_artifacts_yet",
                "message": "This run has not rendered an asset yet.",
            },
        )

    service = runtime.artifact_service()

    # Discover every artifact's newest version in ONE listing, then sign from
    # that. Letting public_url() resolve the version itself costs a separate
    # round trip to object storage per file - eight of them for one carousel.
    async def sign(filename: str) -> Optional[dict]:
        if not filename:
            return None
        try:
            url = await service.public_url_async(
                app_name=settings.app_name,
                user_id=PIPELINE_USER_ID,
                session_id=run_id,
                filename=filename,
                version=versions.get(filename),
                expires_in=ARTIFACT_URL_TTL_S,
            )
        except Exception as exc:
            logger.warning("Could not sign %s for run %s: %s", filename, run_id, exc)
            return {"filename": filename, "url": None, "error": str(exc)}
        return {"filename": filename, "url": url}

    poster, video, cta_img = await asyncio.gather(
        sign(cover.get("poster_artifact", "")),
        sign(cover.get("video_artifact", "")),
        sign(cta.get("artifact", "")),
    )
    signed_slides = await asyncio.gather(
        *(sign(s.get("artifact", "")) for s in slides)
    )

    return {
        "run_id": run_id,
        "expires_in": ARTIFACT_URL_TTL_S,
        "caption": bundle.get("caption", ""),
        "complete": bool(bundle),
        "expected_count": int((state.get(K_PLAN) or {}).get("slide_count") or 0),
        "cover": {
            "poster": poster,
            "video": video,
            # No clip could be sourced, so the "cover" is a still. The viewer
            # must not render a <video> element for it.
            "is_still": bool(cover.get("used_fallback_image")),
            "duration_s": cover.get("duration_s", 0),
        },
        "slides": [
            {**(signed or {}), "index": slide.get("index")}
            for slide, signed in zip(slides, signed_slides)
        ],
        "cta": {**(cta_img or {}), "cta_type": cta.get("cta_type"),
                "link_url": cta.get("link_url", "")},
        "ordered": bundle.get("ordered_artifacts", []),
    }


# ---------------------------------------------------------------------------
# Live events
# ---------------------------------------------------------------------------
def _sse(event: str, data: dict, seq: Optional[int] = None) -> str:
    """Format one SSE frame.

    The ``id:`` line is what makes reconnection free: EventSource replays it as
    Last-Event-ID, so the server knows exactly what the client already has and
    can resume without a gap or a duplicate.
    """
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


@router.get("/runs/{run_id}/trace")
async def run_trace(
    run_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=5000),
    _identity: Identity = Depends(current_identity),
) -> dict:
    """A run's timeline as plain JSON, with no streaming involved.

    The events endpoint is nicer when it works, but SSE does not survive every
    network path: Cloudflare quick tunnels buffer the response body, and since
    an event stream never ends, nothing is ever delivered - the browser holds
    an open connection that produces no bytes while the same request returns
    all its frames locally. Corporate proxies do the same thing.

    So history comes from here, which is an ordinary request that any proxy
    handles, and the stream is used only to append live events on top. A trace
    that renders everywhere beats a trace that renders elegantly on some
    networks and not at all on others.
    """
    try:
        frames, summary = await load_trace_with_summary(run_id, after=after, limit=limit)
    except Exception as exc:
        logger.warning("Could not load the trace for %s: %s", run_id, exc)
        frames, summary = [], {"tokens": None, "ms": None, "agents": []}
    return {"run_id": run_id, "after": after, "items": frames, "summary": summary}


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    _identity: Identity = Depends(current_identity),
) -> StreamingResponse:
    """Stream a run's timeline: history first, then live.

    The subscribe-then-replay order is load-bearing. Subscribing to the bus
    BEFORE reading persisted rows closes the window where an event fires
    between the read and the subscribe - in the other order that event is lost
    forever, and the client has no way to know.
    """
    cursor = after
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def stream() -> AsyncIterator[str]:
        async with BUS.subscribe(run_id) as queue:
            # Flush a comment frame BEFORE any database work.
            #
            # Reading the transcript takes seconds against a distant database,
            # and until the first byte arrives a proxy sees a connection that
            # has produced nothing - Cloudflare gives up and the browser gets
            # an empty stream rather than a slow one. One comment establishes
            # the response so the rest can take its time.
            yield _SSE_COMMENT

            emitted = cursor
            try:
                # ADK's own transcript, not just our distilled table: it
                # records every run whichever surface started it, so a task
                # begun from the CLI or the dev UI still has a trace, and what
                # is shown matches the /dev inspector rather than
                # approximating it.
                history = await load_trace(run_id, after=cursor)
            except Exception as exc:
                logger.warning("Could not replay run %s: %s", run_id, exc)
                history = []
            for index, row in enumerate(history):
                emitted = max(emitted, int(row["seq"]))
                yield _sse("run", dict(row), seq=row["seq"])
                # Hand control back periodically so a long replay streams into
                # the page instead of arriving as one block at the end.
                if index % 10 == 9:
                    await asyncio.sleep(0)

            # The client now has everything the database knows about.
            yield _sse("synced", {"last_seq": emitted})

            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=SSE_KEEPALIVE_S
                    )
                except asyncio.TimeoutError:
                    # A comment frame: keeps proxies from closing a stream that
                    # is legitimately silent while an image model works.
                    yield ": keepalive\n\n"
                    continue

                # Renumber live events onto the end of the replayed history.
                #
                # History is numbered by position in ADK's transcript, while
                # the bus numbers from our own run_events counter, and the two
                # do not line up. Appending keeps the stream monotonic, which
                # is what the SSE cursor depends on.
                #
                # The renumbering is for ORDER ONLY. Each frame also carries
                # its own `id`, which survives it, and that is what the browser
                # deduplicates on - because a renumbered seq is a guess about
                # where the next poll will place the same event, and a wrong
                # guess either hides a real frame or renders one twice (the
                # terminal line was the reliable victim).
                if event.kind == "gap":
                    yield _sse("run", event.to_dict(), seq=emitted)
                    continue
                emitted += 1
                frame = event.to_dict()
                frame["seq"] = emitted
                yield _sse("run", frame, seq=emitted)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Render's proxy buffers responses without this, which turns a
            # live trace into one burst at the end.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Decisions and recovery
# ---------------------------------------------------------------------------
async def _apply_cover_choice(run_id: str, choice: Optional[str]) -> Optional[str]:
    """Put the reviewer's cover choice into the bundle the publisher reads.

    The publisher signs ``bundle.ordered_artifacts`` in order and Instagram
    takes the first entry as the cover, so choosing between the clip and the
    still means rewriting that first entry - there is no separate "cover"
    field for it to consult.

    Written straight to the session row rather than through an ADK event: this
    happens between the verdict and the resume, when no invocation is running,
    and appending an event to a session mid-resume is how you get two writers
    racing on the same state.

    Returns:
        The filename that will be published as the cover, or None when the run
        offered no choice.
    """
    if choice not in ("video", "image"):
        return None

    state = await _session_state(run_id)
    bundle = dict(state.get(K_BUNDLE) or {})
    cover = dict(bundle.get("cover") or {})
    video = str(cover.get("video_artifact") or "")
    poster = str(cover.get("poster_artifact") or "")
    wanted = video if choice == "video" else poster
    if not wanted:
        return None

    ordered = list(bundle.get("ordered_artifacts") or [])
    # Drop whichever cover is currently first, then put the chosen one there.
    ordered = [f for f in ordered if f not in (video, poster)]
    bundle["ordered_artifacts"] = [wanted, *ordered]
    bundle["cover"] = {**cover, "published_artifact": wanted}
    state[K_BUNDLE] = bundle

    pool = await db.get_pool()
    await pool.execute(
        "UPDATE public.sessions SET state = $4, update_time = now() "
        "WHERE app_name = $1 AND user_id = $2 AND id = $3",
        settings.app_name,
        PIPELINE_USER_ID,
        run_id,
        json.dumps(state, default=str),
    )
    logger.info("Run %s will publish %r as its cover.", run_id, wanted)
    return wanted


@router.post("/runs/{run_id}/verdict")
async def post_verdict(
    run_id: str,
    payload: VerdictRequest,
    identity: Identity = Depends(current_identity),
) -> dict:
    """Approve or reject, through the same code path as the Telegram links.

    The 409 is the interesting case: it means someone else decided this run
    first - almost always the same person, from their phone. It is a normal
    outcome, not an error, and the UI shows the decision rather than a failure.
    """
    # The choice has to land BEFORE the verdict, because submitting the
    # verdict resumes the pipeline immediately - write it afterwards and the
    # publisher may already have read the old bundle.
    chosen_cover = None
    if payload.status == "approved":
        try:
            chosen_cover = await _apply_cover_choice(run_id, payload.cover)
        except Exception as exc:
            logger.exception("Could not apply the cover choice for %s.", run_id)
            raise HTTPException(
                500,
                {
                    "code": "cover_choice_failed",
                    "message": f"Could not record which cover to publish ({exc}).",
                },
            ) from exc

    outcome = await submit_verdict(
        run_id,
        payload.status,
        payload.feedback,
        reviewer=identity.email,
        source="web",
        targets=payload.targets,
    )
    if outcome.ok:
        return {
            "result": "accepted",
            "run_id": run_id,
            "status": outcome.status,
            "cover": chosen_cover,
        }

    codes = {
        "not_pending": 409,
        # The decision was recorded; only the automatic restart did not
        # happen (a run cap, or a leg already in flight). 409 so the console
        # branches on the code and offers Resume, rather than telling the
        # reviewer their verdict failed - it did not.
        "not_started": 409,
        "feedback_required": 400,
        "invalid_status": 400,
        "incomplete": 500,
        "db_error": 503,
    }
    raise HTTPException(
        codes.get(outcome.result, 500),
        {
            "code": outcome.result,
            "message": outcome.detail or "No review is pending for this run.",
            "reject_question": REJECT_QUESTION,
        },
    )


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str, identity: Identity = Depends(current_identity)
) -> dict:
    """Re-enter an interrupted run at the phase it stopped in."""
    try:
        started = await resume_interrupted_run(run_id, requested_by=identity.email)
    except RunRefused as exc:
        raise HTTPException(409, {"code": exc.code, "message": exc.detail}) from exc
    if not started:
        raise HTTPException(
            409,
            {
                "code": "not_resumable",
                "message": "That run is unknown or already running.",
            },
        )
    return {"result": "resuming", "run_id": run_id}


@router.post("/runs/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
async def rerun(
    run_id: str, identity: Identity = Depends(current_identity)
) -> dict:
    """Re-run a stopped task IN PLACE, with its rework budget reset.

    This used to start a brand new task from the same story. That threw away
    everything the run had already earned - the researched story, the plan,
    the approved copy, the slides that DID render, the reviewer's feedback and
    the whole transcript - to redo work that was never what failed. When a
    carousel dies rendering its last slide, the useful action is that slide
    again, not another twenty minutes of research and another entry in the
    task list for the same story.

    So it now reuses the session: ``rework_round`` back to 0 (the round cap is
    usually why it stopped) and the phase rewound to where the work actually
    stopped, then the same run is re-entered. The returned ``run_id`` is the
    one that was passed in.

    Resume and Re-run are still different: Resume continues with the budget as
    it stands, Re-run gives the task a fresh budget to spend.
    """
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(404, {"code": "no_such_run", "message": "Unknown task."})

    try:
        started = await restart_run(run_id, requested_by=identity.email)
    except RunRefused as exc:
        code = 409 if exc.code in ("too_many_active_runs", "daily_limit_reached") else 400
        raise HTTPException(code, {"code": exc.code, "message": exc.detail}) from exc

    if not started:
        raise HTTPException(
            409,
            {
                "code": "run_is_active",
                "message": "That task is already running.",
            },
        )

    logger.info("Re-ran %s in place for %s.", run_id, identity.email)
    return {"result": "restarting", "run_id": run_id}


@router.delete("/runs/{run_id}")
async def delete_run(
    run_id: str, identity: Identity = Depends(current_identity)
) -> dict:
    """Erase a finished task, its trace and its media.

    ``db.delete_run`` deliberately does not check the run's status - it is also
    the cleanup path for runs whose process is already gone - so the refusal
    lives here, where there is a person to answer. A running task must be
    stopped first: deleting the row out from under a live driver leaves it
    writing events for a run that no longer exists.
    """
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(404, {"code": "no_such_run", "message": "Unknown task."})

    if run_id in active_run_ids() or (
        str(run.get("status") or "") == db.RUN_STATUS_RUNNING
    ):
        raise HTTPException(
            409,
            {
                "code": "run_is_active",
                "message": "Stop this task before deleting it.",
            },
        )

    counts = await db.delete_run(settings.app_name, PIPELINE_USER_ID, run_id)
    logger.info("Run %s deleted by %s: %s", run_id, identity.email, counts)
    return {"result": "deleted", "run_id": run_id, "deleted": counts}


@router.patch("/runs/{run_id}")
async def rename_run(
    run_id: str,
    payload: RenameRunRequest,
    identity: Identity = Depends(current_identity),
) -> dict:
    """Rename a task.

    PATCH rather than PUT: this changes one field and leaves every other one
    alone, which is exactly the distinction the two methods carry. A PUT here
    would imply the body is the whole run.

    Unlike delete, this is allowed while the task is running. A name is not
    part of the pipeline's state - nothing reads it to decide what to do next -
    so renaming a live task cannot disturb it, and the moment you most want to
    label a task is usually while you are watching it work.

    An empty title is a deliberate outcome, not a validation failure: it
    clears the custom name and lets the generated one show through again.
    """
    if not await db.rename_run(run_id, payload.title):
        raise HTTPException(404, {"code": "no_such_run", "message": "Unknown task."})

    run = await db.get_run(run_id)
    logger.info(
        "Run %s renamed by %s to %r", run_id, identity.email, payload.title.strip()
    )
    return {"result": "renamed", "run_id": run_id, "title": (run or {}).get("title")}


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str, _identity: Identity = Depends(current_identity)) -> dict:
    """Stop a run this process is driving."""
    if not await cancel_run(run_id):
        raise HTTPException(
            409,
            {"code": "not_running", "message": "That run is not currently running."},
        )
    return {"result": "cancelling", "run_id": run_id}


# ---------------------------------------------------------------------------
# Queue and metadata
# ---------------------------------------------------------------------------
@router.get("/queue")
async def list_queue(
    limit: int = Query(50, ge=1, le=200),
    _identity: Identity = Depends(current_identity),
) -> dict:
    """Stories the scheduler has fetched but nobody has turned into a carousel.

    ``fetching`` rides along so the console can show a live dot while the
    sources are being polled - from the cron tick as well as from a manual
    check. It is deliberately NOT ``scheduler_state()["running"]``, which says
    whether the timer is alive, not whether it is doing anything.
    """
    return {
        "items": await db.list_queued_news(limit=limit),
        "fetching": _fetching_now(),
    }


@router.post("/queue", status_code=status.HTTP_201_CREATED)
async def enqueue(
    payload: EnqueueRequest, _identity: Identity = Depends(current_identity)
) -> dict:
    """Add an article URL to the queue without running it now."""
    from app.runs.service import fetch_url_item

    try:
        item = await fetch_url_item(payload.url)
    except RunRefused as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.detail}) from exc
    return await db.enqueue_news(item)


@router.get("/pulse")
async def pulse(_identity: Identity = Depends(current_identity)) -> dict:
    """Everything the sidebar dots need, in one small response.

    Separate from ``/runs`` and ``/queue`` on purpose. Those return fifty rows
    with payloads each and take a second or two against a remote database,
    which is why the dots used to appear late after a reload and lag behind
    what the pipeline was doing. This is five values, so the console can ask
    for it on a short timer and after every action.
    """
    counts = await db.pulse_counts()
    return {**counts, "fetching": _fetching_now()}


@router.get("/meta")
async def meta(_identity: Identity = Depends(current_identity)) -> dict:
    """Agent and phase vocabulary, so the UI cannot drift from the pipeline.

    The frontend needs these names to group a trace and draw the phase rail.
    Hard-coding them in TypeScript means a rename here produces blank rows in
    the UI with no error anywhere; serving them lets the client assert instead.
    """
    return {
        "agents": [
            AGENT_RESEARCH, AGENT_PLANNER, AGENT_FIRST_PAGE_VISUAL,
            AGENT_PHRASING, AGENT_TEMPLATE_DESIGN, AGENT_CTA,
            AGENT_STITCH_VERIFY, AGENT_REVIEW_DISPATCHER,
            AGENT_FEEDBACK_ROUTER, AGENT_PUBLISHER, AGENT_LEARNER,
        ],
        "reworkable_agents": list(REWORKABLE_AGENTS),
        "phases": [
            PHASE_GENERATE, PHASE_QA, PHASE_REVIEW,
            PHASE_REWORK, PHASE_PUBLISH, PHASE_DONE,
        ],
        "statuses": [
            db.RUN_STATUS_RUNNING, db.RUN_STATUS_AWAITING_REVIEW,
            db.RUN_STATUS_DONE, db.RUN_STATUS_INTERRUPTED,
            db.RUN_STATUS_FAILED, db.RUN_STATUS_CANCELLED,
        ],
        "reject_question": REJECT_QUESTION,
        "max_slides": settings.max_carousel_slides,
        # Was "are the IG_* env vars set". Now: is there an account that
        # could actually be published to right now - which also goes false
        # when the only connected account's token has lapsed.
        "publish_configured": instagram_accounts.configured(),
        "accounts": instagram_accounts.listing(),
    }


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------
class ScheduleRequest(BaseModel):
    enabled: bool = True
    fetch_cron: str = Field("0 * * * *", min_length=9, max_length=100)


@router.get("/schedule")
async def get_schedule(_identity: Identity = Depends(current_identity)) -> dict:
    """The automatic news-fetch cadence.

    Fetching only - the scheduler never starts a run. Polling feeds is free;
    turning a story into a carousel costs credits, so that stays a human
    decision.
    """
    from app.scheduler import load_schedule, scheduler_state

    schedule = await load_schedule()
    # scheduler_state() reports what is RUNNING; the config reports what was
    # asked for. They disagree when APScheduler is missing or the cron failed
    # to parse, and that disagreement is exactly what an operator needs to see.
    return {**schedule, **scheduler_state(), "starts_runs": False}


@router.put("/schedule")
async def put_schedule(
    payload: ScheduleRequest, identity: Identity = Depends(current_identity)
) -> dict:
    """Change the cadence, applied immediately without a restart.

    Stored in ``app_config`` rather than an env var precisely so this works:
    ``settings`` is a frozen dataclass read once at import, so anything kept
    there would need a redeploy to change.
    """
    from app.scheduler import save_schedule

    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(payload.fetch_cron)
    except ImportError:
        pass  # validated again when the scheduler starts
    except Exception as exc:
        raise HTTPException(
            400,
            {
                "code": "invalid_cron",
                "message": f"{payload.fetch_cron!r} is not a valid cron expression: {exc}",
            },
        ) from exc

    saved = await save_schedule(payload.model_dump())
    logger.info("Schedule updated by %s: %s", identity.email, saved)
    return saved


#: Strong references to manual fetch tasks (asyncio keeps only weak ones).
_fetch_tasks: set = set()


def _fetching_now() -> bool:
    """True while any feed check is running, however it was triggered.

    Two sources because they cover different windows: the scheduler's counter
    is set once ``run_fetch_once`` is actually executing, while a task created
    here is pending for a tick before that. Checking both means a manual check
    reports itself immediately rather than blinking on a moment later.
    """
    from app.scheduler import fetch_in_progress

    return fetch_in_progress() or any(not t.done() for t in _fetch_tasks)


@router.post("/schedule/run-now", status_code=status.HTTP_202_ACCEPTED)
async def fetch_now(_identity: Identity = Depends(current_identity)) -> dict:
    """Poll the sources now, in the background.

    202 and not the result, because polling every feed and deduping the
    results against the queue takes a couple of minutes against a remote
    database - far longer than any proxy will hold a request open. Awaiting it
    here returned a 502 from Cloudflare even though the fetch itself had
    succeeded, which is the worst of both worlds: the work happened and the
    user was told it failed.

    The caller refreshes the queue afterwards to see what arrived.
    """
    from app.scheduler import run_fetch_once

    # Covers the cron tick too, not just a second click here: starting a fetch
    # on top of one already running only ever ended in the advisory lock
    # dropping it, while the console said "checking your feeds".
    if _fetching_now():
        return {"status": "already_running"}

    task = asyncio.get_running_loop().create_task(run_fetch_once(), name="fetch-now")
    _fetch_tasks.add(task)
    task.add_done_callback(_fetch_tasks.discard)
    return {"status": "started"}


__all__ = ["router"]
