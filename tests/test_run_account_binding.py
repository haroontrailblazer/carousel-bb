"""A run carries its account from the first slide to the published post.

Two things have to agree, and they are set minutes apart: the brand marks
composited into the artwork while the run generates, and the credentials used
when it publishes. Both are derived from one account id recorded on the run,
so they cannot drift.

The binding happens in the driving task rather than at the call sites because
``asyncio.to_thread`` copies the context - so a contextvar set once when the
run starts is visible in the worker thread that renders the slides.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.runs import service as service_mod
from app.services.instagram_accounts import Account
from app.tools import brand_identity

RUN_ID = "run-bind-1"


def _account(account_id: str = "acc-1", username: str = "acme") -> Account:
    return Account(
        id=account_id,
        ig_user_id="1784140000",
        username=username,
        name="Acme",
        avatar_key="instagram/acc-1.png",
        auth_kind="instagram_login",
        token="tok",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_default=True,
        disabled=False,
        connected_by="",
        connected_at=None,
        last_refreshed_at=None,
    )


class BindTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        brand_identity.set_current(None)

    async def test_the_runs_account_becomes_the_brand_identity(self) -> None:
        with patch.object(
            service_mod.db, "get_run_account_id", AsyncMock(return_value="acc-1")
        ), patch.object(
            service_mod.instagram_accounts, "get", lambda i: _account(i)
        ), patch.object(
            service_mod.avatar_store, "load", AsyncMock(return_value=b"picture-bytes")
        ):
            await service_mod._bind_brand_identity(RUN_ID)

        identity = brand_identity.current()
        assert identity is not None
        self.assertEqual(identity.handle, "@acme")
        self.assertEqual(identity.favicon_png, b"picture-bytes")
        self.assertEqual(identity.account_id, "acc-1")

    async def test_a_run_with_no_account_binds_nothing(self) -> None:
        """Rendering then fails loudly rather than stamping someone else."""
        with patch.object(
            service_mod.db, "get_run_account_id", AsyncMock(return_value="")
        ):
            await service_mod._bind_brand_identity(RUN_ID)
        self.assertIsNone(brand_identity.current())

    async def test_a_missing_profile_picture_still_binds_the_handle(self) -> None:
        with patch.object(
            service_mod.db, "get_run_account_id", AsyncMock(return_value="acc-1")
        ), patch.object(
            service_mod.instagram_accounts, "get", lambda i: _account(i)
        ), patch.object(
            service_mod.avatar_store, "load", AsyncMock(side_effect=RuntimeError("gone"))
        ):
            await service_mod._bind_brand_identity(RUN_ID)

        identity = brand_identity.current()
        assert identity is not None
        self.assertEqual(identity.handle, "@acme")
        self.assertEqual(identity.favicon_png, b"")

    async def test_the_bound_identity_reaches_a_worker_thread(self) -> None:
        """The property the whole contextvar design exists for."""
        with patch.object(
            service_mod.db, "get_run_account_id", AsyncMock(return_value="acc-1")
        ), patch.object(
            service_mod.instagram_accounts, "get", lambda i: _account(i)
        ), patch.object(
            service_mod.avatar_store, "load", AsyncMock(return_value=b"")
        ):
            await service_mod._bind_brand_identity(RUN_ID)
            handle = await asyncio.to_thread(brand_identity.require_handle)

        self.assertEqual(handle, "@acme")


if __name__ == "__main__":
    unittest.main()
