"""Postgres-backed ADK memory service + feedback store for the Carousel Factory.

``PostgresMemoryService`` implements the installed google-adk 2.7.0
``BaseMemoryService`` ABC (see
``.venv/Lib/site-packages/google/adk/memory/base_memory_service.py``):

* ``add_session_to_memory(session)`` - stores a compact text digest of the
  session's events (one row per text-bearing event) in the ``memory_entries``
  table, replacing any digest previously stored for the same session.
* ``add_events_to_memory(...)`` - optional delta ingestion (implemented here,
  deduplicated by event id).
* ``search_memory(app_name=..., user_id=..., query=...)`` - simple ILIKE
  keyword search returning the installed ``SearchMemoryResponse`` /
  ``MemoryEntry`` types. Matching rows from the ``feedback`` table are also
  surfaced so past reviewer feedback is searchable memory (the feedback table
  is app-global - it carries no app/user scope columns).

On top of the ADK surface it exposes the feedback persistence used by the
Learner agent and the planner's "recent feedback" context:

* ``store_feedback(record: FeedbackRecord)`` - inserts into ``feedback``.
* ``recent_feedback(limit=20)`` - newest-first ``list[FeedbackRecord]``.

The asyncpg pool is created lazily on first use - importing this module never
opens a network connection. All pool/statement operations carry explicit
timeouts.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

import asyncpg
from google.genai import types
from typing_extensions import override

from google.adk.memory import BaseMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry

from app.config import settings
from app.schemas import FeedbackRecord

if TYPE_CHECKING:  # pragma: no cover - typing only, mirrors the installed ABC
    from google.adk.events.event import Event
    from google.adk.sessions.session import Session

_UNKNOWN_SESSION_ID = "__unknown_session_id__"
_MAX_DIGEST_CHARS = 2000
_MAX_QUERY_WORDS = 8
_SEARCH_LIMIT = 50
_FEEDBACK_SEARCH_LIMIT = 20
_CONNECT_TIMEOUT_S = 30.0
_COMMAND_TIMEOUT_S = 60.0

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id          BIGSERIAL PRIMARY KEY,
    app_name    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    event_id    TEXT NOT NULL DEFAULT '',
    author      TEXT,
    role        TEXT,
    text_content TEXT NOT NULL,
    event_ts    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_entries_scope_idx
    ON memory_entries (app_name, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_entries_event_uq
    ON memory_entries (app_name, user_id, session_id, event_id);
-- Mirrors db/schema.sql's feedback block EXACTLY. Both are
-- CREATE TABLE IF NOT EXISTS, so on a fresh database whichever runs first
-- wins; if the two disagree the surviving shape depends on boot order. They
-- previously disagreed (serial vs BIGSERIAL, feedback_created_at_idx vs
-- idx_feedback_created_at). Change one, change the other.
CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    feedback    TEXT NOT NULL DEFAULT '',
    targets     JSONB NOT NULL DEFAULT '[]'::jsonb,
    news_title  TEXT NOT NULL DEFAULT '',
    decided_by  TEXT,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_feedback_run_id
    ON feedback (run_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created_at
    ON feedback (created_at DESC);
"""


def _normalize_dsn(url: str) -> str:
    """Convert a SQLAlchemy-style URL to a plain asyncpg DSN.

    ``postgresql+asyncpg://...`` (the shape stored in ``DATABASE_URL`` for
    ADK's ``DatabaseSessionService``) becomes ``postgresql://...``; plain
    ``postgresql://`` / ``postgres://`` URLs pass through unchanged.

    Raises:
        ValueError: if the URL is empty (DATABASE_URL not configured).
    """
    if not url:
        raise ValueError(
            "settings.database_url is empty - set DATABASE_URL in .env before "
            "using PostgresMemoryService."
        )
    return re.sub(r"^(postgres(?:ql)?)\+[A-Za-z0-9_]+://", r"\1://", url, count=1)


def _extract_words_lower(text: str) -> list[str]:
    """Extract unique lowercase keywords from *text* (order-preserving)."""
    seen: dict[str, None] = {}
    for word in re.findall(r"\w+", text, re.UNICODE):
        seen.setdefault(word.lower(), None)
    return list(seen)


