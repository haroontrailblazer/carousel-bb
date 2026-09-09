"""Turning ADK events into a timeline the console can render.

One function does the consuming - :func:`consume_invocation` - and BOTH a fresh
run and a review resume go through it. That is the point. If the resume path
kept its own loop, approving from Telegram would advance the pipeline while an
open browser tab showed nothing, and the page would look dead exactly when the
interesting part (rework, then publishing) was happening.

Two rules about what gets recorded:

* **Structure comes from the state delta, never from the log text.** The
  orchestrator emits human lines like ``[phase] generate -> qa`` and
  ``[rework] round 2/5: re-running planner``, but it also puts the real values
  in ``event.actions.state_delta`` (``phase``, ``rework_round``,
  ``review_round``). The frontend reads the delta; the text is display copy
  only. Regex-parsing the prose would couple the UI to log wording and break
  the moment someone improves a message.
* **This is a distilled timeline, not a transcript.** The full ADK event log
  already lives in ADK's own ``events`` table and is what the ``/dev``
  inspector is for. Persisting every function-call payload again would bloat
  the database for no one's benefit.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.runs.bus import (
    BUS,
    KIND_ERROR,
    KIND_TERMINAL,
    KIND_PHASE,
    KIND_PROGRESS,
    KIND_TOOL,
    RunEvent,
)
from app.services import db
from app.state import K_PHASE, K_REVIEW_ROUND, K_REWORK_ROUND, K_TOKEN_USAGE

logger = logging.getLogger(__name__)

#: State-delta keys worth forwarding to the browser. Everything else in the
#: delta is pipeline payload (the plan, the bundle, rendered slides) that the
#: console fetches from the run detail endpoint instead - putting it on the
#: event stream would push megabytes through every open tab.
_FORWARDED_STATE_KEYS = (K_PHASE, K_REWORK_ROUND, K_REVIEW_ROUND, K_TOKEN_USAGE)


def summarize_event(event: Any) -> str:
    """One human-readable line for a pipeline event ('' when there is none).

    Moved here from ``fetcher.fetch_news`` so the CLI, the run service and the
    resume path all describe an event the same way.
    """
    content = getattr(event, "content", None)
    parts = content.parts if content is not None and content.parts else []
    fragments: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text and text.strip():
            fragments.append(text.strip())
        elif getattr(part, "function_call", None) is not None:
            fragments.append(f"-> tool {part.function_call.name}")
        elif getattr(part, "function_response", None) is not None:
            fragments.append(f"<- tool {part.function_response.name}")
    if not fragments:
        return ""
    author = getattr(event, "author", "") or "?"
    return f"[{author}] " + " | ".join(fragments)


def _state_delta(event: Any) -> dict:
    """The event's state delta as a plain dict (empty when it has none)."""
    actions = getattr(event, "actions", None)
    delta = getattr(actions, "state_delta", None) if actions is not None else None
    return dict(delta) if delta else {}


def _tool_names(event: Any) -> tuple[list[str], list[str]]:
    """Tool calls and tool responses named in this event."""
    content = getattr(event, "content", None)
    parts = content.parts if content is not None and content.parts else []
    calls, responses = [], []
    for part in parts:
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None):
            calls.append(call.name)
        resp = getattr(part, "function_response", None)
        if resp is not None and getattr(resp, "name", None):
            responses.append(resp.name)
    return calls, responses


