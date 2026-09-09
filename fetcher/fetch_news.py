"""News fetcher + pipeline kick-off CLI for the Carousel Factory.

Three source types are polled and normalized into :class:`app.schemas.NewsItem`
payloads, deduped by URL hash, and enqueued into the ``news_queue`` table via
:func:`app.services.db.enqueue_news`:

* **Gmail newsletters** - Gmail API (readonly scope), query
  ``settings.newsletter_query``. Auth mirrors ``app.tools.gmail_tools`` (same
  OAuth client secrets file) but uses its own readonly token cache next to
  ``settings.gmail_token_path`` because Google OAuth tokens are scope-bound.
* **RSS feeds** - ``settings.rss_feeds`` parsed with ``feedparser`` (bytes are
  downloaded first with ``requests`` so every network call has an explicit
  timeout; ``feedparser.parse(url)`` itself has none).
* **YouTube channels** - ``settings.youtube_channels`` via the public channel
  feed ``https://www.youtube.com/feeds/videos.xml?channel_id=<id>``; the watch
  URL is put first in ``media_urls`` so the First-Page Visual agent can clip it.

CLI (``python -m fetcher.fetch_news``)::

    --fetch     poll all sources and enqueue new items (duplicates skipped)
    --run-one   pop the next queued item, create the run, and drive one full
                pipeline invocation via app.agent.build_runner(). The run is
                EXPECTED to pause at the human-review phase - the review mail
                is out at that point and the review API resumes the run.

Resume addressing convention (the review API must use the same values):
``app_name = settings.app_name``, ``user_id = PIPELINE_USER_ID`` and
``session_id = run_id`` (also persisted in ``pending_reviews`` by the
Review Dispatcher's ``await_human_review`` tool).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html as html_lib
import logging
import re
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import feedparser
import requests
from pydantic import ValidationError

from app.config import settings
from app.observability import init_observability, shutdown_observability
from app.schemas import NewsItem
from app.services import db
from app.state import K_NEWS_ITEM, K_PHASE, K_RUN_ID, PHASE_DONE, PHASE_REVIEW

try:  # Gmail auth helpers are reused when importable (google API deps present).
    from app.tools import gmail_tools
except Exception as _gmail_import_error:  # pragma: no cover - env dependent
    gmail_tools = None  # type: ignore[assignment]
    _GMAIL_IMPORT_ERROR: Optional[Exception] = _gmail_import_error
else:
    _GMAIL_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

#: Fixed pipeline user id - the review API must resume runs with this exact
#: user id (sessions are addressed by app_name + user_id + session_id).
PIPELINE_USER_ID = "pipeline"

#: Session id convention: the run id doubles as the ADK session id.
GMAIL_READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
YOUTUBE_FEED_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

FEED_TIMEOUT_S = 30  # explicit timeout for every RSS/YouTube feed download
GMAIL_MAX_MESSAGES = 25  # newest matches of settings.newsletter_query per poll
MAX_ENTRIES_PER_FEED = 20
MAX_MEDIA_URLS = 8
MAX_BODY_CHARS = 20_000
_USER_AGENT = "carousel-factory-fetcher/1.0 (+https://github.com/closefuture)"

# --- HTML helpers (newsletters arrive as HTML; feeds embed HTML fragments) ---
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BLOCK_BREAK_RE = re.compile(
    r"<\s*(?:br\s*/?|/p|/div|/h[1-6]|/li|/tr|/table)\s*>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_A_HREF_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_SKIP_LINK_RE = re.compile(
    r"unsubscribe|preferences|view.{0,3}in.{0,3}browser|privacy|terms|mailto:",
    re.IGNORECASE,
)
_TRACKING_PIXEL_RE = re.compile(r"pixel|spacer|beacon|tracking|\b1x1\b", re.IGNORECASE)
_VIDEO_LINK_RE = re.compile(
    r"youtube\.com/watch|youtu\.be/|vimeo\.com/|\.mp4(?:\?|$)", re.IGNORECASE
)


def _html_to_text(markup: str) -> str:
    """Convert an HTML fragment to readable plain text.

    Regex-based on purpose (no bs4 dependency): drops script/style blocks,
    turns block-level closers and ``<br>`` into newlines, strips the remaining
    tags, unescapes entities, and collapses whitespace line by line.

    Args:
        markup: Raw HTML (may be empty).

    Returns:
        Plain text with one line per HTML block, empty lines removed.
    """
    if not markup:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _unique_capped(urls: Iterable[str], cap: int = MAX_MEDIA_URLS) -> list[str]:
    """Deduplicate URLs preserving order and cap the list length."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= cap:
            break
    return out


