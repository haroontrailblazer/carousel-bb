"""Publishing goes to the RUN's account, with that account's own token.

Every credential used to come from ``settings``: one id, one token, one host,
read at import. That is exactly one publishing target for the whole console.
Now each run carries the account it was started against, and the tool is
handed that account's id, token and Graph host.

The host matters as much as the token. A token minted by Instagram Login only
works against ``graph.instagram.com``; sending it to ``graph.facebook.com``
fails with a permissions error that reads like a scope problem and is not one.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.services.instagram_accounts import Account
from app.tools import instagram_tools


def _account(
    *,
    ig_user_id: str = "17841400000000000",
    token: str = "account-token",
    auth_kind: str = "instagram_login",
) -> Account:
    return Account(
        id="acc-1",
        ig_user_id=ig_user_id,
        username="acme",
        name="Acme",
        avatar_key="",
        auth_kind=auth_kind,
        token=token,
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_default=True,
        disabled=False,
        connected_by="",
        connected_at=None,
        last_refreshed_at=None,
    )


class TargetTests(unittest.TestCase):
    def test_a_target_carries_the_accounts_id_token_and_host(self) -> None:
        target = instagram_tools.PublishTarget.from_account(_account())
        self.assertEqual(target.ig_user_id, "17841400000000000")
        self.assertEqual(target.access_token, "account-token")
        self.assertEqual(target.graph_host, "https://graph.instagram.com")

    def test_a_facebook_login_account_keeps_the_facebook_host(self) -> None:
        target = instagram_tools.PublishTarget.from_account(
            _account(auth_kind="facebook_login")
        )
        self.assertEqual(target.graph_host, "https://graph.facebook.com")

    def test_the_api_base_is_the_accounts_host_and_the_configured_version(
        self,
    ) -> None:
        target = instagram_tools.PublishTarget.from_account(_account())
        self.assertTrue(
            target.api_base.startswith("https://graph.instagram.com/v")
        )

    def test_an_account_with_no_usable_token_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            instagram_tools.PublishTarget.from_account(_account(token=""))


class PublishTests(unittest.TestCase):
    """What actually goes on the wire, with no network involved."""

    def _run(self, urls: list[str], account: Account) -> list[dict]:
        calls: list[dict] = []

        def fake_request(client, method, path, *, params=None, data=None, target=None):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "params": params or {},
                    "data": data or {},
                    "target": target,
                }
            )
            fields = (params or {}).get("fields", "")
            if path.endswith("/media_publish"):
                return {"id": "media-99"}
            if path.endswith("/media"):
                return {"id": f"container-{len(calls)}"}
            if "permalink" in fields:
                return {"permalink": "https://instagram.com/p/abc"}
            # Container status. Answering FINISHED first time keeps the real
            # five-minute polling loop out of the test.
            return {"status_code": "FINISHED"}

        original = instagram_tools._graph_request
        instagram_tools._graph_request = fake_request
        try:
            instagram_tools.publish_carousel(
                {"caption": "hello"},
                urls,
                account=account,
            )
        finally:
            instagram_tools._graph_request = original
        return calls

    def test_every_call_uses_the_accounts_token_not_a_global(self) -> None:
        calls = self._run(
            ["https://x/cover.mp4", "https://x/1.png", "https://x/2.png"],
            _account(token="the-account-token"),
        )
        tokens = {
            (call["data"].get("access_token") or call["params"].get("access_token"))
            for call in calls
        }
        tokens.discard(None)
        self.assertEqual(tokens, {"the-account-token"})

    def test_the_media_path_names_the_accounts_ig_user_id(self) -> None:
        calls = self._run(
            ["https://x/cover.mp4", "https://x/1.png"],
            _account(ig_user_id="17999999999999999"),
        )
        creates = [c for c in calls if c["path"].endswith("/media")]
        self.assertTrue(creates)
        for call in creates:
            self.assertEqual(call["path"], "/17999999999999999/media")

    def test_publishing_without_an_account_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            instagram_tools.publish_carousel(
                {"caption": ""},
                ["https://x/1.png", "https://x/2.png"],
                account=None,
            )
        self.assertIn("account", str(caught.exception).lower())

    def test_a_carousel_below_the_minimum_is_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            instagram_tools.publish_carousel(
                {"caption": ""}, ["https://x/only.png"], account=_account()
            )


if __name__ == "__main__":
    unittest.main()
