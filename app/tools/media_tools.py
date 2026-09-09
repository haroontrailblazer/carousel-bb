"""Media tools for the First-Page Visual agent.

Three jobs (see docs/CONTRACTS.md file map):

1. ``find_source_clip(news)``   - rank the best sourced video (preferred) or
   image URL from a news item's ``media_urls``, its source page, every page
   linked in its body text, plus a live trend-aware visual search. Current,
   topical, source-affine imagery outranks generic merely available assets.
   ``placeholder_background`` guarantees a cover can ALWAYS be built.
2. ``download_and_trim(url)``   - fetch the clip via the yt-dlp Python API and
   trim it into the configured cover window (settings.cover_clip_min_s..max_s,
   default 4-15 s), silent H.264 mp4.
3. ``compose_cover(...)``       - subject-aware edge-to-edge crop across the
   visible cover stage, composite the STRANGE-COVER overlay template plus a
   Pillow-rendered title block (warm-white, condensed extra-bold uppercase,
   solid green highlight phrase), and produce the final cover mp4 + poster.

The cover is NEVER AI-generated (skills/cover-style.md): a sourced clip, or -
fallback - the update's own image turned into a 6 s slow-zoom video.

All ffmpeg work shells out to ``settings.ffmpeg_bin`` with explicit timeouts.
All intermediate files live under a caller-provided run-specific ``workdir``
(default: ``settings.workdir / "adhoc"``).  These functions are plain,
type-hinted callables so the agent layer can wrap them in ADK ``FunctionTool``s.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, download_range_func

from app.config import settings
from app.text_rules import require_no_em_dash
from app.tools.brand_layout import (
    ACCENT_GREEN,
    HEADLINE_MAX_LINES,
    headline_font,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".gifv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_HOSTS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "streamable.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "twitch.tv",
    "dailymotion.com",
)

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
# (connect, read) timeouts for every requests call - never hang the pipeline.
_PAGE_TIMEOUT = (10, 30)
_IMAGE_TIMEOUT = (10, 60)
_FFMPEG_TIMEOUT_S = 300
_FFPROBE_TIMEOUT_S = 60
_MAX_PROBES = 4  # cap yt-dlp probes per find_source_clip call
_TREND_PAGE_LIMIT = 4
_IMAGE_CANDIDATE_LIMIT = 5

# Title styling (current baskaranbuilds.com tokens; skills/cover-style.md).
_TEXT_PRIMARY = (232, 228, 214, 255)  # #E8E4D6
_ACCENT_GREEN = (*ACCENT_GREEN, 255)  # #8FB832
_TITLE_MAX_LINES = HEADLINE_MAX_LINES
_COVER_TITLE_FONT_SIZE = 128
_TITLE_MAX_WIDTH_FRAC = 0.78
_TITLE_CENTER_Y_FRAC = 0.79  # matches the template's own title-block center

# Region of the template occupied by its baked-in EXAMPLE title text
# ("STOP PROMPTING YOUR AI, GIVE IT A LOOP") - measured on the shipped
# STRANGE-COVER (1).png. It is scrubbed before compositing the real title.
# Fractions of width/height; excludes the side arrow glyphs (0.093-0.167 and
# 0.839-0.907) and the grid floor.
_TEMPLATE_TEXT_BOX = (0.175, 0.705, 0.83, 0.872)  # (x0, y0, x1, y1)
_STILL_COVER_SECONDS = 6.0
_COVER_FPS = 30
_COVER_VISUAL_HEIGHT_FRAC = 0.75
_SALIENCY_MAX_SIZE = 320
_MIN_COVER_IMAGE_PIXELS = 450_000
_MIN_COVER_IMAGE_SIDE = 360
_MAX_COVER_IMAGE_ASPECT = 3.2

_TOPIC_STOPWORDS = {
    "about", "after", "again", "from", "into", "just", "latest", "more",
    "new", "over", "that", "the", "their", "this", "with", "your",
}
_GOOD_VISUAL_TOKENS = {
    "announcement", "cover", "demo", "event", "hero", "keynote", "launch",
    "product", "release", "screenshot", "stage", "visual",
}
_REPUTABLE_VISUAL_SITES = {
    "apnews.com",
    "caixin.com",
    "douban.com",
    "maoyan.com",
    "sina.cn",
    "sixthtone.com",
    "themoviedb.org",
    "wikipedia.org",
    "wikimedia.org",
}
_BAD_VISUAL_TOKENS = {
    "avatar", "badge", "favicon", "icon", "logo", "placeholder", "sprite",
    "tracking", "transparent",
}
_BANNER_VISUAL_MARKERS = {
    "16_9", "16-9", "banner", "og-image", "og_image", "opengraph",
    "share-card", "share_card", "social-card", "social_card",
}
_VIDEO_QUERY_NOISE = {
    "announcement", "clip", "cover", "current", "demo", "event", "image",
    "keynote", "launch", "latest", "news", "official", "photo", "release",
    "trending", "video", "visual",
}


@dataclass(frozen=True)
class _MediaCandidate:
    url: str
    kind: str
    score: int
    origin: str
    context_url: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _ensure_workdir(workdir: str, subdir: str) -> Path:
    """Resolve (and create) the working folder for intermediate files.

    Args:
        workdir: Run-specific folder passed by the caller; empty string falls
            back to ``settings.workdir / "adhoc"``.
        subdir: Purpose-specific subfolder ("clips", "cover", ...).

    Returns:
        The created directory as a ``Path``.
    """
    base = Path(workdir) if str(workdir).strip() else (settings.workdir / "adhoc")
    target = base / subdir
    target.mkdir(parents=True, exist_ok=True)
    return target


def _ffprobe_bin() -> str:
    """Derive the ffprobe executable from ``settings.ffmpeg_bin``."""
    p = Path(settings.ffmpeg_bin)
    if len(p.parts) > 1 and "ffmpeg" in p.name.lower():
        sibling = p.with_name(p.name.lower().replace("ffmpeg", "ffprobe"))
        if sibling.exists():
            return str(sibling)
    return "ffprobe"


def _run_ffmpeg(args: list[str], timeout_s: int = _FFMPEG_TIMEOUT_S) -> None:
    """Run an ffmpeg command, raising ``RuntimeError`` with the stderr tail."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {tail}")