def _media_urls_from_html(markup: str) -> list[str]:
    """Extract candidate media URLs from newsletter HTML.

    Takes ``<img src>`` values (skipping obvious tracking pixels) plus links
    that point at video content (YouTube/Vimeo/.mp4).

    Args:
        markup: The HTML body of the mail (may be empty).

    Returns:
        Up to :data:`MAX_MEDIA_URLS` unique http(s) URLs.
    """
    urls: list[str] = []
    for src in _IMG_SRC_RE.findall(markup or ""):
        if src.startswith(("http://", "https://")) and not _TRACKING_PIXEL_RE.search(src):
            urls.append(src)
    for href in _A_HREF_RE.findall(markup or ""):
        if href.startswith(("http://", "https://")) and _VIDEO_LINK_RE.search(href):
            urls.append(href)
    return _unique_capped(urls)


def _article_link_from_html(markup: str) -> str:
    """Best-effort primary article link from newsletter HTML.

    Returns the first http(s) ``<a href>`` that does not look like an
    unsubscribe/preferences/legal link, or ``""`` when none qualifies.
    """
    for href in _A_HREF_RE.findall(markup or ""):
        if href.startswith(("http://", "https://")) and not _SKIP_LINK_RE.search(href):
            return href
    return ""


# ---------------------------------------------------------------------------
# Gmail newsletters
# ---------------------------------------------------------------------------
def _readonly_token_path() -> Path:
    """Token cache path for the readonly scope (separate from the send token).

    Google OAuth tokens are scope-bound, so the fetcher must not overwrite the
    send-scope token used by ``app.tools.gmail_tools``. The readonly token
    lives next to it: ``gmail-token.json`` -> ``gmail-token-readonly.json``.
    """
    token = Path(settings.gmail_token_path)
    return token.with_name(f"{token.stem}-readonly{token.suffix or '.json'}")


