"""A run publishes to the account it was started against.

The publisher used to need no routing at all: there was one account, in the
environment, and every run went there. Now the target is part of the run, and
two things have to line up or a carousel lands on the wrong brand - the
account the SLIDES were stamped for, and the account the POST goes to.

Both come from the same place: the account id recorded on the run.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents import publisher as publisher_mod
from app.services.instagram_accounts import Account
from app.state import K_ACCOUNT_ID

RUN_ID = "run-acct-1"


def _account(account_id: str = "acc-1", username: str = "acme") -> Account:
    return Account(
        id=account_id,
        ig_user_id="17841400000000000",
        username=username,
        name="Acme",
        avatar_key="",
        auth_kind="instagram_login",
        token="tok",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_default=True,
        disabled=False,
        connected_by="",
        connected_at=None,
        last_refreshed_at=None,
    )


def _bundle(artifacts: list[str]) -> dict:
    """A Bundle the schema will actually accept."""
    return {
        "cover": {"video_artifact": artifacts[0], "title": "t"},
        "slides": [],
        "cta": {"cta_type": "follow", "artifact": "cta.png"},
        "caption": "hi",
        "ordered_artifacts": artifacts,
    }


class ResolveTests(unittest.TestCase):
    """Which account a run publishes to."""

    def test_the_account_named_on_the_run_is_used(self) -> None:
        with patch.object(
            publisher_mod.instagram_accounts, "get", lambda i: _account(i)
        ):
            account = publisher_mod._account_for_run({K_ACCOUNT_ID: "acc-7"})
        self.assertEqual(account.id, "acc-7")

    def test_a_run_with_no_account_is_refused_rather_than_defaulted(self) -> None:
        """Falling back to the default would post to the wrong brand."""
        with self.assertRaises(publisher_mod.NoPublishAccount):
            publisher_mod._account_for_run({})

    def test_an_account_that_has_since_been_disconnected_is_refused(self) -> None:
        with patch.object(publisher_mod.instagram_accounts, "get", lambda i: None):
            with self.assertRaises(publisher_mod.NoPublishAccount) as caught:
                publisher_mod._account_for_run({K_ACCOUNT_ID: "gone"})
        self.assertIn("gone", str(caught.exception))

    def test_an_account_needing_reconnection_is_refused(self) -> None:
        lapsed = Account(
            id="acc-1",
            ig_user_id="1",
            username="acme",
            name="",
            avatar_key="",
            auth_kind="instagram_login",
            token="",  # unreadable under the current SECRETS_KEY
            token_expires_at=None,
            is_default=True,
            disabled=False,
            connected_by="",
            connected_at=None,
            last_refreshed_at=None,
        )
        with patch.object(publisher_mod.instagram_accounts, "get", lambda i: lapsed):
            with self.assertRaises(publisher_mod.NoPublishAccount):
                publisher_mod._account_for_run({K_ACCOUNT_ID: "acc-1"})


class PublishToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_tool_hands_the_run_s_account_to_the_publisher(self) -> None:
        seen: dict = {}

        def fake_publish(bundle, urls, should_continue=None, *, account=None):
            seen["account"] = account
            return {"media_id": "m-1", "permalink": "https://instagram.com/p/x"}

        state = {
            "run_id": RUN_ID,
            K_ACCOUNT_ID: "acc-9",
            "bundle": _bundle(["cover.mp4", "s1.png", "s2.png"]),
        }
        tool_context = SimpleNamespace(
            state=state,
            session=SimpleNamespace(app_name="app", user_id="u", id=RUN_ID),
            _invocation_context=None,
        )

        service = SimpleNamespace(
            public_url_async=AsyncMock(side_effect=lambda **kw: f"https://x/{kw['filename']}")
        )

        with patch.object(
            publisher_mod, "_resolve_artifact_service", lambda ctx: service
        ), patch.object(
            publisher_mod.instagram_accounts, "get", lambda i: _account(i)
        ), patch.object(
            publisher_mod.instagram_tools, "publish_carousel", fake_publish
        ), patch.object(
            publisher_mod.telegram_tools,
            "send_confirmation_message",
            lambda *a, **k: {"message_id": "1"},
        ), patch.object(
            publisher_mod.db, "update_run_phase", AsyncMock(return_value=None)
        ):
            result = await publisher_mod.publish_approved_carousel(tool_context)

        self.assertEqual(result["status"], "published")
        self.assertIsNotNone(seen["account"])
        self.assertEqual(seen["account"].id, "acc-9")

    async def test_a_run_without_an_account_fails_before_any_upload(self) -> None:
        state = {
            "run_id": RUN_ID,
            "bundle": _bundle(["cover.mp4", "s1.png"]),
        }
        tool_context = SimpleNamespace(
            state=state,
            session=SimpleNamespace(app_name="app", user_id="u", id=RUN_ID),
            _invocation_context=None,
        )

        def explode(*a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("publish_carousel must not be called")

        with patch.object(
            publisher_mod.instagram_tools, "publish_carousel", explode
        ):
            result = await publisher_mod.publish_approved_carousel(tool_context)

        self.assertEqual(result["status"], "error")
        self.assertIn("account", result["message"].lower())


if __name__ == "__main__":
    unittest.main()
