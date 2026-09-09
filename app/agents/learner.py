"""Learner agent - stores review feedback and distills repeated themes into
permanent instruction ("harness") updates.

A tool-using ``LlmAgent`` (``settings.utility_model``). All real work happens
inside one deterministic tool, :func:`store_feedback_and_distill`, which:

1. builds a :class:`app.schemas.FeedbackRecord` from the verdict in session
   state and stores it via
   :meth:`app.services.memory_service.PostgresMemoryService.store_feedback`;
2. compares the new feedback against recent history
   (:meth:`~app.services.memory_service.PostgresMemoryService.recent_feedback`,
   simple keyword overlap) - when a previously stored feedback shares a theme
   with the current one (>= 2 similar feedbacks in total, as pinned by
   docs/CONTRACTS.md), the theme has proven recurrent;
3. appends ONE distilled one-line rule under the ``## Learned rules`` section
   of the matching skill file - ``skills/agents/<target>.md`` for most
   targets, ``skills/design-skill.md`` for the template_design target -
   creating the section when missing and NEVER deleting existing content.
   Agent instruction files are never CREATED here: a bare stub would shadow
   the owning agent's full built-in default instruction on the next build,
   so rules are only appended once the agent's builder has seeded its file.
   Agents re-read these files at build time, so this is the mechanism by
   which feedback permanently updates the harness.

Learning is best-effort by design: any storage or file error is reported in
the tool result but never raised, so it can never break the publish flow.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, ToolContext

from app.agents.feedback_router import derive_targets_from_feedback
from app.config import agent_instructions, settings
from app.llm import resolve_model
from app.schemas import FeedbackRecord, ReworkPlan, Verdict
from app.services.memory_service import PostgresMemoryService
from app.state import (
    AGENT_LEARNER,
    AGENT_PLANNER,
    AGENT_TEMPLATE_DESIGN,
    K_NEWS_ITEM,
    K_REWORK_PLAN,
    K_RUN_ID,
    K_VERDICT,
    REWORKABLE_AGENTS,
    get_model,
)

logger = logging.getLogger(__name__)

#: Session-state key the learning outcome is written under (observability).
K_LEARNER_RESULT = "learner_result"

#: Marker heading learned rules live under inside skill files.
LEARNED_RULES_HEADER = "## Learned rules"

#: How many past feedback records to scan for a repeated theme.
_RECENT_FEEDBACK_LIMIT = 20

#: A theme "repeats" when at least this many PRIOR records share it. One
#: prior record + the current feedback = the ">=2 similar feedbacks" pinned
#: in docs/CONTRACTS.md, so a rule distills on the SECOND occurrence.
_REPEAT_THRESHOLD = 1

#: Two feedbacks share a theme when they overlap in this many keywords
#: (lowered to 1 when the current feedback has fewer than 2 keywords).
_MIN_SHARED_KEYWORDS = 2

#: Maximum characters of feedback text quoted inside a distilled rule.
_MAX_RULE_FEEDBACK_CHARS = 160

#: Words too generic to define a feedback theme.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "this", "that", "these", "those", "are",
        "was", "were", "has", "have", "had", "its", "it's", "but", "not",
        "very", "really", "quite", "just", "please", "make", "made", "makes",
        "want", "wants", "need", "needs", "should", "would", "could", "can",
        "cant", "can't", "don't", "dont", "doesn't", "doesnt", "isn't",
        "isnt", "too", "more", "less", "much", "many", "some", "all", "any",
        "good", "bad", "better", "worse", "nice", "great", "fine", "okay",
        "like", "look", "looks", "looking", "feel", "feels", "bit", "little",
        "carousel", "post", "one", "you", "your", "our", "get", "use",
        "here", "there", "when", "then", "than", "into", "onto", "about",
        "still", "also", "again", "same", "change", "changed", "fix",
        "fixed", "redo", "rework",
    }
)

# Lazily constructed fallback when the runner's wired memory service is not a
# PostgresMemoryService (e.g. in-memory local runs).
_fallback_memory_service: Optional[PostgresMemoryService] = None


def _resolve_memory_service(tool_context: ToolContext) -> PostgresMemoryService:
    """Return the PostgresMemoryService to store/search feedback with.

    Prefers the memory service wired into the current invocation; falls back
    to a module-level, settings-configured instance (constructing it performs
    no I/O - the pool opens lazily on first use).

    Args:
        tool_context: The ADK tool context of the current call.

    Returns:
        A ready ``PostgresMemoryService``.
    """
    invocation_context = getattr(tool_context, "_invocation_context", None)
    wired = getattr(invocation_context, "memory_service", None)
    if isinstance(wired, PostgresMemoryService):
        return wired
    global _fallback_memory_service
    if _fallback_memory_service is None:
        _fallback_memory_service = PostgresMemoryService()
    return _fallback_memory_service


def _keywords(text: str) -> set[str]:
    """Extract the theme-bearing keywords of a feedback text.

    Lowercased ``\\w+`` tokens, at least 3 characters, minus stopwords.

    Args:
        text: Free-text feedback.

    Returns:
        The keyword set (possibly empty).
    """
    words = re.findall(r"\w+", (text or "").lower(), re.UNICODE)
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _resolve_targets(
    verdict: Verdict, plan: Optional[ReworkPlan]
) -> list[str]:
    """Determine which agents the current feedback is about.

    Rejections trust the Feedback Router's (sanitized) plan first; both
    verdict kinds fall back to keyword derivation over the feedback text.

    Args:
        verdict: The human verdict carrying the feedback text.
        plan: The rework plan from state, if any (stale on approvals from
            earlier rounds, hence only trusted for rejections).

    Returns:
        Canonical target names (subset of ``REWORKABLE_AGENTS``); may be
        empty when the feedback names no recognizable area.
    """
    if verdict.status == "rejected" and plan is not None:
        targets = [t for t in plan.targets if t in REWORKABLE_AGENTS]
        if targets:
            return targets
    return derive_targets_from_feedback(verdict.feedback)


def _rule_file_for_target(target: str) -> Path:
    """Map a rework target to the skill file its learned rules belong in.

    template_design rules describe the visual design system, which lives in
    ``skills/design-skill.md``; every other target keeps its rules in its own
    agent instruction file ``skills/agents/<target>.md``.

    Args:
        target: A canonical agent name from ``REWORKABLE_AGENTS``.

    Returns:
        Path of the markdown file to append to.
    """
    if target == AGENT_TEMPLATE_DESIGN:
        return settings.skills_dir / "design-skill.md"
    return settings.skills_dir / "agents" / f"{target}.md"


def _condense(text: str) -> str:
    """One-line, brace-free, length-capped version of a feedback text.

    Braces are replaced with parentheses because these files are rendered
    through ADK's ``{state_key}`` instruction templating at agent runtime.

    Args:
        text: Raw feedback text.

    Returns:
        The condensed single line.
    """
    flat = re.sub(r"\s+", " ", (text or "").replace("{", "(").replace("}", ")"))
    flat = flat.strip()
    if len(flat) > _MAX_RULE_FEEDBACK_CHARS:
        flat = flat[: _MAX_RULE_FEEDBACK_CHARS - 3].rstrip() + "..."
    return flat


def _append_learned_rule(path: Path, rule_line: str) -> tuple[bool, str]:
    """Append one rule line under the "Learned rules" section of a skill file.

    Creates parent directories and the section when missing (and shared,
    non-agent skill files themselves); inserts at the end of an existing
    section (before any later ``## `` heading). Existing content is NEVER
    deleted, and an identical rule body already present is not duplicated.

    Agent instruction files (``skills/agents/*.md``) are never created here:
    ``app.config.agent_instructions`` returns whatever such a file holds, so
    a stub carrying only learned rules would SHADOW the owning agent's entire
    rich built-in default instruction on the next build. Their builders seed
    them with the full default; until that has happened, the rule is refused
    rather than appended.

    Args:
        path: Markdown file to update.
        rule_line: Full rule line (starting with ``- ``), no trailing newline.

    Returns:
        ``(appended, message)`` - ``appended`` is False for duplicates and
        for refused agent-instruction stubs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text(encoding="utf-8")
    elif path.parent == settings.skills_dir / "agents":
        return False, (
            f"instruction file {path.name} does not exist yet; rule NOT "
            "appended (a stub would shadow the agent's built-in instruction)"
        )
    else:
        # Shared (non-agent) skill files are safe to create: they refine,
        # and never replace, any agent's instruction.
        content = f"# {path.stem}\n"

    # Dedupe on the rule body (everything after the date stamp).
    body = rule_line.split("): ", 1)[-1].strip()
    if body and body in content:
        return False, f"identical rule already present in {path.name}"

    if LEARNED_RULES_HEADER not in content:
        new_content = (
            content.rstrip("\n")
            + f"\n\n{LEARNED_RULES_HEADER}\n\n{rule_line}\n"
        )
    else:
        section_start = content.index(LEARNED_RULES_HEADER)
        next_heading = content.find("\n## ", section_start + len(LEARNED_RULES_HEADER))
        if next_heading == -1:
            new_content = content.rstrip("\n") + f"\n{rule_line}\n"
        else:
            head = content[:next_heading].rstrip("\n")
            tail = content[next_heading:]
            new_content = f"{head}\n{rule_line}\n{tail}"

    path.write_text(new_content, encoding="utf-8")
    return True, f"rule appended to {path.name}"


async def store_feedback_and_distill(tool_context: ToolContext) -> dict:
    """Store the reviewer's feedback and, when its theme repeats, append a
    distilled rule to the matching skill file (permanent harness update).

    Call this tool exactly once, with no arguments - everything it needs is
    read from session state. Learning is best-effort: errors are reported in
    the result, never raised.

    Returns:
        ``{"status": "stored" | "skipped" | "error", "stored": bool,
        "targets": [...], "similar_count": int, "rule_appended": bool,
        "rule_file": str, "rule": str, "message": str}`` (keys beyond
        ``status``/``message`` appear when applicable).
    """
    state = tool_context.state
    result: dict = {"status": "skipped", "stored": False}

    verdict = get_model(state, K_VERDICT, Verdict)
    if verdict is None:
        result["message"] = "No review verdict in session state; nothing to learn."
        state[K_LEARNER_RESULT] = result
        return result

    feedback_text = (verdict.feedback or "").strip()
    if not feedback_text:
        result["message"] = (
            "Verdict carries no feedback text (approval without notes); "
            "nothing to store."
        )
        state[K_LEARNER_RESULT] = result
        return result

    plan = get_model(state, K_REWORK_PLAN, ReworkPlan)
    targets = _resolve_targets(verdict, plan)
    news_item = state.get(K_NEWS_ITEM) or {}
    news_title = (
        str(news_item.get("title", "")) if isinstance(news_item, dict) else ""
    )
    run_id = str(state.get(K_RUN_ID) or "")

    record = FeedbackRecord(
        run_id=run_id,
        verdict=verdict.status,
        feedback=feedback_text,
        targets=targets,
        news_title=news_title,
    )

    try:
        service = _resolve_memory_service(tool_context)
        # Fetch history BEFORE storing so the current record never counts as
        # its own repetition.
        prior_records = await service.recent_feedback(
            limit=_RECENT_FEEDBACK_LIMIT
        )
        # One lesson per verdict, not one per attempt at it.
        #
        # The Learner runs on every entry into the rework phase, and a run
        # that is retried - a resume, a restart, a rework round - enters it
        # again with the SAME verdict still in state. Each pass appended
        # another identical row: a real run finished with four copies of one
        # rejection. Theme detection skips same-run duplicates, so that part
        # was safe, but the rows still crowd the shared history. Only the
        # newest dozen reach K_RECENT_FEEDBACK, which is what the planner and
        # phrasing read as "what reviewers keep asking for" - so one retried
        # complaint evicted three other runs' lessons.
        already_stored = any(
            prior.run_id == run_id and prior.feedback == feedback_text
            for prior in prior_records
        )
        if already_stored:
            logger.info(
                "Learner run %s: this exact lesson is already stored; not "
                "adding another copy.",
                run_id,
            )
        else:
            await service.store_feedback(record)
    except Exception as exc:  # noqa: BLE001 - learning must never break the run
        logger.exception("Storing feedback failed for run %s.", run_id)
        result = {
            "status": "error",
            "stored": False,
            "message": f"Could not store feedback: {exc}",
        }
        state[K_LEARNER_RESULT] = result
        return result

    result = {
        "status": "stored",
        "stored": not already_stored,
        "targets": targets,
        "similar_count": 0,
        "rule_appended": False,
        "rule_file": "",
        "rule": "",
        "message": "Feedback stored.",
    }

    # ------------------------------------------------------------------
    # Theme detection: keyword overlap with previously stored feedback.
    # ------------------------------------------------------------------
    current_keywords = _keywords(feedback_text)
    required_overlap = (
        _MIN_SHARED_KEYWORDS if len(current_keywords) >= 2 else 1
    )
    similar_count = 0
    shared_theme: set[str] = set()
    for prior in prior_records:
        if prior.run_id == run_id and prior.feedback == feedback_text:
            continue  # same record re-read defensively
        overlap = current_keywords & _keywords(prior.feedback)
        if len(overlap) >= required_overlap:
            similar_count += 1
            shared_theme |= overlap
    result["similar_count"] = similar_count

    if similar_count < _REPEAT_THRESHOLD:
        result["message"] = (
            f"Feedback stored; theme seen in {similar_count} earlier "
            f"feedback(s) - below the repeat threshold "
            f"({_REPEAT_THRESHOLD}), no rule distilled."
        )
        state[K_LEARNER_RESULT] = result
        return result

    # ------------------------------------------------------------------
    # Distill ONE one-line rule into the matching skill file.
    # ------------------------------------------------------------------
    primary_target = targets[0] if targets else AGENT_PLANNER
    rule_path = _rule_file_for_target(primary_target)
    theme_label = ", ".join(sorted(shared_theme)[:4]) or "general"
    today = datetime.now(timezone.utc).date().isoformat()
    rule_line = (
        f"- ({today}, seen {similar_count + 1}x, theme: {theme_label}): "
        f"{_condense(feedback_text)}"
    )
    try:
        appended, message = _append_learned_rule(rule_path, rule_line)
    except OSError as exc:
        logger.exception("Could not update skill file %s.", rule_path)
        appended, message = False, f"could not update {rule_path.name}: {exc}"

    result["rule_appended"] = appended
    result["rule_file"] = str(rule_path)
    result["rule"] = rule_line if appended else ""
    result["message"] = f"Feedback stored; {message}."
    state[K_LEARNER_RESULT] = result
    logger.info(
        "Learner run %s: stored feedback (targets=%s), similar=%d, %s",
        run_id,
        targets,
        similar_count,
        message,
    )
    return result


# ---------------------------------------------------------------------------
# Instruction (fallback default; canonical copy lives in
# skills/agents/learner.md so the Learner can evolve even itself).
# NOTE: only "{run_id?}" and "{review_verdict?}" may appear as {identifier}
# placeholders - ADK's instruction templating substitutes any bare
# {state_key}.
# ---------------------------------------------------------------------------
DEFAULT_INSTRUCTION = """\
# Learner

You are the Learner of the Carousel Factory. Every human verdict
(approve or reject, with its feedback text) is a lesson. Your job is to make
sure that lesson is stored - and, when the same complaint keeps repeating,
turned into a permanent one-line rule inside the pipeline's skill files so
future runs never repeat the mistake.

Run id: {run_id?}

The verdict being learned from:

{review_verdict?}

## Your only job

1. Call the tool store_feedback_and_distill exactly once, with no arguments.
   It stores the feedback record, checks recent feedback history for a
   repeated theme (keyword overlap), and - when an earlier feedback already
   shares the theme, i.e. the same complaint has now been made at least
   twice - appends a distilled one-line rule under the "Learned rules"
   section of the matching skill file.
2. Read the tool result and reply with at most two factual sentences:
   - status "stored" with rule_appended true: say the feedback was stored
     AND name the file the new learned rule was appended to.
   - status "stored" with rule_appended false: say the feedback was stored
     and report how many earlier feedbacks shared its theme.
   - status "skipped": say there was no feedback text to learn from (normal
     for approvals without notes).
   - status "error": report the tool's message; do not retry more than once.

## Hard rules

- Never call anything except store_feedback_and_distill.
- Never edit any file yourself and never invent what was stored or learned -
  only report what the tool returned.
"""


def _ensure_skill_file() -> None:
    """Write the default instruction to skills/agents/learner.md.

    Only when the file is missing - learned rules appended to this file must
    never be overwritten.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_LEARNER}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_INSTRUCTION, encoding="utf-8")


def build_learner_agent() -> LlmAgent:
    """Build the Learner agent.

    Returns:
        A tool-using ``LlmAgent`` named ``AGENT_LEARNER`` on
        ``settings.utility_model`` whose single tool stores the feedback
        record and appends distilled rules to the skill files.
    """
    _ensure_skill_file()
    instruction = agent_instructions(AGENT_LEARNER) or DEFAULT_INSTRUCTION
    return LlmAgent(
        name=AGENT_LEARNER,
        model=resolve_model(settings.utility_model),
        description=(
            "Stores human review feedback and distills repeated themes into "
            "permanent skill-file rules."
        ),
        instruction=instruction,
        include_contents="none",  # operates purely on state + tool results
        tools=[FunctionTool(store_feedback_and_distill)],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