def _media_duration(path: str | Path) -> float:
    """Return media duration in seconds (0.0 when it cannot be determined)."""
    try:
        proc = subprocess.run(
            [
                _ffprobe_bin(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            dur = float(data.get("format", {}).get("duration", 0.0))
            if dur > 0:
                return dur
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        pass
    # Fallback: parse `ffmpeg -i` banner output.
    try:
        proc = subprocess.run(
            [settings.ffmpeg_bin, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return 0.0


def _url_ext(url: str) -> str:
    """Lower-cased file extension of a URL path ('' when none)."""
    return Path(urlparse(url).path).suffix.lower()


def _classify_url(url: str) -> str:
    """Classify a URL as 'video', 'image' or 'unknown' without network I/O."""
    ext = _url_ext(url)
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    host = (urlparse(url).netloc or "").lower()
    if any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS):
        return "video"
    return "unknown"


def _probe_with_ytdlp(url: str) -> Optional[dict[str, Any]]:
    """Probe a URL with the yt-dlp Python API without downloading.

    Returns a small dict ``{"duration": float, "title": str}`` when the URL
    resolves to playable video, ``None`` otherwise.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 20,
        "retries": 1,
        "skip_download": True,
        "http_headers": dict(_HTTP_HEADERS),
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 - extractor errors vary widely (DownloadError etc.)
        return None
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            return None
        info = entries[0] or {}
    has_video = bool(info.get("formats") or info.get("url") or info.get("ext"))
    if not has_video:
        return None
    return {
        "duration": float(info.get("duration") or 0.0),
        "title": str(info.get("title") or ""),
    }


# ---------------------------------------------------------------------------
# 1) find_source_clip
# ---------------------------------------------------------------------------

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_KEY_RE = re.compile(r"""(?:property|name)\s*=\s*["']([^"']+)["']""", re.I)
_META_CONTENT_RE = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)
_VIDEO_SRC_RE = re.compile(
    r"""<(?:video|source)\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I
)
_IFRAME_SRC_RE = re.compile(r"""<iframe\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_HREF_MEDIA_RE = re.compile(
    r"""href\s*=\s*["']([^"']+\.(?:mp4|mov|webm|m4v))(?:\?[^"']*)?["']""", re.I
)
_IMAGE_TAG_RE = re.compile(r"<(?:img|source)\b[^>]*>", re.I)
_IMAGE_SRC_RE = re.compile(
    r"""(?:src|data-src|data-lazy-src)\s*=\s*["']([^"']+)["']""", re.I
)
_IMAGE_SRCSET_RE = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.I)
_IMAGE_ALT_RE = re.compile(r"""(?:alt|title)\s*=\s*["']([^"']+)["']""", re.I)

# meta key -> (kind, score)
_META_SCORES: dict[str, tuple[str, int]] = {
    "og:video": ("video", 90),
    "og:video:url": ("video", 90),
    "og:video:secure_url": ("video", 90),
    "twitter:player:stream": ("video", 85),
    "og:image": ("image", 45),
    "og:image:url": ("image", 45),
    "twitter:image": ("image", 42),
    "twitter:image:src": ("image", 42),
}


def _scrape_page_media(page_url: str) -> list[tuple[str, str, int]]:
    """Scrape a source page for media candidates.

    Args:
        page_url: The news item's source page URL.

    Returns:
        List of ``(url, kind, score)`` tuples; kind is 'video'/'image'/'unknown'.
        Network failures return an empty list - scraping is best-effort.
    """
    try:
        resp = requests.get(page_url, headers=_HTTP_HEADERS, timeout=_PAGE_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return []
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "xml" not in ctype:
        return []
    text = resp.text[:3_000_000]

    found: list[tuple[str, str, int]] = []

    def add(raw: str, kind: str, score: int) -> None:
        url = urljoin(page_url, html.unescape(raw.strip()))
        if url.startswith(("http://", "https://")):
            found.append((url, kind, score))

    for tag in _META_TAG_RE.findall(text):
        key_m = _META_KEY_RE.search(tag)
        content_m = _META_CONTENT_RE.search(tag)
        if not key_m or not content_m:
            continue
        entry = _META_SCORES.get(key_m.group(1).strip().lower())
        if entry:
            add(content_m.group(1), entry[0], entry[1])

    for raw in _VIDEO_SRC_RE.findall(text):
        add(raw, "video", 88)
    for raw in _IFRAME_SRC_RE.findall(text):
        if _classify_url(urljoin(page_url, raw)) == "video":
            add(raw, "video", 80)
    for raw in _HREF_MEDIA_RE.findall(text):
        add(raw, "video", 82)

    # Social/OG cards are often text-only banners. Also inspect the actual
    # article images so a subject-led photo can compete with the page's share
    # card. Modern sites commonly lazy-load via data-src or <picture> srcset.
    for tag in _IMAGE_TAG_RE.findall(text):
        alt_match = _IMAGE_ALT_RE.search(tag)
        alt = html.unescape(alt_match.group(1)).lower() if alt_match else ""
        score = 48
        if any(word in alt for word in ("photo", "holding", "presents", "unveil")):
            score += 5
        raw_urls = list(_IMAGE_SRC_RE.findall(tag))
        for srcset in _IMAGE_SRCSET_RE.findall(tag):
            raw_urls.extend(
                item.strip().split()[0]
                for item in html.unescape(srcset).split(",")
                if item.strip()
            )
        for raw in raw_urls:
            resolved = urljoin(page_url, html.unescape(raw.strip()))
            if _classify_url(resolved) == "image":
                add(resolved, "image", score)
    return found


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_BODY_URL_SCRAPE_LIMIT = 4  # pages linked in the body text scraped per call


def _site(url: str) -> str:
    """Return a comparable hostname for source-affinity scoring."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _topic_tokens(news: dict) -> set[str]:
    """Extract useful topic words for lightweight visual relevance scoring."""
    text = " ".join(
        [str(news.get("title") or ""), *[str(tag) for tag in news.get("tags") or []]]
    ).lower()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if len(token) >= 3 and token not in _TOPIC_STOPWORDS
    }


def _is_reputable_visual_site(site: str) -> bool:
    """Return whether a host belongs to a trusted source or media catalog."""
    return any(
        site == trusted or site.endswith("." + trusted)
        for trusted in _REPUTABLE_VISUAL_SITES
    )


def _context_has_marker(context: str, marker: str) -> bool:
    """Match URL/slug markers without treating ``icon`` as part of ``silicon``."""
    if any(char in marker for char in "_-"):
        return marker in context
    return re.search(
        rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])",
        context,
    ) is not None


def _meaningful_search_tokens(text: str) -> set[str]:
    """Return distinctive terms suitable for rejecting unrelated video hits."""
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) >= 3
        and token not in _TOPIC_STOPWORDS
        and token not in _VIDEO_QUERY_NOISE
    }


def _video_title_matches_topic(title: str, query: str) -> bool:
    """Reject a playable search hit when its title has no topic connection."""
    query_tokens = _meaningful_search_tokens(query)
    if not query_tokens:
        return True
    title_tokens = _meaningful_search_tokens(title)
    overlap = len(query_tokens & title_tokens)
    # Rich queries normally carry a named company/person plus a product or
    # event. Requiring two signals prevents a broad shared word such as
    # "OpenAI" from admitting an unrelated Sora/anime trend result.
    required = 2 if len(query_tokens) >= 3 else 1
    return overlap >= required


def _default_visual_query(news: dict) -> str:
    """Build a clean topic query even when an ad-hoc title contains only URLs."""
    raw = " ".join(
        str(news.get(key) or "") for key in ("title", "summary", "body")
    )
    urls = _URL_IN_TEXT_RE.findall(raw)
    without_urls = _URL_IN_TEXT_RE.sub(" ", raw)
    clean_text = re.sub(r"\s+", " ", without_urls).strip()
    slug_phrases: list[str] = []
    for url in urls:
        path = unquote(urlparse(url).path).strip("/")
        if not path:
            continue
        slug = path.split("/")[-1]
        phrase = re.sub(r"[-_]+", " ", slug)
        phrase = re.sub(
            r"\b(?:index|article|detail|movie|movies|news)\b",
            " ",
            phrase,
            flags=re.I,
        )
        phrase = re.sub(r"\b\d{4,}\b", " ", phrase)
        phrase = re.sub(r"\s+", " ", phrase).strip()
        if phrase and any(char.isalpha() for char in phrase):
            slug_phrases.append(phrase)
    topic = slug_phrases[0] if slug_phrases else clean_text
    topic = re.sub(r"\s+", " ", topic).strip()
    if clean_text and clean_text.lower() not in topic.lower():
        topic = f"{topic} {clean_text}".strip()
    return f"{topic} official current news image film still poster".strip()


def _rank_candidate(
    candidate: _MediaCandidate,
    news: dict,
    source_url: str,
) -> _MediaCandidate:
    """Score topicality, freshness signals, source affinity, and visual quality."""
    score = candidate.score
    reasons = [candidate.reason] if candidate.reason else []
    context = f"{candidate.url} {candidate.context_url}".lower()
    overlap = sorted(
        token for token in _topic_tokens(news)
        if _context_has_marker(context, token)
    )
    if overlap:
        bonus = min(len(overlap) * 3, 12)
        score += bonus
        reasons.append(f"topic match +{bonus}")

    source_site = _site(source_url)
    context_site = _site(candidate.context_url or candidate.url)
    if source_site and context_site == source_site:
        score += 12
        reasons.append("official/source-site +12")
    if _is_reputable_visual_site(context_site):
        score += 14
        reasons.append("trusted visual source +14")
    if context_site.endswith(".blog"):
        score -= 30
        reasons.append("unverified blog source -30")

    good = sorted(
        token for token in _GOOD_VISUAL_TOKENS
        if _context_has_marker(context, token)
    )
    if good:
        bonus = min(len(good) * 2, 8)
        score += bonus
        reasons.append(f"visual signal +{bonus}")
    bad = sorted(
        token for token in _BAD_VISUAL_TOKENS
        if _context_has_marker(context, token)
    )
    if bad:
        penalty = min(len(bad) * 12, 36)
        score -= penalty
        reasons.append(f"generic asset -{penalty}")

    banner = sorted(
        marker for marker in _BANNER_VISUAL_MARKERS
        if _context_has_marker(context, marker)
    )
    if banner:
        penalty = min(len(banner) * 18, 36)
        score -= penalty
        reasons.append(f"banner/social card -{penalty}")

    return _MediaCandidate(
        url=candidate.url,
        kind=candidate.kind,
        score=score,
        origin=candidate.origin,
        context_url=candidate.context_url,
        reason="; ".join(reasons),
    )


def _search_trending_pages(
    news: dict,
    search_query: str = "",
) -> tuple[list[str], str]:
    """Find current pages carrying prominent, source-grounded topic visuals.

    This deliberately searches beyond URLs already attached to the news item.
    The returned pages are scraped for their own og:image/og:video assets and
    ranked with a freshness/relevance bonus by :func:`find_source_clip`.
    """
    topic = re.sub(
        r"\s+",
        " ",
        (search_query or _default_visual_query(news)).strip(),
    )
    if not topic:
        return [], "trend search skipped: empty topic"
    published = str(news.get("published_at") or "").strip()
    date_hint = published[:10] if published else str(datetime.now(timezone.utc).date())
    query = (
        f'As of {date_hint}, find the newest prominent visual coverage for "{topic}". '
        "Prefer an official subject-led launch photo, product demo screenshot, "
        "keynote still, or current reputable news photo. Return pages that visibly "
        "carry a real person, product, place, or event from the story. Avoid text-only "
        "social banners, generic stock art, logos, icons, and old unrelated media."
    )
    try:
        # Local import keeps the media layer usable in minimal/offline contexts.
        from app.tools.research_tools import search_web

        result = search_web(query)
    except Exception as exc:  # noqa: BLE001 - trend search is best-effort
        return [], f"trend search unavailable: {exc}"
    if result.get("status") != "ok":
        return [], f"trend search unavailable: {result.get('message', 'unknown error')}"
    pages = [
        str(url).strip()
        for url in result.get("sources") or []
        if str(url).strip().startswith(("http://", "https://"))
    ]
    return pages[:_TREND_PAGE_LIMIT], f"live trend search checked {len(pages[:_TREND_PAGE_LIMIT])} page(s)"


def _search_video_online(query: str) -> Optional[dict[str, Any]]:
    """Web-search (YouTube via yt-dlp ``ytsearch``) for a playable event clip.

    Args:
        query: Free-text search, e.g. "Niu Lai movie official trailer".

    Returns:
        ``{"url", "duration", "title"}`` for the first playable result, or
        ``None`` when the search fails or returns nothing.
    """
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        return None
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 20,
        "retries": 1,
        "skip_download": True,
        "http_headers": dict(_HTTP_HEADERS),
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch3:{query}", download=False)
    except Exception:  # noqa: BLE001 - extractor errors vary widely
        return None
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        title = str(entry.get("title") or "").strip()
        if not _video_title_matches_topic(title, query):
            continue
        url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
        if url:
            return {
                "url": url,
                "duration": float(entry.get("duration") or 0.0),
                "title": title,
            }
    return None


def find_source_clip(news: dict, search_query: str = "") -> dict:
    """Pick the best sourced video (preferred) or image URL for the cover.

    Candidates come from the news item's ``media_urls`` list, the
    ``source_url`` itself (it may be a YouTube/Vimeo watch page), media
    scraped off the source page (og:video / og:image / <video> / iframes),
    AND every page linked inside the item's body/summary text (each scraped
    the same way - newsletter blurbs usually carry the links inline). When no
    sourced video is playable, a bounded web search (``ytsearch``) hunts for
    an event/announcement clip before falling back to the best image.

    Args:
        news: A ``NewsItem``-shaped dict (keys: ``media_urls``, ``source_url``,
            ``title``, ``body``, ...).
        search_query: Optional web-search override for the video hunt; empty
            uses the news title.

    Returns:
        Dict with keys: ``found`` (bool), ``url`` (str), ``is_video`` (bool),
        ``duration_s`` (float, 0.0 when unknown), ``origin``
        ('media_urls' | 'media_page' | 'source_url' | 'source_page' | 'body_url' |
        'body_page' | 'trend_search' | 'web_search' | ''),
        ``image_candidates`` (ranked still alternatives), ``trend_search``
        (live-search status), and ``note`` (str).
    """
    candidate_map: dict[str, _MediaCandidate] = {}

    def add(
        url: str,
        kind: str,
        score: int,
        origin: str,
        context_url: str = "",
        reason: str = "",
    ) -> None:
        key = url.split("#", 1)[0]
        if not key:
            return
        candidate = _MediaCandidate(url, kind, score, origin, context_url, reason)
        previous = candidate_map.get(key)
        if previous is None or candidate.score > previous.score:
            candidate_map[key] = candidate

    for url in news.get("media_urls") or []:
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        kind = _classify_url(url)
        score = {"video": 100, "image": 50}.get(kind, 25)
        add(url, kind, score, "media_urls", reason="attached source media")
        if kind == "unknown":
            for media_url, media_kind, scraped_score in _scrape_page_media(url):
                add(
                    media_url,
                    media_kind,
                    max(scraped_score, 66 if media_kind == "image" else 94),
                    "media_page",
                    url,
                    "media from an attached research source page",
                )

    source_url = str(news.get("source_url") or "").strip()
    if source_url and _classify_url(source_url) == "video":
        add(source_url, "video", 95, "source_url", source_url, "official source video")
    if source_url:
        for url, kind, score in _scrape_page_media(source_url):
            add(url, kind, score, "source_page", source_url, "official source page")

    # Pages linked inside the body/summary text (ad-hoc runs put the links
    # there): direct media URLs become candidates, other pages are scraped
    # for og:image / og:video just like the source page.
    body_text = " ".join(
        str(news.get(k) or "") for k in ("title", "summary", "body")
    )
    scraped_pages = 0
    for raw in _URL_IN_TEXT_RE.findall(body_text):
        url = raw.rstrip(".,;:!?")
        if url.split("#", 1)[0] in candidate_map or url == source_url:
            continue
        kind = _classify_url(url)
        if kind == "video":
            add(url, "video", 92, "body_url", reason="linked from news body")
        elif kind == "image":
            add(url, "image", 48, "body_url", reason="linked from news body")
        elif scraped_pages < _BODY_URL_SCRAPE_LIMIT:
            scraped_pages += 1
            for media_url, media_kind, score in _scrape_page_media(url):
                add(
                    media_url,
                    media_kind,
                    max(score - 2, 1),
                    "body_page",
                    url,
                    "page linked from news body",
                )

    # Do not settle for the first attached/available image. Search the live web
    # for the topic's current prominent visual coverage, scrape those pages,
    # then rank the resulting assets alongside the source-owned candidates.
    trend_pages, trend_note = _search_trending_pages(news, search_query)
    for rank, page_url in enumerate(trend_pages, start=1):
        page_kind = _classify_url(page_url)
        if page_kind in {"image", "video"}:
            direct_score = (58 if page_kind == "image" else 93) - rank * 2
            add(
                page_url,
                page_kind,
                direct_score,
                "trend_search",
                page_url,
                f"direct live trend result rank {rank}",
            )
            continue
        for media_url, media_kind, scraped_score in _scrape_page_media(page_url):
            if media_kind == "video":
                score = max(scraped_score, 93 - rank * 2)
            elif media_kind == "image":
                score = max(scraped_score, 58 - rank * 2)
            else:
                score = scraped_score
            add(
                media_url,
                media_kind,
                score,
                "trend_search",
                page_url,
                f"live trend result rank {rank}",
            )

    candidates = [
        _rank_candidate(candidate, news, source_url)
        for candidate in candidate_map.values()
    ]
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    # Best image candidate - reported alongside every result so the caller
    # can drop to the image path the moment video downloads fail, without
    # re-searching.
    ranked_images = [candidate for candidate in candidates if candidate.kind == "image"]
    image_candidates = [
        {
            "url": candidate.url,
            "origin": candidate.origin,
            "score": candidate.score,
            "reason": candidate.reason,
            "context_url": candidate.context_url,
        }
        for candidate in ranked_images[:_IMAGE_CANDIDATE_LIMIT]
    ]
    image_url = image_candidates[0]["url"] if image_candidates else ""
    image_origin = image_candidates[0]["origin"] if image_candidates else ""

    def result(found: bool, url: str, is_video: bool, duration: float,
               origin: str, note: str) -> dict:
        return {
            "found": found,
            "url": url,
            "is_video": is_video,
            "duration_s": duration,
            "origin": origin,
            "image_url": image_url,
            "image_origin": image_origin,
            "image_candidates": image_candidates,
            "trend_search": trend_note,
            "note": note,
        }

    # Confirm video candidates (bounded number of network probes).
    probes = 0
    for candidate in candidates:
        if candidate.kind not in ("video", "unknown"):
            continue
        if probes >= _MAX_PROBES:
            break
        probes += 1
        info = _probe_with_ytdlp(candidate.url)
        if info is not None:
            if candidate.origin == "trend_search" and not _video_title_matches_topic(
                str(info.get("title") or ""),
                search_query.strip() or _default_visual_query(news),
            ):
                continue
            return result(
                True,
                candidate.url,
                True,
                round(info["duration"], 2),
                candidate.origin,
                info["title"] or "probed with yt-dlp",
            )
        if _url_ext(candidate.url) in VIDEO_EXTS:
            # Direct video file that yt-dlp could not probe (e.g. signed CDN
            # URL) - trust the extension and let download_and_trim try.
            return result(
                True, candidate.url, True, 0.0, candidate.origin,
                "direct video file (probe skipped/failed)",
            )

    # No sourced video played - hunt the web for an event/announcement clip
    # (the original spec: "a small video piece on that event from web").
    query = search_query.strip() or _default_visual_query(news)
    searched = _search_video_online(query)
    if searched is not None:
        return result(
            True, searched["url"], True, round(searched["duration"], 2),
            "web_search", f"web search hit: {searched['title'] or query}",
        )

    if image_url:
        return result(
            True, image_url, False, 0.0, image_origin,
            "no playable video found; image fallback",
        )

    return result(
        False, "", False, 0.0, "",
        "no video or image candidates found (media_urls, source page, "
        "body links and web search all came up empty) - use "
        "placeholder_background for a text-only cover",
    )


# ---------------------------------------------------------------------------
# 2) download_and_trim  (+ download_image helper for the fallback path)
# ---------------------------------------------------------------------------


def download_and_trim(
    url: str, max_s: Optional[int] = None, min_s: Optional[int] = None, workdir: str = ""
) -> str:
    """Download a video via the yt-dlp Python API and trim it to the cover window.

    Long videos are section-downloaded (first ~4x``max_s`` seconds) to avoid
    pulling whole streams. The trim re-encodes to silent H.264 mp4 with
    ``+faststart``. Clips shorter than ``min_s`` are looped up to ``min_s``.

    Args:
        url: Video URL (direct file or any yt-dlp-supported page).
        max_s: Maximum clip length in seconds (default:
            ``settings.cover_clip_max_s``).
        min_s: Minimum clip length in seconds (default:
            ``settings.cover_clip_min_s``).
        workdir: Run-specific working folder (see ``_ensure_workdir``).

    Returns:
        Absolute path (str) of the trimmed mp4.

    Raises:
        RuntimeError: When the download or the ffmpeg trim fails.
    """
    max_s = int(max_s if max_s else settings.cover_clip_max_s)
    min_s = int(min_s if min_s else settings.cover_clip_min_s)
    wd = _ensure_workdir(workdir, "clips")
    stem = uuid.uuid4().hex[:12]

    pre = _probe_with_ytdlp(url)
    est_duration = float((pre or {}).get("duration") or 0.0)

    ydl_opts: dict[str, Any] = {
        "outtmpl": str(wd / f"src-{stem}.%(ext)s"),
        "format": (
            "bestvideo[height<=1080][ext=mp4]/bestvideo[height<=1080]"
            "/best[height<=1080]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 30,
        "retries": 2,
        "overwrites": True,
        "http_headers": dict(_HTTP_HEADERS),
    }
    ffmpeg_dir = Path(settings.ffmpeg_bin).parent
    if len(Path(settings.ffmpeg_bin).parts) > 1 and ffmpeg_dir.exists():
        ydl_opts["ffmpeg_location"] = str(ffmpeg_dir)
    if est_duration > 120:
        # Only fetch the head of long videos; keyframe cuts keep it seekable.
        ydl_opts["download_ranges"] = download_range_func([], [(0.0, float(max_s) * 4)])
        ydl_opts["force_keyframes_at_cuts"] = True

    src_path: Optional[str] = None
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info and info.get("_type") == "playlist":
            entries = info.get("entries") or [{}]
            info = entries[0] or {}
        for req in (info or {}).get("requested_downloads") or []:
            if req.get("filepath"):
                src_path = req["filepath"]
                break
        if not src_path:
            matches = sorted(wd.glob(f"src-{stem}.*"))
            if matches:
                src_path = str(matches[0])
    except DownloadError as exc:
        # Some CDNs 403 yt-dlp's client; for plain video files fall back to a
        # direct streamed download with a browser User-Agent.
        if _url_ext(url) not in VIDEO_EXTS:
            raise RuntimeError(f"yt-dlp could not download {url}: {exc}") from exc
        direct = wd / f"src-{stem}{_url_ext(url)}"
        try:
            with requests.get(
                url, headers=_HTTP_HEADERS, timeout=_IMAGE_TIMEOUT, stream=True
            ) as resp:
                resp.raise_for_status()
                with direct.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
        except requests.RequestException as exc2:
            raise RuntimeError(
                f"could not download {url} (yt-dlp: {exc}; direct: {exc2})"
            ) from exc2
        src_path = str(direct)
    if not src_path or not Path(src_path).exists():
        raise RuntimeError(f"download reported success but no file found for {url}")

    duration = _media_duration(src_path) or est_duration
    out_path = wd / f"clip-{stem}.mp4"

    cmd: list[str] = [settings.ffmpeg_bin, "-y"]
    if 0.0 < duration < float(min_s):
        loops = max(0, math.ceil(float(min_s) / duration) - 1)
        cmd += ["-stream_loop", str(loops), "-i", src_path, "-t", f"{float(min_s):.3f}"]
    else:
        clip_len = min(float(max_s), duration) if duration > 0 else float(max_s)
        clip_len = max(clip_len, float(min_s)) if duration >= float(min_s) else clip_len
        start = 0.0
        if duration > float(max_s):
            # Skip a touch of intro, but never run past the end.
            start = min(duration * 0.08, duration - clip_len)
        cmd += ["-ss", f"{start:.3f}", "-i", src_path, "-t", f"{clip_len:.3f}"]
    cmd += [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    _run_ffmpeg(cmd)
    return str(out_path)


def placeholder_background(workdir: str = "") -> str:
    """Build the deterministic dark 1080x1350 fallback background still.

    Used when NO sourced media exists at all: a subtle top-to-bottom dark
    gradient the STRANGE-COVER overlay + title composite onto, so a cover is
    ALWAYS produced. Pure Pillow - this is a drawn background, not AI-generated
    imagery, so it does not violate the sourced-cover rule.

    Args:
        workdir: Run-specific folder (empty falls back to workdir/adhoc).

    Returns:
        Path of the written PNG as a string.
    """
    out_dir = _ensure_workdir(workdir, "cover")
    out_path = out_dir / "placeholder-bg.png"
    width, height = 1080, 1350
    top, bottom = 30, 6  # near-black gradient, slightly lighter at the top
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        shade = int(top + (bottom - top) * (y / max(height - 1, 1)))
        draw.line([(0, y), (width, y)], fill=(shade, shade, shade))
    img.save(out_path, format="PNG")
    return str(out_path)


def download_image(url: str, workdir: str = "") -> str:
    """Download a still image (the cover fallback source) and validate it.

    Args:
        url: Direct image URL.
        workdir: Run-specific working folder.

    Returns:
        Absolute path (str) of the downloaded image file.

    Raises:
        RuntimeError: On HTTP failure or when the payload is not a readable image.
    """
    wd = _ensure_workdir(workdir, "images")
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=_IMAGE_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"image download failed for {url}: {exc}") from exc

    ext = _url_ext(url)
    if ext not in IMAGE_EXTS:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(ctype.split(";", 1)[0].strip(), ".jpg")
    path = wd / f"img-{uuid.uuid4().hex[:12]}{ext}"
    path.write_bytes(resp.content)
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded file is not a readable image: {url}") from exc
    short_side = min(width, height)
    aspect = max(width / max(height, 1), height / max(width, 1))
    if (
        width * height < _MIN_COVER_IMAGE_PIXELS
        or short_side < _MIN_COVER_IMAGE_SIDE
        or aspect > _MAX_COVER_IMAGE_ASPECT
    ):
        path.unlink(missing_ok=True)
        raise RuntimeError(
            "image is unsuitable for a 1080x1350 cover "
            f"({width}x{height}, aspect {aspect:.2f}); try the next ranked "
            "image_candidates entry"
        )
    return str(path)


# ---------------------------------------------------------------------------
# 3) compose_cover - title rendering + overlay + ffmpeg render
# ---------------------------------------------------------------------------


def _load_title_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the exact shared headline face used by the carousel system."""
    return headline_font(size)


def _line_width(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    text: str,
) -> float:
    """Width of ``text`` measured char-by-char (matches per-char drawing)."""
    return sum(font.getlength(ch) for ch in text)


def _wrap_title(
    title: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_w: float,
) -> list[str]:
    """Wrap a cover hook into the fewest balanced shared-headline lines."""
    if _line_width(font, title) <= max_w:
        return [title]
    words = title.split(" ")
    if len(words) < 2:
        return [title]
    fallback: tuple[float, list[str]] = (float("inf"), [title])
    max_lines = min(_TITLE_MAX_LINES, len(words))
    for line_count in range(2, max_lines + 1):
        best: tuple[float, list[str]] = (float("inf"), [title])
        for cuts in combinations(range(1, len(words)), line_count - 1):
            boundaries = (0, *cuts, len(words))
            lines = [
                " ".join(words[boundaries[i] : boundaries[i + 1]])
                for i in range(line_count)
            ]
            widths = [_line_width(font, line) for line in lines]
            score = max(widths) + (max(widths) - min(widths)) * 0.12
            if score < best[0]:
                best = (score, lines)
        fallback = best
        if all(_line_width(font, line) <= max_w for line in best[1]):
            return best[1]
    return fallback[1]


def _highlight_color() -> tuple[int, int, int, int]:
    """Return the one fixed brand green for every highlight character."""
    return _ACCENT_GREEN


def _render_title_block(title: str, highlight: str) -> Image.Image:
    """Render the cover title onto a transparent 1080x1350 RGBA image.

    Centered in the lower third, up to three lines, 128 px condensed bold
    grotesk typography,
    uppercase, white, with the highlight phrase in a per-character horizontal
    single brand green (#8FB832), with no gradient or shade variation.
    """
    width, height = settings.slide_width, settings.slide_height
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    text = re.sub(r"\s+", " ", str(title or "")).strip().upper()
    if not text:
        return canvas
    hl = re.sub(r"\s+", " ", str(highlight or "")).strip().upper()
    hl_start = text.find(hl) if hl else -1
    hl_end = hl_start + len(hl) if hl_start >= 0 else -1

    max_w = width * _TITLE_MAX_WIDTH_FRAC
    font = _load_title_font(_COVER_TITLE_FONT_SIZE)
    lines = _wrap_title(text, font, max_w)
    if len(lines) > _TITLE_MAX_LINES or any(
        _line_width(font, line) > max_w for line in lines
    ):
        raise ValueError(
            f"cover title does not fit at the fixed {_COVER_TITLE_FONT_SIZE}px size "
            f"within {_TITLE_MAX_LINES} lines"
        )

    draw = ImageDraw.Draw(canvas)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    gap = int(line_h * 0.10)
    total_h = len(lines) * line_h + (len(lines) - 1) * gap
    top = int(height * _TITLE_CENTER_Y_FRAC - total_h / 2)
    top = min(top, height - int(height * 0.06) - total_h)  # keep off the grid floor

    global_idx = 0  # char index into `text` (lines re-join with single spaces)
    for line_no, line in enumerate(lines):
        y = top + line_no * (line_h + gap)
        x = (width - _line_width(font, line)) / 2
        for ch in line:
            if hl_start <= global_idx < hl_end:
                color = _highlight_color()
            else:
                color = _TEXT_PRIMARY
            draw.text((x, y), ch, font=font, fill=color)
            x += font.getlength(ch)
            global_idx += 1
        global_idx += 1  # the space (or line break) between joined lines
    return canvas


_SCRUBBED_TEMPLATE_CACHE: dict[tuple[str, float], Image.Image] = {}


def _scrub_template_text(tpl: Image.Image) -> Image.Image:
    """Remove the template's baked-in example title text, in place.

    The shipped STRANGE-COVER template contains its reference title
    ("STOP PROMPTING YOUR AI, GIVE IT A LOOP") rendered into the dissolve
    zone. Clear the complete reserved title box, interpolating the surrounding
    overlay alpha across each row. Clearing the whole reservation removes the
    original glyphs and their dark anti-aliased shadows; glyph-only masking
    left a visible ghost behind newly rendered headlines. Arrows and the grid
    sit outside this reservation and remain untouched.
    """
    width, height = tpl.size
    x0 = int(width * _TEMPLATE_TEXT_BOX[0])
    y0 = int(height * _TEMPLATE_TEXT_BOX[1])
    x1 = int(width * _TEMPLATE_TEXT_BOX[2])
    y1 = int(height * _TEMPLATE_TEXT_BOX[3])
    px = tpl.load()
    left = max(x0 - 1, 0)
    right = min(x1, width - 1)
    span = max(x1 - x0, 1)
    for y in range(y0, y1):
        left_a = px[left, y][3]
        right_a = px[right, y][3]
        for offset, x in enumerate(range(x0, x1)):
            alpha = round(left_a + (right_a - left_a) * offset / span)
            px[x, y] = (0, 0, 0, alpha)
    return tpl


def _recolor_legacy_accents(tpl: Image.Image) -> Image.Image:
    """Convert the overlay's baked legacy-orange arrows to current lime."""
    px = tpl.load()
    for y in range(tpl.height):
        for x in range(tpl.width):
            r, g, b, a = px[x, y]
            if a and r > 120 and g > 55 and b < 120 and r > g * 1.15:
                px[x, y] = _ACCENT_GREEN[:3] + (a,)
    return tpl


#: Whether the missing-template warning has already been emitted. The overlay
#: is loaded once per cover render, so warning every time would bury the log.
_TEMPLATE_WARNED = False


def _load_scrubbed_template() -> Optional[Image.Image]:
    """Load the overlay template with its example text scrubbed (cached).

    Returns ``None`` when the file is absent, which is a supported state:
    ``_build_overlay_png`` then draws a plain gradient so a missing brand asset
    degrades the cover instead of failing the run. It is still worth saying out
    loud once - an unbranded cover that nobody noticed shipping is worse than a
    run that stopped.
    """
    global _TEMPLATE_WARNED
    path = settings.cover_overlay_template
    if not path.exists():
        if not _TEMPLATE_WARNED:
            _TEMPLATE_WARNED = True
            logging.getLogger(__name__).warning(
                "Cover overlay template not found at %s - covers will be "
                "rendered with a plain gradient instead of the brand overlay. "
                "Set COVER_OVERLAY_TEMPLATE or restore the file to fix.",
                path,
            )
        return None
    key = (str(path), path.stat().st_mtime)
    cached = _SCRUBBED_TEMPLATE_CACHE.get(key)
    if cached is None:
        with Image.open(path) as tpl:
            cached = _recolor_legacy_accents(
                _scrub_template_text(tpl.convert("RGBA"))
            )
        _SCRUBBED_TEMPLATE_CACHE.clear()
        _SCRUBBED_TEMPLATE_CACHE[key] = cached
    return cached


def _build_overlay_png(title: str, highlight: str, wd: Path) -> Path:
    """Composite the overlay template + rendered title into one RGBA PNG.

    The template (2160x2700 = 2x output) is scaled to 1080x1350. If the
    template file is missing, a plain bottom-black gradient stands in so the
    pipeline still produces a reviewable cover.
    """
    width, height = settings.slide_width, settings.slide_height
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    template = _load_scrubbed_template()
    if template is not None:
        canvas.alpha_composite(
            template.resize((width, height), Image.Resampling.LANCZOS)
        )
    else:
        # Emergency stand-in: black rising from the bottom ~40%.
        ramp_top = int(height * 0.55)
        solid_top = int(height * 0.75)
        px = canvas.load()
        for y in range(ramp_top, height):
            if y >= solid_top:
                alpha = 255
            else:
                alpha = int(255 * (y - ramp_top) / max(solid_top - ramp_top, 1))
            for x in range(width):
                px[x, y] = (0, 0, 0, alpha)
    canvas.alpha_composite(_render_title_block(title, highlight))
    out = wd / "overlay-composite.png"
    canvas.save(out)
    return out


def _saliency_weights(image: Image.Image) -> tuple[int, int, list[int]]:
    """Build a lightweight subject map using detail, edges, and skin tones."""
    sample = image.convert("RGB")
    sample.thumbnail(
        (_SALIENCY_MAX_SIZE, _SALIENCY_MAX_SIZE),
        Image.Resampling.LANCZOS,
    )
    gray = sample.convert("L")
    blur_radius = max(2.0, min(sample.size) / 42)
    detail = ImageChops.difference(
        gray,
        gray.filter(ImageFilter.GaussianBlur(radius=blur_radius)),
    )
    edges = gray.filter(ImageFilter.FIND_EDGES)
    ycbcr = sample.convert("YCbCr")

    def pixels(layer: Image.Image) -> Any:
        flattened = getattr(layer, "get_flattened_data", None)
        return flattened() if flattened is not None else layer.getdata()

    weights: list[int] = []
    for detail_px, edge_px, (luma, cb, cr) in zip(
        pixels(detail),
        pixels(edges),
        pixels(ycbcr),
    ):
        # The broad YCbCr range works for photography and illustrated skin.
        # It is only a bonus; product/object detail still drives non-people art.
        skin_bonus = 170 if luma > 35 and 72 <= cb <= 132 and 135 <= cr <= 180 else 0
        weights.append(int(detail_px) * 2 + int(edge_px) + skin_bonus)
    return sample.width, sample.height, weights


def _best_crop_axis_start(
    weights: list[int],
    map_w: int,
    map_h: int,
    window: int,
    *,
    horizontal: bool,
) -> int:
    """Choose the crop start that keeps salient content away from cut edges."""
    length = map_w if horizontal else map_h
    window = min(max(1, window), length)
    if window >= length:
        return 0

    axis = [0] * length
    for y in range(map_h):
        row = y * map_w
        for x in range(map_w):
            axis[x if horizontal else y] += weights[row + x]

    prefix = [0]
    for value in axis:
        prefix.append(prefix[-1] + value)

    max_start = length - window
    preferred_start = max_start * (0.5 if horizontal else 0.34)
    total_weight = max(prefix[-1], 1)
    edge_band = max(1, window // 12)
    best_start = round(preferred_start)
    best_score = float("-inf")
    for start in range(max_start + 1):
        end = start + window
        inside = prefix[end] - prefix[start]
        cut_risk = (
            prefix[start + edge_band]
            - prefix[start]
            + prefix[end]
            - prefix[end - edge_band]
        )
        distance = abs(start - preferred_start) / max(max_start, 1)
        score = inside - cut_risk * 0.72 - total_weight * 0.055 * distance * distance
        if score > best_score:
            best_score = score
            best_start = start
    return best_start


def _smart_crop_box(
    image: Image.Image,
    target_aspect: float,
) -> tuple[int, int, int, int]:
    """Return a fill crop positioned around the image's likely real subject."""
    width, height = image.size
    if width <= 0 or height <= 0:
        return (0, 0, max(width, 1), max(height, 1))
    source_aspect = width / height
    if math.isclose(source_aspect, target_aspect, rel_tol=0.01):
        return (0, 0, width, height)

    map_w, map_h, weights = _saliency_weights(image)
    if source_aspect > target_aspect:
        crop_w = min(width, max(1, round(height * target_aspect)))
        map_crop_w = min(map_w, max(1, round(map_w * crop_w / width)))
        map_left = _best_crop_axis_start(
            weights,
            map_w,
            map_h,
            map_crop_w,
            horizontal=True,
        )
        available_map = max(map_w - map_crop_w, 1)
        left = round((width - crop_w) * map_left / available_map)
        left = min(max(left, 0), width - crop_w)
        return (left, 0, left + crop_w, height)

    crop_h = min(height, max(1, round(width / target_aspect)))
    map_crop_h = min(map_h, max(1, round(map_h * crop_h / height)))
    map_top = _best_crop_axis_start(
        weights,
        map_w,
        map_h,
        map_crop_h,
        horizontal=False,
    )
    available_map = max(map_h - map_crop_h, 1)
    top = round((height - crop_h) * map_top / available_map)
    top = min(max(top, 0), height - crop_h)
    return (0, top, width, top + crop_h)


def _crop_anchor(image: Image.Image, target_aspect: float) -> tuple[float, float]:
    """Convert a smart crop box to FFmpeg's normalized crop offsets."""
    left, top, right, bottom = _smart_crop_box(image, target_aspect)
    x_room = image.width - (right - left)
    y_room = image.height - (bottom - top)
    return (
        left / x_room if x_room > 0 else 0.5,
        top / y_room if y_room > 0 else 0.5,
    )


def _video_crop_anchor(media: Path, wd: Path, duration_s: float) -> tuple[float, float]:
    """Evaluate several clip moments and return a stable subject-aware crop."""
    visual_h = round(settings.slide_height * _COVER_VISUAL_HEIGHT_FRAC)
    target_aspect = settings.slide_width / visual_h
    if duration_s > 0:
        times = [duration_s * fraction for fraction in (0.16, 0.5, 0.82)]
    else:
        times = [0.5]

    anchors: list[tuple[float, float]] = []
    for index, timestamp in enumerate(times):
        frame = wd / f"crop-probe-{index}.png"
        try:
            _run_ffmpeg(
                [
                    settings.ffmpeg_bin,
                    "-y",
                    "-ss",
                    f"{max(timestamp, 0):.3f}",
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    str(frame),
                ],
                timeout_s=_FFPROBE_TIMEOUT_S,
            )
            with Image.open(frame) as image:
                anchors.append(_crop_anchor(image.convert("RGB"), target_aspect))
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            continue
        finally:
            frame.unlink(missing_ok=True)
    if not anchors:
        return (0.5, 0.5)
    return (
        float(median(anchor[0] for anchor in anchors)),
        float(median(anchor[1] for anchor in anchors)),
    )


def _prepare_still_cover(media_path: str, wd: Path) -> Path:
    """Smart-crop a still edge-to-edge across the visible cover stage."""
    target_w, target_h = settings.slide_width * 2, settings.slide_height * 2
    visual_h = round(target_h * _COVER_VISUAL_HEIGHT_FRAC)
    target_aspect = target_w / visual_h
    with Image.open(media_path) as img:
        source = img.convert("RGB")
        cropped = source.crop(_smart_crop_box(source, target_aspect)).resize(
            (target_w, visual_h),
            Image.Resampling.LANCZOS,
        )
        background = Image.new("RGB", (target_w, target_h), (0, 0, 0))
        background.paste(cropped, (0, 0))
        out = wd / "still-base.png"
        background.save(out)
    return out


def compose_cover(
    media_path: str, title: str, highlight: str, is_video: bool, workdir: str = ""
) -> dict:
    """Compose the final 1080x1350 cover video + poster from sourced media.

    The media fills the visible upper cover stage with a subject-aware crop;
    the lower headline area is black rather than a blurred/shrunken duplicate.
    The STRANGE-COVER overlay template plus the Pillow-rendered title block are
    composited on top, and ffmpeg renders a silent H.264 mp4 (``+faststart``)
    with a first-frame poster PNG. Still images become a 6 s restrained
    slow-zoom video (static video fallback if the zoom filter fails).

    Args:
        media_path: Local path of the trimmed clip or downloaded image.
        title: Cover hook title (rendered uppercase in the shared inside-slide
            headline style, up to three balanced lines).
        highlight: Verbatim phrase inside ``title`` rendered in solid #8FB832.
        is_video: True when ``media_path`` is a video clip.
        workdir: Run-specific working folder.

    Returns:
        Dict with ``video_path`` (str), ``poster_path`` (str) and
        ``duration_s`` (float).

    Raises:
        RuntimeError: When ffmpeg rendering fails.
        FileNotFoundError: When ``media_path`` does not exist.
    """
    require_no_em_dash([title, highlight], "cover copy")
    media = Path(media_path)
    if not media.exists():
        raise FileNotFoundError(f"media file not found: {media_path}")
    wd = _ensure_workdir(workdir, "cover")
    width, height = settings.slide_width, settings.slide_height

    overlay = _build_overlay_png(title, highlight, wd)
    out_video = wd / "cover.mp4"
    poster = wd / "cover-poster.png"

    encode_args = [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "19",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ]

    if is_video:
        cap = float(settings.cover_clip_max_s)
        in_duration = _media_duration(media)
        clip_t = f"{min(in_duration, cap):.3f}" if in_duration > 0 else f"{cap:g}"
        visual_h = round(height * _COVER_VISUAL_HEIGHT_FRAC)
        anchor_x, anchor_y = _video_crop_anchor(media, wd, in_duration)
        filter_complex = (
            f"[0:v]scale={width}:{visual_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{visual_h}:(iw-ow)*{anchor_x:.6f}:"
            f"(ih-oh)*{anchor_y:.6f},"
            f"pad={width}:{height}:0:0:black,"
            f"setsar=1,fps={_COVER_FPS}[base];"
            "[base][1:v]overlay=0:0:format=auto[out]"
        )
        _run_ffmpeg(
            [
                settings.ffmpeg_bin,
                "-y",
                "-i",
                str(media),
                "-i",
                str(overlay),
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
                "-t",
                clip_t,
                *encode_args,
                str(out_video),
            ]
        )
    else:
        base_png = _prepare_still_cover(str(media), wd)
        frames = int(_STILL_COVER_SECONDS * _COVER_FPS)
        zoom_filter = (
            "[0:v]zoompan=z='min(1+0.00014*on,1.025)'"
            ":x='iw/2-(iw/zoom/2)':y='0'"
            f":d={frames}:s={width}x{height}:fps={_COVER_FPS}[base];"
            "[base][1:v]overlay=0:0:format=auto[out]"
        )
        zoom_cmd = [
            settings.ffmpeg_bin,
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(_COVER_FPS),
            "-i",
            str(base_png),
            "-i",
            str(overlay),
            "-filter_complex",
            zoom_filter,
            "-map",
            "[out]",
            "-frames:v",
            str(frames),
            *encode_args,
            str(out_video),
        ]
        try:
            _run_ffmpeg(zoom_cmd)
        except (RuntimeError, subprocess.TimeoutExpired):
            static_filter = (
                f"[0:v]scale={width}:{height},setsar=1,fps={_COVER_FPS}[base];"
                "[base][1:v]overlay=0:0:format=auto[out]"
            )
            _run_ffmpeg(
                [
                    settings.ffmpeg_bin,
                    "-y",
                    "-loop",
                    "1",
                    "-framerate",
                    str(_COVER_FPS),
                    "-i",
                    str(base_png),
                    "-i",
                    str(overlay),
                    "-filter_complex",
                    static_filter,
                    "-map",
                    "[out]",
                    "-t",
                    f"{_STILL_COVER_SECONDS:.1f}",
                    *encode_args,
                    str(out_video),
                ]
            )

    _run_ffmpeg(
        [
            settings.ffmpeg_bin,
            "-y",
            "-i",
            str(out_video),
            "-frames:v",
            "1",
            str(poster),
        ],
        timeout_s=_FFPROBE_TIMEOUT_S,
    )
    duration = _media_duration(out_video)
    return {
        "video_path": str(out_video),
        "poster_path": str(poster),
        "duration_s": round(duration, 2),
    }