def classify_event(event: Any) -> tuple[str, dict]:
    """Decide an event's kind and the structured payload to send with it.

    Returns:
        ``(kind, data)`` where kind is one of the bus KIND_* constants.
    """
    data: dict = {}
    delta = _state_delta(event)
    for key in _FORWARDED_STATE_KEYS:
        if key in delta:
            data[key] = delta[key]

    if getattr(event, "error_message", None):
        data["error"] = str(event.error_message)
        return KIND_ERROR, data

    # A phase transition is the single most useful thing the UI can know, so it
    # wins over the tool/progress classification below.
    if K_PHASE in delta:
        return KIND_PHASE, data

    # A paused review, flagged BEFORE the tool branch below can return.
    #
    # The single event that ends an invocation is the model response that
    # CALLS the long-running tool: it carries the function call and the
    # pending id in long_running_tool_ids at the same time. Checking for the
    # pause after the tool branch therefore meant the one event that matters
    # always returned early as an ordinary tool call, `awaiting_review` was
    # never set, and consume_invocation reported paused=False for a run that
    # had genuinely stopped and was waiting for a human.
    #
    # Downstream, _drive_run reads that flag to choose the ending. Getting it
    # wrong recorded a paused run as 'interrupted', which on the resume path -
    # where no phase transition had already written 'awaiting_review' - left
    # the console offering Resume on a task that was actually waiting for a
    # decision. Resuming re-entered review, paused again, and was recorded
    # interrupted again: a loop with no exit.
    if getattr(event, "long_running_tool_ids", None):
        data["awaiting_review"] = True

    calls, responses = _tool_names(event)
    if calls or responses:
        if calls:
            data["tool_calls"] = calls
        if responses:
            data["tool_responses"] = responses
        return KIND_TOOL, data

    return KIND_PROGRESS, data



# ---------------------------------------------------------------------------
# Merging the two records of what happened
# ---------------------------------------------------------------------------
#: Kinds that CAN exist only in run_events. Necessary but not sufficient - see
#: the author test in ``_lifecycle_frames``.
_LIFECYCLE_KINDS = (KIND_TERMINAL, KIND_ERROR)


async def _lifecycle_frames(run_id: str, limit: int) -> list[dict]:
    """The endings and restarts that live only in ``run_events``.

    Six timeline entries are written by ``record_event`` from outside an
    invocation, so ADK never sees them: ``_drive_run``'s terminal line,
    ``_finish_badly``'s "Run failed: {exc}" and "Run cancelled.",
    ``resume_interrupted_run``'s "Resuming interrupted run at phase ...",
    ``cancel_run``'s stale-stop notice, and recovery's "the service restarted".

    They are the only record of WHY a task died, which is exactly what the
    console opens a failed task's trace tab to show.
    """
    try:
        rows = await db.load_run_events(run_id, after=0, limit=limit)
    except Exception as exc:
        logger.warning("Could not read run_events for %s: %s", run_id, exc)
        return []
    # The kind alone is not enough. consume_invocation ALSO records KIND_ERROR
    # for an ordinary agent error inside an invocation - and ADK has that event
    # too, so merging on kind duplicated every one of them: the same failure
    # printed twice in the trace, once from each source.
    #
    # What actually separates the two is the author. Everything synthesised
    # from outside an invocation is written with no author, because there is no
    # agent to name; everything consume_invocation records carries the agent
    # that emitted it.
    return [
        {**r, "id": f"evt:{r.get('seq')}"}
        for r in rows
        if str(r.get("kind") or "") in _LIFECYCLE_KINDS
        and not str(r.get("author") or "").strip()
    ]


def _merge_by_time(frames: list[dict], extra: list[dict]) -> list[dict]:
    """Interleave two already-sorted frame lists on ``created_at``.

    Sequence numbers are reassigned across the merged result, because the two
    sources count different things and a client that dedupes on ``seq`` has to
    be handed one coherent space. Frames with no usable timestamp sort last
    rather than being dropped - an ending with a missing timestamp is still
    the answer to "what happened to my carousel".
    """
    if not extra:
        return frames

    def key(frame: dict) -> tuple[int, float]:
        parsed = _parse_ts(frame.get("created_at") or frame.get("ts"))
        return (0, parsed.timestamp()) if parsed else (1, 0.0)

    merged = sorted([*frames, *extra], key=key)
    for index, frame in enumerate(merged, start=1):
        frame["seq"] = index
    return merged


