"""Publisher agent - publishes the approved carousel to Instagram.

A tool-using ``LlmAgent`` (``settings.utility_model``). Because google-adk
2.7.0 tool-calling agents deliver results through tools (not
``output_schema``), all real work happens inside one deterministic tool,
:func:`publish_approved_carousel`, which:

1. reads the approved :class:`app.schemas.Bundle` from ``state[K_BUNDLE]``;
2. signs a public HTTPS URL for every ``bundle.ordered_artifacts`` entry via
   :meth:`app.services.artifact_service.SupabaseArtifactService.public_url`
   (cover video first - its aspect ratio governs the carousel);
3. calls :func:`app.tools.instagram_tools.publish_carousel`;
4. sends the confirmation via
   :func:`app.tools.telegram_tools.send_confirmation_message`;
5. writes the outcome into ``state["publish_result"]`` (via
   ``tool_context.state``) and records the run's completion in the ``runs``
   table via :func:`app.services.db.update_run_phase`.

The tool is idempotent: when ``state["publish_result"]`` already carries a
published media id, it returns that result instead of double-posting.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, ToolContext

from app.config import agent_instructions, settings
from app.llm import resolve_model
from app.schemas import Bundle
from app.services import db, instagram_accounts
from app.services.artifact_service import SupabaseArtifactService
from app.state import (
    AGENT_PUBLISHER,
    K_ACCOUNT_ID,
    K_BUNDLE,
    K_RUN_ID,
    PHASE_DONE,
    get_model,
)
from app.tools import instagram_tools, telegram_tools

logger = logging.getLogger(__name__)

#: Session-state key the publish outcome is written under (per CONTRACTS).
# Re-exported from app.state, which is the single place session-state keys
# are declared - the web console reads this key too and must not have to
# import the whole agent stack to learn its name.
from app.runs import cancellation  # noqa: E402
from app.state import K_PUBLISH_RESULT  # noqa: E402

# Lazily constructed fallback when the runner's wired artifact service is not
# a SupabaseArtifactService (e.g. in-memory local runs).
_fallback_artifact_service: Optional[SupabaseArtifactService] = None


class NoPublishAccount(Exception):
    """The run names no usable Instagram account to publish to."""


def _account_for_run(state: Any) -> instagram_accounts.Account:
    """The account this run was started against.

    Deliberately NOT falling back to the default account. The slides were
    already stamped with one account's handle and profile picture when they
    were generated; posting them anywhere else publishes a carousel that
    visibly belongs to a different brand. A named failure here is recoverable
    - a wrong post is not.

    Raises:
        NoPublishAccount: when the run names no account, the account has since
            been disconnected, or its token can no longer be used.
    """
    account_id = str(state.get(K_ACCOUNT_ID) or "")
    if not account_id:
        raise NoPublishAccount(
            "This run is not linked to an Instagram account, so there is "
            "nowhere to publish it. Runs started before accounts were "
            "connected have to be re-run against one."
        )
    account = instagram_accounts.get(account_id)
    if account is None:
        raise NoPublishAccount(
            f"The Instagram account this run was created for ({account_id}) "
            f"is no longer connected."
        )
    if account.needs_reconnect:
        raise NoPublishAccount(
            f"The connection to {account.handle or account_id} has expired. "
            f"Reconnect it from Profile -> Instagram and re-run."
        )
    return account


def _resolve_artifact_service(
    tool_context: ToolContext,
) -> SupabaseArtifactService:
    """Return the SupabaseArtifactService to sign public URLs with.

    Prefers the artifact service wired into the current invocation (so the
    exact same bucket/keys used to save the artifacts are used to sign them);
    falls back to a module-level, settings-configured instance.

    Args:
        tool_context: The ADK tool context of the current call.

    Returns:
        A ready ``SupabaseArtifactService``.

    Raises:
        ValueError: When no wired service exists and Supabase S3 settings are
            missing (message names the missing env vars).
    """
    invocation_context = getattr(tool_context, "_invocation_context", None)
    wired = getattr(invocation_context, "artifact_service", None)
    if isinstance(wired, SupabaseArtifactService):
        return wired
    global _fallback_artifact_service
    if _fallback_artifact_service is None:
        _fallback_artifact_service = SupabaseArtifactService()
    return _fallback_artifact_service


async def publish_approved_carousel(tool_context: ToolContext) -> dict:
    """Publish the approved carousel bundle to Instagram and confirm by mail.

    Signs a public URL for every artifact in ``bundle.ordered_artifacts``
    (cover video first), publishes the carousel through the Instagram Graph
    API, sends the confirmation email, and records the outcome in session
    state (``publish_result``) and the ``runs`` table.

    Call this tool exactly once, with no arguments - everything it needs is
    read from session state.

    Returns:
        On success: ``{"status": "published", "media_id", "permalink",
        "public_url_count", "confirmation_message_id", ...}``.
        If already published earlier in this run: ``{"status":
        "already_published", ...}`` with the stored result.
        On failure: ``{"status": "error", "message": <what went wrong>}``.
    """
    state = tool_context.state

    # Idempotency guard: never double-post the same run.
    existing = state.get(K_PUBLISH_RESULT)
    if isinstance(existing, dict) and existing.get("media_id"):
        logger.info(
            "publish_approved_carousel: already published (media_id=%s)",
            existing.get("media_id"),
        )
        return {**existing, "status": "already_published"}

    bundle = get_model(state, K_BUNDLE, Bundle)
    if bundle is None:
        return {
            "status": "error",
            "message": (
                "No bundle in session state (key 'bundle'); the stitch/verify "
                "phase must run before publishing."
            ),
        }
    if not bundle.ordered_artifacts:
        return {
            "status": "error",
            "message": "Bundle has no ordered_artifacts; nothing to publish.",
        }

    # Resolved BEFORE any URL is signed or any container is created: an
    # account problem is a configuration problem, and it costs nothing to
    # discover it while nothing has been uploaded yet.
    try:
        account = _account_for_run(state)
    except NoPublishAccount as exc:
        logger.error("Run %s cannot publish: %s", state.get(K_RUN_ID), exc)
        result = {"status": "error", "retryable": False, "message": str(exc)}
        state[K_PUBLISH_RESULT] = result
        return result

    run_id = str(state.get(K_RUN_ID) or "")
    session = tool_context.session

    # (1) Public URLs - one presigned HTTPS GET per artifact, in order.
    try:
        service = _resolve_artifact_service(tool_context)
        public_urls: list[str] = []
        for filename in bundle.ordered_artifacts:
            url = await service.public_url_async(
                app_name=session.app_name,
                user_id=session.user_id,
                session_id=session.id,
                filename=filename,
            )
            public_urls.append(url)
    except Exception as exc:  # noqa: BLE001 - ValueError/FileNotFoundError/S3
        logger.exception("Could not sign public URLs for the bundle.")
        return {
            "status": "error",
            "message": f"Could not build public URLs for the bundle: {exc}",
        }

    # (2) Publish to Instagram (sync Graph API client with its own polling
    # loop - run in a worker thread so the event loop stays responsive).
    #
    # The last point at which stopping is still free. A worker thread cannot be
    # interrupted by task.cancel(), so once this call starts the post can go
    # live minutes after someone pressed Stop, on a carousel they deliberately
    # abandoned. Checking a flag here is the difference between "stopped" and
    # "stopped, but published anyway".
    try:
        cancellation.raise_if_cancelled(run_id)
    except cancellation.RunCancelled:
        logger.info("Run %s was stopped before publishing; nothing posted.", run_id)
        result = {
            "status": "error",
            "message": "Stopped before publishing - nothing was posted.",
            "cancelled": True,
        }
        state[K_PUBLISH_RESULT] = result
        return result

    try:
        ig_result: dict[str, Any] = await asyncio.to_thread(
            instagram_tools.publish_carousel,
            bundle.model_dump(mode="json"),
            public_urls,
            should_continue=lambda: not cancellation.is_requested(run_id),
            account=account,
        )
    except asyncio.CancelledError:
        # Stop cancels the driving task, and CancelledError lands HERE - at
        # the await, long before the worker thread reaches its next
        # checkpoint. It is a BaseException, so neither handler below sees it;
        # without this branch the PublishAborted the thread raises moments
        # later lands on an abandoned future and asyncio logs "exception was
        # never retrieved" instead of anything a person could act on.
        #
        # The stop flag is already up (cancel_run raises it before
        # cancelling), so the thread will abort at its next checkpoint and
        # nothing is posted. The run's own ending is recorded by _drive_run.
        logger.info(
            "Run %s: publish cancelled; the upload aborts at its next "
            "checkpoint and nothing is posted.",
            run_id,
        )
        raise
    except instagram_tools.PublishUncertain as exc:
        logger.error(
            "Run %s: the media_publish reply was lost; the carousel may be "
            "live (container %s). Not retrying.",
            run_id,
            exc.creation_id,
        )
        result = {
            "status": "error",
            "retryable": False,
            "message": str(exc),
            "creation_id": exc.creation_id,
            "public_url_count": len(public_urls),
        }
        state[K_PUBLISH_RESULT] = result
        return result
    except instagram_tools.PublishAborted:
        logger.info("Run %s was stopped mid-publish; nothing posted.", run_id)
        result = {
            "status": "error",
            "message": "Stopped while publishing - nothing was posted.",
            "cancelled": True,
        }
        state[K_PUBLISH_RESULT] = result
        return result
    except Exception as exc:  # noqa: BLE001 - ValueError/RuntimeError/HTTP
        logger.exception("Instagram publish failed for run %s.", run_id)
        result = {
            "status": "error",
            "message": f"Instagram publish failed: {exc}",
            "public_url_count": len(public_urls),
        }
        state[K_PUBLISH_RESULT] = result
        return result

    media_id = str(ig_result.get("media_id", ""))
    permalink = str(ig_result.get("permalink", ""))

    # (3) Confirmation mail - best-effort: the post is already live, so a
    # mail failure must not fail the publish.
    confirmation_message_id = ""
    mail_error = ""
    try:
        mail_result = await asyncio.to_thread(
            telegram_tools.send_confirmation_message, run_id, permalink
        )
        confirmation_message_id = str(mail_result.get("message_id", ""))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Confirmation mail failed for run %s.", run_id)
        mail_error = str(exc)

    # (4) Record completion in the runs table - best-effort as well.
    db_error = ""
    if run_id:
        try:
            await db.update_run_phase(run_id, PHASE_DONE)
        except Exception as exc:  # noqa: BLE001
            logger.exception("runs-table update failed for run %s.", run_id)
            db_error = str(exc)
    else:
        db_error = "run_id missing from session state; runs table not updated."

    result = {
        "status": "published",
        "media_id": media_id,
        "permalink": permalink,
        "public_url_count": len(public_urls),
        "confirmation_message_id": confirmation_message_id,
        "mail_error": mail_error,
        "db_error": db_error,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    state[K_PUBLISH_RESULT] = result
    logger.info(
        "Published run %s to Instagram: media_id=%s permalink=%s",
        run_id,
        media_id,
        permalink,
    )
    return result


# ---------------------------------------------------------------------------
# Instruction (fallback default; canonical copy lives in
# skills/agents/publisher.md so the Learner can evolve it).
# NOTE: only "{run_id?}" may appear as an {identifier} placeholder - ADK's
# instruction templating substitutes any bare {state_key}.
# ---------------------------------------------------------------------------
DEFAULT_INSTRUCTION = """\
# Publisher