def _load_readonly_credentials() -> Any:
    """Load (or interactively create) Gmail readonly OAuth credentials.

    Mirrors ``gmail_tools._load_credentials`` - cached token, refresh with an
    explicit HTTP timeout, bounded interactive InstalledAppFlow only on
    attended runs - but with the readonly scope and its own token cache file.

    Returns:
        Valid ``google.oauth2.credentials.Credentials`` for the readonly scope.

    Raises:
        FileNotFoundError: OAuth client secrets file missing.
        RuntimeError: token missing/expired on an unattended run.
    """
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = _readonly_token_path()
    credentials_path = Path(settings.gmail_credentials_path)

    creds: Optional[Credentials] = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), GMAIL_READONLY_SCOPES
            )
        except (ValueError, OSError) as exc:  # corrupt/partial token file
            logger.warning(
                "Ignoring unreadable Gmail readonly token %s: %s", token_path, exc
            )
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(gmail_tools._TimeoutRequest())
        except Exception as exc:
            logger.warning(
                "Gmail readonly token refresh failed (%s); starting OAuth flow.", exc
            )
            creds = None

    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                "Gmail OAuth client secrets not found at "
                f"{credentials_path}. Set GMAIL_CREDENTIALS_PATH in .env."
            )
        if not gmail_tools._interactive_auth_allowed():
            raise RuntimeError(
                "Gmail readonly token missing/expired - run the fetcher once "
                "locally (set GMAIL_ALLOW_INTERACTIVE_AUTH=1) to create "
                f"{token_path}. Interactive OAuth is disabled on unattended "
                "runs so the fetcher fails loudly instead of blocking."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), GMAIL_READONLY_SCOPES
        )
        creds = flow.run_local_server(
            port=0, timeout_seconds=gmail_tools.INTERACTIVE_AUTH_TIMEOUT_S
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _gmail_readonly_service() -> Any:
    """Build a readonly Gmail API service with explicit per-request timeouts."""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    creds = _load_readonly_credentials()
    authed_http = AuthorizedHttp(
        creds, http=httplib2.Http(timeout=gmail_tools.HTTP_TIMEOUT_S)
    )
    return build(
        "gmail",
        "v1",
        http=authed_http,
        cache_discovery=False,
        static_discovery=True,  # bundled discovery doc: no network fetch
    )


def _decode_b64url(data: str) -> str:
    """Decode a Gmail base64url body chunk to text ('' on bad input)."""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except ValueError:  # binascii.Error is a ValueError subclass
        return ""


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Walk a Gmail message payload tree and collect text bodies.

    Args:
        payload: ``message["payload"]`` from ``messages.get(format="full")``.

    Returns:
        ``(plain_text, html_markup)`` - either may be empty.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    stack: list[dict] = [payload]
    while stack:
        part = stack.pop(0)
        stack.extend(part.get("parts") or [])
        mime = str(part.get("mimeType") or "")
        data = str(((part.get("body") or {}).get("data")) or "")
        if not data:
            continue
        text = _decode_b64url(data)
        if mime.startswith("text/plain"):
            plain_parts.append(text)
        elif mime.startswith("text/html"):
            html_parts.append(text)
    return "\n".join(plain_parts), "\n".join(html_parts)


def _newsletter_payload(msg: dict) -> Optional[dict]:
    """Normalize one Gmail message into an enqueue-ready NewsItem payload.

    Args:
        msg: Full message resource (``messages.get(format="full")``).

    Returns:
        ``NewsItem.model_dump(mode="json")`` plus an explicit ``url_hash``
        derived from the Gmail message id (newsletters rarely have one
        canonical URL), or ``None`` when the message carries no usable text.
    """
    msg_id = str(msg.get("id") or "")
    payload = msg.get("payload") or {}
    headers = {
        str(h.get("name") or "").lower(): str(h.get("value") or "")
        for h in payload.get("headers") or []
    }
    subject = headers.get("subject", "").strip() or "(no subject)"
    sender = headers.get("from", "").strip()

    published: Optional[datetime] = None
    if headers.get("date"):
        try:
            published = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError):
            published = None

    plain, markup = _extract_bodies(payload)
    body_text = plain.strip() or _html_to_text(markup)
    snippet = str(msg.get("snippet") or "").strip()
    if not (body_text or snippet):
        logger.warning("Gmail message %s has no readable body; skipped.", msg_id)
        return None

    item = NewsItem(
        id=f"gmail-{msg_id}" if msg_id else uuid.uuid4().hex,
        title=subject,
        summary=snippet,
        body=body_text[:MAX_BODY_CHARS],
        source_name=sender,
        source_url=_article_link_from_html(markup),
        media_urls=_media_urls_from_html(markup),
        published_at=published,
        tags=["newsletter", "gmail"],
    )
    data = item.model_dump(mode="json")
    # Dedupe on the message id, not on a body link that may be a shared story.
    data["url_hash"] = db.url_hash(f"gmail:{msg_id or subject}")
    return data


def fetch_gmail_newsletters() -> list[dict]:
    """Fetch newsletter mails matching ``settings.newsletter_query``.

    Returns:
        Enqueue-ready payload dicts (possibly empty). Auth/API failures are
        logged and yield an empty list so the other sources still run.
    """
    if gmail_tools is None:
        logger.warning(
            "Gmail fetching disabled - app.tools.gmail_tools not importable: %s",
            _GMAIL_IMPORT_ERROR,
        )
        return []
    query = settings.newsletter_query.strip()
    if not query:
        logger.info("NEWSLETTER_QUERY empty; skipping Gmail.")
        return []
    try:
        service = _gmail_readonly_service()
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=GMAIL_MAX_MESSAGES)
            .execute()
        )
    except Exception as exc:
        logger.error("Gmail newsletter fetch failed: %s", exc)
        return []

    payloads: list[dict] = []
    for ref in listing.get("messages") or []:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            data = _newsletter_payload(msg)
            if data:
                payloads.append(data)
        except Exception as exc:
            logger.warning("Skipping Gmail message %s: %s", ref.get("id"), exc)
    logger.info("Gmail: %d newsletter item(s) for query %r.", len(payloads), query)
    return payloads


# ---------------------------------------------------------------------------
# RSS + YouTube channel feeds
# ---------------------------------------------------------------------------
def _download_feed(url: str) -> feedparser.FeedParserDict:
    """Download a feed with an explicit timeout and parse it with feedparser.

    Args:
        url: The feed URL.

    Returns:
        The parsed feed.

    Raises:
        requests.RequestException: download failure (incl. timeout/4xx/5xx).
        ValueError: the payload could not be parsed as a feed at all.
    """
    resp = requests.get(
        url, timeout=FEED_TIMEOUT_S, headers={"User-Agent": _USER_AGENT}
    )
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(
            f"unparseable feed: {getattr(parsed, 'bozo_exception', 'unknown error')}"
        )
    return parsed


