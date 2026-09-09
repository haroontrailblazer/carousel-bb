"""Serving the built single-page app, including its deep links.

Two things this handles that a plain ``StaticFiles`` mount does not.

**Deep links.** ``StaticFiles(html=True)`` serves ``index.html`` for a
*directory* request, but 404s on ``/runs/run-1a2b3c`` - a path the SPA router
owns and the filesystem knows nothing about. So a reload or a pasted link into
any screen but the root would break. Unmatched GETs fall back to the shell.

**A missing build.** The backend has to be runnable before anyone has run
``npm run build``, and on a deploy where the frontend build step failed the
useful outcome is a page that says so, not a stack trace or a bare 404 that
looks like a routing bug.

**Caching.** ``StaticFiles`` sends ``ETag`` and ``Last-Modified`` but no
``Cache-Control`` at all, which is not the same as saying "do not cache" - it
leaves the browser to guess, and what it guesses is a revalidation request for
every file on every load. Vite already content-hashes everything under
``/assets/``, so those filenames can never mean two different things and are
safe to keep forever; ``index.html`` is the opposite and must be re-checked
every time or a deploy never reaches anyone. See ``_cache_control``.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

logger = logging.getLogger(__name__)

_PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Carousel Factory</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:#F7F7F5; color:#161811;
         font:16px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif; }
  .card { max-width:34rem; padding:2rem 2.25rem; background:#fff;
          border:1px solid #DEDDD5; border-radius:14px;
          box-shadow:0 8px 24px -12px rgba(22,24,17,.14); }
  h1 { margin:0 0 .5rem; font-size:1.25rem; }
  code { background:#F2F1EC; padding:.15rem .4rem; border-radius:6px;
         font:0.9em ui-monospace,SFMono-Regular,Menlo,monospace; }
  ul { margin:.75rem 0 0; padding-left:1.1rem; }
  li { margin:.35rem 0; }
  .muted { color:#5C6350; font-size:.9rem; margin-top:1rem; }
</style></head>
<body><div class="card">
  <h1>The console UI has not been built yet</h1>
  <p>The API is running, but no frontend bundle was found. Build it with:</p>
  <ul>
    <li><code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></li>
  </ul>
  <p class="muted">Meanwhile these still work:
    <code>/healthz</code>, <code>/api/…</code>, <code>/review-api/…</code>
    and the ADK dev UI at <code>/dev</code>.</p>
</div></body></html>
"""


#: One year. The convention for "as long as you like" - HTTP caps the useful
#: value around here and every CDN and browser understands it as forever.
_IMMUTABLE = "public, max-age=31536000, immutable"

#: Ten minutes for the handful of files whose names do NOT carry a hash (the
#: brand mark, robots.txt). Long enough to stop re-requesting them all session,
#: short enough that replacing one reaches people the same day.
_SHORT = "public, max-age=600"

#: Always ask. The shell names the hashed bundles, so a cached copy of it
#: pins every user to the previous deploy - which is exactly the failure the
#: hashes were meant to make impossible.
_REVALIDATE = "no-cache"


def _cache_control(path: str) -> str:
    r"""How long this file may be reused without asking again.

    The split is by whether the FILENAME identifies the contents. Vite writes
    ``index-DtipvlfQ.js``; change one character of source and the name changes,
    so the old name can never refer to the new file and there is nothing to
    revalidate. ``index.html`` keeps its name across every deploy and is the
    file that names the hashed ones, so it is the single thing that has to be
    checked - and it is 1.5 KB, which makes that check nearly free.

    A path with no extension is the shell too, not just one ending ``.html``.
    A request for ``/`` reaches here as the empty string and is answered with
    ``index.html`` by ``html=True``; anything else without a dot either does
    the same or 404s into the SPA fallback below. Missing that case would have
    given the site's front door a ten-minute cache and pinned every visitor to
    the previous deploy for ten minutes after each release - the one outcome
    this function exists to prevent.

    Decided from the path alone, deliberately. Reading the response's
    content-type would be the more direct question and is not answerable: a
    304 carries almost no headers, and a Not Modified response is exactly the
    moment the browser is being told whether to ask again.

    The separators are normalised first because Starlette hands this an OS
    path, not a URL path - ``StaticFiles.get_path`` runs the URL through
    ``os.path.normpath``, so on Windows ``/assets/index-abc.js`` arrives as
    ``assets\index-abc.js``. ``PurePosixPath`` then reads that as a single
    filename with no directory, every asset silently misses the rule, and the
    bug shows up only on the platform the developer is not deploying to.
    """
    posix = PurePosixPath(path.replace("\\", "/"))
    name = posix.name
    if "." not in name or name.endswith(".html"):
        return _REVALIDATE
    if posix.parts[:1] == ("assets",):
        return _IMMUTABLE
    return _SHORT


