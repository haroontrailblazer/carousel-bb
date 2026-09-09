"""Review Dispatcher agent - mails the human review and pauses the pipeline.

The two-sided human-in-the-loop gate of the Carousel Factory:

- **Outbound** (phase ``review``): the ``send_review_request`` tool pulls local
  preview files out of the artifact store, sends them to the reviewers via
  :func:`app.tools.telegram_tools.send_review_message` (Approve/Reject
  buttons), and
  increments ``K_REVIEW_ROUND``. The LLM then calls ``await_human_review`` - a
  :class:`google.adk.tools.LongRunningFunctionTool` that returns ``None``.
  Verified against installed google-adk 2.7.0
  (``flows/llm_flows/functions.py``): a long-running tool returning a falsy
  value produces NO function-response event, the model-response event carries
  the pending call id in ``Event.long_running_tool_ids``, and
  ``Event.is_final_response()`` becomes True - the invocation ends *paused*.
  The tool persists ``(run_id, session_id, function_call_id)`` via
  :func:`app.services.db.save_pending_review`, taking the id from
  ``tool_context.function_call_id`` (2.7.0 ``Context`` populates it from the
  ``types.FunctionCall.id`` in ``_create_tool_context``).

- **Inbound** (resume): the review API builds ``types.Content`` with a
  ``types.Part(function_response=...)`` matching the pending call id/name and
  payload ``{"status": "approved"|"rejected", "feedback": "..."}``, then calls
  the Runner again - a NEW invocation whose ``user_content`` is that function
  response (2.7.0 rearranges async function responses into history in
  ``flows/llm_flows/contents.py``). This module handles it twice over for
  robustness:

  1. A ``before_agent_callback`` deterministically extracts the verdict,
     writes :class:`app.schemas.Verdict` to ``K_VERDICT`` (state deltas from
     callbacks are event-committed by ``BaseAgent._handle_before_agent_callback``
     when ``state.has_delta()``), and stamps ``temp:``-prefixed bookkeeping so
     the same function response is never consumed twice within one invocation
     (e.g. round 2 review entered after an in-invocation rework loop).
  2. The LLM is instructed (via a dynamic ``InstructionProvider`` directive)
     to call the ``set_verdict`` tool, which re-reads the authoritative
     payload from ``tool_context.user_content`` - so even a confused model
     cannot record a verdict different from what the human submitted.

State signalling uses ``temp:`` keys (``State.TEMP_PREFIX`` in 2.7.0), which
are invocation-visible immediately (``State.__setitem__`` mutates the live
``session.state`` dict) but are not durably persisted - exactly the lifetime
needed to tell "fresh verdict" from "already-consumed verdict".
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import FunctionTool, LongRunningFunctionTool, ToolContext
from google.genai import types

from app.config import agent_instructions, settings
from app.llm import resolve_model
from app.schemas import Bundle, Verdict
from app.services import db
from app.state import (
    AGENT_REVIEW_DISPATCHER,
    K_REVIEW_NOTICE_FAILED,
    K_BUNDLE,
    K_NEWS_ITEM,
    K_REVIEW_ROUND,
    K_REWORK_FEEDBACK,
    K_RUN_ID,
    K_VERDICT,
    set_model,
)
from app.tools import telegram_tools

logger = logging.getLogger(__name__)

# The exact tool name the review API must reference in its FunctionResponse
# (google-adk 2.7.0 FunctionTool names tools after the wrapped callable).
AWAIT_REVIEW_TOOL_NAME = "await_human_review"

# temp:-prefixed session keys (never durably persisted; see module docstring).
_DIRECTIVE_KEY = "temp:review_dispatcher_directive"
_CONSUMED_KEY = "temp:review_consumed_call_ids"
#: The review round whose mail actually reached Telegram. await_human_review
#: refuses to pause without a stamp for the current round, so a failed send
#: can never strand the run waiting on a human nobody asked.
_SENT_KEY = "temp:review_sent_round"
_DIRECTIVE_SEND_MAIL = "send_mail"
_DIRECTIVE_HANDLE_VERDICT = "handle_verdict"

_DEFAULT_INSTRUCTION = """\
# Review Dispatcher

