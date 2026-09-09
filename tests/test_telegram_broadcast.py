"""Multiple bots share one prepared output, without per-recipient model work."""
import asyncio
import copy
import io
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx

from app.services import telegram_config as config, secret_box
from app.agents import review_dispatcher
from app.state import K_TELEGRAM_REVIEW_DELIVERY
from app.tools import telegram_tools as tg
from web_api import routes_settings


def bot(number):
    return {"bot_id": str(number), "bot_token": f"{number}:secret-{number}",
            "bot_username": f"bot{number}", "chat_id": "same-chat",
            "connected_by": "owner", "connected_at": "now"}


class FakeStore:
    """Transactional in-memory app_config, including the connection row lock."""
    def __init__(self, value=None):
        self.value = value
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield

    async def fetchval(self, query, *args):
        return copy.deepcopy(self.value)

    async def execute(self, query, *args):
        if query.startswith("INSERT") and self.value is None:
            self.value = copy.deepcopy(args[1])
        if query.startswith("UPDATE"):
            self.value = copy.deepcopy(args[1])


class ConnectionListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()
        for patcher in (
            patch.object(config, "_cache", None),
            patch.object(config.db, "get_pool", AsyncMock(return_value=self.store)),
            patch.object(config.db, "get_config", AsyncMock(side_effect=lambda *args: self.store.value)),
            patch.object(secret_box, "settings", SimpleNamespace(secrets_key=secret_box.generate_key())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_concurrent_connections_preserve_both_bots(self):
        await asyncio.gather(config.save(**bot(1)), config.save(**bot(2)))
        await config.load()
        self.assertEqual({b["bot_id"] for b in config.all_credentials()}, {"1", "2"})
        self.assertNotIn("secret-1", str(self.store.value))
        self.assertNotIn("secret-2", str(self.store.value))

    async def test_reconnect_refreshes_same_bot_without_duplicating(self):
        await config.save(**bot(1))
        await config.save(**bot(2))
        await config.save(**{**bot(1), "chat_id": "new-chat", "bot_token": "1:rotated"})
        self.assertEqual(len(config.all_credentials()), 2)
        self.assertEqual(config.all_credentials()[0]["chat_id"], "new-chat")

    async def test_disconnect_preserves_other_bots(self):
        await config.save(**bot(1))
        await config.save(**bot(2))
        await config.clear("1")
        self.assertEqual([b["bot_id"] for b in config.all_credentials()], ["2"])
        await config.clear("missing")
        self.assertTrue(config.configured())

    async def test_legacy_bot_survives_adding_another(self):
        self.store.value = {"bot_token_enc": secret_box.encrypt("1:legacy"), "chat_id": "original", "bot_username": "old"}
        await config.load()
        self.assertEqual(config.credentials()["bot_id"], "1")
        await config.save(**bot(2))
        self.assertEqual([b["bot_id"] for b in config.all_credentials()], ["1", "2"])
        await config.save(**bot(1))
        self.assertEqual(len(self.store.value["bots"]), 2)

    async def test_status_lists_bots_without_tokens_and_deletes_only_named_bot(self):
        await config.save(**bot(1))
        await config.save(**bot(2))
        status = routes_settings._status()
        self.assertEqual(len(status["bots"]), 2)
        self.assertNotIn("1:secret-1", str(status))
        result = await routes_settings.telegram_disconnect("1", SimpleNamespace(email="owner"))
        self.assertEqual([b["bot_id"] for b in result["bots"]], ["2"])


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(config, "all_credentials", return_value=[bot(1), bot(2), bot(3)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_partial_failure_still_attempts_all_and_retry_skips_successes(self):
        calls = []
        def send(destination):
            calls.append(destination["bot_id"])
            if destination["bot_id"] == "2":
                raise RuntimeError("https://api.telegram.org/bot2:secret-2")
            return {"message_id": destination["bot_id"]}
        with self.assertRaises(tg.BroadcastError) as caught:
            tg._broadcast(send)
        self.assertEqual(calls, ["1", "2", "3"])
        self.assertNotIn("secret-2", str(caught.exception.result))
        self.assertNotIn("secret-2", str(caught.exception))
        calls.clear()
        def retry(destination):
            calls.append(destination["bot_id"])
            return {"message_id": "retry"}
        result = tg._broadcast(retry, caught.exception.result)
        self.assertEqual(calls, ["2"])
        self.assertEqual(result["sent_count"], 3)

    def test_changed_chat_is_a_new_destination(self):
        previous = {"deliveries": [{"bot_id": "1", "chat_id": "old-chat", "status": "sent"}]}
        with patch.object(tg, "_send_confirmation_message", return_value={"message_id": "m"}) as send:
            tg.send_confirmation_message("r", "https://instagram.com/p/1", previous=previous)
        self.assertEqual(send.call_count, 3)

    def test_review_has_identical_content_and_one_send_per_bot(self):
        bundle = {"caption": "Prepared once", "news_title": "Same headline"}
        with patch.object(tg, "_send_review_message", return_value={"message_id": "m"}) as send:
            result = tg.send_review_message("r", bundle, 1)
        self.assertEqual(send.call_count, 3)
        for call in send.call_args_list:
            self.assertIs(call.args[1], bundle)
            self.assertEqual(call.args[:1], ("r",))
        self.assertEqual(result["recipient_count"], 3)

    def test_every_completed_archive_upload_contains_full_bytes(self):
        seen = []
        def request(client, method, *, data, files):
            self.assertEqual(method, "sendDocument")
            seen.append((str(client.base_url), files["document"][1].read()))
            return {"message_id": "m"}
        with patch.object(tg, "_request", request):
            result = tg.send_completed_carousel("r", io.BytesIO(b"entire-archive"), "Title")
        self.assertEqual([data for _, data in seen], [b"entire-archive"] * 3)
        self.assertEqual(len({url for url, _ in seen}), 3)
        self.assertEqual(result["sent_count"], 3)

    def test_confirmation_uses_each_bot_token_and_the_same_post_link(self):
        calls = []
        def handler(request):
            calls.append((request.url.path, parse_qs(request.content.decode())))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        real_client = httpx.Client
        def client(**kwargs):
            return real_client(**kwargs, transport=httpx.MockTransport(handler))
        with patch.object(tg.httpx, "Client", client):
            tg.send_confirmation_message("r", "https://instagram.com/p/same")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({path for path, _ in calls}), 3)
        self.assertEqual(len({form["text"][0] for _, form in calls}), 1)


class ReviewRoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_skips_delivered_bots_but_new_review_round_sends_to_all(self):
        state = {"run_id": "r", "bundle": {
            "cover": {"poster_artifact": "cover.png"},
            "cta": {"cta_type": "follow", "artifact": "cta.png"},
            "caption": "Prepared once",
        }}
        calls = []
        failed_once = False
        def send(run_id, bundle, round_no, destination):
            nonlocal failed_once
            calls.append((round_no, destination["bot_id"]))
            if destination["bot_id"] == "2" and not failed_once:
                failed_once = True
                raise RuntimeError("offline")
            return {"message_id": "m"}
        with patch.object(config, "all_credentials", return_value=[bot(1), bot(2)]), patch.object(tg, "_send_review_message", send), patch.object(review_dispatcher, "_materialize_artifact", AsyncMock(return_value="")):
            context = SimpleNamespace(state=state)
            self.assertEqual((await review_dispatcher.send_review_request(context))["status"], "error")
            self.assertEqual(state[K_TELEGRAM_REVIEW_DELIVERY]["sent_count"], 1)
            self.assertEqual((await review_dispatcher.send_review_request(context))["status"], "sent")
            self.assertEqual(calls, [(1, "1"), (1, "2"), (1, "2")])
            self.assertEqual((await review_dispatcher.send_review_request(context))["status"], "sent")
            self.assertEqual(calls[-2:], [(2, "1"), (2, "2")])
