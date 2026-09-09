"""Sign-in, sign-out, and telling the SPA how to reach Supabase.

The exchange is deliberately small. The browser does the Supabase sign-in
itself with supabase-js; all this does is take the resulting access token,
verify it, check the allowlist, and hand back our own session cookie. Passwords
never touch this service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from app.config import settings
from web_api.auth import (
    AuthError,
    Identity,
    authorize_email,
    build_verifier,
    clear_cookie_headers,
    issue_session_token,
    session_cookie_headers,
)
from web_api.deps import current_identity

logger = logging.getLogger(__name__)

router = APIRouter()


class SessionRequest(BaseModel):
    access_token: str = Field(..., min_length=10)


def _secure_cookies(request: Request) -> bool:
    """Should the session cookie carry ``Secure``?

    Yes in production, but not on plain-HTTP localhost - a Secure cookie is
    never stored there, so development would appear to sign in and then fail
    every subsequent request with no visible reason. The forwarded proto is
    checked first because the TLS terminator sits in front of this process.
    """
    proto = request.headers.get("x-forwarded-proto", "") or request.url.scheme
    if proto == "https":
        return True
    host = (request.url.hostname or "").lower()
    return host not in ("localhost", "127.0.0.1", "::1")


@router.get("/config")
async def auth_config() -> dict:
    """Public configuration the SPA needs to talk to Supabase.

    The anon key belongs in a browser bundle by design - it identifies the
    project, it does not authorise anything on its own, and the database is
    locked down against it (see db/migrations/003_lockdown.sql). The service
    key is never returned here.
    """
    return {
        "supabase_url": settings.supabase_url,
        "supabase_anon_key": settings.supabase_anon_key,
        "configured": bool(settings.supabase_url and settings.supabase_anon_key),
    }


@router.post("/session")
async def create_session(payload: SessionRequest, request: Request) -> Response:
    """Exchange a verified Supabase token for our session cookie."""
    verifier = build_verifier()
    try:
        verified = verifier.verify(payload.access_token)
        identity = await authorize_email(verified["email"])
    except AuthError as exc:
        # 403 when we know who they are but will not let them in; 401 when the
        # credential itself is the problem. The SPA shows a different message
        # for each, so the distinction has to survive to the client.
        code = (
            status.HTTP_403_FORBIDDEN
            if exc.code in ("not_allowed", "access_revoked")
            else status.HTTP_401_UNAUTHORIZED
        )
        logger.info("Sign-in refused (%s).", exc.code)
        return _json(
            {"error": exc.detail, "code": exc.code}, status_code=code
        )

    if not settings.session_secret:
        logger.error("SESSION_SECRET is not set; refusing to issue a session.")
        return _json(
            {
                "error": "This console is not configured for sign-in yet "
                         "(SESSION_SECRET is unset).",
                "code": "auth_not_configured",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    token = issue_session_token(
        identity, ttl_s=settings.session_ttl_s, secret=settings.session_secret
    )
    response = _json(
        {
            "email": identity.email,
            "role": identity.role,
            "expires_in": settings.session_ttl_s,
        }
    )
    for key, value in session_cookie_headers(
        token, ttl_s=settings.session_ttl_s, secure=_secure_cookies(request)
    ):
        response.raw_headers.append((key, value))
    logger.info("Signed in: %s (%s).", identity.email, identity.role)
    return response


@router.delete("/session")
async def destroy_session(request: Request) -> Response:
    """Sign out by clearing the cookie.

    Unauthenticated on purpose: signing out must work even when the session has
    already expired, which is exactly when a user reaches for it.
    """
    response = _json({"signed_out": True})
    for key, value in clear_cookie_headers(secure=_secure_cookies(request)):
        response.raw_headers.append((key, value))
    return response


@router.get("/me")
async def whoami(identity: Identity = Depends(current_identity)) -> dict:
    """Who the current session belongs to."""
    return {
        "email": identity.email,
        "role": identity.role,
        "is_admin": identity.is_admin,
        "source": identity.source,
    }


def _json(payload: dict, status_code: int = 200) -> Response:
    """A JSON response we can append raw Set-Cookie headers to."""
    import json

    return Response(
        content=json.dumps(payload),
        status_code=status_code,
        media_type="application/json",
    )


__all__ = ["router"]
