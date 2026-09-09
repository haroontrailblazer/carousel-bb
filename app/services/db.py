"""Postgres access layer for the Carousel Factory (asyncpg).

A single lazily-created connection pool over ``settings.database_url`` backs
the operational tables defined in ``db/schema.sql``:

- ``news_queue``      - fetched news items waiting for a pipeline run
- ``runs``            - one row per pipeline run (phase tracking)
- ``feedback``        - every human verdict + feedback text (learning input)
- ``pending_reviews`` - the paused review invocation (session + call id)

Nothing here touches the network at import time: the pool is created on the
first call that needs it. When ``DATABASE_URL`` is unset every public function
raises a clear ``RuntimeError`` telling the operator what to configure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

# Statuses used in news_queue.status.
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Explicit timeouts (seconds) for every network interaction with Postgres.
_ACQUIRE_TIMEOUT_S = 15.0
_COMMAND_TIMEOUT_S = 30.0

#: How many connections THIS pool may hold.
#:
#: There is a hard budget to share. Supabase's pooler on port 5432 runs in
#: SESSION mode, which holds a server connection for the whole client session
#: and caps the project at a fixed number of clients - 15 here. This process
#: opens connections from two independent places: this asyncpg pool, and the
#: SQLAlchemy engine behind ADK's DatabaseSessionService (see
#: ``app.runtime._ENGINE_KWARGS``). Their maxima ADD.
#:
#: At the old defaults that sum was 10 + (5 + 5) = 20 against a budget of 15,
#: so under load the pooler simply refused:
#:
#:     asyncpg.exceptions.InternalServerError: (EMAXCONNSESSION)
#:     max clients reached in session mode - max clients are limited to
#:     pool_size: 15
#:
#: A refusal there is not a slow query, it is a write that never happens - and
#: the write that happened to lose was ``save_pending_review``, which strands
#: a run at 'review' with no way for any surface to answer it. Raising
#: MAX_CONCURRENT_RUNS made it likelier, not less.
#:
#: Keep ``DB_MAX_CONNECTIONS`` plus ``DB_POOL_SIZE + DB_POOL_MAX_OVERFLOW``
#: comfortably under the pooler's limit, with room left for a psql session.
_MAX_CONNECTIONS = int(os.getenv("DB_MAX_CONNECTIONS", "5"))
_MIN_CONNECTIONS = min(int(os.getenv("DB_MIN_CONNECTIONS", "2")), _MAX_CONNECTIONS)

#: Attempts for the one write whose failure strands a run (see
#: ``save_pending_review``). Three tries over ~4.5 s covers a pooler briefly at
#: capacity and a connection that died idle, without holding the invocation.
#: asyncpg's prepared-statement cache. Zero whenever the DSN points at a
#: transaction-mode pooler, because prepared statements do not survive one.
#: Detected from the port so a URL change is all a mode switch takes;
#: ``DB_STATEMENT_CACHE`` overrides it either way.
def _statement_cache_size(url: Optional[str] = None) -> int:
    override = os.getenv("DB_STATEMENT_CACHE")
    if override is not None:
        return int(override)
    dsn = settings.database_url if url is None else url
    return 0 if ":6543" in (dsn or "") else 100


_STATEMENT_CACHE_SIZE = _statement_cache_size()

_PENDING_REVIEW_ATTEMPTS = int(os.getenv("PENDING_REVIEW_ATTEMPTS", "3"))
_PENDING_REVIEW_BACKOFF_S = float(os.getenv("PENDING_REVIEW_BACKOFF_S", "1.5"))

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


def _dsn() -> str:
    """Return a plain-postgres DSN, or raise if DATABASE_URL is not set.

    ``settings.database_url`` may carry the SQLAlchemy-style
    ``postgresql+asyncpg://`` scheme (used by ADK's DatabaseSessionService
    config); asyncpg itself wants the bare ``postgresql://`` scheme, so the
    ``+asyncpg`` marker is stripped here.
    """
    url = settings.database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Supabase/Postgres "
            "instance (e.g. postgresql+asyncpg://user:pass@host:5432/postgres) "
            "in .env before using app.services.db."
        )
    return url.replace("+asyncpg", "", 1)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup: encode/decode json & jsonb as Python objects."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first use.

    Raises:
        RuntimeError: if ``DATABASE_URL`` is unset (clear operator message).
    """
    global _pool
    if _pool is not None:
        return _pool
    dsn = _dsn()  # raise early, before taking the lock
    async with _pool_lock:
        if _pool is None:
            # min_size=3 rather than 1: Supabase is a remote host, so opening
            # a connection costs a TCP and TLS handshake - roughly a third of a
            # second, which showed up as every first API call after an idle
            # moment being slow. Keeping a few warm removes that from the path
            # a user actually waits on.
            _pool = await asyncpg.create_pool(
                dsn,
                min_size=_MIN_CONNECTIONS,
                max_size=_MAX_CONNECTIONS,
                # Prepared statements cannot survive a TRANSACTION-mode
                # pooler. Supabase serves session mode on 5432 and
                # transaction mode on 6543; the latter hands your connection
                # to someone else between statements, so a statement prepared
                # on one backend is executed on another and every query dies
                # with "prepared statement _asyncpg_stmt_N does not exist".
                #
                # Transaction mode is the answer to the 15-client ceiling that
                # stranded a run here, so switching to 6543 should be a URL
                # change and nothing else. Turning the cache off costs a
                # little planning time per query and makes both modes work.
                statement_cache_size=_STATEMENT_CACHE_SIZE,
                init=_init_connection,
                timeout=_ACQUIRE_TIMEOUT_S,
                command_timeout=_COMMAND_TIMEOUT_S,
            )
    return _pool


