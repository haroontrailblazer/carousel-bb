"""The deployed console: React SPA and JSON API.

One ASGI app, one process, one Render service:

===================  ====================================================
``/``                the React console (static bundle, SPA history fallback)
``/api/...``         the console's JSON API and event stream (signed in)
``/healthz``         platform health probe
===================  ====================================================

There is no longer a ``/review-api`` surface. It served standalone Approve and
Reject pages that needed no credentials, on the reasoning that a Telegram link
opens where nobody can sign in. But approving auto-publishes to Instagram, so
any leaked URL was a permanent, anonymous publish button. Telegram now links to
``/tasks/{run_id}?tab=review`` - the console's own review screen, behind the
login, where the decision is recorded against a person and made with the actual
carousel on screen. The decision logic those pages used lives in ``app.review``
and is unchanged; only the pages are gone.

**Google ADK runs the whole pipeline, and none of its web surface is served.**
The agents, the orchestrator, the session store and the artifact store are all
ADK; what is gone is ``get_fast_api_app`` - its bundled dev UI, its own HTTP
routes, and everything they dragged along:

* an origin/DNS-rebinding check that 403s every asset unless the deployment
  hostname is registered with it,
* a vendored Angular bundle served with no Cache-Control, so a browser
  heuristically caches the shell and shows a dead page after a session ends,
* a mounted sub-app whose lifespan Starlette will not run,
* and a startup step that rewrites a config file inside site-packages.

Runs reach ADK directly through ``app.agent.build_runner()``, and the console's
trace reads ADK's own ``events`` table, so the inspector was never on the path
that matters - only in front of it.

**Deployment note.** Run uvicorn with ``--proxy-headers
--forwarded-allow-ips='*'`` behind a reverse proxy so client IPs and the
forwarded scheme are honoured.

Run locally::

    python -m uvicorn web_app:app --port 8000 --proxy-headers
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.types import ASGIApp

from app import runtime
from app.config import PROJECT_ROOT, settings
from app.observability import init_observability, shutdown_observability
from app.review.resume import drain_resume_tasks
from app.runs.recovery import reconcile_on_startup, release_stuck_queue_items
from app.runs.service import drain_run_tasks
from app.scheduler import shutdown_scheduler, start_scheduler
from app.services import db, instagram_accounts, telegram_config
from web_api.auth import AuthMiddleware, build_verifier, validate_session_secret
from web_api.routes_auth import router as auth_router
from web_api.routes_runs import router as runs_router
from web_api.routes_settings import router as settings_router
from web_api.spa import SPAStaticFiles


def _configure_logging() -> None:
    """Send this application's logs somewhere they can actually be read.

    uvicorn configures only its OWN loggers, so anything logged by app.* and
    web_api.* goes to a root logger with no handler and is dropped. Python's
    last-resort handler still emits WARNING and above, which is why errors
    appear while every INFO line - "scheduler started", "reconcile marked N
    runs interrupted", "verdict accepted" - vanishes.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for ours in ("app", "web_api", "fetcher"):
        logging.getLogger(ours).setLevel(level)


_configure_logging()

logger = logging.getLogger(__name__)

#: The built SPA. Absent until `npm run build` has run, in which case
#: SPAStaticFiles serves an explanatory page instead of failing.
SPA_DIST = PROJECT_ROOT / "frontend" / "dist"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the background machinery, and shut it down honestly."""
    init_observability()

    for problem in validate_session_secret(settings.session_secret):
        logger.error("AUTH CONFIG: %s", problem)

    # Building the agent tree here surfaces an import or configuration error at
    # boot, where the health check will catch it, rather than on the first run
    # a person starts.
    try:
        from app.agent import build_root_agent

        agent = build_root_agent()
        logger.info(
            "Pipeline ready: %s with %d agents (app_name=%r).",
            agent.name,
            len(agent.sub_agents),
            settings.app_name,
        )
    except Exception:
        logger.exception("The agent tree failed to build; runs will not start.")

    scheduler = None
    if settings.database_url:
        # Order matters: reconcile first (which demotes killed runs out of
        # "running"), THEN release queue items, so an item is only freed once
        # nothing live still claims it.
        await reconcile_on_startup()
        await release_stuck_queue_items()
        # Load whatever Telegram credentials the console stored, so the
        # review dispatcher can send without a restart after someone connects
        # a bot from the profile page.
        await telegram_config.load()
        # And the connected Instagram accounts, for the same reason plus one
        # more: the slide renderer reads them SYNCHRONOUSLY from inside a
        # worker thread, so the cache has to be warm before any run starts.
        await instagram_accounts.load()
        try:
            seeded = await db.seed_app_users(list(settings.auth_bootstrap_emails))
            if seeded:
                logger.warning(
                    "Seeded %d bootstrap user(s) into an empty allowlist.", seeded
                )
        except Exception as exc:
            logger.warning("Could not seed the user allowlist: %s", exc)
        scheduler = await start_scheduler()

    try:
        yield
    finally:
        # Not a drain to completion: a generate phase runs for minutes and no
        # platform grace period covers it, so waiting only guarantees being
        # SIGKILLed mid-write. Cancel instead - the resume path restores its
        # pending review and reconcile marks the rest interrupted with a
        # Resume button.
        if scheduler is not None:
            shutdown_scheduler()
        await drain_resume_tasks(timeout=10.0)
        await drain_run_tasks(timeout=10.0)
        await runtime.close_services()
        try:
            await db.close_pool()
        except Exception as exc:  # pragma: no cover - shutdown best effort
            logger.warning("Closing the database pool failed: %s", exc)
        shutdown_observability()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def build_app() -> ASGIApp:
    """Assemble the console, the API and the review pages."""
    root = FastAPI(
        title="Carousel Factory",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @root.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        """Liveness probe. Touches no dependencies, so it cannot restart-loop."""
        return {"status": "ok"}

    root.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    root.include_router(runs_router, prefix="/api", tags=["runs"])
    root.include_router(settings_router, prefix="/api", tags=["settings"])

    # Registered LAST: it is the catch-all, and anything mounted after it would
    # never be reached.
    root.mount("/", SPAStaticFiles(directory=SPA_DIST, html=True), name="spa")

    logger.info(
        "Console assembled: SPA at /, API at /api. "
        "Set PUBLIC_BASE_URL to this service's public URL so the Telegram "
        "review button can link back to it."
    )

    return AuthMiddleware(
        root,
        verifier=build_verifier(),
        secret=settings.session_secret,
        secure_cookies=_secure_cookies_default(),
    )


def _secure_cookies_default() -> bool:
    """Mark cookies Secure unless this is plainly a local dev run.

    A Secure cookie is silently dropped over plain HTTP, which would make local
    development look like a broken login with no error anywhere.
    """
    base = (settings.public_base_url or "").lower()
    if base.startswith("https://"):
        return True
    if base.startswith("http://"):
        return False
    return os.getenv("ENV", "").lower() in ("prod", "production")


app = build_app()


__all__ = ["app", "build_app"]
