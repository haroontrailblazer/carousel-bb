"""Web research tools for the Research agent.

Two ``FunctionTool``-wrapped functions:

- :func:`search_web` - one web search via the OpenAI Responses API
  ``web_search`` built-in tool (verified working on this key), returning the
  synthesized answer plus its citation URLs.
- :func:`save_research_brief` - validates the gathered material as a
  :class:`app.schemas.ResearchBrief`, writes it to session state under
  ``K_RESEARCH``, and merges any discovered official media URLs into the
  news item's ``media_urls`` so the First-Page Visual agent can clip them
  without any change on its side.

The search model is the utility model's bare OpenAI id (cheap; the Research
agent itself does the judgment). Search is best-effort: failures return an
``error`` status result instead of raising, so a flaky network can never
kill the pipeline - the agent falls back to the news item's own text.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from google.adk.tools import ToolContext
from openai import OpenAI, OpenAIError

from app.config import settings
from app.llm import OPENAI_REASONING_EFFORT
from app.schemas import ResearchBrief
from app.state import K_NEWS_ITEM, K_RESEARCH

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT_S: float = 120.0
_RETRY_DELAY_S: float = 4.0
_MAX_QUERY_CHARS = 400
_MAX_MEDIA_URLS = 8  # cap on news_item.media_urls after merging candidates

_client_singleton: Optional[OpenAI] = None

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read one field from an SDK model or a mapping response fixture."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalize_http_url(value: Any) -> str:
    """Return a stable http(s) URL, stripping fragments/tracking parameters."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def _unique_urls(values: list[Any]) -> list[str]:
    """Normalize and de-duplicate URLs while preserving their first order."""
    result: list[str] = []
    for value in values:
        url = _normalize_http_url(value)
        if url and url not in result:
            result.append(url)
    return result


def _response_sources(response: Any) -> list[str]:
    """Parse cited and consulted URLs from a Responses API result robustly."""
    found: list[Any] = []
    for item in _field(response, "output", []) or []:
        item_type = str(_field(item, "type", ""))
        if item_type == "web_search_call":
            action = _field(item, "action", {}) or {}
            for source in _field(action, "sources", []) or []:
                found.append(_field(source, "url", ""))
            action_url = _field(action, "url", "")
            if action_url:
                found.append(action_url)
        if item_type != "message":
            continue
        for part in _field(item, "content", []) or []:
            for annotation in _field(part, "annotations", []) or []:
                url = _field(annotation, "url", "")
                if url:
                    found.append(url)
    return _unique_urls(found)


def _client() -> OpenAI:
    """Lazily created OpenAI client (key from env via app.config)."""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = OpenAI(timeout=_SEARCH_TIMEOUT_S, max_retries=0)
    return _client_singleton


def _search_model_id() -> str:
    """Bare OpenAI model id used for web search (utility model, unprefixed)."""
    model = settings.utility_model
    return model.split("/", 1)[-1] if model.startswith("openai/") else model


def search_web(query: str) -> dict:
    """Search the live web and return a synthesized, cited answer.

    Uses the OpenAI Responses API ``web_search`` tool. Ask focused questions
    (one topic per call) and make several calls rather than one broad one.

    Args:
        query: The search question, e.g. "gpt-image-2 pricing and token
            costs official announcement".

    Returns:
        ``{"status": "ok", "answer": str, "sources": [url, ...]}`` on
        success, or ``{"status": "error", "message": str}`` - on error,
        continue with the facts already gathered instead of retrying forever.
    """
    query = (query or "").strip()[:_MAX_QUERY_CHARS]
    if not query:
        return {"status": "error", "message": "Empty query."}

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            response = _client().responses.create(
                model=_search_model_id(),
                tools=[{"type": "web_search"}],
                include=["web_search_call.action.sources"],
                instructions=(
                    "Research one focused claim. Prefer primary/official sources, "
                    "state exact dates and figures, separate confirmed facts from "
                    "inference, and cite every factual paragraph."
                ),
                input=query,
                reasoning={"effort": OPENAI_REASONING_EFFORT},
                store=False,
                timeout=_SEARCH_TIMEOUT_S,
            )
            sources = _response_sources(response)
            answer = (response.output_text or "").strip()
            if not answer:
                return {
                    "status": "error",
                    "message": "Search returned no text.",
                }
            logger.info(
                "[research] search %r -> %d chars, %d source(s)",
                query[:80],
                len(answer),
                len(sources),
            )
            return {"status": "ok", "answer": answer, "sources": sources}
        except OpenAIError as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(_RETRY_DELAY_S)
                continue
    return {"status": "error", "message": f"Search failed: {last_exc}"}