async def close_pool() -> None:
    """Close the shared pool (call on process shutdown). Safe if never opened."""
    global _pool
    async with _pool_lock:
        if _pool is not None:
            await _pool.close()
            _pool = None


def url_hash(url: str) -> str:
    """Stable sha256 hex digest of a URL - the news_queue dedupe key."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _item_url_hash(item: dict) -> str:
    """Derive the dedupe hash for a news item dict.

    Prefers an explicit ``url_hash`` key, then the source URL, then the id or
    title so that even URL-less items still dedupe deterministically.
    """
    explicit = item.get("url_hash")
    if explicit:
        return str(explicit)
    basis = (
        item.get("source_url")
        or item.get("url")
        or item.get("id")
        or item.get("title")
        or ""
    )
    if not basis:
        raise ValueError(
            "news item has no source_url/url/id/title to derive a dedupe hash from"
        )
    return url_hash(str(basis))


# ---------------------------------------------------------------------------
# news_queue
# ---------------------------------------------------------------------------


async def enqueue_news(item: dict) -> dict:
    """Insert a news item into ``news_queue``, deduping on its URL hash.

    Args:
        item: a JSON-serializable dict (typically ``NewsItem.model_dump``).
            An ``id`` is assigned if missing; ``url_hash`` may be supplied
            explicitly, otherwise it is derived from source_url/url/id/title.

    Returns:
        ``{"id": str, "url_hash": str, "enqueued": bool}`` - ``enqueued`` is
        False when an item with the same hash already exists (its stored id
        is returned instead).
    """
    payload = dict(item)
    payload.setdefault("id", uuid.uuid4().hex)
    h = _item_url_hash(payload)
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO news_queue (id, url_hash, payload, status)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (url_hash) DO NOTHING
        RETURNING id
        """,
        str(payload["id"]),
        h,
        payload,
        STATUS_QUEUED,
    )
    if row is not None:
        return {"id": row["id"], "url_hash": h, "enqueued": True}
    existing = await pool.fetchrow(
        "SELECT id FROM news_queue WHERE url_hash = $1", h
    )
    return {
        "id": existing["id"] if existing else str(payload["id"]),
        "url_hash": h,
        "enqueued": False,
    }


async def next_queued_news() -> Optional[dict]:
    """Pop the oldest queued news item and mark it ``processing``.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never grab the same
    row.

    Returns:
        The item's payload dict (with ``id`` guaranteed to match the queue
        row), or ``None`` when the queue is empty.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        WITH next AS (
            SELECT id
            FROM news_queue
            WHERE status = $1
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE news_queue q
        SET status = $2
        FROM next
        WHERE q.id = next.id
        RETURNING q.id, q.payload
        """,
        STATUS_QUEUED,
        STATUS_PROCESSING,
    )
    if row is None:
        return None
    payload: dict[str, Any] = dict(row["payload"] or {})
    payload["id"] = row["id"]
    return payload


async def list_queued_news(limit: int = 50) -> list[dict]:
    """Stories waiting to be turned into a carousel, oldest first.

    Oldest first because the queue is a backlog, not a feed: the item that has
    been waiting longest is the one most at risk of going stale.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, payload, created_at
        FROM news_queue
        WHERE status = $1
        ORDER BY created_at ASC
        LIMIT $2
        """,
        STATUS_QUEUED,
        max(1, min(int(limit), 200)),
    )
    items = []
    for row in rows:
        payload = dict(row["payload"] or {})
        items.append(
            {
                "id": row["id"],
                "title": payload.get("title", ""),
                "summary": payload.get("summary", ""),
                "source_name": payload.get("source_name", ""),
                "source_url": payload.get("source_url", ""),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
        )
    return items


async def next_queued_news_by_id(news_id: str) -> Optional[dict]:
    """Claim ONE specific queued item, by id.

    The console lets a person pick a story rather than take whatever is next,
    but the claim still has to be atomic: the ``status = 'queued'`` predicate
    inside the UPDATE means two people clicking the same card produce one
    winner and one ``None``, instead of two runs on the same story.

    Returns:
        The NewsItem payload for the winner, or ``None`` if it was already
        claimed or does not exist.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE news_queue
        SET status = $2
        WHERE id = $1 AND status = $3
        RETURNING id, payload
        """,
        str(news_id),
        STATUS_PROCESSING,
        STATUS_QUEUED,
    )
    if row is None:
        return None
    payload = dict(row["payload"] or {})
    payload["id"] = row["id"]
    return payload


async def claim_news_by_url_hash(url_hash_value: str) -> Optional[str]:
    """Claim a queued story by its URL, if one is waiting for it.

    Pasting a link is the same act as picking the matching card in the
    newsroom, but only the card claimed the row - so a story could be run
    twice: once by whoever pasted the URL, once by whoever clicked it later,
    paying for the same carousel twice.

    Best effort and non-blocking: a URL that is not in the queue is the normal
    case, and returns None.

    Returns:
        The claimed row's id, or None when nothing was queued for that URL.
    """
    pool = await get_pool()
    return await pool.fetchval(
        """
        UPDATE news_queue SET status = $2
        WHERE url_hash = $1 AND status = $3
        RETURNING id
        """,
        str(url_hash_value),
        STATUS_PROCESSING,
        STATUS_QUEUED,
    )


async def release_news_claim(news_id: str) -> bool:
    """Put a claimed story back in the queue.

    The console claims a story the moment someone picks it, which is right -
    two people clicking the same card must not produce two runs. But the claim
    happens before the run is known to be startable, so a refusal (every slot
    busy, the daily cap) left the story at ``processing``: invisible in the
    newsroom, attached to no run, and freed only by the startup sweep.

    Only a row still sitting at ``processing`` is released, so this can never
    resurrect a story whose carousel actually shipped.

    Returns:
        True when a row went back to ``queued``.
    """
    pool = await get_pool()
    updated = await pool.fetchval(
        """
        UPDATE news_queue SET status = $2
        WHERE id = $1 AND status = $3
        RETURNING id
        """,
        str(news_id),
        STATUS_QUEUED,
        STATUS_PROCESSING,
    )
    return updated is not None