async def record_event(
    run_id: str,
    seq: int,
    kind: str,
    author: str = "",
    text: str = "",
    data: Optional[dict] = None,
) -> RunEvent:
    """Persist a timeline event and hand it to every watching browser.

    Persistence first, then publish: a browser that receives an event and then
    reloads must find that event in the replay. The other order would let a
    reload appear to LOSE something the user had already seen.

    Database failures are logged, never raised. Losing a log line must not kill
    a run that is otherwise fine, and the run's real state lives in the ADK
    session either way.
    """
    event = RunEvent(
        run_id=run_id,
        seq=seq,
        kind=kind,
        author=author,
        text=text,
        data=dict(data or {}),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        await db.append_run_event(
            run_id, seq, kind, author=author, text=text, data=event.data
        )
    except Exception as exc:
        logger.warning("Could not persist event %s for run %s: %s", seq, run_id, exc)
    await BUS.publish(event)
    return event


async def consume_invocation(
    runner: Any,
    *,
    run_id: str,
    session_id: str,
    user_id: str,
    new_message: Any,
    seq_start: Optional[int] = None,
) -> dict:
    """Drive one invocation to its end, recording the timeline as it goes.

    Used for a fresh run AND for a review resume, which is what lets a verdict
    submitted in Telegram stream its rework and publish progress into a browser
    tab that is already open on that run.

    An invocation ends either at the next review pause or at ``done``; both are
    normal. Nothing here decides what happens next - the caller does.

    Args:
        runner: A built ADK Runner.
        run_id: The run being driven (timeline key).
        session_id: The ADK session id.
        user_id: The ADK user id (the fixed pipeline user).
        new_message: The Content starting this invocation.
        seq_start: Sequence to continue from. Defaults to the run's current
            maximum, so a resumed leg continues the numbering instead of
            restarting at 1 and colliding with the first leg's events.

    Returns:
        ``{"events": int, "last_seq": int, "paused": bool, "phase": str|None}``
    """
    if seq_start is None:
        try:
            seq_start = await db.max_run_seq(run_id)
        except Exception as exc:
            logger.warning(
                "Could not read the event cursor for run %s (%s); starting at 0.",
                run_id,
                exc,
            )
            seq_start = 0

    counter = itertools.count(seq_start + 1)
    count = 0
    last_seq = seq_start
    paused = False
    phase: Optional[str] = None

    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=new_message
    ):
        count += 1
        kind, data = classify_event(event)
        text = summarize_event(event)
        author = getattr(event, "author", "") or ""

        if data.get(K_PHASE):
            phase = str(data[K_PHASE])
        if data.get("awaiting_review"):
            paused = True
        if kind == KIND_ERROR:
            logger.warning(
                "Run %s: event from %s reported an error: %s",
                run_id,
                author or "?",
                data.get("error"),
            )

        # Events with no text and nothing structured say nothing a human or the
        # UI can use; recording them would pad the timeline with blanks.
        if not text and not data:
            continue

        last_seq = next(counter)
        await record_event(
            run_id, last_seq, kind, author=author, text=text, data=data
        )

    return {"events": count, "last_seq": last_seq, "paused": paused, "phase": phase}


__all__ = [
    "classify_event",
    "consume_invocation",
    "record_event",
    "summarize_event",
]


