"""CTA agent - picks the call-to-action type and renders the closing slide.

Builds an :class:`~google.adk.agents.LlmAgent` (utility model) that chooses
the CTA variant (``follow`` / ``comment`` / ``redirect``) from the planner's
``cta_hint`` plus its own judgment of the content, composes short CTA copy,
and calls the :func:`render_cta_slide` tool. The tool renders the 1080x1350
PNG via :func:`app.tools.image_gen.generate_cta_image`, resolves the link
destination from :data:`app.config.settings` (``ig_handle`` /
``substack_url`` / ``youtube_url`` - never model-invented), saves the PNG as
an artifact and writes the resulting ``CTASlide`` to ``state[K_CTA_SLIDE]``.

The CTA template reference image is discovered from the "CTA slide" section of
``skills/design-skill.md``; until the designer delivers one, the renderer
falls back to its style prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Optional, Tuple

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from app.config import agent_instructions, load_skill, settings
from app.llm import resolve_model
from app.schemas import CTASlide
from app.state import AGENT_CTA, K_CTA_SLIDE, K_RUN_ID, set_model
from app.text_rules import require_no_em_dash
from app.tools import brand_identity, image_gen

logger = logging.getLogger(__name__)

_CTA_TYPES = ("follow", "comment", "redirect")
_ARTIFACT_NAME = "cta_slide.png"
_MAX_SUPPORTING_LINES = 3

_DEFAULT_HEADLINES = {
    "follow": "FOLLOW FOR MORE",
    "comment": "DROP A COMMENT",
    "redirect": "FULL BREAKDOWN",
}

# Same discovery patterns as the Template Design agent: markdown image,
# backticked image path, or bare image-file token inside design-skill.md.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_BACKTICK_IMAGE_RE = re.compile(r"`([^`]+\.(?:png|jpe?g|webp))`", re.IGNORECASE)
_BARE_IMAGE_RE = re.compile(r"([\w\-./\\]+\.(?:png|jpe?g|webp))\b", re.IGNORECASE)

_DEFAULT_INSTRUCTION = """\
# CTA agent

You create the FINAL slide of an Instagram carousel: the call-to-action. You
decide WHICH CTA to run and write its short copy; a tool renders the slide in
the design system from skills/design-skill.md and attaches the correct handle
or link from configuration.

## Choosing the CTA type

Start from the planner's hint (carousel plan below) but apply judgment:
- "follow" - the default. Use when the carousel is a news brief whose value is
  the account itself: promise more of the same coverage.
- "comment" - use when the topic naturally invites opinions, debate or picks
  (hot takes, versus posts, "which would you choose" material). The copy must
  contain ONE concrete question about the topic.
- "redirect" - use ONLY when there is genuinely deeper material to point to
  (a full breakdown on Substack or a video on YouTube), typically hinted by
  the plan or the caption. Pick `redirect_destination` = "substack" for
  written deep-dives, "youtube" for video material.

## Writing the CTA copy

- Headline: at most 6 words, punchy, imperative, reads naturally in uppercase
  (for example "FOLLOW FOR DAILY AI NEWS").
- Supporting lines: at most 3 short lines, one thought per line - a value
  promise, a question (compulsory for "comment"), or what the reader gets at
  the destination (for "redirect").
- Never use an em dash. Use a period, comma, colon, or parentheses instead.
- Use only complete, correctly spelled, understandable words. Never use
  invented words, placeholder copy, keyboard mash, corrupted characters, or
  decorative pseudo-writing.
- NEVER write a handle, username, URL or link in the headline or supporting
  lines - the tool appends the correct one from configuration and any you
  invent would be wrong.

## How to work

1. Decide the type and compose the copy per the rules above.
2. Call `render_cta_slide` exactly once with cta_type, headline,
   supporting_lines (and redirect_destination when cta_type is "redirect").
   The tool renders your text VERBATIM - send final, typo-free copy.
3. If the tool returns status "error", fix what its message indicates (or
   simply retry once for transient render failures). If it fails twice,
   report the error plainly and stop.
4. On success, reply in one line: the chosen type, why, and the artifact
   filename.

## Context from state

Carousel plan: {carousel_plan?}
Approved copy and caption: {copy_set?}

## Rework feedback (highest priority when present)

{rework_feedback?}

If reviewer feedback appears directly above this line, it is your
HIGHEST-PRIORITY instruction and overrides everything else - including the
planner's cta_hint. Examples: "make it a comment CTA" means switch the type;
"CTA text is weak" means rewrite the copy before re-rendering; "wrong link"
means re-check the type/destination pair you chose. Then call
`render_cta_slide` again with the corrected arguments.