async def mark_news_done(id: str, status: str = STATUS_DONE) -> None:
    """Set the final status of a news_queue row (``done`` or ``failed``).

    Args:
        id: the news_queue row id (as returned by enqueue/next_queued_news).
        status: one of ``STATUS_DONE`` / ``STATUS_FAILED`` (free-form allowed
            but stick to the module constants).
    """
    pool = await get_pool()
    await pool.execute(
        "UPDATE news_queue SET status = $2 WHERE id = $1", str(id), status
    )


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
#: Phases from which a run cannot proceed on its own. A row sitting in one of
#: these with no process behind it is an interrupted run, not a running one.
ACTIVE_PHASES = ("generate", "qa", "rework", "publish")

RUN_STATUS_RUNNING = "running"
RUN_STATUS_AWAITING_REVIEW = "awaiting_review"
RUN_STATUS_DONE = "done"
RUN_STATUS_INTERRUPTED = "interrupted"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"


async def create_run(run_id: str, news_id: str) -> None:
    """Insert a run row (phase ``generate``, review_round 0). Idempotent."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO runs (run_id, news_id, phase, review_round)
        VALUES ($1, $2, 'generate', 0)
        ON CONFLICT (run_id) DO UPDATE
        SET news_id = EXCLUDED.news_id, updated_at = now()
        """,
        str(run_id),
        str(news_id),
    )


#: Lifecycle status implied by a phase. The orchestrator only knows about
#: phases, so deriving status here keeps the console's history accurate for
#: EVERY caller - the CLI, the web service and a resumed leg alike - instead of
#: relying on each one to remember to set it.
_PHASE_STATUS = {
    "review": RUN_STATUS_AWAITING_REVIEW,
    "done": RUN_STATUS_DONE,
}


async def update_run_phase(
    run_id: str,
    phase: str,
    review_round: Optional[int] = None,
    status: Optional[str] = None,
) -> None:
    """Record a phase transition for a run (and optionally its review round).

    Also moves ``status`` to match: a run entering ``review`` is awaiting a
    human, one reaching ``done`` is finished, and any other phase means it is
    working. This doubles as the phase-transition heartbeat - ``updated_at``
    moves here too - but transitions are far too infrequent to rely on for
    liveness on their own, which is why ``touch_run`` exists.

    Args:
        run_id: the run to update.
        phase: one of the ``PHASE_*`` constants from ``app.state``.
        review_round: when given, also updates ``runs.review_round``.
        status: overrides the status derived from *phase*. Needed because
            reaching the ``done`` phase does not always mean success: the
            orchestrator also lands there when it gives up, and a run that
            gave up must not be recorded as finished.
    """
    pool = await get_pool()
    status = status or _PHASE_STATUS.get(phase, RUN_STATUS_RUNNING)
    if review_round is None:
        await pool.execute(
            """
            UPDATE runs SET phase = $2, status = $3, updated_at = now()
            WHERE run_id = $1
            """,
            str(run_id),
            phase,
            status,
        )
    else:
        await pool.execute(
            """
            UPDATE runs
            SET phase = $2, status = $3, review_round = $4, updated_at = now()
            WHERE run_id = $1
            """,
            str(run_id),
            phase,
            status,
            int(review_round),
        )


# ---------------------------------------------------------------------------
# pending_reviews - the paused LongRunningFunctionTool call
# ---------------------------------------------------------------------------


