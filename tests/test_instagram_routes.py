"""Connecting an account from the console.

The callback is the interesting one. It is a plain GET that arrives from
Instagram's servers by way of the user's browser, so the only thing proving
this console started the flow is the signed ``state`` it issued ten minutes
earlier. Everything here is about what happens when that proof is absent,
stale or forged - and about the console never showing a raw token.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.services import instagram_oauth
from app.services.instagram_accounts import Account
from web_api import routes_settings
from web_api.auth import Identity

IDENTITY = Identity(email="someone@example.com", subject="someone@example.com")
SECRET = "session-secret-long-enough-for-hs256-padding-aaaa"


def _account(account_id: str = "acc-1", username: str = "acme") -> Account:
    return Account(
        id=account_id,
        ig_user_id="1784140000",
        username=username,
        name="Acme",
        avatar_key="",
        auth_kind="instagram_login",
        token="a-real-secret-token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_default=True,
        disabled=False,
        connected_by="someone@example.com",
        connected_at=datetime.now(timezone.utc),
        last_refreshed_at=None,
    )


def _settings(**overrides):
    base = {
        "ig_app_id": "app-123",
        "ig_app_secret": "app-secret",
        "public_base_url": "https://console.example.com",
        "session_secret": SECRET,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_status_never_carries_a_token(self) -> None:
        with patch.object(
            routes_settings.instagram_accounts, "listing", lambda: [_account().public()]
        ), patch.object(routes_settings, "settings", _settings()):
            status = await routes_settings.instagram_status(_identity=IDENTITY)

        rendered = repr(status)
        self.assertNotIn("a-real-secret-token", rendered)
        self.assertEqual(status["accounts"][0]["username"], "acme")
        self.assertTrue(status["app_configured"])

    async def test_status_says_when_the_meta_app_is_not_configured(self) -> None:
        with patch.object(routes_settings.instagram_accounts, "listing", lambda: []), \
             patch.object(routes_settings, "settings", _settings(ig_app_id="")):
            status = await routes_settings.instagram_status(_identity=IDENTITY)
        self.assertFalse(status["app_configured"])


class AuthorizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorize_redirects_to_instagram_with_a_signed_state(self) -> None:
        with patch.object(routes_settings, "settings", _settings()):
            response = await routes_settings.instagram_authorize(identity=IDENTITY)

        self.assertEqual(response.status_code, 307)
        location = response.headers["location"]
        self.assertTrue(location.startswith("https://www.instagram.com/oauth/authorize"))

        state = location.split("state=")[1].split("&")[0]
        self.assertEqual(
            instagram_oauth.read_state(state, secret=SECRET), "someone@example.com"
        )

    async def test_authorize_refuses_without_meta_credentials(self) -> None:
        with patch.object(routes_settings, "settings", _settings(ig_app_id="")):
            with self.assertRaises(HTTPException) as caught:
                await routes_settings.instagram_authorize(identity=IDENTITY)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "not_configured")

    async def test_authorize_refuses_without_a_public_base_url(self) -> None:
        """Meta rejects a redirect_uri that is not absolute and allowlisted."""
        with patch.object(routes_settings, "settings", _settings(public_base_url="")):
            with self.assertRaises(HTTPException) as caught:
                await routes_settings.instagram_authorize(identity=IDENTITY)
        self.assertEqual(caught.exception.detail["code"], "no_public_url")


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def _callback(self, **kwargs):
        defaults = {
            "code": "the-code",
            "state": instagram_oauth.issue_state(
                "someone@example.com", secret=SECRET
            ),
            "error": "",
            "error_description": "",
        }
        defaults.update(kwargs)
        return await routes_settings.instagram_callback(**defaults)

    async def test_a_forged_state_is_refused_and_nothing_is_saved(self) -> None:
        saved = AsyncMock()
        with patch.object(routes_settings, "settings", _settings()), patch.object(
            routes_settings.instagram_accounts, "save", saved
        ):
            response = await self._callback(state="not-a-real-state")

        self.assertIn("instagram_error=bad_state", response.headers["location"])
        saved.assert_not_awaited()

    async def test_a_successful_connection_saves_and_returns_to_the_profile(
        self,
    ) -> None:
        saved = AsyncMock(return_value=_account())

        with patch.object(routes_settings, "settings", _settings()), patch.object(
            routes_settings.instagram_accounts, "save", saved
        ), patch.object(
            routes_settings.instagram_oauth,
            "exchange_code",
            lambda **kw: {"access_token": "short"},
        ), patch.object(
            routes_settings.instagram_oauth,
            "exchange_long_lived",
            lambda *a: ("long-token", 5183944),
        ), patch.object(
            routes_settings.instagram_oauth,
            "fetch_identity",
            lambda tok: {
                "ig_user_id": "1784140000",
                "username": "acme",
                "name": "Acme",
                "profile_picture_url": "https://cdn/pic.jpg",
            },
        ), patch.object(
            routes_settings.instagram_oauth, "fetch_avatar", lambda url: b"pic"
        ), patch.object(
            routes_settings, "_store_avatar", AsyncMock(return_value="instagram/x.png")
        ):
            response = await self._callback()

        self.assertIn("instagram=connected", response.headers["location"])
        kwargs = saved.await_args.kwargs
        self.assertEqual(kwargs["token"], "long-token")
        self.assertEqual(kwargs["username"], "acme")
        self.assertEqual(kwargs["connected_by"], "someone@example.com")

    async def test_the_user_declining_on_instagram_is_not_an_error_page(self) -> None:
        with patch.object(routes_settings, "settings", _settings()):
            response = await self._callback(
                error="access_denied", error_description="User denied"
            )
        self.assertIn("instagram_error=access_denied", response.headers["location"])

    async def test_a_missing_secrets_key_refuses_rather_than_storing_plaintext(
        self,
    ) -> None:
        from app.services import secret_box

        with patch.object(routes_settings, "settings", _settings()), patch.object(
            routes_settings.instagram_accounts,
            "save",
            AsyncMock(side_effect=secret_box.SecretsNotConfigured("no key")),
        ), patch.object(
            routes_settings.instagram_oauth,
            "exchange_code",
            lambda **kw: {"access_token": "short"},
        ), patch.object(
            routes_settings.instagram_oauth,
            "exchange_long_lived",
            lambda *a: ("long-token", 100),
        ), patch.object(
            routes_settings.instagram_oauth,
            "fetch_identity",
            lambda tok: {
                "ig_user_id": "1",
                "username": "acme",
                "name": "",
                "profile_picture_url": "",
            },
        ):
            response = await self._callback()

        self.assertIn("instagram_error=secrets_unconfigured", response.headers["location"])


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_setting_a_default_moves_the_flag(self) -> None:
        set_default = AsyncMock()
        with patch.object(
            routes_settings.instagram_accounts, "set_default", set_default
        ), patch.object(
            routes_settings.instagram_accounts, "listing", lambda: []
        ), patch.object(routes_settings, "settings", _settings()):
            await routes_settings.instagram_set_default(
                routes_settings.AccountRef(account_id="acc-2"), identity=IDENTITY
            )
        set_default.assert_awaited_once_with("acc-2")

    async def test_disconnecting_forgets_the_account(self) -> None:
        delete = AsyncMock()
        with patch.object(routes_settings.instagram_accounts, "delete", delete), \
             patch.object(routes_settings.instagram_accounts, "listing", lambda: []), \
             patch.object(routes_settings, "settings", _settings()):
            await routes_settings.instagram_disconnect("acc-3", identity=IDENTITY)
        delete.assert_awaited_once_with("acc-3")


if __name__ == "__main__":
    unittest.main()