You are the Review Dispatcher of the Carousel Factory pipeline - the human-in-
the-loop gate. A finished carousel Bundle sits in session state; nothing gets
published without a human verdict, and you are the agent that requests it and
records it. You operate in exactly one of two modes each time you are called.
A "CURRENT MODE" directive is appended to this instruction every time - obey it
literally, including on the second and later review rounds.

## Mode SEND_MAIL - request a review and pause

1. Call `send_review_request` (no arguments). It sends the reviewers a preview
   (cover poster + slide thumbnails + caption) with Approve/Reject buttons and
   increments the review round counter.
2. If (and ONLY if) the tool result has status "sent", immediately call
   `await_human_review` (no arguments). This is a long-running operation: the
   pipeline PAUSES on it until the human clicks a link. Never call it twice,
   and never call it when the send failed.
3. If `send_review_request` returned an error, do NOT call `await_human_review`.
   Reply with one short sentence describing the failure so the operator can
   fix it.

## Mode HANDLE_VERDICT - record the human's decision

The paused run has resumed: the latest message contains the reviewer's
response from `await_human_review` with their status and feedback.

1. Call `set_verdict` with that exact status ("approved" or "rejected") and
   the exact feedback text - verbatim, never paraphrased. The tool itself
   re-reads the authoritative reviewer response, so honesty is enforced.
2. Do NOT call `send_review_request` or `await_human_review` in this mode.
3. After the tool succeeds, reply with one short sentence stating the verdict
   (and the feedback, if any) so the orchestrator log reads cleanly.

## Hard rules

- Never invent, soften or reinterpret reviewer feedback: it is recorded
  verbatim and later routed to the responsible agents.
- One review request per TURN, maximum - never call `send_review_request`
  twice in the same reply. A run can legitimately need several, one per review
  round: every time a rejection is reworked, the carousel comes back here and
  the reviewer must be asked again. Refusing a later round strands the run with
  nobody notified.
- You never publish and never edit content - you only dispatch the review
  and record the verdict.