async def save_pending_review(
    run_id: str, session_id: str, function_call_id: str
) -> None:
    """Persist the paused review invocation so the review API can resume it.

    Upserts, since a rework loop sends a fresh review mail (and a fresh
    pending function call) for the same run.

    Retried, unlike almost everything else here. Most writes in this module
    are bookkeeping: losing one costs a log line. This one decides whether a
    paused run can ever be answered - the invocation ends either way, so a
    single failed INSERT strands a finished carousel at 'review' with no live
    function call for any surface to address. The failures worth retrying are
    exactly the transient ones seen in practice: the pooler refusing a client
    under load, or a connection that died while the pipeline spent four
    minutes rendering slides without touching the database.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, _PENDING_REVIEW_ATTEMPTS + 1):
        try:
            pool = await get_pool()
            await pool.execute(
                """
                INSERT INTO pending_reviews (run_id, session_id, function_call_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (run_id) DO UPDATE
                SET session_id = EXCLUDED.session_id,
                    function_call_id = EXCLUDED.function_call_id,
                    created_at = now()
                """,
                str(run_id),
                str(session_id),
                str(function_call_id),
            )
            return
        except Exception as exc:
            last = exc
            if attempt == _PENDING_REVIEW_ATTEMPTS:
                break
            delay = _PENDING_REVIEW_BACKOFF_S * attempt
            logger.warning(
                "save_pending_review failed for run %s (attempt %d/%d: %s); "
                "retrying in %.1fs.",
                run_id,
                attempt,
                _PENDING_REVIEW_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last


async def load_pending_review(run_id: str) -> Optional[dict]:
    """Load the pending review for a run.

    Returns:
        ``{"run_id", "session_id", "function_call_id", "created_at"}`` (with
        ``created_at`` as an ISO-8601 string) or ``None`` if nothing pends.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT run_id, session_id, function_call_id, created_at
        FROM pending_reviews
        WHERE run_id = $1
        """,
        str(run_id),
    )
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "function_call_id": row["function_call_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def claim_pending_review(run_id: str) -> Optional[dict]:
    """Atomically consume the pending review, returning it to ONE caller.

    ``load_pending_review`` + ``clear_pending_review`` is a check-then-act
    race: two reviewers deciding the same run at the same moment (the Telegram
    link and the web button) would both see a row, both delete it, and both
    resume the same paused invocation - publishing the carousel twice.

    A single ``DELETE ... RETURNING`` collapses the check and the act into one
    statement. Postgres serialises the two deletes, so the loser's ``RETURNING``
    yields no row and it gets ``None``. That is what makes a double decision
    impossible, and it is why callers deciding a verdict must use this rather
    than the load/clear pair.

    Args:
        run_id: The run whose pending review is being decided.

    Returns:
        ``{"run_id", "session_id", "function_call_id"}`` for the single winning
        caller, or ``None`` if nothing was pending (unknown run, already
        decided, or a concurrent caller won the race).
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        DELETE FROM pending_reviews
        WHERE run_id = $1
        RETURNING run_id, session_id, function_call_id
        """,
        str(run_id),
    )
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "function_call_id": row["function_call_id"],
    }


async def clear_pending_review(run_id: str) -> None:
    """Delete the pending review row once the run has been resumed.

    Idempotent and non-returning: this is the dispatcher's post-resume tidy-up
    (``review_dispatcher`` clears the row again on the resumed leg). To DECIDE a
    verdict use ``claim_pending_review`` instead - this function cannot tell a
    winner from a loser."""
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM pending_reviews WHERE run_id = $1", str(run_id)
    )


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


async def record_verdict(
    run_id: str,
    status: str,
    feedback: str,
    targets: Optional[list[str]] = None,
    news_title: str = "",
    decided_by: str = "",
    source: str = "",
) -> int:
    """Store a human verdict in the ``feedback`` table.

    Args:
        run_id: the run the verdict belongs to.
        status: ``"approved"`` or ``"rejected"``.
        feedback: reviewer text (may be empty on approve).
        targets: optional rework targets (agent names) the router derived.
        news_title: title of the news item, for readable feedback history.
        decided_by: who decided - an email for a web verdict, empty for a
            Telegram link (those are capability URLs and carry no identity).
        source: which channel decided - ``"telegram"``, ``"web"`` or ``"api"``.
            With two surfaces able to approve the same run, the verdict alone
            no longer says where it came from.

    Returns:
        The new feedback row id.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO feedback
            (run_id, verdict, feedback, targets, news_title, decided_by, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        str(run_id),
        status,
        feedback or "",
        list(targets or []),
        news_title or "",
        decided_by or None,
        source or None,
    )
    return int(row["id"])


# ---------------------------------------------------------------------------
# runs - metadata the web console lists, filters and recovers on
# ---------------------------------------------------------------------------


def _as_timestamptz(value: Any) -> Any:
    """Coerce an ISO-8601 string to a datetime for asyncpg binding.

    asyncpg binds parameters by their INFERRED type, so writing
    ``$1::timestamptz`` in the SQL does not make it accept a string - it makes
    asyncpg demand a datetime and reject anything else. The API layer receives
    ISO strings from query parameters, so the conversion happens here rather
    than at every call site.

    A trailing ``Z`` is normalised because ``datetime.fromisoformat`` did not
    accept it before Python 3.11 and callers copy timestamps around freely.
    """
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


async def set_run_meta(
    run_id: str,
    title: str = "",
    source: str = "",
    requested_by: str = "",
) -> None:
    """Attach console metadata to a run.

    Only non-empty values are written, so a later call cannot blank out a title
    that an earlier one set. Runs started from the ADK dev UI never call this
    and keep NULLs - every reader must tolerate that.
    """
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE runs
        SET title        = COALESCE(NULLIF($2, ''), title),
            source       = COALESCE(NULLIF($3, ''), source),
            requested_by = COALESCE(NULLIF($4, ''), requested_by),
            updated_at   = now()
        WHERE run_id = $1
        """,
        str(run_id),
        title or "",
        source or "",
        requested_by or "",
    )


async def rename_run(run_id: str, title: str) -> bool:
    """Set a run's title to exactly what a person typed.

    Separate from ``set_run_meta`` for two reasons, both of which would be
    bugs if this were folded into it.

    ``set_run_meta`` coalesces an empty value away so that a later automated
    call cannot blank a title an earlier one set. That is right for the
    pipeline and wrong for a person: clearing the name is a thing a user is
    allowed to do, and it means "go back to the generated title".

    And it deliberately does NOT move ``updated_at``. That column is how
    liveness is inferred - ``interrupted_run_candidates`` reads it to decide
    which runs died in a redeploy - so touching it here would make renaming a
    dead task look like the task waking up, and startup recovery would leave
    it stranded for another idle window.

    Returns False when there is no such run, so the route can 404 rather than
    reporting success for a rename that went nowhere.
    """
    pool = await get_pool()
    result = await pool.execute(
        # `title_locked` is set here and nowhere else: this is the only path a
        # human names a task through, and it is what stops the pipeline
        # renaming it back on the next rework. See 004_title_lock.sql.
        "UPDATE runs SET title = NULLIF($2, ''), title_locked = true "
        "WHERE run_id = $1",
        str(run_id),
        title.strip(),
    )
    # asyncpg returns the command tag, e.g. "UPDATE 1".
    return result.rsplit(" ", 1)[-1] != "0"


async def set_auto_title(run_id: str, title: str) -> bool:
    """Let the pipeline name a task, unless a person already has.

    A topic run is created with the prompt as its title, because at that
    moment nothing better exists. Once the planner has read the story it has a
    real name for the carousel, and that is what belongs in the sidebar and in
    the Telegram notice.

    Guarded on ``title_locked`` rather than on the text, because a rework
    re-runs the planner: without the guard a name the user chose would be
    replaced, silently, part-way through their own task.

    Returns True when the title was actually written, so a caller can log the
    difference between "renamed it" and "left the human's name alone".
    """
    clean = (title or "").strip()
    if not clean:
        return False
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE runs SET title = $2 WHERE run_id = $1 AND NOT title_locked",
        str(run_id),
        clean[:150],
    )
    return result.rsplit(" ", 1)[-1] != "0"


async def set_run_status(run_id: str, status: str) -> None:
    """Set the run's lifecycle status.

    Distinct from ``phase``: phase mirrors the orchestrator state machine,
    status is what an operator sees. A run killed by a redeploy keeps
    ``phase='generate'`` but becomes ``status='interrupted'`` - still
    re-enterable, which is what the Resume action uses.
    """
    pool = await get_pool()
    await pool.execute(
        "UPDATE runs SET status = $2, updated_at = now() WHERE run_id = $1",
        str(run_id),
        status,
    )


async def touch_run(run_id: str) -> None:
    """Mark a run as still alive, right now.

    ``runs.updated_at`` otherwise only moves on a PHASE transition, and a
    single phase can easily run for fifteen minutes - template_design renders
    each slide with an image model, first_page_visual downloads and re-encodes
    video. Anything that infers liveness from ``updated_at`` without this
    heartbeat will conclude that a perfectly healthy run has died. That is not
    hypothetical: it is what made startup recovery reclaim a live run twice
    during development.

    Called on a timer by the task driving the run, so the gap between
    heartbeats is bounded by the timer rather than by the pipeline's work.
    """
    pool = await get_pool()
    await pool.execute(
        "UPDATE runs SET updated_at = now() WHERE run_id = $1", str(run_id)
    )


def _run_row(row: Any) -> dict:
    """Normalise a runs row into JSON-friendly types."""
    data = dict(row)
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        data[key] = value.isoformat() if value else None
    return data


async def get_run(run_id: str) -> Optional[dict]:
    """Load one run row, or ``None`` if there is no such run."""
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM runs WHERE run_id = $1", str(run_id))
    return _run_row(row) if row is not None else None


async def list_runs(
    limit: int = 25,
    before: Optional[str] = None,
    phase: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List runs newest first, for the console's history screen.

    Args:
        limit: page size (clamped to 1..100).
        before: ISO timestamp cursor - return runs created strictly earlier.
        phase: optional exact phase filter.
        status: optional exact status filter.
    """
    pool = await get_pool()
    clauses: list[str] = []
    args: list[Any] = []

    if before:
        args.append(_as_timestamptz(before))
        clauses.append(f"created_at < ${len(args)}")
    if phase:
        args.append(phase)
        clauses.append(f"phase = ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"status = ${len(args)}")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(max(1, min(int(limit), 100)))
    rows = await pool.fetch(
        f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ${len(args)}",
        *args,
    )
    return [_run_row(r) for r in rows]


#: How many recent runs the sidebar dots look at.
#:
#: A window, not the whole table: "something failed" is worth a dot for as
#: long as it is recent work, but a red dot lit forever by a failure from
#: three months ago is just a red dot people stop seeing. Anything running or
#: awaiting review is by definition inside this window - those are the newest
#: rows there are.
PULSE_WINDOW = 50


async def pulse_counts() -> dict:
    """The four numbers behind the sidebar dots, in ONE round trip.

    Its own query rather than counting the history list in the browser: the
    dots are the first thing on screen after a reload and the history list is
    fifty full rows with payloads, which is a second or two against a remote
    database. This is four integers and can be polled hard without anyone
    noticing.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        f"""
        WITH recent AS (
            SELECT status FROM runs ORDER BY created_at DESC LIMIT {PULSE_WINDOW}
        )
        SELECT
            count(*) FILTER (WHERE status = $1) AS running,
            count(*) FILTER (WHERE status = $2) AS awaiting_review,
            count(*) FILTER (WHERE status IN ($3, $4)) AS stopped,
            (SELECT count(*) FROM news_queue WHERE status = $5) AS queued
        FROM recent
        """,
        RUN_STATUS_RUNNING,
        RUN_STATUS_AWAITING_REVIEW,
        RUN_STATUS_FAILED,
        RUN_STATUS_INTERRUPTED,
        STATUS_QUEUED,
    )
    return {
        "running": int(row["running"] or 0),
        "awaiting_review": int(row["awaiting_review"] or 0),
        "stopped": int(row["stopped"] or 0),
        "queued": int(row["queued"] or 0),
    }


async def count_runs_since(since: str) -> int:
    """How many runs were created since an ISO timestamp.

    Backs the daily spend cap: a carousel costs real image and reasoning
    credits, so the console refuses to start one past the limit.
    """
    pool = await get_pool()
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM runs WHERE created_at >= $1", _as_timestamptz(since)
        )
    )


async def interrupted_run_candidates(min_idle_seconds: int = 180) -> list[dict]:
    """Runs stuck in a phase that cannot advance without a process driving it.

    Called at startup. Because exactly one instance ever runs this pipeline, a
    run still sitting in an active phase when the process boots was killed -
    no other process could still own it.

    ``min_idle_seconds`` guards that assumption instead of merely trusting it.
    On a genuine cold boot nothing has been touched for far longer than two
    minutes, so real recovery is unaffected; but if this is ever called while
    another process IS driving a run - a second instance, or a developer
    running a script - the live run has almost certainly written a phase
    transition recently and is left alone. Without the guard, that mistake
    silently marks a healthy run interrupted and frees its queued news item
    for someone else to pick up.

    The guard only means something because a live run HEARTBEATS via
    :func:`touch_run`. Without that, ``updated_at`` moves only on phase
    transitions and a run busy inside one long phase looks idle - so the
    threshold must stay comfortably above the heartbeat interval in
    ``app.runs.service``, not merely above a typical phase duration.

    Args:
        min_idle_seconds: leave alone any run touched more recently than this.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM runs
        WHERE phase = ANY($1::text[])
          AND status <> ALL($2::text[])
          AND updated_at < now() - make_interval(secs => $3)
        ORDER BY created_at DESC
        """,
        list(ACTIVE_PHASES),
        [RUN_STATUS_INTERRUPTED, RUN_STATUS_CANCELLED, RUN_STATUS_FAILED],
        float(max(0, min_idle_seconds)),
    )
    return [_run_row(r) for r in rows]



async def rewind_session_for_restart(
    run_id: str, app_name: str, user_id: str, phase: str
) -> bool:
    """Clear a run's rework budget and put it back into ``phase``.

    What "Re-run" needs in order to mean "try the rework again" rather than
    "start a brand new task":

    * ``rework_round`` AND ``qa_round`` back to 0, so whichever cap stopped
      the run is not still exhausted the instant it restarts.
    * ``phase`` back to where the work actually stopped. The rework hard stop
      writes DONE into session state because that is what ends the
      orchestrator's loop - so without this the resumed invocation would read
      DONE, emit a summary and stop again immediately.

    Written with one UPDATE against the session row rather than through
    ``DatabaseSessionService``: the same read path the console already uses
    (see ``_session_state``), for the same reason - the supported API loads
    the event transcript and costs seconds per call.

    Returns:
        True when a session row was updated.
    """
    pool = await get_pool()
    updated = await pool.fetchval(
        """
        UPDATE sessions
        SET state = jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                COALESCE(state, '{}'::jsonb),
                                '{rework_round}', '0'::jsonb, true
                            ),
                            '{qa_round}', '0'::jsonb, true
                        ),
                        '{phase}', to_jsonb($4::text), true
                    ),
            update_time = now()
        WHERE app_name = $1 AND user_id = $2 AND id = $3
        RETURNING id
        """,
        str(app_name),
        str(user_id),
        str(run_id),
        str(phase),
    )
    return updated is not None


async def set_session_verdict(
    run_id: str, app_name: str, user_id: str, verdict: dict
) -> bool:
    """Write a verdict straight into a run's session state.

    For the case where the carousel is ready but the pipeline is NOT paused on
    ``await_human_review`` - the review notification failed, so the dispatcher
    never got to pause. There is no function call to answer, but the
    orchestrator's review phase already knows how to route a verdict it finds
    in state ("recorded by an earlier invocation that stopped before
    routing"), so putting one there and re-entering the run is a supported
    path rather than a trick.

    This is also the single-winner gate for that path, which is why the WHERE
    clause tests for an absent verdict. Both Telegram and the console can
    decide the same run, and the paused path serialises them with a
    ``DELETE ... RETURNING`` on ``pending_reviews``; a halted run has no
    pending row to claim, so the exclusivity has to come from here. An
    unconditional UPDATE returned True to every concurrent caller, and each
    then re-entered the run - two invocations driving one session, with
    last-write-wins deciding which verdict survived.

    Postgres serialises the two statements, so exactly one sees a NULL
    ``review_verdict`` and updates; the loser gets False and resumes nothing.
    The orchestrator clears the key on every transition out of review, so a
    later round is free to write again.

    Returns:
        True when this caller wrote the verdict; False when the row is
        missing, or when another caller had already recorded one.
    """
    import json as _json

    pool = await get_pool()
    updated = await pool.fetchval(
        """
        UPDATE sessions
        SET state = jsonb_set(
                        COALESCE(state, '{}'::jsonb),
                        '{review_verdict}', $4::jsonb, true
                    ),
            update_time = now()
        WHERE app_name = $1 AND user_id = $2 AND id = $3
          AND COALESCE(state -> 'review_verdict', 'null'::jsonb) = 'null'::jsonb
        RETURNING id
        """,
        str(app_name),
        str(user_id),
        str(run_id),
        _json.dumps(verdict),
    )
    return updated is not None

# ---------------------------------------------------------------------------
# run_events - the distilled timeline the console replays
# ---------------------------------------------------------------------------
async def append_run_event(
    run_id: str,
    seq: int,
    kind: str,
    author: str = "",
    text: str = "",
    data: Optional[dict] = None,
) -> None:
    """Persist one timeline event.

    ``ON CONFLICT DO NOTHING`` on (run_id, seq): a retried append must not
    raise, because losing the run over a duplicate log line would be absurd.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO run_events (run_id, seq, kind, author, text, data)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (run_id, seq) DO NOTHING
        """,
        str(run_id),
        int(seq),
        kind,
        author or "",
        text or "",
        dict(data or {}),
    )


async def load_run_events(
    run_id: str, after: int = 0, limit: int = 2000
) -> list[dict]:
    """Replay a run's timeline from a cursor, oldest first.

    ``after`` is the SSE ``Last-Event-ID``: the client says what it already
    has, and gets everything since. That is what makes a reconnect lose
    nothing and duplicate nothing.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT seq, kind, author, text, data, created_at
        FROM run_events
        WHERE run_id = $1 AND seq > $2
        ORDER BY seq ASC
        LIMIT $3
        """,
        str(run_id),
        int(after),
        max(1, min(int(limit), 5000)),
    )
    out = []
    for r in rows:
        item = dict(r)
        item["created_at"] = (
            item["created_at"].isoformat() if item["created_at"] else None
        )
        out.append(item)
    return out


async def max_run_seq(run_id: str) -> int:
    """Highest sequence number recorded for a run (0 when it has none).

    A resumed leg continues the numbering rather than restarting at 1, so the
    console's cursor stays monotonic across the review pause.
    """
    pool = await get_pool()
    value = await pool.fetchval(
        "SELECT max(seq) FROM run_events WHERE run_id = $1", str(run_id)
    )
    return int(value or 0)


# ---------------------------------------------------------------------------
# app_config - settings that must change without a redeploy
# ---------------------------------------------------------------------------
async def get_config(key: str, default: Any = None) -> Any:
    """Read a runtime-editable setting."""
    pool = await get_pool()
    value = await pool.fetchval("SELECT value FROM app_config WHERE key = $1", key)
    return default if value is None else value


async def set_config(key: str, value: Any) -> None:
    """Write a runtime-editable setting."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO app_config (key, value)
        VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        value,
    )