def _is_navigation(scope: Scope) -> bool:
    """Is this a browser navigating to a page, rather than code fetching data?

    ``Sec-Fetch-Mode: navigate`` is sent by every current browser on a
    top-level navigation and never on fetch/XHR. The Accept header is the
    fallback for anything that does not send fetch metadata: a navigation asks
    for text/html, while fetch defaults to */*.
    """
    headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
    mode = headers.get(b"sec-fetch-mode", b"").decode("latin-1")
    if mode:
        return mode == "navigate"
    accept = headers.get(b"accept", b"").decode("latin-1")
    return "text/html" in accept


class SPAStaticFiles(StaticFiles):
    """Static files with an SPA history fallback and a missing-build page."""

    def __init__(self, *, directory: Path, **kwargs) -> None:
        self._dir = Path(directory)
        self._available = (self._dir / "index.html").is_file()
        if not self._available:
            logger.warning(
                "No SPA build at %s - serving a placeholder page. Run "
                "`npm run build` in frontend/ (the API itself is unaffected).",
                self._dir,
            )
        # check_dir=False so a missing directory is a placeholder page rather
        # than an exception at import time.
        super().__init__(directory=str(self._dir), check_dir=False, **kwargs)

    async def check_config(self) -> None:
        """Skip Starlette's directory existence check when there is no build.

        ``check_dir=False`` only relaxes the constructor; ``check_config`` still
        stats the directory on the FIRST request and raises, which happens
        before ``get_response`` can serve the placeholder. The result is a 500
        on every page instead of the explanatory page - so the check is skipped
        exactly when there is nothing to check.
        """
        if not self._available:
            return
        await super().check_config()

    async def get_response(self, path: str, scope: Scope) -> Response:
        if not self._available:
            return HTMLResponse(_PLACEHOLDER, status_code=200)

        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            # Starlette RAISES for a missing file rather than returning a 404
            # response, so inspecting response.status_code never fires.
            if exc.status_code != 404:
                raise
            not_found = exc
        else:
            # Set on the 304 as well as the 200: a conditional request that
            # comes back Not Modified is the browser's chance to learn it did
            # not need to ask, and dropping the header there means it asks
            # again next time anyway.
            response.headers["Cache-Control"] = _cache_control(path)
            return response

        # A missing ASSET is a real 404. Answering index.html for a stale
        # script tag hands the browser HTML where it expects JavaScript, and
        # the resulting "Unexpected token '<'" tells the reader nothing about
        # what actually went wrong.
        if "." in PurePosixPath(path).name:
            raise not_found

        # Only a BROWSER NAVIGATION gets the SPA shell. A fetch() or XHR must
        # get a clean 404 instead.
        #
        # Without this, the catch-all answers every unknown path with HTML and
        # a 200 - so any request that misses its API prefix (say
        # /config/telemetry instead of /dev/config/telemetry) receives a web
        # page where it expected JSON. The caller's .json() then throws inside
        # a promise chain, and an app that is merely misrouted looks like an
        # app that has hung, with nothing in the network tab but 200s.
        if not _is_navigation(scope):
            raise not_found

        # Anything else is a client-side route (/runs/run-1a2b3c): the SPA
        # router owns it and the filesystem has never heard of it.
        shell = await super().get_response("index.html", scope)
        shell.headers["Cache-Control"] = _cache_control("index.html")
        return shell


__all__ = ["SPAStaticFiles"]