"""


# ---------------------------------------------------------------------------
# Resume-detection helpers
# ---------------------------------------------------------------------------
def _latest_review_response(
    content: Optional[types.Content],
) -> Optional[types.FunctionResponse]:
    """Extract the last ``await_human_review`` function response, if any.

    Args:
        content: The ``user_content`` that started the current invocation
            (the review API resumes the run with exactly one such part).

    Returns:
        The matching :class:`types.FunctionResponse`, or ``None`` when the
        invocation was not started by a review resume.
    """
    if content is None or not content.parts:
        return None
    found: Optional[types.FunctionResponse] = None
    for part in content.parts:
        response = part.function_response
        if response is not None and response.name == AWAIT_REVIEW_TOOL_NAME:
            found = response
    return found


def _verdict_from_payload(payload: dict) -> Verdict:
    """Build a :class:`Verdict` from the review API's function-response payload.

    Defensive: an unknown status is coerced to ``rejected`` (never
    auto-approve on malformed input) and a rejection without feedback gets a
    generic correction request so the feedback router still has text to work
    with.

    Args:
        payload: ``{"status": ..., "feedback": ...,
            ["reviewer": ...], ["targets": [...]]}``.

    Returns:
        A validated Verdict model.
    """
    raw_status = str(payload.get("status") or "").strip().lower()
    feedback = str(payload.get("feedback") or "").strip()
    reviewer = str(payload.get("reviewer") or "").strip()
    # Carried through as strings and canonicalised later, by the feedback
    # router's sanitizer. Validating agent names here would put the same list
    # of valid targets in two modules, and the sanitizer is already the one
    # place that owns it.
    raw_targets = payload.get("targets")
    targets = (
        [str(t).strip() for t in raw_targets if str(t).strip()]
        if isinstance(raw_targets, (list, tuple))
        else []
    )
    if raw_status not in ("approved", "rejected"):
        feedback = (
            f"Malformed review response (status={raw_status!r}); treated as "
            f"rejected for safety. {feedback}"
        ).strip()
        raw_status = "rejected"
    if raw_status == "rejected" and not feedback:
        feedback = (
            "Reviewer rejected without feedback text; re-check overall quality "
            "(visual, texts, design, CTA)."
        )
    return Verdict(
        status=raw_status, feedback=feedback, reviewer=reviewer, targets=targets
    )


def _fresh_review_response(
    content: Optional[types.Content], state
) -> Optional[types.FunctionResponse]:
    """Return the review response only if it has not been consumed yet.

    Within one invocation the run can loop review → rework → review; the old
    function response is still the invocation's ``user_content`` on the second
    review entry, so consumed call ids are tracked under a ``temp:`` state key
    and matched against ``FunctionResponse.id``.

    Args:
        content: The invocation's ``user_content``.
        state: Any dict-like state view (``.get`` is all that is used).

    Returns:
        The fresh, not-yet-consumed function response, or ``None``.
    """
    response = _latest_review_response(content)
    if response is None:
        return None
    consumed = state.get(_CONSUMED_KEY) or []
    if response.id and response.id in consumed:
        return None
    return response


async def _clear_pending_review_quietly(run_id: str) -> None:
    """Best-effort delete of the consumed pending_reviews row (idempotent)."""
    if not run_id:
        return
    try:
        await db.clear_pending_review(run_id)
    except Exception as exc:  # DB may be absent in local runs - never fatal
        logger.warning("Could not clear pending review for run %s: %s", run_id, exc)


async def capture_verdict_on_resume(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """``before_agent_callback``: deterministically record a resumed verdict.

    Runs before every dispatcher LLM turn. When the invocation was started by
    the review API's function response (and it has not been consumed yet), the
    Verdict is written to ``K_VERDICT`` directly - the pipeline stays correct
    even if the model never calls ``set_verdict``. The chosen mode is stamped
    into ``temp:`` state so the instruction provider issues a stable directive
    for every LLM call within this run.

    Args:
        callback_context: google-adk 2.7.0 ``Context`` (delta-aware state,
            ``user_content`` of the invocation).

    Returns:
        Always ``None`` - the agent proceeds normally; the state delta alone
        is committed by ``BaseAgent._handle_before_agent_callback``.
    """
    state = callback_context.state
    response = _fresh_review_response(callback_context.user_content, state)
    if response is None:
        state[_DIRECTIVE_KEY] = _DIRECTIVE_SEND_MAIL
        return None

    verdict = _verdict_from_payload(dict(response.response or {}))
    set_model(state, K_VERDICT, verdict)
    consumed = list(state.get(_CONSUMED_KEY) or [])
    if response.id:
        consumed.append(response.id)
    state[_CONSUMED_KEY] = consumed
    state[_DIRECTIVE_KEY] = _DIRECTIVE_HANDLE_VERDICT

    run_id = str(state.get(K_RUN_ID) or "")
    await _clear_pending_review_quietly(run_id)
    logger.info(
        "Review resume for run %s: verdict '%s' recorded (call id %s).",
        run_id,
        verdict.status,
        response.id,
    )
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
async def send_review_request(tool_context: ToolContext) -> dict:
    """Send the reviewers the assembled carousel with Approve/Reject buttons.

    Loads the preview artifacts (cover poster, body slides, CTA slide) from
    the artifact store, writes them to local files under the workdir, and
    sends them to Telegram as a photo album plus a message carrying the
    Approve/Reject links. Increments the review round in session state -
    but only after the message was actually sent.

    Returns:
        ``{"status": "sent", "message_id": ..., "round": ...}`` on success or
        ``{"status": "error", "error": ...}`` on failure (in which case the
        run must NOT be paused for review).
    """
    state = tool_context.state
    run_id = str(state.get(K_RUN_ID) or "")
    if not run_id:
        return {"status": "error", "error": f"'{K_RUN_ID}' missing from state."}
    raw_bundle = state.get(K_BUNDLE)
    if not raw_bundle:
        return {
            "status": "error",
            "error": f"'{K_BUNDLE}' missing from state - run stitch_verify first.",
        }
    try:
        bundle = (
            raw_bundle
            if isinstance(raw_bundle, Bundle)
            else Bundle.model_validate(raw_bundle)
        )
    except Exception as exc:
        return {"status": "error", "error": f"Bundle in state is malformed: {exc}"}

    round_no = int(state.get(K_REVIEW_ROUND) or 0) + 1

    preview_dir = settings.workdir / "review_previews" / run_id
    poster_path = await _materialize_artifact(
        tool_context, bundle.cover.poster_artifact, preview_dir
    )
    slide_paths: list[str] = []
    slide_artifacts = [s.artifact for s in bundle.slides if s.artifact]
    if bundle.cta.artifact:
        slide_artifacts.append(bundle.cta.artifact)
    for artifact_name in slide_artifacts:
        local = await _materialize_artifact(tool_context, artifact_name, preview_dir)
        if local:
            slide_paths.append(local)

    payload = bundle.model_dump(mode="json")
    payload["preview_paths"] = {
        "poster": poster_path,
        "slides": slide_paths,
    }
    news_item = state.get(K_NEWS_ITEM) or {}
    payload["news_title"] = (
        str(news_item.get("title") or "") or bundle.cover.title or "Untitled carousel"
    )

    try:
        # telegram_tools.send_review_message is synchronous (httpx with an
        # explicit 60 s timeout); run it off the event loop.
        result = await asyncio.to_thread(
            telegram_tools.send_review_message, run_id, payload, round_no
        )
    except Exception as exc:
        logger.exception("Review message failed for run %s round %s.", run_id, round_no)
        return {"status": "error", "error": f"Review message failed: {exc}"}

    state[K_REVIEW_ROUND] = round_no  # count the round only after a real send
    state[_SENT_KEY] = round_no  # ...and authorise the pause for THIS round
    logger.info(
        "Review message sent for run %s (round %s, %d preview file(s)).",
        run_id,
        round_no,
        len(slide_paths) + (1 if poster_path else 0),
    )
    return {
        "status": "sent",
        "message_id": result.get("message_id", ""),
        "round": round_no,
        "previews_attached": len(slide_paths) + (1 if poster_path else 0),
    }


async def _materialize_artifact(
    tool_context: ToolContext, filename: str, target_dir: Path
) -> str:
    """Load one artifact and write its bytes to a local preview file.

    Args:
        tool_context: The tool context (artifact access).
        filename: Artifact filename ("" is silently skipped).
        target_dir: Local directory for preview files (created if needed).

    Returns:
        The absolute local path as ``str``, or ``""`` when the artifact is
        unavailable (missing service, missing artifact, no inline bytes) - the
        mail then simply carries fewer previews.
    """
    if not filename:
        return ""
    try:
        part = await tool_context.load_artifact(filename)
    except Exception as exc:
        logger.warning("Could not load artifact '%s' for preview: %s", filename, exc)
        return ""
    if part is None or part.inline_data is None or part.inline_data.data is None:
        logger.warning("Artifact '%s' has no inline bytes; preview skipped.", filename)
        return ""
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / Path(filename).name  # flatten: never traverse
    local_path.write_bytes(part.inline_data.data)
    return str(local_path)


async def await_human_review(tool_context: ToolContext) -> Optional[dict]:
    """Pause the pipeline until a human approves or rejects the carousel.

    Long-running operation: it returns no result now; the reviewer's decision
    arrives later as the function response that resumes this run. Before
    pausing, the pending call is persisted (run id, session id, function call
    id) so the review API can address the resume to exactly this call.

    Pausing is not this tool's decision to make, which is the thing that
    matters here. google-adk reads ``long_running_tool_ids`` off the MODEL
    RESPONSE that calls this tool - the invocation is already ending by the
    time the tool body runs, so returning a value cannot call it back. An
    earlier version returned an error dict to "refuse" the pause; the run
    paused regardless and simply gained a second, contradictory story, with
    the model reporting "the run did not pause" into a trace whose very next
    line was "waiting for a human".

    So this records whether the pause is ANSWERABLE, and lets it happen either
    way. It is answerable when the reviewers were actually told (``send_review
    _request`` stamps the round it sent) and the pending call was persisted -
    without a ``pending_reviews`` row no surface can build a
    ``FunctionResponse`` addressed to this call.

    When it is not answerable, ``K_REVIEW_NOTICE_FAILED`` goes up and any
    stale pending row is dropped. That is not a cosmetic flag: it is what
    routes the console to ``_decide_without_pause``, which records a verdict
    straight into session state and re-enters the run - the one path that
    works without a live function call. Leaving the flag down instead made a
    stranded run look perfectly healthy.

    Returns:
        Always ``None``. A falsy long-running result produces no
        function-response event, which is the shape ADK expects for a paused
        call; the honest signal about whether the pause can be answered is in
        session state, not in a return value the model would narrate.
    """
    state = tool_context.state
    run_id = str(state.get(K_RUN_ID) or "")
    session_id = tool_context.session.id
    call_id = tool_context.function_call_id or ""

    async def _unanswerable(reason: str) -> None:
        """Flag the pause so the console can decide the run without it."""
        logger.error(
            "Review pause for run %s cannot be answered (%s); flagging "
            "'%s' so the console offers the decide-without-pause path.",
            run_id or "(unset)",
            reason,
            K_REVIEW_NOTICE_FAILED,
        )
        state[K_REVIEW_NOTICE_FAILED] = True
        # Any surviving row belongs to an earlier round whose call id is dead.
        # Left in place, submit_verdict would take the claim path and resume
        # against a function call that no longer exists.
        await _clear_pending_review_quietly(run_id)

    round_no = int(state.get(K_REVIEW_ROUND) or 0)
    sent_round = int(state.get(_SENT_KEY) or 0)
    if not round_no or sent_round != round_no:
        await _unanswerable(
            f"no successful send for review round {round_no or '(unset)'}; "
            f"last sent round was {sent_round or 'none'}"
        )
        return None

    if not run_id or not call_id:
        await _unanswerable(
            f"missing identifiers (run_id={run_id!r}, call_id={call_id!r})"
        )
        return None

    try:
        await db.save_pending_review(run_id, session_id, call_id)
    except Exception as exc:
        await _unanswerable(f"the pending review could not be saved: {exc}")
        return None

    state[K_REVIEW_NOTICE_FAILED] = False
    logger.info(
        "Pipeline paused for human review: run %s, session %s, call %s.",
        run_id,
        session_id,
        call_id,
    )
    return None


async def set_verdict(
    status: str, feedback: str = "", tool_context: ToolContext = None
) -> dict:
    """Record the human review verdict in session state.

    Call after the run resumed with the reviewer's response. The authoritative
    values are re-read from the resuming function response itself whenever it
    is present - the ``status``/``feedback`` arguments only matter when no
    reviewer response exists in the invocation (manual override paths).

    Args:
        status: "approved" or "rejected".
        feedback: The reviewer's feedback text, verbatim. Optional when
            approving; required when rejecting.

    A verdict is consumed exactly once. Within a single invocation the run can
    loop review -> rework -> review, and the round-1 function response is still
    the invocation's ``user_content`` on the round-2 entry - so a model that
    calls this tool during a SEND_MAIL turn would re-record the round-1
    decision against a carousel that no longer exists. An old "approved" would
    send the reworked carousel to publish with no human ever seeing it. The
    consumed-id set that ``capture_verdict_on_resume`` maintains is the record
    of what has already been acted on, and this tool now honours it.

    Returns:
        ``{"status": "recorded", "verdict": ..., "feedback": ...}`` or an
        ``{"status": "error", ...}`` dict describing what to correct.
    """
    state = tool_context.state
    latest = _latest_review_response(tool_context.user_content)
    authoritative = _fresh_review_response(tool_context.user_content, state)
    directive = state.get(_DIRECTIVE_KEY)

    # A consumed id means two completely different things, and telling them
    # apart is the whole job here.
    #
    # ``capture_verdict_on_resume`` runs BEFORE this tool on every resume and
    # consumes the id itself - so on the ordinary HANDLE_VERDICT turn, the one
    # where a human just clicked Approve, the id is already consumed by the
    # time the model does as it was told and calls this. Refusing there fired
    # on 100% of legitimate verdicts and told the model to raise a NEW review
    # round for a carousel that had just been approved.
    #
    # The case the guard was actually written for is the SEND_MAIL re-entry: a
    # rework loop inside one invocation, where the round-1 response is still
    # the invocation's user_content and recording it again would apply an old
    # decision to a carousel that has since been rebuilt.
    #
    # The directive separates them, because it is set by the same callback
    # from the same evidence.
    if latest is not None and authoritative is None:
        if directive == _DIRECTIVE_HANDLE_VERDICT:
            # The callback already recorded exactly this verdict; saying so is
            # both true and idempotent.
            recorded = _verdict_from_payload(dict(latest.response or {}))
            logger.info(
                "set_verdict for run %s: verdict '%s' was already recorded by "
                "the resume callback; reporting it rather than re-recording.",
                state.get(K_RUN_ID),
                recorded.status,
            )
            return {
                "status": "recorded",
                "verdict": recorded.status,
                "feedback": recorded.feedback,
            }
        logger.warning(
            "set_verdict refused for run %s: the reviewer response %s was "
            "consumed by an earlier round of this invocation. Recording it "
            "again would apply an old decision to a reworked carousel.",
            state.get(K_RUN_ID),
            latest.id,
        )
        return {
            "status": "error",
            "error": (
                "That verdict was already recorded and acted on. You are "
                "requesting a NEW review round, not handling a decision - "
                "call send_review_request instead."
            ),
        }

    # No reviewer response at all, on a turn whose job is to ASK for one.
    #
    # The docstring below promises that "even a confused model cannot record a
    # verdict different from what the human submitted" - which holds only when
    # a reviewer response exists to check against. Without one, the argument
    # branch would take the model's word for it, and a model that called this
    # instead of send_review_request could approve a carousel no human had
    # seen. The argument branch exists for manual overrides; nothing in the
    # codebase calls it that way (the console writes K_VERDICT directly), so
    # the only reachable caller was a mistake.
    if latest is None and directive == _DIRECTIVE_SEND_MAIL:
        logger.warning(
            "set_verdict refused for run %s: no reviewer response in this "
            "invocation and the dispatcher is in SEND_MAIL mode.",
            state.get(K_RUN_ID),
        )
        return {
            "status": "error",
            "error": (
                "There is no reviewer decision to record - nobody has replied "
                "yet. Call send_review_request to ask for one."
            ),
        }

    if authoritative is not None:
        verdict = _verdict_from_payload(dict(authoritative.response or {}))
        arg_status = str(status or "").strip().lower()
        if arg_status and arg_status != verdict.status:
            logger.warning(
                "set_verdict args (%r) disagree with the reviewer response "
                "(%r); the reviewer response wins.",
                arg_status,
                verdict.status,
            )
    else:
        clean_status = str(status or "").strip().lower()
        if clean_status not in ("approved", "rejected"):
            return {
                "status": "error",
                "error": "status must be 'approved' or 'rejected'.",
            }
        clean_feedback = str(feedback or "").strip()
        if clean_status == "rejected" and not clean_feedback:
            return {
                "status": "error",
                "error": "feedback is required when the verdict is 'rejected'.",
            }
        verdict = Verdict(status=clean_status, feedback=clean_feedback)

    set_model(state, K_VERDICT, verdict)
    if authoritative is not None and authoritative.id:
        consumed = list(state.get(_CONSUMED_KEY) or [])
        if authoritative.id not in consumed:
            consumed.append(authoritative.id)
            state[_CONSUMED_KEY] = consumed

    run_id = str(state.get(K_RUN_ID) or "")
    await _clear_pending_review_quietly(run_id)
    logger.info("Verdict recorded for run %s: %s", run_id, verdict.status)
    return {
        "status": "recorded",
        "verdict": verdict.status,
        "feedback": verdict.feedback,
    }


# ---------------------------------------------------------------------------
# Instruction + builder
# ---------------------------------------------------------------------------
def _ensure_default_instruction_file() -> None:
    """Write the fallback instruction to ``skills/agents/review_dispatcher.md`` if absent.

    The file is the editable source of truth for this agent's instruction
    (the Learner agent appends learned rules to it), so it is only created
    when missing - an existing file is never overwritten.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_REVIEW_DISPATCHER}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_INSTRUCTION, encoding="utf-8")