# ---------------------------------------------------------------------------
# ADK's raw events -> the console's timeline
# ---------------------------------------------------------------------------
#: ADK serialises events with snake_case keys, but google-genai types round-trip
#: as camelCase in some versions. Reading both means the trace does not silently
#: go blank after an ADK upgrade.
def _pick(data: dict, *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def _summarize_parts(parts: list) -> tuple[str, list[str], list[str], list[str]]:
    """Render one event's parts, and name the tools it used.

    Returns ``(text, tool_calls, tool_responses, thoughts)``.
    """
    fragments: list[str] = []
    calls: list[str] = []
    responses: list[str] = []
    thoughts: list[str] = []

    for part in parts or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        call = _pick(part, "function_call", "functionCall")
        resp = _pick(part, "function_response", "functionResponse")

        if text and str(text).strip():
            # A "thought" part is the model reasoning, which the dev UI shows
            # separately from its answer.
            if part.get("thought"):
                thoughts.append(str(text).strip())
            else:
                fragments.append(str(text).strip())
        elif isinstance(call, dict):
            name = str(call.get("name") or "tool")
            calls.append(name)
            args = call.get("args") or {}
            # One short line per call, the way the dev UI previews arguments.
            preview = ", ".join(
                f"{k}={json.dumps(v, default=str)[:60]}" for k, v in list(args.items())[:3]
            )
            fragments.append(f"-> {name}({preview})" if preview else f"-> {name}()")
        elif isinstance(resp, dict):
            name = str(resp.get("name") or "tool")
            responses.append(name)
            payload = resp.get("response")
            preview = json.dumps(payload, default=str)[:140] if payload is not None else ""
            fragments.append(f"<- {name}: {preview}" if preview else f"<- {name}")

    return " | ".join(fragments), calls, responses, thoughts


def adk_event_to_frame(row: dict) -> dict:
    """Convert one stored ADK event into a timeline frame.

    Deliberately faithful to what /dev shows - author, tool calls with their
    arguments, tool responses, model thoughts, token usage - because a trace
    that quietly says less than the inspector is worse than no trace: it looks
    complete while hiding the thing you are looking for.
    """
    data = row.get("event_data") or {}
    author = str(data.get("author") or "")
    content = data.get("content") or {}
    parts = content.get("parts") or []

    text, calls, responses, thoughts = _summarize_parts(parts)

    actions = data.get("actions") or {}
    delta = _pick(actions, "state_delta", "stateDelta") or {}
    payload: dict = {}
    for key in _FORWARDED_STATE_KEYS:
        if key in delta:
            payload[key] = delta[key]

    if calls:
        payload["tool_calls"] = calls
    if responses:
        payload["tool_responses"] = responses
    if thoughts:
        payload["thoughts"] = thoughts

    usage = _pick(data, "usage_metadata", "usageMetadata") or {}
    if usage:
        payload["tokens"] = {
            "prompt": _pick(usage, "prompt_token_count", "promptTokenCount"),
            "output": _pick(usage, "candidates_token_count", "candidatesTokenCount"),
            "total": _pick(usage, "total_token_count", "totalTokenCount"),
        }
    model = _pick(data, "model_version", "modelVersion")
    if model:
        payload["model"] = model

    error = _pick(data, "error_message", "errorMessage")
    if _pick(data, "long_running_tool_ids", "longRunningToolIds"):
        payload["awaiting_review"] = True

    if error:
        kind = KIND_ERROR
        payload["error"] = str(error)
    elif K_PHASE in payload:
        kind = KIND_PHASE
    elif calls or responses:
        kind = KIND_TOOL
    else:
        kind = KIND_PROGRESS

    return {
        # Stable across every read, unlike `seq`.
        #
        # seq is a POSITION, and the two sources number positions differently:
        # ADK frames are numbered by their place in the transcript, live bus
        # frames by the run_events counter, and the merge renumbers everything
        # again by timestamp. A client deduping on seq therefore compares two
        # different coordinate systems - which silently drops a real frame
        # when the numbers happen to collide and shows one twice when they do
        # not. An identity that comes from the event itself has neither
        # problem.
        "id": f"adk:{_pick(row.get('event_data') or {}, 'id') or row.get('seq', 0)}",
        "seq": row.get("seq", 0),
        "kind": kind,
        "author": author,
        "text": text,
        "data": payload,
        "created_at": row.get("created_at"),
    }


async def load_trace(run_id: str, after: int = 0, limit: int = 2000) -> list[dict]:
    """A run's timeline, preferring ADK's own transcript.

    ADK records every run it drives, whichever surface started it - the
    console, the CLI, or the dev UI - so reading from there means every task
    has a trace and it matches the inspector. Our ``run_events`` table only
    covers console-started runs, so it is the fallback rather than the source.
    """
    from app.config import settings

    try:
        from app.runs.service import PIPELINE_USER_ID
    except Exception:  # pragma: no cover - import wiring
        PIPELINE_USER_ID = "pipeline"

    rows: list[dict] = []
    try:
        rows = await db.load_adk_events(
            settings.app_name, PIPELINE_USER_ID, run_id, after=0, limit=limit
        )
    except Exception as exc:
        logger.warning("Could not read the ADK transcript for %s: %s", run_id, exc)

    if not rows:
        return await db.load_run_events(run_id, after=after, limit=limit)

    # Merge, do not choose. ADK's transcript is the better source for what the
    # agents did, but it cannot contain the lifecycle lines - so preferring it
    # exclusively meant a failed task showed a red chip above a trace that
    # simply stopped, with the exception text sitting unread in run_events.
    frames = _merge_by_time(
        [adk_event_to_frame(r) for r in rows],
        await _lifecycle_frames(run_id, limit),
    )
    if len(rows) >= limit:
        # The ADK read was capped before the merge, so the newest frames of a
        # very long run are not in `frames` at all. Say so: a silently short
        # trace is indistinguishable from a run that stopped early.
        logger.warning(
            "Trace for %s hit the %d-frame limit; the newest events are not "
            "in this page.",
            run_id,
            limit,
        )
    return frames[after : after + limit]


# ---------------------------------------------------------------------------
# Trace assembly: latency, tokens, and tool detail
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp, tolerating a trailing Z and None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tool_parts(parts: list) -> tuple[list[dict], list[dict]]:
    """Extract structured tool calls and responses from an event's parts."""
    calls: list[dict] = []
    responses: list[dict] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        call = _pick(part, "function_call", "functionCall")
        resp = _pick(part, "function_response", "functionResponse")
        if isinstance(call, dict):
            calls.append(
                {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or "tool"),
                    "args": call.get("args") or {},
                }
            )
        elif isinstance(resp, dict):
            responses.append(
                {
                    "id": str(resp.get("id") or ""),
                    "name": str(resp.get("name") or "tool"),
                    "response": resp.get("response"),
                }
            )
    return calls, responses


