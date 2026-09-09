"""Process-wide ADK service singletons.

Every ``build_runner()`` used to construct a fresh ``DatabaseSessionService``,
and each of those builds its own SQLAlchemy async engine with its own
connection pool. In the CLI that was harmless - one process, one run, then
exit. In a long-lived web service it is not: a run, a resume, and a scheduler
tick each mint another pool, and Supabase's pooler starts refusing connections
long before anything looks wrong in the application.

So the three services live here, built once and shared. Note what is NOT cached:
the agent tree. ``build_root_agent()`` re-reads ``skills/agents/*.md`` on every
call, which is how the Learner's appended rules take effect without a redeploy -
caching that would quietly disable the learning loop.

Sharing is safe because ``Runner.close()`` (verified in google-adk 2.7.0) closes
toolsets and plugins - both per-runner - and then calls
``session_service.flush()``, which is a no-op on the base class. It never
disposes the engine, so one run finishing cannot pull the pool out from under
another that is still going.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from google.adk.artifacts import BaseArtifactService
from google.adk.memory import BaseMemoryService
from google.adk.sessions import BaseSessionService

from app.config import settings
from app.services.artifact_service import SupabaseArtifactService
from app.services.memory_service import PostgresMemoryService

logger = logging.getLogger(__name__)

# Built lazily on first use so importing this module performs no I/O and needs
# no configuration - the CLI, the tests and the web app all import it freely.
_lock = threading.Lock()
_session_service: Optional[BaseSessionService] = None
_artifact_service: Optional[BaseArtifactService] = None
_memory_service: Optional[BaseMemoryService] = None


#: SQLAlchemy engine options for the session store.
#:
#: This pipeline does minutes of work with no database traffic at all - a video
#: download and ffmpeg render, or a batch of image generations - and then asks
#: ADK to write session state. A pooled connection that has been sitting idle
#: through that is very likely dead: Supabase's pooler closes idle connections,
#: and the failure surfaces as
#: ``ConnectionDoesNotExistError: connection was closed in the middle of
#: operation`` from whatever statement happened to run next. That is not a
#: hypothetical - it killed a real run during first_page_visual.
#:
#: ADK already defaults ``pool_pre_ping=True``, which tests a connection at
#: checkout. That is necessary but not sufficient here: pre-ping cannot help
#: with a connection that dies mid-statement, and a half-open socket can pass
#: the ping. ``pool_recycle`` is the part that actually matters - it retires
#: connections by age, before the pooler has a reason to drop them.
_ENGINE_KWARGS: dict = {
    "pool_pre_ping": True,
    # Comfortably under Supabase's idle timeout, so we retire a connection
    # before the far end does it for us.
    "pool_recycle": int(os.getenv("DB_POOL_RECYCLE_S", "280")),
    # Sized against a SHARED budget, not this engine's own appetite. Supabase's
    # session-mode pooler caps the whole project at 15 clients, and
    # app.services.db opens its own asyncpg pool alongside this one - the two
    # maxima add. At the previous defaults they summed to 20, and the pooler
    # answered EMAXCONNSESSION to whichever write asked last. See the budget
    # note on _MAX_CONNECTIONS in app/services/db.py.
    "pool_size": int(os.getenv("DB_POOL_SIZE", "3")),
    "max_overflow": int(os.getenv("DB_POOL_MAX_OVERFLOW", "2")),
    "pool_timeout": 30,
}

# The SQLAlchemy half of the transaction-pooler rule (see
# _statement_cache_size in app/services/db.py). SQLAlchemy's asyncpg driver
# keeps its own prepared-statement cache, and it fails the same way behind a
# transaction-mode pooler - so it is disabled under exactly the same
# condition, and a switch to port 6543 needs no code change.
if ":6543" in (settings.database_url or ""):
    _ENGINE_KWARGS["connect_args"] = {
        "prepared_statement_cache_size": 0,
        "statement_cache_size": 0,
    }


def _build_session_service() -> BaseSessionService:
    """Build the session service: Postgres-backed, else in-memory.

    ``DatabaseSessionService`` persists sessions so a review pause survives a
    process restart and any surface can resume the run. Without
    ``DATABASE_URL`` the in-memory service keeps local ``adk web`` working, at
    the cost of losing every run on exit.
    """
    if settings.database_url:
        try:
            from google.adk.sessions import DatabaseSessionService

            return DatabaseSessionService(settings.database_url, **_ENGINE_KWARGS)
        except Exception as exc:
            logger.warning(
                "DatabaseSessionService unavailable (%s); falling back to "
                "InMemorySessionService.",
                exc,
            )
    else:
        logger.warning(
            "DATABASE_URL not set; using InMemorySessionService - sessions "
            "will not survive a restart and a paused run cannot be resumed "
            "from another process."
        )
    from google.adk.sessions import InMemorySessionService

    return InMemorySessionService()


def _build_artifact_service() -> BaseArtifactService:
    """Build the artifact service: Supabase S3-backed, else in-memory."""
    if settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key:
        try:
            return SupabaseArtifactService()
        except Exception as exc:
            logger.warning(
                "SupabaseArtifactService unavailable (%s); falling back to "
                "InMemoryArtifactService.",
                exc,
            )
    else:
        logger.warning(
            "Supabase S3 settings missing (SUPABASE_S3_ENDPOINT / "
            "SUPABASE_S3_ACCESS_KEY / SUPABASE_S3_SECRET_KEY); using "
            "InMemoryArtifactService - artifacts stay local to this process, "
            "so slides vanish on exit and none can be signed for publishing."
        )
    from google.adk.artifacts import InMemoryArtifactService

    return InMemoryArtifactService()


def _build_memory_service() -> BaseMemoryService:
    """Build the memory service: Postgres-backed, else in-memory.

    ``PostgresMemoryService`` opens its pool lazily, so constructing it here
    performs no I/O; runtime database errors degrade gracefully inside the
    orchestrator and learner, which treat memory as best-effort.
    """
    if settings.database_url:
        return PostgresMemoryService()
    logger.warning(
        "DATABASE_URL not set; using InMemoryMemoryService - recent-feedback "
        "injection and permanent feedback storage are disabled."
    )
    from google.adk.memory import InMemoryMemoryService

    return InMemoryMemoryService()


def session_service() -> BaseSessionService:
    """The shared session service (built on first use)."""
    global _session_service
    if _session_service is None:
        with _lock:
            if _session_service is None:
                _session_service = _build_session_service()
    return _session_service


def artifact_service() -> BaseArtifactService:
    """The shared artifact service (built on first use)."""
    global _artifact_service
    if _artifact_service is None:
        with _lock:
            if _artifact_service is None:
                _artifact_service = _build_artifact_service()
    return _artifact_service


def memory_service() -> BaseMemoryService:
    """The shared memory service (built on first use)."""
    global _memory_service
    if _memory_service is None:
        with _lock:
            if _memory_service is None:
                _memory_service = _build_memory_service()
    return _memory_service


async def close_services() -> None:
    """Release the shared services on process shutdown.

    Best effort by design: this runs while the process is going down, and a
    service that cannot be closed cleanly must not stop the others from
    trying.
    """
    global _session_service, _artifact_service, _memory_service
    for name, service in (
        ("session", _session_service),
        ("artifact", _artifact_service),
        ("memory", _memory_service),
    ):
        closer = getattr(service, "close", None)
        if closer is None:
            continue
        try:
            result = closer()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # pragma: no cover - shutdown best effort
            logger.warning("Closing the %s service failed: %s", name, exc)
    _session_service = _artifact_service = _memory_service = None


def reset_services() -> None:
    """Drop the cached services without closing them (tests only)."""
    global _session_service, _artifact_service, _memory_service
    _session_service = _artifact_service = _memory_service = None


__all__ = [
    "artifact_service",
    "close_services",
    "memory_service",
    "reset_services",
    "session_service",
]