def _instruction_provider(ctx: ReadonlyContext) -> str:
    """Assemble the dispatcher instruction with the current-mode directive.

    InstructionProvider (google-adk 2.7.0 resolves callables with
    ``bypass_state_injection=True`` - no ``{var}`` templating pitfalls).
    Loads ``skills/agents/review_dispatcher.md`` fresh from disk, then appends
    run context and the SEND_MAIL / HANDLE_VERDICT directive computed by the
    ``before_agent_callback`` (with an equivalent read-only fallback when the
    stamp is absent). The directive is stable across every LLM call of one
    agent run, including the calls made after tool responses.

    Args:
        ctx: Read-only invocation view (state + user_content).

    Returns:
        The complete instruction string for this LLM call.
    """
    base = agent_instructions(AGENT_REVIEW_DISPATCHER) or _DEFAULT_INSTRUCTION
    state = ctx.state
    parts = [base]

    run_id = str(state.get(K_RUN_ID) or "")
    round_no = int(state.get(K_REVIEW_ROUND) or 0)
    parts.append(
        "## Run context\n\n"
        f"- run_id: {run_id or '(unset)'}\n"
        f"- review mails sent so far: {round_no}"
    )

    rework = str(state.get(K_REWORK_FEEDBACK) or "").strip()
    if rework:
        parts.append(
            "## Context: latest correction feedback (highest priority)\n\n"
            "This run went through corrections driven by the feedback below. "
            "Treat it as the authoritative context when summarizing - the new "
            "review round exists because of it:\n\n"
            f"{rework}"
        )

    directive = state.get(_DIRECTIVE_KEY)
    if directive is None:  # defensive: callback should always have stamped it
        directive = (
            _DIRECTIVE_HANDLE_VERDICT
            if _fresh_review_response(ctx.user_content, state) is not None
            else _DIRECTIVE_SEND_MAIL
        )

    if directive == _DIRECTIVE_HANDLE_VERDICT:
        raw_verdict = state.get(K_VERDICT) or {}
        status = str(raw_verdict.get("status") or "") if isinstance(raw_verdict, dict) else ""
        feedback = str(raw_verdict.get("feedback") or "") if isinstance(raw_verdict, dict) else ""
        parts.append(
            "## CURRENT MODE: HANDLE_VERDICT\n\n"
            "The human reviewer has responded. Recorded verdict so far: "
            f"status='{status}', feedback='{feedback}'.\n"
            "Call `set_verdict` NOW with exactly this status and feedback "
            "(the tool re-checks the reviewer response itself). Do NOT call "
            "`send_review_request` or `await_human_review`. Then reply with one "
            "short sentence stating the verdict."
        )
    else:
        # Name the round explicitly on rounds 2+. Without it a rework round
        # looks like a repeat of a request already sent, and the model talks
        # itself out of sending - which leaves the reworked carousel
        # finished, the run halted at 'review', and nobody told about it.
        next_round = int(state.get(K_REVIEW_ROUND) or 0) + 1
        again = (
            f" This is review round {next_round}: the carousel has been "
            "reworked since the last request, so a NEW review request is "
            "required and expected. Do not refuse it on the grounds that "
            "one was already sent."
            if next_round > 1
            else ""
        )
        parts.append(
            "## CURRENT MODE: SEND_MAIL\n\n"
            "No fresh human verdict is pending. Call `send_review_request` now; "
            "if its result status is 'sent', call `await_human_review` to "
            "pause for the reviewer. If the mail failed, call nothing else "
            "and report the error in one short sentence." + again
        )
    return "\n\n".join(parts)