## Distilled feedback from past runs

{recent_feedback_notes?}

Apply any relevant distilled rules above to your type choice and copy.
"""


def _ensure_default_instruction_file() -> None:
    """Write the fallback instruction to ``skills/agents/cta.md`` if absent.

    The file is the editable source of truth for this agent's instruction
    (the Learner agent appends learned rules to it), so it is only created
    when missing - an existing file is never overwritten. Seeding it here
    guarantees the Learner only ever appends to a file that already carries
    the full default instruction, never to a bare stub that would shadow it.
    """
    path = settings.skills_dir / "agents" / f"{AGENT_CTA}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_INSTRUCTION, encoding="utf-8")


def _skill_section(markdown: str, heading_hint: str) -> str:
    """Return the body of the first ``##`` section whose heading contains hint.

    Args:
        markdown: Full markdown text of the skill file.
        heading_hint: Case-insensitive substring to look for in ``##`` headings.

    Returns:
        The section body (text until the next ``##`` heading), or ``""``.
    """
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    hint = heading_hint.lower()
    for heading, body in sections.items():
        if hint in heading.lower():
            return "\n".join(body)
    return ""


def _discover_template_ref(heading_hint: str) -> str:
    """Find a CTA template image path in a design-skill.md section (or "").

    Returns ``""`` while the skill file holds no image reference (placeholder
    state until the designer delivers), which makes
    :mod:`app.tools.image_gen` fall back to its style prompt.

    Args:
        heading_hint: Case-insensitive substring of the target ``##`` heading.

    Returns:
        The template reference (path string) or ``""``.
    """
    section = _skill_section(load_skill("design-skill.md"), heading_hint)
    if not section:
        return ""
    # Only an explicitly labelled template line is eligible. Other reference
    # images in the section must never be passed to images.edit as templates.
    for line in section.splitlines():
        if "template" not in line.lower():
            continue
        for pattern in (_MD_IMAGE_RE, _BACKTICK_IMAGE_RE, _BARE_IMAGE_RE):
            match = pattern.search(line)
            if match:
                return match.group(1).strip()
    return ""


def _handle_for_run() -> str:
    """The handle of the Instagram account this run publishes to.

    Was ``settings.ig_handle`` - one global for the whole console. With
    several accounts connectable the answer is per run, and comes from the
    brand identity set when the run started.
    """
    return brand_identity.require_handle()


def _normalized_handle() -> str:
    """The run's IG handle with a leading ``@`` (or ``""`` outside a run).

    Returns empty rather than raising because the CTA copy path can run
    before any account context exists, and a CTA without a handle simply
    omits the follow line - whereas the RAIL, which cannot omit anything,
    does raise.
    """
    try:
        return _handle_for_run()
    except brand_identity.NoBrandIdentity:
        return ""


def _display_link(url: str) -> str:
    """Compact a URL for on-slide display (strip scheme, www., trailing /)."""
    text = url.strip()
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    return text.rstrip("/")


def _resolve_link(cta_type: str, redirect_destination: str) -> Tuple[str, str]:
    """Resolve (link_url, on-slide link_text) from settings for a CTA type.

    - ``follow`` / ``comment`` → the configured IG handle.
    - ``redirect`` → ``settings.substack_url`` or ``settings.youtube_url`` per
      ``redirect_destination``, falling back to whichever is configured, then
      to the IG handle if neither is set.

    Args:
        cta_type: One of ``"follow"``, ``"comment"``, ``"redirect"``.
        redirect_destination: ``"substack"`` or ``"youtube"`` (redirect only).

    Returns:
        ``(link_url, link_text)``; both may be ``""`` when nothing is
        configured (the renderer then omits the link line).
    """
    handle = _normalized_handle()
    if cta_type != "redirect":
        return handle, handle
    dest = (redirect_destination or "substack").strip().lower()
    if dest == "youtube":
        ordered = [settings.youtube_url, settings.substack_url]
    else:
        ordered = [settings.substack_url, settings.youtube_url]
    for url in ordered:
        if url.strip():
            return url.strip(), _display_link(url)
    logger.warning(
        "Redirect CTA requested but no substack_url/youtube_url configured; "
        "falling back to the IG handle."
    )
    return handle, handle


def _run_workdir(state: Any) -> Path:
    """Return (and create) the slide output directory for the current run."""
    run_id = str(state.get(K_RUN_ID) or "run")
    out_dir = settings.workdir / run_id / "slides"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


async def render_cta_slide(
    cta_type: str,
    headline: str,
    supporting_lines: list[str],
    tool_context: ToolContext,
    redirect_destination: str = "substack",
) -> dict:
    """Render the final CTA slide as a PNG artifact and record it in state.

    Renders a 1080x1350 CTA slide in the design system with the given copy
    (your text is rendered VERBATIM), appends the correct handle/link from
    configuration for the chosen type, saves the PNG as a session artifact and
    writes the CTASlide record to state.

    Args:
        cta_type: One of "follow", "comment", "redirect".
        headline: The big centered CTA headline (at most 6 words; do NOT
            include any handle or URL - it is added from configuration).
        supporting_lines: Up to 3 short supporting lines (no handles/URLs);
            pass an empty list for none.
        redirect_destination: Only used when cta_type is "redirect":
            "substack" (default) or "youtube" - selects which configured link
            the slide points to.

    Returns:
        On success: {"status": "ok", "cta_type": str, "artifact": str,
        "link_url": str, "template_used": str}. On failure:
        {"status": "error", "message": str}.
    """
    kind = (cta_type or "").strip().lower()
    if kind not in _CTA_TYPES:
        return {
            "status": "error",
            "message": f"Unknown cta_type {cta_type!r}; use one of {list(_CTA_TYPES)}.",
        }

    head = (headline or "").strip() or _DEFAULT_HEADLINES[kind]
    lines = [line.strip() for line in (supporting_lines or []) if line and line.strip()]
    try:
        require_no_em_dash([head, *lines], "CTA copy")
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if len(lines) > _MAX_SUPPORTING_LINES:
        return {
            "status": "error",
            "message": (
                f"{len(lines)} supporting lines given; the CTA slide allows at "
                f"most {_MAX_SUPPORTING_LINES}. Trim and call again."
            ),
        }

    link_url, link_text = _resolve_link(kind, redirect_destination)
    template_ref = _discover_template_ref("CTA slide")
    out_path = _run_workdir(tool_context.state) / _ARTIFACT_NAME

    try:
        # generate_cta_image blocks on a slow image API call - keep the event
        # loop free by running it in a worker thread.
        written = await asyncio.to_thread(
            image_gen.generate_cta_image,
            kind,
            head,
            lines,
            link_text,
            template_ref,
            str(out_path),
        )
        png_bytes = Path(written).read_bytes()
        await tool_context.save_artifact(
            _ARTIFACT_NAME,
            types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
        )
    except Exception as exc:  # noqa: BLE001 - surface any failure to the LLM
        logger.exception("CTA slide (%s) failed to render.", kind)
        return {"status": "error", "message": f"Rendering the CTA slide failed: {exc}"}

    cta_slide = CTASlide(cta_type=kind, artifact=_ARTIFACT_NAME, link_url=link_url)
    set_model(tool_context.state, K_CTA_SLIDE, cta_slide)
    logger.info("CTA slide (%s) rendered -> artifact %s", kind, _ARTIFACT_NAME)
    return {
        "status": "ok",
        "cta_type": kind,
        "artifact": _ARTIFACT_NAME,
        "link_url": link_url,
        "template_used": template_ref or "style-prompt fallback (no template image yet)",
    }


def build_cta_agent() -> LlmAgent:
    """Build the CTA agent.

    Returns:
        An ``LlmAgent`` named ``AGENT_CTA`` on the utility model with the
        single ``render_cta_slide`` tool. State is written by the tool
        (``K_CTA_SLIDE``); the agent itself carries no output_schema.
    """
    _ensure_default_instruction_file()
    instruction = agent_instructions(AGENT_CTA) or _DEFAULT_INSTRUCTION
    return LlmAgent(
        name=AGENT_CTA,
        model=resolve_model(settings.utility_model),
        description=(
            "Picks the call-to-action type (follow/comment/redirect), writes "
            "its copy, and renders the closing CTA slide as a PNG artifact "
            "with the configured handle or link."
        ),
        instruction=instruction,
        tools=[FunctionTool(render_cta_slide)],
        # Orchestrator-driven pipeline node: never LLM-transfer elsewhere.
        # See the same block in template_design - without both flags,
        # google-adk 2.7 gives this agent AutoFlow and a transfer_to_agent
        # tool, and a transfer is indistinguishable from a completed stage
        # to the orchestrator's phase loop.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
