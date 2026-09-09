"""Long-lived tokens are renewed before they lapse.

Meta's long-lived token lasts 60 days and can be extended any time after its
first 24 hours - but only while it is still valid. Once it lapses there is no
recovery call; somebody has to reconnect the account by hand.

So the job runs daily and renews anything inside a fortnight of expiry. The
window is generous on purpose: two weeks of failed refreshes would have to go
unnoticed before a token actually died.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import scheduler as scheduler_mod
from app.services import instagram_accounts
from app.services.instagram_accounts import Account


def _account(
    account_id: str = "acc-1",
    *,
    days_left: int = 5,
    token: str = "tok",
    disabled: bool = False,
) -> Account:
    return Account(
        id=account_id,
        ig_user_id="1784140000",
        username="acme",
        name="",
        avatar_key="",
        auth_kind="instagram_login",
        token=token,
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=days_left),
        is_default=True,
        disabled=disabled,
        connected_by="",
        connected_at=None,
        last_refreshed_at=None,
    )


class DueTests(unittest.TestCase):
    """Which accounts the job picks up."""

    def setUp(self) -> None:
        instagram_accounts.reset_cache()

    def tearDown(self) -> None:
        instagram_accounts.reset_cache()

    def _cache(self, *accounts: Account) -> None:
        for account in accounts:
            instagram_accounts._cache[account.id] = account

    def test_a_token_inside_the_window_is_due(self) -> None:
        self._cache(_account(days_left=5))
        self.assertEqual([a.id for a in instagram_accounts.due_for_refresh()], ["acc-1"])

    def test_a_token_with_plenty_of_time_is_left_alone(self) -> None:
        self._cache(_account(days_left=50))
        self.assertEqual(instagram_accounts.due_for_refresh(), [])

    def test_an_already_lapsed_token_is_not_retried_daily(self) -> None:
        """Meta cannot revive one, so retrying is a permanent log error."""
        self._cache(_account(days_left=-1))
        self.assertEqual(instagram_accounts.due_for_refresh(), [])

    def test_an_unreadable_token_is_skipped(self) -> None:
        self._cache(_account(token=""))
        self.assertEqual(instagram_accounts.due_for_refresh(), [])

    def test_a_disabled_account_is_skipped(self) -> None:
        self._cache(_account(disabled=True))
        self.assertEqual(instagram_accounts.due_for_refresh(), [])


class RefreshJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        instagram_accounts.reset_cache()

    def tearDown(self) -> None:
        instagram_accounts.reset_cache()

    async def test_a_due_token_is_renewed_and_stored(self) -> None:
        instagram_accounts._cache["acc-1"] = _account(days_left=3)
        recorded = AsyncMock()

        with patch.object(
            scheduler_mod.instagram_oauth,
            "refresh_long_lived",
            lambda tok: ("fresh-token", 5183944),
        ), patch.object(
            scheduler_mod.instagram_accounts, "record_refreshed", recorded
        ):
            result = await scheduler_mod.refresh_instagram_tokens()

        self.assertEqual(result["refreshed"], 1)
        recorded.assert_awaited_once_with("acc-1", "fresh-token", 5183944)

    async def test_one_account_failing_does_not_stop_the_others(self) -> None:
        instagram_accounts._cache["acc-1"] = _account("acc-1", days_left=3)
        instagram_accounts._cache["acc-2"] = _account("acc-2", days_left=3)

        def flaky(token: str):
            if not hasattr(flaky, "called"):
                flaky.called = True
                raise scheduler_mod.instagram_oauth.OAuthError("boom", "nope")
            return ("fresh", 100)

        with patch.object(
            scheduler_mod.instagram_oauth, "refresh_long_lived", flaky
        ), patch.object(
            scheduler_mod.instagram_accounts, "record_refreshed", AsyncMock()
        ):
            result = await scheduler_mod.refresh_instagram_tokens()

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["failed"], 1)

    async def test_nothing_due_does_no_work(self) -> None:
        instagram_accounts._cache["acc-1"] = _account(days_left=50)

        def explode(token):  # pragma: no cover - must not be reached
            raise AssertionError("no refresh should have been attempted")

        with patch.object(
            scheduler_mod.instagram_oauth, "refresh_long_lived", explode
        ):
            result = await scheduler_mod.refresh_instagram_tokens()

        self.assertEqual(result, {"refreshed": 0, "failed": 0})


if __name__ == "__main__":
    unittest.main()