# ---------------------------------------------------------------------------
# app_users - the authorization allowlist
# ---------------------------------------------------------------------------
async def get_app_user(email: str) -> Optional[dict]:
    """Look up an allowlisted user, or ``None`` if not allowed.

    A disabled row is deliberately returned rather than hidden, so the caller
    can say "your access was revoked" instead of "no such user".
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT email, role, disabled FROM app_users WHERE lower(email) = lower($1)",
        str(email),
    )
    return dict(row) if row is not None else None


async def list_app_users() -> list[dict]:
    """Every allowlisted user, for an admin view."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT email, role, disabled, created_at FROM app_users ORDER BY email"
    )
    out = []
    for r in rows:
        item = dict(r)
        item["created_at"] = (
            item["created_at"].isoformat() if item["created_at"] else None
        )
        out.append(item)
    return out


async def seed_app_users(emails: list[str], role: str = "admin") -> int:
    """Seed the allowlist from configuration, but ONLY while it is empty.

    Runs at startup so a fresh database is not locked out of its own console.
    It deliberately does nothing once anyone has been added: otherwise an env
    var left over from bootstrap would silently resurrect a user who had been
    removed on purpose.

    Returns:
        How many users were inserted (0 when the table was already populated).
    """
    cleaned = [e.strip() for e in emails if e and e.strip()]
    if not cleaned:
        return 0
    pool = await get_pool()
    existing = int(await pool.fetchval("SELECT count(*) FROM app_users"))
    if existing:
        return 0
    await pool.executemany(
        """
        INSERT INTO app_users (email, role) VALUES (lower($1), $2)
        ON CONFLICT (email) DO NOTHING
        """,
        [(e, role) for e in cleaned],
    )
    return len(cleaned)