def build_review_dispatcher_agent() -> LlmAgent:
    """Build the Review Dispatcher agent.

    Returns:
        An :class:`~google.adk.agents.LlmAgent` named
        :data:`app.state.AGENT_REVIEW_DISPATCHER` running
        ``settings.utility_model`` with three tools - ``send_review_request``,
        ``await_human_review`` (LongRunningFunctionTool: pauses the
        invocation) and ``set_verdict`` - plus a ``before_agent_callback``
        that deterministically records the resumed verdict in ``K_VERDICT``.
        Tool-using agent: no ``output_schema``; all state is written inside
        tools/callbacks via the 2.7.0 delta-aware context state.
    """
    _ensure_default_instruction_file()
    return LlmAgent(
        name=AGENT_REVIEW_DISPATCHER,
        model=resolve_model(settings.utility_model),
        description=(
            "Review Dispatcher: sends the reviewers the assembled carousel on "
            "Telegram with Approve/Reject buttons, pauses the pipeline on a "
            "long-running "
            "await_human_review call, and records the human verdict in state "
            "when the run resumes."
        ),
        instruction=_instruction_provider,
        tools=[
            FunctionTool(send_review_request),
            LongRunningFunctionTool(await_human_review),
            FunctionTool(set_verdict),
        ],
        before_agent_callback=capture_verdict_on_resume,
        # Orchestrator-driven pipeline node: never LLM-transfer elsewhere.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


__all__ = [
    "AWAIT_REVIEW_TOOL_NAME",
    "await_human_review",
    "build_review_dispatcher_agent",
    "capture_verdict_on_resume",
    "send_review_request",
    "set_verdict",
]