def _truncate(value: Any, limit: int = 1200) -> str:
    """Render a value for display, bounded so one huge payload cannot bloat
    the whole trace response."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
    return text if len(text) <= limit else text[:limit] + f"\n… (+{len(text) - limit} chars)"


_URL_RE = re.compile(r"https?://[^\s<>\"'\]\[)]+")


def _source_urls(value: Any, limit: int = 24) -> list[str]:
    """Collect ordered HTTP references from one tool response.

    Search tools return sources in a few shapes (``sources`` arrays,
    ``source_url`` fields, citations embedded in an answer).  Normalising
    those shapes here keeps the frontend presentational and, importantly,
    preserves the exact references recorded in ADK's transcript.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        cleaned = url.rstrip(".,;:!?")
        if cleaned in seen or not cleaned.startswith(("http://", "https://")):
            return
        seen.add(cleaned)
        found.append(cleaned)

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, str):
            for match in _URL_RE.findall(node):
                add(match)
                if len(found) >= limit:
                    break
            return
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
                if len(found) >= limit:
                    break
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
                if len(found) >= limit:
                    break

    walk(value)
    return found


_BRIEF_TOOL = "save_research_brief"
_MAX_FACTS = 12
_MAX_FACT_CHARS = 400


def _research_facts(args: Any) -> list[dict]:
    """Lift the brief's per-fact citations out of a ``save_research_brief`` call.

    The Research agent already records WHICH URL each fact was verified at -
    ``ResearchFact.source_url`` - but that mapping only ever existed inside
    the tool arguments, which the trace truncates to 600 characters for
    display. Pulling it out here is what lets the console cite a claim rather
    than list a bag of links beside it and leave the reader to guess which
    link proved what.

    A fact with no URL is kept, uncited: it came from the news item's own
    text, which is honest and different from an unsourced invention.
    """
    if not isinstance(args, dict):
        return []
    facts: list[dict] = []
    for item in args.get("key_facts") or []:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact") or "").strip()
        if not fact:
            continue
        url = str(item.get("source_url") or "").strip()
        facts.append(
            {
                "fact": fact[:_MAX_FACT_CHARS],
                "source_url": url if url.startswith(("http://", "https://")) else "",
            }
        )
        if len(facts) >= _MAX_FACTS:
            break
    return facts