# ---------------------------------------------------------------------------
# ADK's own event log - the authoritative transcript
# ---------------------------------------------------------------------------
async def count_adk_events(app_name: str, user_id: str, session_id: str) -> int:
    """How many events ADK recorded for a session."""
    pool = await get_pool()
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM public.events "
            "WHERE app_name = $1 AND user_id = $2 AND session_id = $3",
            app_name,
            user_id,
            session_id,
        )
        or 0
    )


async def load_adk_events(
    app_name: str,
    user_id: str,
    session_id: str,
    after: int = 0,
    limit: int = 2000,
) -> list[dict]:
    """Read a session's raw ADK events, oldest first.

    This is the same log the /dev inspector renders, so a trace built from it
    matches what ADK shows rather than approximating it. It also means EVERY
    run has a trace - including ones started from the CLI or the dev UI, which
    never write to our own ``run_events`` table.

    Read with one query rather than through ``DatabaseSessionService``, whose
    ORM path measured at ~1.7 s PER EVENT over this link (58 s for 34 events).

    Args:
        after: how many leading events the caller already has. Events are
            append-only and ordered by timestamp, so an offset is a stable
            cursor.

    Returns:
        ``[{"seq", "event_data", "created_at"}]`` with seq starting at
        ``after + 1``.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, timestamp, event_data
        FROM public.events
        WHERE app_name = $1 AND user_id = $2 AND session_id = $3
        ORDER BY timestamp ASC, id ASC
        OFFSET $4 LIMIT $5
        """,
        app_name,
        user_id,
        session_id,
        max(0, int(after)),
        max(1, min(int(limit), 5000)),
    )
    out: list[dict] = []
    for index, row in enumerate(rows, start=max(0, int(after)) + 1):
        data = row["event_data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        out.append(
            {
                "seq": index,
                "event_data": data or {},
                "created_at": row["timestamp"].isoformat() if row["timestamp"] else None,
            }
        )
    return out


