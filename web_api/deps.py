"""FastAPI dependencies for the console API.

Authentication happens in ``AuthMiddleware`` (raw ASGI, so it does not break
streaming responses) and leaves the result on the ASGI scope. These
dependencies read it back.

They are dependencies rather than a second middleware on purpose: a
``BaseHTTPMiddleware`` that inspected requests would buffer response bodies and
silently break every SSE stream in the app.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from web_api.auth import Identity


def current_identity(request: Request) -> Identity:
    """The signed-in user for this request.

    Reads ``request.scope`` rather than ``request.state`` because the latter
    depends on the server populating ``scope["state"]``, which is not
    guaranteed for every ASGI server; the middleware writes to the scope
    directly.

    A 401 here means the route was reachable without an identity, i.e. its
    prefix is missing from ``PROTECTED``. That is a wiring bug, so it is worth
    failing loudly rather than treating the request as anonymous.
    """
    identity = request.scope.get("carousel_identity")
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "Sign in to continue."},
        )
    return identity


def require_admin(identity: Identity = Depends(current_identity)) -> Identity:
    """Restrict a route to administrators."""
    if not identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": "This action needs an administrator account.",
            },
        )
    return identity


__all__ = ["current_identity", "require_admin"]