def save_research_brief(
    summary: str,
    key_facts: list[dict],
    suggested_angle: str,
    media_candidates: list[str],
    sources: list[str],
    tool_context: ToolContext,
) -> dict:
    """Validate and store the research brief; feed media finds to the cover.

    Call this exactly once, after searching. The brief lands in session state
    under ``research_brief`` (read by the planner and phrasing agents), and
    ``media_candidates`` are appended to the news item's ``media_urls`` so
    the First-Page Visual agent can source the cover clip from them.

    Args:
        summary: 3-6 sentence briefing on what happened and why it matters.
        key_facts: List of ``{"fact": str, "source_url": str}`` entries -
            exact numbers/names/dates, each with the URL it was verified at
            (empty source_url only for facts taken from the news item text).
        suggested_angle: One line - the most compelling hook angle found.
        media_candidates: Direct URLs of OFFICIAL announcement videos/images
            (event clips, launch videos, demo footage) usable for the cover.
        sources: All URLs consulted.
        tool_context: Injected by ADK.

    Returns:
        ``{"status": "saved", "fact_count": int, "media_added": int}`` or
        ``{"status": "error", "message": str}`` (fix the arguments and call
        again once).
    """
    try:
        normalized_sources = _unique_urls(list(sources or []))
        normalized_media = _unique_urls(list(media_candidates or []))
        normalized_facts: list[dict] = []
        for item in key_facts or []:
            fact = str(_field(item, "fact", "")).strip()
            raw_source_url = str(_field(item, "source_url", "") or "").strip()
            source_url = _normalize_http_url(raw_source_url)
            if not fact:
                continue
            if raw_source_url and not source_url:
                raise ValueError(
                    f"Fact source URL is not a valid http(s) URL: {raw_source_url!r}"
                )
            normalized_facts.append({"fact": fact, "source_url": source_url})
            if source_url and source_url not in normalized_sources:
                normalized_sources.append(source_url)

        brief = ResearchBrief(
            summary=summary,
            key_facts=normalized_facts,
            suggested_angle=suggested_angle,
            media_candidates=normalized_media,
            sources=normalized_sources,
        )
    except Exception as exc:
        return {"status": "error", "message": f"Invalid brief: {exc}"}

    state = tool_context.state
    state[K_RESEARCH] = brief.model_dump(mode="json")

    # Merge discovered official media into the news item for the cover agent.
    media_added = 0
    news = state.get(K_NEWS_ITEM)
    if isinstance(news, dict):
        existing = [u for u in (news.get("media_urls") or []) if u]
        for url in brief.media_candidates:
            if url and url not in existing and len(existing) < _MAX_MEDIA_URLS:
                existing.append(url)
                media_added += 1
        if media_added:
            news = dict(news)
            news["media_urls"] = existing
            state[K_NEWS_ITEM] = news

    logger.info(
        "[research] brief saved: %d fact(s), %d source(s), %d media added",
        len(brief.key_facts),
        len(brief.sources),
        media_added,
    )
    return {
        "status": "saved",
        "fact_count": len(brief.key_facts),
        "media_added": media_added,
    }


__all__ = ["save_research_brief", "search_web"]
