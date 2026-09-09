"""The connected-account store: encrypted at rest, readable synchronously.

Two properties matter more than the rest. The token must never reach the
database in the clear - `app_config` taught that lesson once already and this
table holds a credential that can post as someone's brand. And the store must
be readable from SYNCHRONOUS code, because the publishing tool and the slide
renderer both run inside `asyncio.to_thread` and cannot await a query.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import instagram_accounts as accounts
from app.services import secret_box

KEY = secret_box.generate_key()


def _with_key(key: str = KEY):
    """Swap the whole settings object - it is a frozen dataclass."""
    return patch.object(secret_box, "settings", SimpleNamespace(secrets_key=key))


def _row(
    *,
    id: str = "acc-1",
    ig_user_id: str = "17841400000000000",
    username: str = "baskaranbuilds",
    token: str = "long-lived-token",
    is_default: bool = True,
    disabled: bool = False,
    expires_in_days: int = 60,
    auth_kind: str = "instagram_login",
) -> dict:
    """A stored row as the database hands it back."""
    with _with_key():
        token_enc = secret_box.encrypt(token)
    return {
        "id": id,
        "ig_user_id": ig_user_id,
        "username": username,
        "name": "Baskaran Builds",
        "avatar_key": f"instagram/{id}.png",
        "auth_kind": auth_kind,
        "token_enc": token_enc,
        "token_expires_at": datetime.now(timezone.utc)
        + timedelta(days=expires_in_days),
        "is_default": is_default,
        "disabled": disabled,
        "connected_by": "someone@example.com",
        "connected_at": datetime.now(timezone.utc),
        "last_refreshed_at": None,
    }


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        accounts.reset_cache()
        self._key = _with_key()
        self._key.start()

    def tearDown(self) -> None:
        self._key.stop()
        accounts.reset_cache()

    async def test_load_decrypts_the_token_into_the_cache(self) -> None:
        with patch.object(
            accounts.db, "list_instagram_accounts", AsyncMock(return_value=[_row()])
        ):
            await accounts.load()

        account = accounts.get("acc-1")
        assert account is not None
        self.assertEqual(account.token, "long-lived-token")
        self.assertEqual(account.username, "baskaranbuilds")

    async def test_the_token_is_encrypted_before_it_reaches_the_database(self) -> None:
        written: dict = {}

        async def capture(**kwargs) -> None:
            written.update(kwargs)

        with patch.object(accounts.db, "upsert_instagram_account", capture), patch.object(
            accounts.db, "list_instagram_accounts", AsyncMock(return_value=[])
        ):
            await accounts.save(
                ig_user_id="17841400000000000",
                username="baskaranbuilds",
                name="Baskaran Builds",
                token="plaintext-token",
                expires_in=5183944,
                connected_by="someone@example.com",
            )

        self.assertNotIn("plaintext-token", written["token_enc"])
        self.assertTrue(written["token_enc"].startswith(secret_box.PREFIX))
        self.assertEqual(
            secret_box.decrypt(written["token_enc"]), "plaintext-token"
        )

    async def test_save_refuses_when_there_is_no_secrets_key(self) -> None:
        """Storing a bearer token in the clear is not offered as a fallback."""
        with _with_key(""):
            with self.assertRaises(secret_box.SecretsNotConfigured):
                await accounts.save(
                    ig_user_id="1",
                    username="x",
                    name="",
                    token="plaintext-token",
                    expires_in=100,
                    connected_by="a@b.c",
                )

    async def test_the_first_account_connected_becomes_the_default(self) -> None:
        written: dict = {}

        async def capture(**kwargs) -> None:
            written.update(kwargs)

        with patch.object(accounts.db, "upsert_instagram_account", capture), patch.object(
            accounts.db, "list_instagram_accounts", AsyncMock(return_value=[])
        ):
            await accounts.save(
                ig_user_id="1",
                username="first",
                name="",
                token="tok",
                expires_in=100,
                connected_by="a@b.c",
            )

        self.assertTrue(written["is_default"])

    async def test_a_second_account_does_not_steal_the_default(self) -> None:
        written: dict = {}

        async def capture(**kwargs) -> None:
            written.update(kwargs)

        with patch.object(accounts.db, "upsert_instagram_account", capture), patch.object(
            accounts.db, "list_instagram_accounts", AsyncMock(return_value=[_row()])
        ):
            await accounts.load()
            await accounts.save(
                ig_user_id="2",
                username="second",
                name="",
                token="tok",
                expires_in=100,
                connected_by="a@b.c",
            )

        self.assertFalse(written["is_default"])

    async def test_expiry_is_computed_from_the_seconds_meta_returns(self) -> None:
        written: dict = {}

        async def capture(**kwargs) -> None:
            written.update(kwargs)

        with patch.object(accounts.db, "upsert_instagram_account", capture), patch.object(
            accounts.db, "list_instagram_accounts", AsyncMock(return_value=[])
        ):
            await accounts.save(
                ig_user_id="1",
                username="x",
                name="",
                token="tok",
                expires_in=5183944,  # Meta's 60 days
                connected_by="a@b.c",
            )

        days = (written["token_expires_at"] - datetime.now(timezone.utc)).days
        self.assertGreaterEqual(days, 59)
        self.assertLessEqual(days, 60)


class ReadTests(unittest.IsolatedAsyncioTestCase):
    """The synchronous accessors the renderer and publisher use."""

    def setUp(self) -> None:
        accounts.reset_cache()
        self._key = _with_key()
        self._key.start()

    def tearDown(self) -> None:
        self._key.stop()
        accounts.reset_cache()

    async def _load(self, rows: list[dict]) -> None:
        with patch.object(
            accounts.db, "list_instagram_accounts", AsyncMock(return_value=rows)
        ):
            await accounts.load()

    async def test_default_returns_the_flagged_account(self) -> None:
        await self._load(
            [_row(id="a", is_default=False), _row(id="b", ig_user_id="2", is_default=True)]
        )
        default = accounts.default()
        assert default is not None
        self.assertEqual(default.id, "b")

    async def test_default_is_none_when_nothing_is_connected(self) -> None:
        await self._load([])
        self.assertIsNone(accounts.default())
        self.assertFalse(accounts.configured())

    async def test_a_disabled_account_is_never_the_default(self) -> None:
        await self._load([_row(id="a", is_default=True, disabled=True)])
        self.assertIsNone(accounts.default())

    async def test_an_undecryptable_token_marks_the_account_for_reconnection(
        self,
    ) -> None:
        """A rotated SECRETS_KEY must not break every page that lists accounts."""
        row = _row()
        row["token_enc"] = secret_box.PREFIX + "not-decryptable-under-this-key"
        await self._load([row])

        account = accounts.get("acc-1")
        assert account is not None
        self.assertEqual(account.token, "")
        self.assertTrue(account.needs_reconnect)
        self.assertFalse(accounts.configured())

    async def test_an_expired_token_needs_reconnection(self) -> None:
        await self._load([_row(expires_in_days=-1)])
        account = accounts.get("acc-1")
        assert account is not None
        self.assertTrue(account.needs_reconnect)

    async def test_the_graph_host_follows_the_auth_kind(self) -> None:
        await self._load(
            [
                _row(id="new", auth_kind="instagram_login"),
                _row(id="old", ig_user_id="2", auth_kind="facebook_login"),
            ]
        )
        self.assertEqual(
            accounts.get("new").graph_host, "https://graph.instagram.com"
        )
        self.assertEqual(
            accounts.get("old").graph_host, "https://graph.facebook.com"
        )

    async def test_listing_never_exposes_the_token(self) -> None:
        """What the console renders must be safe to put on a screen."""
        await self._load([_row()])
        listed = accounts.listing()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertNotIn("token_enc", listed[0])
        self.assertEqual(listed[0]["username"], "baskaranbuilds")
        self.assertIn("expires_in_days", listed[0])

    async def test_the_cache_survives_a_thread_hop(self) -> None:
        """The publisher reads this from inside asyncio.to_thread."""
        await self._load([_row()])
        token = await asyncio.to_thread(lambda: accounts.get("acc-1").token)
        self.assertEqual(token, "long-lived-token")

    async def test_delete_removes_the_account_from_the_cache(self) -> None:
        await self._load([_row()])
        with patch.object(
            accounts.db, "delete_instagram_account", AsyncMock(return_value=None)
        ):
            await accounts.delete("acc-1")
        self.assertIsNone(accounts.get("acc-1"))


if __name__ == "__main__":
    unittest.main()
