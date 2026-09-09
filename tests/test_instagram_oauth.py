"""The Instagram Login OAuth exchange, with no live Meta calls anywhere.

The `state` parameter is the only CSRF cover this flow has: the callback is a
plain GET that anyone can hit, and without a signed state a stranger could
walk their own `code` through our callback and attach THEIR Instagram account
to this console. So the tests below care as much about what state REJECTS as
about what it accepts.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.services import instagram_oauth as oauth

SECRET = "test-session-secret-long-enough-for-hs256-aaaaaaaa"


class StateTokenTests(unittest.TestCase):
    """Sign, verify, and refuse."""

    def test_round_trip_returns_the_connecting_user(self) -> None:
        token = oauth.issue_state("someone@example.com", secret=SECRET)
        self.assertEqual(
            oauth.read_state(token, secret=SECRET), "someone@example.com"
        )

    def test_tampered_token_is_refused(self) -> None:
        token = oauth.issue_state("someone@example.com", secret=SECRET)
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.read_state(token + "x", secret=SECRET)
        self.assertEqual(caught.exception.code, "bad_state")

    def test_token_signed_with_another_secret_is_refused(self) -> None:
        token = oauth.issue_state("someone@example.com", secret=SECRET)
        with self.assertRaises(oauth.OAuthError):
            oauth.read_state(token, secret=SECRET + "different")

    def test_expired_token_is_refused(self) -> None:
        past = time.time() - (oauth.STATE_TTL_S + 60)
        with patch.object(oauth.time, "time", return_value=past):
            token = oauth.issue_state("someone@example.com", secret=SECRET)
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.read_state(token, secret=SECRET)
        self.assertEqual(caught.exception.code, "state_expired")

    def test_two_tokens_for_the_same_user_differ(self) -> None:
        """A nonce, so a state cannot be replayed from a shoulder-surfed URL."""
        first = oauth.issue_state("someone@example.com", secret=SECRET)
        second = oauth.issue_state("someone@example.com", secret=SECRET)
        self.assertNotEqual(first, second)

    def test_empty_token_is_refused(self) -> None:
        with self.assertRaises(oauth.OAuthError):
            oauth.read_state("", secret=SECRET)


class AuthorizeUrlTests(unittest.TestCase):
    """The URL the browser is sent to."""

    def test_carries_client_id_redirect_scope_and_state(self) -> None:
        url = oauth.authorize_url(
            client_id="app-123",
            redirect_uri="https://console.example.com/api/settings/instagram/callback",
            state="signed-state",
        )
        self.assertTrue(url.startswith("https://www.instagram.com/oauth/authorize?"))
        self.assertIn("client_id=app-123", url)
        self.assertIn("response_type=code", url)
        self.assertIn("state=signed-state", url)
        self.assertIn("instagram_business_basic", url)
        self.assertIn("instagram_business_content_publish", url)
        # The redirect must be percent-encoded or Meta reads it as truncated.
        self.assertIn(
            "redirect_uri=https%3A%2F%2Fconsole.example.com%2Fapi%2Fsettings"
            "%2Finstagram%2Fcallback",
            url,
        )


class ExchangeTests(unittest.TestCase):
    """Code -> short-lived -> long-lived, and the identity lookup."""

    def test_exchange_code_returns_the_short_lived_token(self) -> None:
        calls: list[dict] = []

        def fake_post(url: str, data: dict, timeout) -> dict:
            calls.append({"url": url, "data": data})
            return {"access_token": "short-tok", "user_id": "17841400000000000"}

        with patch.object(oauth, "_post_form", fake_post):
            result = oauth.exchange_code(
                code="the-code",
                client_id="app-123",
                client_secret="sh",
                redirect_uri="https://c.example.com/cb",
            )

        self.assertEqual(result["access_token"], "short-tok")
        self.assertEqual(calls[0]["data"]["grant_type"], "authorization_code")
        self.assertEqual(calls[0]["data"]["code"], "the-code")

    def test_exchange_code_surfaces_metas_error_message(self) -> None:
        def fake_post(url: str, data: dict, timeout) -> dict:
            return {
                "error_type": "OAuthException",
                "code": 400,
                "error_message": "Invalid platform app",
            }

        with patch.object(oauth, "_post_form", fake_post):
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth.exchange_code(
                    code="c", client_id="a", client_secret="s", redirect_uri="r"
                )
        self.assertIn("Invalid platform app", str(caught.exception))

    def test_long_lived_exchange_returns_token_and_expiry(self) -> None:
        def fake_get(url: str, params: dict, timeout) -> dict:
            assert params["grant_type"] == "ig_exchange_token"
            return {"access_token": "long-tok", "expires_in": 5183944}

        with patch.object(oauth, "_get_json", fake_get):
            token, expires_in = oauth.exchange_long_lived("short-tok", "app-secret")

        self.assertEqual(token, "long-tok")
        self.assertEqual(expires_in, 5183944)

    def test_refresh_returns_a_fresh_token(self) -> None:
        def fake_get(url: str, params: dict, timeout) -> dict:
            assert params["grant_type"] == "ig_refresh_token"
            assert params["access_token"] == "old-tok"
            return {"access_token": "new-tok", "expires_in": 5183944}

        with patch.object(oauth, "_get_json", fake_get):
            token, expires_in = oauth.refresh_long_lived("old-tok")

        self.assertEqual(token, "new-tok")
        self.assertEqual(expires_in, 5183944)

    def test_fetch_identity_returns_the_account_fields(self) -> None:
        def fake_get(url: str, params: dict, timeout) -> dict:
            return {
                "user_id": "17841400000000000",
                "username": "baskaranbuilds",
                "name": "Baskaran Builds",
                "profile_picture_url": "https://cdn.example.com/pic.jpg",
            }

        with patch.object(oauth, "_get_json", fake_get):
            identity = oauth.fetch_identity("long-tok")

        self.assertEqual(identity["ig_user_id"], "17841400000000000")
        self.assertEqual(identity["username"], "baskaranbuilds")
        self.assertEqual(identity["name"], "Baskaran Builds")
        self.assertEqual(
            identity["profile_picture_url"], "https://cdn.example.com/pic.jpg"
        )

    def test_fetch_identity_without_a_user_id_is_an_error(self) -> None:
        """A 200 with no id means the token is not for a professional account."""
        with patch.object(oauth, "_get_json", lambda url, params, timeout: {}):
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth.fetch_identity("long-tok")
        self.assertEqual(caught.exception.code, "no_identity")


if __name__ == "__main__":
    unittest.main()