async def delete_run(app_name: str, user_id: str, run_id: str) -> dict:
    """Erase a run and everything attached to it.

    Deletes across five tables plus ADK's own session and transcript, because a
    half-deleted run is worse than no delete: an orphaned ``pending_reviews``
    row keeps a dead run "waiting for review" forever, and an orphaned session
    keeps its artifacts addressable. (That exact orphan already exists in this
    database, from a session deleted through the ADK inspector while its runs
    row stayed behind.)

    A news item still marked ``processing`` is returned to the queue - the
    story was never turned into anything, so it should be pickable again rather
    than silently lost.

    Callers must refuse to delete a RUNNING run; this function does not check,
    because it is also the cleanup path for runs whose process is already gone.

    Returns:
        Row counts per table, for the caller to report.
    """
    pool = await get_pool()
    counts: dict[str, int] = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT news_id FROM runs WHERE run_id = $1", str(run_id)
            )
            news_id = row["news_id"] if row else None

            for table, sql in (
                ("run_events", "DELETE FROM run_events WHERE run_id = $1"),
                ("pending_reviews", "DELETE FROM pending_reviews WHERE run_id = $1"),
                ("feedback", "DELETE FROM feedback WHERE run_id = $1"),
                ("runs", "DELETE FROM runs WHERE run_id = $1"),
            ):
                result = await conn.execute(sql, str(run_id))
                counts[table] = int(result.rsplit(" ", 1)[-1] or 0)

            # Keyed on session_id rather than run_id, which is exactly how it
            # got missed the first time: an audit of every column named
            # run_id/session_id is what surfaced it.
            try:
                result = await conn.execute(
                    "DELETE FROM memory_entries "
                    "WHERE app_name = $1 AND user_id = $2 AND session_id = $3",
                    app_name,
                    user_id,
                    str(run_id),
                )
                counts["memory_entries"] = int(result.rsplit(" ", 1)[-1] or 0)
            except Exception:
                counts["memory_entries"] = 0

            # ADK's own tables, addressed the way it addresses them.
            for table, sql in (
                (
                    "events",
                    "DELETE FROM public.events "
                    "WHERE app_name = $1 AND user_id = $2 AND session_id = $3",
                ),
                (
                    "sessions",
                    "DELETE FROM public.sessions "
                    "WHERE app_name = $1 AND user_id = $2 AND id = $3",
                ),
            ):
                try:
                    result = await conn.execute(sql, app_name, user_id, str(run_id))
                    counts[table] = int(result.rsplit(" ", 1)[-1] or 0)
                except Exception:
                    counts[table] = 0

            if news_id:
                result = await conn.execute(
                    "UPDATE news_queue SET status = $2 "
                    "WHERE id = $1 AND status = $3",
                    str(news_id),
                    STATUS_QUEUED,
                    STATUS_PROCESSING,
                )
                counts["requeued"] = int(result.rsplit(" ", 1)[-1] or 0)

    return counts