You are the Publisher of the Carousel Factory - the final step of the
pipeline, running only AFTER a human approved the carousel. Everything you
need is already in session state; the tool does all real work.

Run id: {run_id?}

## Your only job

1. Call the tool publish_approved_carousel exactly once, with no arguments.
   It signs public URLs for every slide of the approved bundle (cover video
   first), publishes the carousel via the Instagram Graph API, sends the
   confirmation email to the reviewers, and records the result in state and
   the runs table.
2. Read the tool result and reply with a short, factual summary:
   - status "published": report the Instagram permalink and media id, and
     whether the confirmation mail was sent (mention mail_error if any).
   - status "already_published": say the carousel was already live, give the
     permalink, and do NOT call the tool again.
   - status "error": reply "PUBLISH FAILED: " followed by the tool's message.
     If the result has "retryable": false, do NOT call the tool again under
     any circumstances - that result means the carousel may already be live
     and a retry would post it a second time. Otherwise you may retry at most
     ONCE, and only when the message clearly looks transient (a timeout or
     temporary network problem) - never retry validation or credential
     errors.

## Hard rules

- Never invent a permalink or media id - only report what the tool returned.
- Never call anything except publish_approved_carousel.
- Keep the final reply to at most three sentences.
"""


def _ensure_skill_file() -> None:
    """Write the default instruction to skills/agents/publisher.md.

    Only when the file is missing - the Learner agent appends "Learned rules"
    to this file, and those must never be overwritten.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_PUBLISHER}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_INSTRUCTION, encoding="utf-8")


def build_publisher_agent() -> LlmAgent:
    """Build the Publisher agent.

    Returns:
        A tool-using ``LlmAgent`` named ``AGENT_PUBLISHER`` on
        ``settings.utility_model`` whose single tool publishes the approved
        bundle and writes ``state["publish_result"]``.
    """
    _ensure_skill_file()
    instruction = agent_instructions(AGENT_PUBLISHER) or DEFAULT_INSTRUCTION
    return LlmAgent(
        name=AGENT_PUBLISHER,
        model=resolve_model(settings.utility_model),
        description=(
            "Publishes the approved carousel to Instagram and sends the "
            "confirmation mail."
        ),
        instruction=instruction,
        include_contents="none",  # operates purely on state + tool results
        tools=[FunctionTool(publish_approved_carousel)],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