def _brief_sources(args: Any) -> list[str]:
    """The URLs a ``save_research_brief`` call declares it consulted.

    Deliberately narrow: only ``sources`` and each fact's ``source_url``, not
    a walk of the whole payload. ``media_candidates`` are also URLs, but they
    are footage to clip for the cover rather than anything that was read, and
    listing them under "sources" would be a lie of categorisation.
    """
    if not isinstance(args, dict):
        return []
    urls = list(args.get("sources") or [])
    for item in args.get("key_facts") or []:
        if isinstance(item, dict) and item.get("source_url"):
            urls.append(item["source_url"])
    return _source_urls(urls)


def build_trace(rows: list[dict]) -> tuple[list[dict], dict]:
    """Turn stored ADK events into display frames plus a summary.

    Two things here are worth more than the raw log:

    * **Tool latency.** A call and its response are separate events, linked by
      the call id. Pairing them gives the wall-clock cost of each individual
      tool - which search took nine seconds, which image render took ninety -
      and that is usually the first question anyone asks of a slow run.
    * **Per-agent cost.** ADK reports token usage per event; rolling it up by
      author answers "what did this run actually spend, and where".

    Returns:
        ``(frames, summary)``.
    """
    frames: list[dict] = []
    pending: dict[str, dict] = {}          # call id -> {frame, tool, started}
    agents: dict[str, dict] = {}
    order: list[str] = []
    totals = {"prompt": 0, "output": 0, "total": 0}
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None

    # Time is accumulated PER INVOCATION, not measured first-event-to-last.
    #
    # A run pauses for a human at review and only continues when someone
    # decides, so the wall clock between its first and last event includes
    # every hour it sat waiting. Measured on a real task: 1,393 seconds of
    # actual work reported as 84,634 seconds - a run that took 23 minutes
    # displayed as 23 hours.
    #
    # Each stretch of work is one ADK invocation: the initial run is one, and
    # every resume starts another. Summing their spans gives the time agents
    # actually ran, and makes a resumed task add to its earlier total rather
    # than absorbing the gap. A re-run is a different run id entirely, so it
    # starts from zero without any special handling.
    spans: dict[str, list[datetime]] = {}
    agent_spans: dict[tuple[str, str], list[datetime]] = {}

    for row in rows:
        data = row.get("event_data") or {}
        author = str(data.get("author") or "")
        ts = _parse_ts(row.get("created_at"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        invocation = str(
            _pick(data, "invocation_id", "invocationId") or "single"
        )
        if ts:
            spans.setdefault(invocation, []).append(ts)
            if author and author != "user":
                agent_spans.setdefault((author, invocation), []).append(ts)

        frame = adk_event_to_frame(row)
        content = data.get("content") or {}
        calls, responses = _tool_parts(content.get("parts") or [])

        tools: list[dict] = []
        # Declared on the frame that CARRIES the call, not the one that
        # answers it: the brief's citations live in the arguments, and the
        # response is only a status line.
        frame_sources: list[str] = []
        frame_facts: list[dict] = []
        for call in calls:
            tool = {
                "id": call["id"],
                "name": call["name"],
                "args": _truncate(call["args"], 600),
                "status": "running",
                "ms": None,
                "result": None,
            }
            tools.append(tool)
            if call["id"]:
                pending[call["id"]] = {"tool": tool, "started": ts}
            if call["name"] == _BRIEF_TOOL:
                frame_facts.extend(_research_facts(call["args"]))
                for url in _brief_sources(call["args"]):
                    if url not in frame_sources:
                        frame_sources.append(url)

        # A response completes the call recorded earlier, wherever it was.
        for resp in responses:
            started = pending.pop(resp["id"], None) if resp["id"] else None
            payload = resp.get("response")
            for url in _source_urls(payload):
                if url not in frame_sources:
                    frame_sources.append(url)
            failed = isinstance(payload, dict) and str(
                payload.get("status", "")
            ).lower() in ("error", "failed")
            if started:
                tool = started["tool"]
                tool["result"] = _truncate(payload)
                tool["status"] = "error" if failed else "ok"
                if started["started"] and ts:
                    tool["ms"] = int((ts - started["started"]).total_seconds() * 1000)
            else:
                # A response with no matching call (resumed leg, trimmed log).
                tools.append(
                    {
                        "id": resp["id"],
                        "name": resp["name"],
                        "args": "",
                        "status": "error" if failed else "ok",
                        "ms": None,
                        "result": _truncate(payload),
                    }
                )

        if frame_sources:
            frame.setdefault("data", {})["sources"] = frame_sources

        if frame_facts:
            frame.setdefault("data", {})["facts"] = frame_facts

        if tools:
            frame["tools"] = tools

        usage = _pick(data, "usage_metadata", "usageMetadata") or {}
        prompt = int(_pick(usage, "prompt_token_count", "promptTokenCount") or 0)
        output = int(_pick(usage, "candidates_token_count", "candidatesTokenCount") or 0)
        total = int(_pick(usage, "total_token_count", "totalTokenCount") or 0) or (
            prompt + output
        )
        totals["prompt"] += prompt
        totals["output"] += output
        totals["total"] += total

        if author and author != "user":
            if author not in agents:
                agents[author] = {
                    "name": author,
                    "tokens": {"prompt": 0, "output": 0, "total": 0},
                    "tool_calls": 0,
                    "events": 0,
                    "errors": 0,
                }
                order.append(author)
            entry = agents[author]
            entry["events"] += 1
            entry["tool_calls"] += len(calls)
            entry["tokens"]["prompt"] += prompt
            entry["tokens"]["output"] += output
            entry["tokens"]["total"] += total
            if frame["kind"] == KIND_ERROR:
                entry["errors"] += 1

        frame["ts"] = row.get("created_at")
        frames.append(frame)

    def _sum_spans(groups: list[list[datetime]]) -> Optional[int]:
        """Total milliseconds across independent stretches of work."""
        total = 0
        seen = False
        for stamps in groups:
            if len(stamps) < 2:
                continue
            seen = True
            total += int((max(stamps) - min(stamps)).total_seconds() * 1000)
        return total if seen else (0 if groups else None)

    agent_summary = []
    for name in order:
        entry = agents[name]
        entry["ms"] = _sum_spans(
            [s for (a, _inv), s in agent_spans.items() if a == name]
        )
        agent_summary.append(entry)

    summary = {
        "tokens": totals,
        # Time the agents actually ran, excluding waits for a human.
        "ms": _sum_spans(list(spans.values())),
        # Wall clock from first event to last, kept separate so "started 2
        # days ago" and "took 23 minutes" can both be told truthfully.
        "span_ms": int((last_ts - first_ts).total_seconds() * 1000)
        if first_ts and last_ts
        else None,
        # One per stretch of work: the initial run, plus one per resume.
        "invocations": len(spans),
        "agents": agent_summary,
        "event_count": len(frames),
        "tool_calls": sum(a["tool_calls"] for a in agent_summary),
    }
    return frames, summary


async def load_trace_with_summary(
    run_id: str, after: int = 0, limit: int = 2000
) -> tuple[list[dict], dict]:
    """A run's timeline plus its cost and timing summary."""
    from app.config import settings

    try:
        from app.runs.service import PIPELINE_USER_ID
    except Exception:  # pragma: no cover - import wiring
        PIPELINE_USER_ID = "pipeline"

    rows: list[dict] = []
    try:
        rows = await db.load_adk_events(
            settings.app_name, PIPELINE_USER_ID, run_id, after=0, limit=limit
        )
    except Exception as exc:
        logger.warning("Could not read the ADK transcript for %s: %s", run_id, exc)

    if rows:
        frames, summary = build_trace(rows)
        # Same merge as load_trace. This is the endpoint the console reads for
        # history, so a lifecycle line missing here is missing everywhere.
        # The summary is unaffected: these frames carry no tokens or tools.
        frames = _merge_by_time(frames, await _lifecycle_frames(run_id, limit))
        summary["total_events"] = len(frames)
        # Truncation is a fact about this response, so it travels with it. A
        # short trace that says nothing is indistinguishable from a run that
        # stopped early.
        summary["truncated"] = len(rows) >= limit
        page = frames[after : after + limit]
        summary["event_count"] = len(page)
        return page, summary

    frames = await db.load_run_events(run_id, after=after, limit=limit)
    return frames, {"tokens": None, "ms": None, "agents": [], "event_count": len(frames)}
