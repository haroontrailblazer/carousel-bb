"""Who gets past the front door, and how failures are shaped.

The rules encoded here are the ones that would be expensive to get wrong:

* ``/review-api`` must stay reachable with no credentials, because those links
  are opened from a Telegram message where nobody can log in. Protecting it by
  accident would silently strand every paused run.
* The SPA bundle must load unauthenticated, or the browser can never render the
  login screen that would obtain a credential.
* ``/api`` must NOT be reachable without one: it can start runs that spend
  real image and reasoning credits.
* An unauthenticated websocket has to be refused with a close frame; returning
  an HTTP response into a websocket scope raises inside the server.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_api import auth as auth_mod
from web_api.auth import (
    COOKIE_NAME,
    AuthError,
    AuthMiddleware,
    Identity,
    SupabaseJWTVerifier,
    issue_session_token,
    read_session_token,
    validate_session_secret,
)

SECRET = "t" * 48
ALLOWED = Identity(email="a@b.co", subject="a@b.co", role="reviewer")


def _app(secret: str = SECRET) -> TestClient:
    """A stand-in app with one route per protection class."""
    inner = FastAPI()

    @inner.get("/healthz")
    async def healthz():
        return {"ok": True}

    @inner.get("/review-api/review/{run_id}/approve")
    async def review(run_id: str):
        return {"run_id": run_id}

    @inner.get("/api/runs")
    async def runs():
        return {"runs": []}

    @inner.get("/api/queue")
    async def queue():
        return {"items": []}

    @inner.get("/api/meta")
    async def meta():
        return {"agents": []}

    @inner.get("/")
    async def spa():
        return {"spa": True}

    @inner.get("/runs/abc")
    async def deep_link():
        return {"spa": True}

    class _Verifier(SupabaseJWTVerifier):
        def verify(self, token):
            if token == "good":
                return {"email": ALLOWED.email, "subject": "sub", "claims": {}}
            raise AuthError("token_invalid", "no")

    app = AuthMiddleware(
        inner,
        verifier=_Verifier(supabase_url="https://x.supabase.co"),
        secret=secret,
        secure_cookies=False,
    )
    return TestClient(app)


def _cookie(role: str = "reviewer", ttl: int = 3600, secret: str = SECRET) -> dict:
    token = issue_session_token(
        Identity(email=ALLOWED.email, subject="s", role=role),
        ttl_s=ttl,
        secret=secret,
    )
    return {COOKIE_NAME: token}


class OpenPathTests(unittest.TestCase):
    def test_there_is_no_anonymous_approval_surface_left(self) -> None:
        """The standalone Approve/Reject pages are gone, not merely closed.

        They were open on the reasoning that a Telegram link opens where
        nobody can sign in, and an unguessable run_id was capability enough.
        But approving auto-publishes to Instagram, so any leaked URL - a
        forwarded message, a screenshot, a chat backup - was a permanent
        anonymous publish button. Telegram links to /tasks/{id}?tab=review
        now, which is behind the login like everything else.

        What matters is that nothing there can ACT. A GET may still be answered
        by whatever catch-all sits at the root - that is a static page with no
        handler behind it - but the submit endpoint that actually recorded a
        verdict must be gone.
        """
        post = _app().post(
            "/review-api/review/run-1/submit", data={"status": "approved"}
        )
        self.assertEqual(post.status_code, 404)

    def test_the_app_registers_no_review_routes_at_all(self) -> None:
        """Stronger than probing URLs: the routes do not exist to be reached."""
        import web_app

        inner = getattr(web_app.app, "app", web_app.app)
        paths = [getattr(route, "path", "") for route in inner.routes]
        self.assertEqual([p for p in paths if "review" in p], [])

    def test_the_verdict_api_is_the_only_way_in_and_it_needs_a_session(self) -> None:
        r = _app().post("/api/runs/run-1/verdict", json={"status": "approved"})
        self.assertEqual(r.status_code, 401)

    def test_the_health_probe_is_open(self) -> None:
        """A 401 here would make the platform restart-loop the service."""
        self.assertEqual(_app().get("/healthz").status_code, 200)

    def test_the_spa_shell_loads_signed_out(self) -> None:
        """Otherwise there is no way to reach a login screen."""
        self.assertEqual(_app().get("/").status_code, 200)
        self.assertEqual(_app().get("/runs/abc").status_code, 200)


class ProtectedPathTests(unittest.TestCase):
    def test_the_api_is_closed_when_signed_out(self) -> None:
        r = _app().get("/api/runs")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthenticated")

    def test_every_api_route_is_closed_when_signed_out(self) -> None:
        """The API can start runs that spend real money - never leave it open."""
        client = _app()
        for path in ("/api/runs", "/api/queue", "/api/meta"):
            self.assertEqual(
                client.get(path, headers={"Accept": "application/json"}).status_code,
                401,
                f"{path} answered without a session",
            )

    def test_a_valid_cookie_opens_the_api(self) -> None:
        client = _app()
        self.assertEqual(client.get("/api/runs", cookies=_cookie()).status_code, 200)

    def test_an_expired_cookie_is_refused(self) -> None:
        stale = issue_session_token(ALLOWED, ttl_s=-10, secret=SECRET)
        r = _app().get("/api/runs", cookies={COOKIE_NAME: stale})
        self.assertEqual(r.status_code, 401)

    def test_a_cookie_signed_with_another_secret_is_refused(self) -> None:
        """The forgery case - this is the whole point of signing it."""
        forged = _cookie(secret="w" * 48)
        self.assertEqual(_app().get("/api/runs", cookies=forged).status_code, 401)

    def test_a_valid_bearer_token_is_accepted_for_scripts(self) -> None:
        async def ok(email):
            return ALLOWED

        with patch.object(auth_mod, "authorize_email", ok):
            r = _app().get("/api/runs", headers={"Authorization": "Bearer good"})
        self.assertEqual(r.status_code, 200)

    def test_a_rejected_bearer_token_does_not_pass(self) -> None:
        r = _app().get("/api/runs", headers={"Authorization": "Bearer bad"})
        self.assertEqual(r.status_code, 401)


class FailureShapeTests(unittest.TestCase):
    def test_a_browser_navigation_is_redirected_to_login(self) -> None:
        """Sending raw JSON to someone who clicked a bookmark is a dead end."""
        r = _app().get(
            "/api/runs",
            headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["location"])
        self.assertIn("next=", r.headers["location"])

    def test_an_xhr_gets_json_not_a_redirect(self) -> None:
        """A redirect would turn an auth failure into a confusing 200 of HTML."""
        r = _app().get(
            "/api/runs",
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 401)


class SessionTokenTests(unittest.TestCase):
    def test_round_trip_preserves_email_and_role(self) -> None:
        token = issue_session_token(
            Identity(email="x@y.z", subject="s", role="admin"),
            ttl_s=60,
            secret=SECRET,
        )
        back = read_session_token(token, secret=SECRET)
        self.assertEqual(back.email, "x@y.z")
        self.assertTrue(back.is_admin)

    def test_an_unsigned_alg_none_token_is_refused(self) -> None:
        """The classic JWT bypass: claim alg=none and sign nothing."""
        forged = jwt.encode(
            {"email": "attacker@evil.co", "role": "admin", "exp": int(time.time()) + 60},
            key="",
            algorithm="none",
        )
        self.assertIsNone(read_session_token(forged, secret=SECRET))

    def test_a_token_without_an_email_is_refused(self) -> None:
        token = jwt.encode(
            {"sub": "s", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256"
        )
        self.assertIsNone(read_session_token(token, secret=SECRET))


class SecretValidationTests(unittest.TestCase):
    def test_a_missing_secret_is_reported(self) -> None:
        self.assertTrue(validate_session_secret(""))

    def test_a_short_secret_is_reported(self) -> None:
        """Anyone who guesses it can mint a session for any allowed user."""
        self.assertTrue(validate_session_secret("short"))

    def test_a_strong_secret_passes(self) -> None:
        self.assertEqual(validate_session_secret("z" * 48), [])


class AllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unknown_email_is_refused(self) -> None:
        async def none(_email):
            return None

        with patch.object(auth_mod.db, "get_app_user", none):
            with self.assertRaises(AuthError) as ctx:
                await auth_mod.authorize_email("stranger@x.co")
        self.assertEqual(ctx.exception.code, "not_allowed")

    async def test_a_disabled_user_is_told_access_was_revoked(self) -> None:
        """They know they had an account; 'no such user' just confuses them."""

        async def disabled(_email):
            return {"email": "a@b.co", "role": "reviewer", "disabled": True}

        with patch.object(auth_mod.db, "get_app_user", disabled):
            with self.assertRaises(AuthError) as ctx:
                await auth_mod.authorize_email("a@b.co")
        self.assertEqual(ctx.exception.code, "access_revoked")

    async def test_the_role_comes_from_the_allowlist_not_the_token(self) -> None:
        """A Supabase token must never be able to claim its own role."""

        async def admin(_email):
            return {"email": "a@b.co", "role": "admin", "disabled": False}

        with patch.object(auth_mod.db, "get_app_user", admin):
            identity = await auth_mod.authorize_email("A@B.CO")
        self.assertEqual(identity.email, "a@b.co", "email should be normalised")
        self.assertTrue(identity.is_admin)

    async def test_an_unreachable_allowlist_fails_closed(self) -> None:
        """If we cannot check permission, we do not grant it."""

        async def boom(_email):
            raise ConnectionError("db down")

        with patch.object(auth_mod.db, "get_app_user", boom):
            with self.assertRaises(AuthError) as ctx:
                await auth_mod.authorize_email("a@b.co")
        self.assertEqual(ctx.exception.code, "allowlist_unavailable")


if __name__ == "__main__":
    unittest.main()