def _entry_media_urls(entry: Any) -> list[str]:
    """Collect media URLs from a feed entry (enclosures, links, media:*).

    Args:
        entry: A feedparser entry (dict-like).

    Returns:
        Up to :data:`MAX_MEDIA_URLS` unique http(s) URLs.
    """
    urls: list[str] = []
    for enc in entry.get("enclosures") or []:
        href = enc.get("href") or enc.get("url")
        if href:
            urls.append(str(href))
    for link in entry.get("links") or []:
        href = link.get("href")
        if not href:
            continue
        rel = str(link.get("rel") or "")
        ltype = str(link.get("type") or "")
        if rel == "enclosure" or ltype.startswith(("image/", "video/")):
            urls.append(str(href))
    for media in list(entry.get("media_content") or []) + list(
        entry.get("media_thumbnail") or []
    ):
        href = media.get("url")
        if href:
            urls.append(str(href))
    return _unique_capped(
        u for u in urls if u.startswith(("http://", "https://"))
    )


def _entry_payload(
    entry: Any,
    source_name: str,
    tags: list[str],
    id_prefix: str,
    link_is_media: bool = False,
) -> dict:
    """Normalize one feed entry into an enqueue-ready NewsItem payload.

    Args:
        entry: A feedparser entry.
        source_name: Human-readable source (feed/channel title).
        tags: Tags to stamp on the NewsItem (e.g. ``["rss"]``).
        id_prefix: Item-id prefix (``"rss"`` / ``"yt"``).
        link_is_media: When True the entry link itself is playable media (a
            YouTube watch URL) and is put FIRST in ``media_urls``.

    Returns:
        ``NewsItem.model_dump(mode="json")`` (plus an explicit ``url_hash``
        when the entry has no link to dedupe on).
    """
    link = str(entry.get("link") or "").strip()
    title = _html_to_text(str(entry.get("title") or "")).strip() or "(untitled)"
    summary = _html_to_text(str(entry.get("summary") or ""))[:2000]

    body = ""
    content_list = entry.get("content") or []
    if content_list:
        body = _html_to_text(str(content_list[0].get("value") or ""))

    published: Optional[datetime] = None
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if stamp:
        try:
            published = datetime(*stamp[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = None

    media = _entry_media_urls(entry)
    if link_is_media and link:
        media = _unique_capped([link, *media])

    item = NewsItem(
        id=f"{id_prefix}-{db.url_hash(link)[:16]}" if link else uuid.uuid4().hex,
        title=title,
        summary=summary,
        body=body[:MAX_BODY_CHARS],
        source_name=source_name,
        source_url=link,
        media_urls=media,
        published_at=published,
        tags=tags,
    )
    data = item.model_dump(mode="json")
    if not link:  # keep dedupe deterministic even without a URL
        data["url_hash"] = db.url_hash(f"{id_prefix}:{source_name}:{title}")
    return data


def fetch_rss_feeds() -> list[dict]:
    """Fetch every feed in ``settings.rss_feeds``.

    Returns:
        Enqueue-ready payload dicts; per-feed failures are logged and skipped.
    """
    payloads: list[dict] = []
    for url in settings.rss_feeds:
        try:
            parsed = _download_feed(url)
        except Exception as exc:
            logger.error("RSS feed failed (%s): %s", url, exc)
            continue
        feed_title = str((parsed.feed or {}).get("title") or url)
        for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
            try:
                payloads.append(_entry_payload(entry, feed_title, ["rss"], "rss"))
            except Exception as exc:
                logger.warning("Skipping RSS entry from %s: %s", url, exc)
    logger.info(
        "RSS: %d item(s) from %d feed(s).", len(payloads), len(settings.rss_feeds)
    )
    return payloads


def _youtube_feed_url(channel: str) -> str:
    """Build the public feed URL for a YouTube channel setting value.

    Accepts a bare channel id (``UC...``), a full feed URL, or a channel page
    URL containing ``/channel/<id>``.

    Raises:
        ValueError: for URLs the channel id cannot be derived from
            (e.g. ``@handle`` pages, which need an API lookup).
    """
    value = channel.strip()
    if value.startswith(("http://", "https://")):
        if "youtube.com/feeds/videos.xml" in value:
            return value
        match = re.search(r"channel/([A-Za-z0-9_-]+)", value)
        if match:
            return YOUTUBE_FEED_TEMPLATE.format(channel_id=match.group(1))
        raise ValueError(
            f"cannot derive a channel feed from {value!r} - configure the "
            "channel_id (UC...) or a /channel/<id> URL in YOUTUBE_CHANNELS"
        )
    return YOUTUBE_FEED_TEMPLATE.format(channel_id=value)


def fetch_youtube_feeds() -> list[dict]:
    """Fetch the channel feeds for ``settings.youtube_channels``.

    Returns:
        Enqueue-ready payload dicts; each entry's watch URL leads
        ``media_urls`` so the cover agent can source the clip from it.
    """
    payloads: list[dict] = []
    for channel in settings.youtube_channels:
        try:
            feed_url = _youtube_feed_url(channel)
            parsed = _download_feed(feed_url)
        except Exception as exc:
            logger.error("YouTube channel feed failed (%s): %s", channel, exc)
            continue
        channel_title = str((parsed.feed or {}).get("title") or channel)
        for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
            try:
                payloads.append(
                    _entry_payload(
                        entry,
                        channel_title,
                        ["youtube", "video"],
                        "yt",
                        link_is_media=True,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping YouTube entry from %s: %s", channel, exc)
    logger.info(
        "YouTube: %d item(s) from %d channel(s).",
        len(payloads),
        len(settings.youtube_channels),
    )
    return payloads


def fetch_all() -> list[dict]:
    """Poll all configured sources (Gmail, RSS, YouTube). Blocking I/O."""
    payloads: list[dict] = []
    payloads.extend(fetch_gmail_newsletters())
    payloads.extend(fetch_rss_feeds())
    payloads.extend(fetch_youtube_feeds())
    return payloads


# ---------------------------------------------------------------------------
# Queue + pipeline driving
# ---------------------------------------------------------------------------
def _payload_dedupe_hash(data: dict) -> str:
    """The url_hash ``db.enqueue_news`` will dedupe this payload on."""
    explicit = data.get("url_hash")
    if explicit:
        return str(explicit)
    basis = data.get("source_url") or data.get("id") or data.get("title") or ""
    return db.url_hash(str(basis)) if basis else uuid.uuid4().hex


async def enqueue_items(payloads: list[dict]) -> tuple[int, int]:
    """Enqueue payloads into ``news_queue`` (dedupe: batch-local + DB unique).

    Args:
        payloads: NewsItem payload dicts from the ``fetch_*`` functions.

    Returns:
        ``(enqueued, skipped)`` counts (skipped = duplicates).
    """
    enqueued = 0
    skipped = 0
    seen: set[str] = set()
    for data in payloads:
        h = _payload_dedupe_hash(data)
        if h in seen:
            skipped += 1
            continue
        seen.add(h)
        result = await db.enqueue_news(data)
        if result.get("enqueued"):
            enqueued += 1
            logger.info("Enqueued: %s", data.get("title", "?"))
        else:
            skipped += 1
    return enqueued, skipped


def _summarize_event(event: Any) -> str:
    """One console line for a pipeline event ('' when there is nothing to say)."""
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


async def _mark_news_quietly(news_id: str, status: str) -> None:
    """Best-effort final status update on the popped news_queue row."""
    if not news_id:
        return
    try:
        await db.mark_news_done(news_id, status)
    except Exception as exc:
        logger.warning("Could not mark news %s as %s: %s", news_id, status, exc)


async def run_one() -> Optional[str]:
    """Pop the next queued news item and drive one full pipeline invocation.

    Creates the ``runs`` row, seeds a fresh ADK session (``session_id`` ==
    ``run_id``, ``user_id`` == :data:`PIPELINE_USER_ID`) with the news item in
    state, and streams the invocation via ``app.agent.build_runner()``. The
    invocation ENDS at the human-review pause by design - the review mail is
    out at that point and the review API resumes the run later.

    Returns:
        The run id, or ``None`` when the queue was empty / the item was bad.
    """
    item = await db.next_queued_news()
    if item is None:
        print("News queue is empty - nothing to run. (Try --fetch first.)")
        return None
    news_id = str(item.get("id") or "")
    try:
        news = NewsItem.model_validate(item)
    except ValidationError as exc:
        logger.error("Queued item %s is not a valid NewsItem: %s", news_id, exc)
        await _mark_news_quietly(news_id, db.STATUS_FAILED)
        return None

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    await db.create_run(run_id, news_id)
    print(f"Starting run {run_id} for: {news.title}")

    # Imported lazily: app.agent builds the full agent tree at import time.
    from google.genai import types

    from app.agent import build_runner

    runner = build_runner()
    phase = ""
    paused_on_tool = False
    # Say "still alive" on a timer for as long as this run is going.
    #
    # runs.updated_at otherwise only moves on a PHASE transition, and a single
    # phase runs for many minutes (template_design renders every slide with an
    # image model). Startup recovery in the web service treats a long-idle run
    # in an active phase as killed, so without this a healthy CLI run gets
    # reclaimed out from under itself the moment the web service restarts -
    # which is exactly what happened twice during development.
    from app.runs.service import HEARTBEAT_INTERVAL_S

    async def _heartbeat() -> None:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                await db.touch_run(run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Heartbeat failed for run %s: %s", run_id, exc)

    beat = asyncio.get_running_loop().create_task(
        _heartbeat(), name=f"heartbeat-{run_id}"
    )

    try:
        await runner.session_service.create_session(
            app_name=settings.app_name,
            user_id=PIPELINE_USER_ID,
            session_id=run_id,
            state={
                K_RUN_ID: run_id,
                K_NEWS_ITEM: news.model_dump(mode="json"),
            },
        )
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Create an Instagram carousel for the queued news item: "
                        f"{news.title}"
                    )
                )
            ],
        )
        async for event in runner.run_async(
            user_id=PIPELINE_USER_ID, session_id=run_id, new_message=message
        ):
            line = _summarize_event(event)
            if line:
                print(line)
            if getattr(event, "long_running_tool_ids", None):
                paused_on_tool = True
        session = await runner.session_service.get_session(
            app_name=settings.app_name,
            user_id=PIPELINE_USER_ID,
            session_id=run_id,
        )
        phase = str((session.state if session else {}).get(K_PHASE) or "")
    except Exception:
        await _mark_news_quietly(news_id, db.STATUS_FAILED)
        raise
    finally:
        beat.cancel()
        try:
            await runner.close()
        except Exception as exc:
            logger.warning("Runner close failed: %s", exc)

    # The queue row is consumed either way; the runs table tracks the pipeline.
    await _mark_news_quietly(news_id, db.STATUS_DONE)

    print()
    print(f"run_id: {run_id}")
    if phase == PHASE_DONE:
        print(f"Run {run_id} completed the whole pipeline (phase 'done').")
    elif phase == PHASE_REVIEW or paused_on_tool:
        print(
            f"Run {run_id} is paused for human review - the review mail is "
            "out. Approve or Reject from the email; the review API resumes "
            "the run."
        )
    else:
        print(
            f"Run {run_id} ended in phase '{phase or 'unknown'}' - check the "
            "log above for errors."
        )
    return run_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    """The ``python -m fetcher.fetch_news`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m fetcher.fetch_news",
        description=(
            "Carousel Factory fetcher: poll Gmail newsletters, RSS feeds and "
            "YouTube channel feeds into the news queue, and/or start one "
            "pipeline run for the next queued item."
        ),
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="poll all sources and enqueue new items (duplicates are skipped)",
    )
    parser.add_argument(
        "--run-one",
        action="store_true",
        help=(
            "pop the next queued item and drive one pipeline invocation; the "
            "run pauses at human review (the review mail is out) - expected"
        ),
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    """Async entrypoint: run the requested actions, then close the DB pool."""
    try:
        if args.fetch:
            payloads = await asyncio.to_thread(fetch_all)
            enqueued, skipped = await enqueue_items(payloads)
            print(
                f"Fetched {len(payloads)} item(s): {enqueued} enqueued, "
                f"{skipped} duplicate(s) skipped."
            )
        if args.run_one:
            await run_one()
    finally:
        await db.close_pool()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: parse args and dispatch. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not (args.fetch or args.run_one):
        parser.print_help()
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_observability()
    try:
        return asyncio.run(_amain(args))
    finally:
        shutdown_observability()  # flush buffered Langfuse spans before exit


if __name__ == "__main__":
    raise SystemExit(main())
