"""Verify the account identified by an Instagram Login user access token."""

import httpx

from app.config import settings


def verify_token(token: str) -> dict:
    """Discover the Instagram identity without accepting a supplied account ID."""
    if not token:
        raise ValueError("Paste the Instagram account's own access token.")
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=10)) as client:
            response = client.get(
                f"https://graph.instagram.com/{settings.ig_api_version}/me",
                params={"fields": "user_id,username"},
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError("Could not verify the Instagram token. Try again.") from None
    if response.is_error or not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(
            "Instagram rejected this token. Use an Instagram Login user access "
            "token for a Business or Creator account with publishing permission."
        )
    user_id, username = str(payload.get("user_id") or ""), str(payload.get("username") or "")
    if not user_id or not username:
        raise ValueError("The token did not identify an Instagram account.")
    return {"user_id": user_id, "username": username}
