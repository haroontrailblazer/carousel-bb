"""Resuming a paused pipeline run once a human has decided.

Moved out of ``review_api`` so that every surface which can decide a verdict -
the Telegram links, the web console, the CLI - resumes through the SAME code.
Nothing here imports a web framework: the only thing a resume needs is the
paused invocation's ``function_call_id`` and a Runner.

The protocol, in one paragraph. ``review_dispatcher`` pauses a run by returning
``None`` from the ``await_human_review`` LongRunningFunctionTool, which makes
ADK end the invocation without a function-response event. Resuming means
starting a NEW invocation on the same session whose ``new_message`` is a
``types.Content`` carrying a ``FunctionResponse`` addressed to that original
call id. The dispatcher's ``before_agent_callback`` then lifts the verdict out
of it deterministically, so a confused model cannot record a verdict different
from what the human submitted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.services import db

logger = logging.getLogger(__name__)

# The tool name the FunctionResponse must carry (google-adk 2.7.0 FunctionTool
# names tools after the wrapped callable). Imported from the dispatcher when
# possible so the two sides can never drift; the literal fallback keeps this
# module importable even when heavy agent deps are missing.
try:  # pragma: no cover - trivial import wiring
    from app.agents.review_dispatcher import AWAIT_REVIEW_TOOL_NAME
except Exception:  # pragma: no cover - env dependent
    AWAIT_REVIEW_TOOL_NAME = "await_human_review"

# Fixed pipeline user id - sessions are addressed by (app_name, user_id,
# session_id); the fetcher seeds sessions with this user id.
try:  # pragma: no cover - trivial import wiring
    from fetcher.fetch_news import PIPELINE_USER_ID
except Exception:  # pragma: no cover - env dependent
    PIPELINE_USER_ID = "pipeline"

#: Safety cap for one resumed pipeline leg. A resume ends either at the next
#: review pause (rework path) or at ``done`` (publish path); an hour covers
#: image generation + IG publishing with a wide margin.
RESUME_TIMEOUT_S = 3600.0

#: Strong references to in-flight resume tasks (asyncio only keeps weak ones).
_resume_tasks: set["asyncio.Task[None]"] = set()


def build_resume_content(
    function_call_id: str,
    status: str,
    feedback: str,
    targets: Optional[list[str]] = None,
) -> Any:
    """Build the ``types.Content`` that answers the paused review tool call.

    google-genai types (verified in the installed package):
    ``types.FunctionResponse(id=..., name=..., response=dict)`` wrapped in
    ``types.Part(function_response=...)`` inside a role-``user`` Content.

    Args:
        function_call_id: The original ``await_human_review`` call id.
        status: ``"approved"`` or ``"rejected"``.
        feedback: Reviewer feedback text (may be empty on approve).
        targets: Agents the human named, when they pointed at one. Left out of
            the payload entirely when empty, so a verdict that names nobody
            looks exactly as it always did to everything downstream.

    Returns:
        The ``types.Content`` to pass as ``Runner.run_async(new_message=...)``.
    """
    from google.genai import types  # deferred: keep module import light

    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=function_call_id,
                    name=AWAIT_REVIEW_TOOL_NAME,
                    response={
                        "status": status,
                        "feedback": feedback,
                        **({"targets": list(targets)} if targets else {}),
                    },
                )
            )
        ],
    )


async def restore_pending_review(
    run_id: str, session_id: str, function_call_id: str
) -> None:
    """Re-persist a consumed pending review after a failed/timed-out resume.

    ``submit_verdict`` consumes the ``pending_reviews`` row before the resume
    is known to have succeeded; if the resume then dies the run would be
    stranded with no retry path (the mail links would render "Nothing
    pending"). Restoring the row lets the reviewer simply re-submit - a
    duplicate resume is harmless because the dispatcher's consumed-id guard
    turns it into a no-op for an already-recorded verdict.

    The restore is skipped when a NEWER pending row already exists (a resumed
    leg that reached the next review pause saved a fresh function call id
    before failing later) so the newer id is never clobbered by the stale one.
    """
    try:
        if await db.load_pending_review(run_id) is None:
            await db.save_pending_review(run_id, session_id, function_call_id)
            logger.info(
                "Restored pending review for run %s so the reviewer can "
                "re-submit the verdict.",
                run_id,
            )
        else:
            logger.info(
                "A newer pending review already exists for run %s; "
                "not restoring the consumed one.",
                run_id,
            )
    except Exception:
        logger.exception(
            "Could not restore the pending review for run %s (session %s, "
            "call %s); the run may need a manual pending_reviews insert.",
            run_id,
            session_id,
            function_call_id,
        )


async def resume_pipeline(
    run_id: str,
    session_id: str,
    function_call_id: str,
    status: str,
    feedback: str,
    targets: Optional[list[str]] = None,
) -> None:
    """Resume the paused run with the reviewer's verdict (background work).

    Builds a fresh Runner (``app.agent.build_runner`` - same app_name and
    session database as the paused run) and streams the resumed invocation to
    completion: rejected verdicts end at the next review pause, approved ones
    at ``done``. Errors are logged, never raised - the HTTP response has long
    been sent by the time this runs. On failure or timeout the consumed
    ``pending_reviews`` row is restored so the reviewer can re-submit.

    Args:
        run_id: The run being resumed (logging only).
        session_id: The ADK session id from ``pending_reviews``.
        function_call_id: The paused ``await_human_review`` call id.
        status: ``"approved"`` or ``"rejected"``.
        feedback: The reviewer's feedback text.
    """
    logger.info(
        "Resuming run %s (session %s, call %s) with verdict '%s'.",
        run_id,
        session_id,
        function_call_id,
        status,
    )
    runner = None
    beat = None
    try:
        # Deferred imports: building the agent tree is heavy and needs the full
        # agent/tool dependency stack - the API itself must start without it.
        # consume_invocation is deferred for import-cycle reasons only.
        from app import observability
        from app.agent import build_runner
        from app.runs import cancellation
        from app.runs.bus import KIND_TERMINAL
        from app.runs.service import _heartbeat
        from app.runs.stream import consume_invocation, record_event

        # A resumed leg runs for minutes and writes to the run row only at
        # phase boundaries, so without this it looks idle to startup recovery
        # for the whole of a rework. _drive_run has always heartbeated for
        # exactly this reason; this path simply never did.
        observability.bind_run(run_id)
        # And the brand marks. A rejected carousel comes back through HERE to
        # be reworked, and a rework re-renders slides - which stamp the
        # account's handle and profile picture. This path never went through
        # _drive_run, so without binding the identity the first re-rendered
        # slide would refuse for want of an account.
        from app.runs.service import _bind_brand_identity

        await _bind_brand_identity(run_id)
        cancellation.clear(run_id)
        beat = asyncio.get_running_loop().create_task(
            _heartbeat(run_id, deadline=RESUME_TIMEOUT_S),
            name=f"heartbeat-{run_id}",
        )

        runner = build_runner()
        content = build_resume_content(function_call_id, status, feedback, targets)

        result = await asyncio.wait_for(
            consume_invocation(
                runner,
                run_id=run_id,
                session_id=session_id,
                user_id=PIPELINE_USER_ID,
                new_message=content,
            ),
            timeout=RESUME_TIMEOUT_S,
        )
        logger.info(
            "Resume for run %s finished after %d event(s).",
            run_id,
            result["events"],
        )

        # Record how the leg ended, exactly as _drive_run does. Without this
        # the run row keeps whatever the last phase transition wrote, and the
        # console's onEnd - which fires on the terminal event - never runs, so
        # the task list and the sidebar are not refreshed when the carousel
        # actually goes live.
        if result["paused"]:
            end_status = db.RUN_STATUS_AWAITING_REVIEW
            text = "Waiting for your review."
        elif result.get("phase") == "done":
            end_status = db.RUN_STATUS_DONE
            text = "Run finished."
        else:
            current = ""
            try:
                row = await db.get_run(run_id)
                current = str((row or {}).get("status") or "")
            except Exception as exc:  # pragma: no cover - advisory read
                logger.warning("Could not re-read status for %s: %s", run_id, exc)
            end_status = (
                current
                if current and current != db.RUN_STATUS_RUNNING
                else db.RUN_STATUS_INTERRUPTED
            )
            text = f"Run stopped ({end_status})."
        await db.set_run_status(run_id, end_status)
        await record_event(
            run_id,
            result["last_seq"] + 1,
            KIND_TERMINAL,
            text=text,
            data={"status": end_status, "paused": result["paused"]},
        )
    except asyncio.TimeoutError:
        logger.error(
            "Resume for run %s timed out after %.0f s; check the run logs.",
            run_id,
            RESUME_TIMEOUT_S,
        )
        await restore_pending_review(run_id, session_id, function_call_id)
    except asyncio.CancelledError:
        # CancelledError derives from BaseException, so `except Exception`
        # below does NOT catch it. Without this branch a shutdown-time
        # task.cancel() skips the restore, leaving the pending_reviews row
        # consumed and the run permanently unresumable ("Nothing pending" on
        # every link). Restore, then re-raise so cancellation still propagates.
        logger.warning(
            "Resume for run %s was cancelled (shutdown?); restoring the "
            "pending review so the verdict can be re-submitted.",
            run_id,
        )
        await restore_pending_review(run_id, session_id, function_call_id)
        raise
    except Exception:
        logger.exception("Resume for run %s failed.", run_id)
        await restore_pending_review(run_id, session_id, function_call_id)
    finally:
        if beat is not None:
            beat.cancel()
        if runner is not None:
            try:
                await runner.close()
            except Exception as exc:
                logger.warning("Runner close failed for run %s: %s", run_id, exc)


def spawn_resume(
    run_id: str,
    session_id: str,
    function_call_id: str,
    status: str,
    feedback: str,
    targets: Optional[list[str]] = None,
) -> None:
    """Fire the resume as a fire-and-forget asyncio task (strongly referenced)."""
    task = asyncio.get_running_loop().create_task(
        resume_pipeline(
            run_id, session_id, function_call_id, status, feedback, targets
        ),
        name=f"resume-{run_id}",
    )
    _resume_tasks.add(task)
    task.add_done_callback(_resume_tasks.discard)



def active_resume_run_ids() -> set[str]:
    """Run ids with a resume leg in flight in this process.

    ``app.runs.service.active_run_ids`` folds this in, so the concurrency cap
    and the "already being driven" guards can see a rework - which is the
    longest and most expensive part of a review cycle, and was previously
    invisible to both.
    """
    prefix = "resume-"
    return {
        task.get_name()[len(prefix):]
        for task in _resume_tasks
        if not task.done() and task.get_name().startswith(prefix)
    }


def cancel_resume(run_id: str) -> bool:
    """Cancel an in-flight resume for ``run_id``. False when there is none.

    Stop has to reach these tasks too. A rework triggered by a rejection runs
    HERE, not in ``app.runs.service`` - so a Stop that only cancelled the
    service's tasks did nothing at all during the longest, most expensive part
    of a review cycle, which is exactly when someone reaches for it.

    Cancelling raises CancelledError inside ``resume_pipeline``, whose handler
    restores the pending_reviews row before re-raising, so the verdict stays
    submittable afterwards.
    """
    cancelled = False
    for task in list(_resume_tasks):
        if task.get_name() == f"resume-{run_id}" and not task.done():
            task.cancel()
            cancelled = True
    return cancelled

async def drain_resume_tasks(timeout: float = 10.0) -> None:
    """Give in-flight resumes a moment to finish, then cancel the rest.

    Deliberately NOT a drain-to-completion. A resumed leg can run for minutes
    (image generation, then the Instagram publish) and no platform grace period
    covers that - Render will SIGKILL long before it finishes, which is a worse
    outcome than cancelling cleanly. So: wait briefly for anything nearly done,
    then cancel. ``resume_pipeline`` handles the cancellation by restoring the
    consumed ``pending_reviews`` row, so the reviewer can simply click again.

    Args:
        timeout: seconds to wait before cancelling the stragglers.
    """
    if not _resume_tasks:
        return
    logger.info(
        "Shutdown: waiting up to %.0f s for %d in-flight resume task(s).",
        timeout,
        len(_resume_tasks),
    )
    _, pending = await asyncio.wait(set(_resume_tasks), timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        # Let each cancelled task run its restore before the loop closes.
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info(
            "Cancelled %d resume task(s); their pending reviews were restored "
            "so the verdicts can be re-submitted.",
            len(pending),
        )


__all__ = [
    "AWAIT_REVIEW_TOOL_NAME",
    "active_resume_run_ids",
    "PIPELINE_USER_ID",
    "RESUME_TIMEOUT_S",
    "build_resume_content",
    "cancel_resume",
    "drain_resume_tasks",
    "restore_pending_review",
    "resume_pipeline",
    "spawn_resume",
]