def _like_pattern(word: str) -> str:
    """Build a ``%word%`` ILIKE pattern with LIKE metacharacters escaped."""
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _format_timestamp(timestamp: Optional[float]) -> Optional[str]:
    """Render an event's epoch timestamp as ISO 8601 (matches ADK's helper)."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).isoformat()


def _event_digest(event: "Event") -> str:
    """Compact one event into a single searchable text line.

    Joins the text parts, collapses whitespace and truncates so memory rows
    stay small ("compact text digest" per the file contract).
    """
    if not event.content or not event.content.parts:
        return ""
    text = " ".join(part.text for part in event.content.parts if part.text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_DIGEST_CHARS]


def _decode_targets(raw: Any) -> list[str]:
    """Defensively decode the jsonb ``targets`` column to ``list[str]``."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup: transparent jsonb <-> Python codec."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class PostgresMemoryService(BaseMemoryService):
    """ADK memory service backed by Postgres (Supabase) via asyncpg.

    Keyword (ILIKE) search over compact per-event text digests - no
    embeddings. Also owns the ``feedback`` table used by the Learner agent
    and the planner's recent-feedback context.

    The connection pool is created lazily on first use; constructing the
    service performs no I/O, so ``adk web`` can import the module without a
    database present.
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
    ) -> None:
        """Create the service (no connection is opened here).

        Args:
            database_url: Override for ``settings.database_url``. Accepts
                plain ``postgresql://`` / ``postgres://`` DSNs as well as
                SQLAlchemy-style ``postgresql+asyncpg://`` URLs.
            min_pool_size: Minimum pool connections.
            max_pool_size: Maximum pool connections.
        """
        self._database_url = database_url or settings.database_url
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: Optional[asyncpg.Pool] = None
        self._pool_lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        """Return the shared pool, creating it (and the schema) on first use."""
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                dsn = _normalize_dsn(self._database_url)
                pool = await asyncpg.create_pool(
                    dsn,
                    min_size=self._min_pool_size,
                    max_size=self._max_pool_size,
                    timeout=_CONNECT_TIMEOUT_S,
                    command_timeout=_COMMAND_TIMEOUT_S,
                    init=_init_connection,
                )
                async with pool.acquire() as conn:
                    await conn.execute(_SCHEMA_DDL, timeout=_COMMAND_TIMEOUT_S)
                self._pool = pool
        return self._pool

    async def close(self) -> None:
        """Close the connection pool (safe to call when never connected)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # BaseMemoryService interface (google-adk 2.7.0)
    # ------------------------------------------------------------------
    @override
    async def add_session_to_memory(self, session: "Session") -> None:
        """Store a compact text digest of the session's events.

        One row per text-bearing event. A session may be ingested multiple
        times during its lifetime, so previously stored rows for the same
        (app_name, user_id, session_id) scope are replaced.

        Args:
            session: The ADK session to digest into memory.
        """
        params = [
            (
                session.app_name,
                session.user_id,
                session.id,
                event.id or "",
                event.author,
                event.content.role if event.content else None,
                digest,
                _format_timestamp(event.timestamp),
            )
            for event, digest in ((e, _event_digest(e)) for e in session.events)
            if digest
        ]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM memory_entries"
                    " WHERE app_name = $1 AND user_id = $2 AND session_id = $3",
                    session.app_name,
                    session.user_id,
                    session.id,
                    timeout=_COMMAND_TIMEOUT_S,
                )
                await conn.executemany(
                    "INSERT INTO memory_entries"
                    " (app_name, user_id, session_id, event_id, author, role,"
                    "  text_content, event_ts)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                    " ON CONFLICT (app_name, user_id, session_id, event_id)"
                    " DO UPDATE SET text_content = EXCLUDED.text_content,"
                    "               author = EXCLUDED.author,"
                    "               role = EXCLUDED.role,"
                    "               event_ts = EXCLUDED.event_ts",
                    params,
                    timeout=_COMMAND_TIMEOUT_S,
                )

    @override
    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence["Event"],
        session_id: Optional[str] = None,
        custom_metadata: Optional[Mapping[str, object]] = None,
    ) -> None:
        """Add an incremental batch of events to memory (delta ingestion).

        Rows are deduplicated by event id within the (app, user, session)
        scope; re-sent events are ignored. ``custom_metadata`` is accepted
        for ABC compatibility but not persisted.

        Args:
            app_name: Application name for memory scope.
            user_id: User id for memory scope.
            events: The events to add (treated as a delta, not a full session).
            session_id: Optional session scope; a sentinel is used when absent.
            custom_metadata: Ignored by this implementation.
        """
        _ = custom_metadata
        scoped_session_id = session_id or _UNKNOWN_SESSION_ID
        params = [
            (
                app_name,
                user_id,
                scoped_session_id,
                event.id or "",
                event.author,
                event.content.role if event.content else None,
                digest,
                _format_timestamp(event.timestamp),
            )
            for event, digest in ((e, _event_digest(e)) for e in events)
            if digest
        ]
        if not params:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO memory_entries"
                " (app_name, user_id, session_id, event_id, author, role,"
                "  text_content, event_ts)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                " ON CONFLICT (app_name, user_id, session_id, event_id)"
                " DO NOTHING",
                params,
                timeout=_COMMAND_TIMEOUT_S,
            )

    @override
    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        """ILIKE keyword search over stored session digests and feedback.

        Every keyword in *query* is turned into a ``%word%`` ILIKE pattern;
        a row matches when ANY keyword matches (same any-word semantics as
        ADK's ``InMemoryMemoryService``). Matching reviewer feedback rows are
        appended as additional memories (the feedback table is app-global).

        Args:
            app_name: The application name scope.
            user_id: The user id scope.
            query: Free-text query; split into ``\\w+`` keywords.

        Returns:
            A ``SearchMemoryResponse`` with matching ``MemoryEntry`` items,
            newest first; empty when the query has no keywords or no rows
            match.
        """
        words = _extract_words_lower(query)[:_MAX_QUERY_WORDS]
        response = SearchMemoryResponse()
        if not words:
            return response
        patterns = [_like_pattern(word) for word in words]

        like_clause = " OR ".join(
            f"text_content ILIKE ${i}" for i in range(3, 3 + len(patterns))
        )
        memory_sql = (
            "SELECT id, author, role, text_content, event_ts, created_at"
            " FROM memory_entries"
            f" WHERE app_name = $1 AND user_id = $2 AND ({like_clause})"
            f" ORDER BY created_at DESC LIMIT {_SEARCH_LIMIT}"
        )
        fb_like_clause = " OR ".join(
            f"(feedback ILIKE ${i} OR news_title ILIKE ${i})"
            for i in range(1, 1 + len(patterns))
        )
        feedback_sql = (
            "SELECT id, run_id, verdict, feedback, targets, news_title, created_at"
            " FROM feedback"
            f" WHERE {fb_like_clause}"
            f" ORDER BY created_at DESC LIMIT {_FEEDBACK_SEARCH_LIMIT}"
        )

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            memory_rows = await conn.fetch(
                memory_sql, app_name, user_id, *patterns, timeout=_COMMAND_TIMEOUT_S
            )
            feedback_rows = await conn.fetch(
                feedback_sql, *patterns, timeout=_COMMAND_TIMEOUT_S
            )

        for row in memory_rows:
            response.memories.append(
                MemoryEntry(
                    id=str(row["id"]),
                    content=types.Content(
                        role=row["role"] or "model",
                        parts=[types.Part(text=row["text_content"])],
                    ),
                    author=row["author"],
                    timestamp=row["event_ts"]
                    or self._iso(row["created_at"]),
                )
            )
        for row in feedback_rows:
            targets = _decode_targets(row["targets"])
            text = (
                f"Reviewer feedback ({row['verdict']}) on run {row['run_id']}"
                + (f" [{row['news_title']}]" if row["news_title"] else "")
                + f": {row['feedback']}"
                + (f" (targets: {', '.join(targets)})" if targets else "")
            )
            response.memories.append(
                MemoryEntry(
                    id=f"feedback-{row['id']}",
                    content=types.Content(
                        role="user", parts=[types.Part(text=text)]
                    ),
                    author="human_reviewer",
                    timestamp=self._iso(row["created_at"]),
                    custom_metadata={
                        "kind": "feedback",
                        "run_id": row["run_id"],
                        "verdict": row["verdict"],
                        "targets": targets,
                    },
                )
            )
        return response

    # ------------------------------------------------------------------
    # Feedback store (Learner agent / planner context)
    # ------------------------------------------------------------------
    async def store_feedback(self, record: FeedbackRecord) -> None:
        """Persist one reviewer feedback record to the ``feedback`` table.

        Args:
            record: The feedback to store (run id, verdict, text, targets,
                news title, created-at).
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO feedback"
                " (run_id, verdict, feedback, targets, news_title, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6)",
                record.run_id,
                record.verdict,
                record.feedback,
                list(record.targets),
                record.news_title,
                record.created_at,
                timeout=_COMMAND_TIMEOUT_S,
            )

    async def recent_feedback(self, limit: int = 20) -> list[FeedbackRecord]:
        """Return the newest feedback records, most recent first.

        Args:
            limit: Maximum number of records to return (default 20).

        Returns:
            Up to *limit* ``FeedbackRecord`` items ordered newest first.
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT run_id, verdict, feedback, targets, news_title, created_at"
                " FROM feedback ORDER BY created_at DESC LIMIT $1",
                limit,
                timeout=_COMMAND_TIMEOUT_S,
            )
        return [
            FeedbackRecord(
                run_id=row["run_id"],
                verdict=row["verdict"],
                feedback=row["feedback"],
                targets=_decode_targets(row["targets"]),
                news_title=row["news_title"] or "",
                created_at=row["created_at"] or datetime.now(timezone.utc),
            )
            for row in rows
        ]

    @staticmethod
    def _iso(value: Optional[datetime]) -> Optional[str]:
        """ISO-8601 string for a datetime column (None-safe)."""
        return value.isoformat() if value is not None else None