async def news_payload(news_id: str) -> Optional[dict]:
    """The stored NewsItem for a queue id, whatever its status.

    Used to re-run a task from the same story - unlike ``next_queued_news_by_id``
    this does not claim the row, because a re-run of a failed task should work
    even though that row is already marked processing or done.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, payload FROM news_queue WHERE id = $1", str(news_id)
    )
    if row is None:
        return None
    payload = dict(row["payload"] or {})
    payload["id"] = row["id"]
    return payload


# ---------------------------------------------------------------------------
# instagram_accounts - the connected publishing targets
#
# A table rather than an app_config key because these are plural, each with
# its own encrypted token and its own 60-day expiry. The token is written and
# read as CIPHERTEXT here; app/services/instagram_accounts.py owns the
# encryption, so nothing in this module ever holds the plaintext.
# ---------------------------------------------------------------------------
_INSTAGRAM_COLUMNS = """
    id, ig_user_id, username, name, avatar_key, auth_kind, token_enc,
    token_expires_at, is_default, disabled, connected_by, connected_at,
    last_refreshed_at
"""


async def list_instagram_accounts() -> list[dict]:
    """Every connected account, ciphertext included."""
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT {_INSTAGRAM_COLUMNS} FROM instagram_accounts ORDER BY username"
    )
    return [dict(row) for row in rows]


async def upsert_instagram_account(
    *,
    id: str,
    ig_user_id: str,
    username: str,
    name: str,
    avatar_key: str,
    auth_kind: str,
    token_enc: str,
    token_expires_at: Any,
    is_default: bool,
    connected_by: str,
) -> None:
    """Insert an account, or replace the token of one already connected.

    The conflict target is ``ig_user_id``, not ``id``: reconnecting an account
    is the ordinary way to replace a lapsed token, and it must update the row
    that already exists rather than create a second one for the same handle.
    ``connected_at`` is deliberately left alone on update - it records when the
    account first joined, and ``last_refreshed_at`` covers the rest.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO instagram_accounts (
            id, ig_user_id, username, name, avatar_key, auth_kind,
            token_enc, token_expires_at, is_default, connected_by
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (ig_user_id) DO UPDATE SET
            username          = EXCLUDED.username,
            name              = EXCLUDED.name,
            avatar_key        = COALESCE(NULLIF(EXCLUDED.avatar_key, ''),
                                         instagram_accounts.avatar_key),
            auth_kind         = EXCLUDED.auth_kind,
            token_enc         = EXCLUDED.token_enc,
            token_expires_at  = EXCLUDED.token_expires_at,
            disabled          = false,
            connected_by      = EXCLUDED.connected_by,
            last_refreshed_at = now()
        """,
        str(id),
        str(ig_user_id),
        str(username),
        str(name),
        str(avatar_key),
        str(auth_kind),
        str(token_enc),
        token_expires_at,
        bool(is_default),
        str(connected_by),
    )


async def update_instagram_token(
    *, id: str, token_enc: str, token_expires_at: Any
) -> None:
    """Store a refreshed token."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE instagram_accounts
        SET token_enc = $2, token_expires_at = $3, last_refreshed_at = now()
        WHERE id = $1
        """,
        str(id),
        str(token_enc),
        token_expires_at,
    )


async def set_default_instagram_account(id: str) -> None:
    """Make one account the default, in one statement.

    A single UPDATE rather than a clear-then-set pair: two statements can be
    interrupted between them, and the state in the gap is "no default at all",
    which makes every run refuse to start.
    """
    pool = await get_pool()
    await pool.execute(
        "UPDATE instagram_accounts SET is_default = (id = $1)", str(id)
    )


async def delete_instagram_account(id: str) -> None:
    """Forget an account. Runs that targeted it keep their account_id."""
    pool = await get_pool()
    await pool.execute("DELETE FROM instagram_accounts WHERE id = $1", str(id))


async def set_run_account(run_id: str, account_id: str) -> None:
    """Record which Instagram account a run was created for."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE runs SET account_id = $2, updated_at = now() WHERE run_id = $1",
        str(run_id),
        str(account_id) or None,
    )


async def get_run_account_id(run_id: str) -> str:
    """The account a run was created for, or ``""``.

    Read from the run row rather than session state because the resume and
    recovery paths rebuild a run from the database, and the brand identity has
    to be restored before the first slide is re-rendered.
    """
    pool = await get_pool()
    value = await pool.fetchval(
        "SELECT account_id FROM runs WHERE run_id = $1", str(run_id)
    )
    return str(value or "")
