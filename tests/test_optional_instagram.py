"""Optional publishing is a deterministic gate, independent of model output."""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zipfile import ZipFile

import httpx

from app import orchestrator
from app.agents import publisher
from app.services import instagram_config as config, instagram_connect, secret_box, telegram_delivery
from app.state import K_PHASE, K_QA_REPORT, K_VERDICT
from app.tools import instagram_tools, telegram_tools
from fastapi import HTTPException
from web_api import routes_settings


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_api_uses_discovered_identity_and_does_not_return_token(self):
        identity = SimpleNamespace(email="owner@example.com")
        payload = routes_settings.InstagramConnectRequest(token="private-token")
        async def save(**kwargs):
            config._cache = kwargs
        with patch.object(config, "_cache", {}), patch.object(secret_box, "configured", return_value=True), patch.object(routes_settings, "verify_instagram_token", return_value={"user_id": "42", "username": "separate"}), patch.object(config, "save", AsyncMock(side_effect=save)) as persist:
            result = await routes_settings.instagram_connect(payload, identity)
        self.assertTrue(result["connected"])
        self.assertEqual(result["username"], "separate")
        self.assertEqual(persist.call_args.kwargs["user_id"], "42")
        self.assertNotIn("private-token", str(result))

    async def test_connection_api_reports_second_account_as_conflict(self):
        with patch.object(secret_box, "configured", return_value=True), patch.object(routes_settings, "verify_instagram_token", return_value={"user_id": "42", "username": "separate"}), patch.object(config, "save", AsyncMock(side_effect=ValueError("Disconnect first"))):
            with self.assertRaises(HTTPException) as error:
                await routes_settings.instagram_connect(routes_settings.InstagramConnectRequest(token="t"), SimpleNamespace(email="owner@example.com"))
        self.assertEqual(error.exception.status_code, 409)

    async def test_connection_refuses_unencrypted_storage_before_contacting_instagram(self):
        with patch.object(secret_box, "configured", return_value=False), patch.object(routes_settings, "verify_instagram_token") as verify:
            with self.assertRaises(HTTPException) as error:
                await routes_settings.instagram_connect(routes_settings.InstagramConnectRequest(token="t"), SimpleNamespace(email="owner@example.com"))
        self.assertEqual(error.exception.status_code, 503)
        verify.assert_not_called()

    async def test_token_is_encrypted_and_never_comes_from_environment(self):
        pool = SimpleNamespace(fetchval=AsyncMock(return_value="instagram"))
        with patch.object(config, "_cache", {}), patch.object(config.db, "get_pool", AsyncMock(return_value=pool)), patch.object(secret_box, "settings", SimpleNamespace(secrets_key=secret_box.generate_key())):
            self.assertFalse(config.configured())
            await config.save(access_token="private-token", user_id="1", username="one", connected_by="a", connected_at="now")
            stored = pool.fetchval.call_args.args[2]
            self.assertNotIn("access_token", stored)
            self.assertNotIn("private-token", str(stored))
            self.assertEqual(secret_box.decrypt(stored["access_token_enc"]), "private-token")
            with patch.object(config.db, "get_config", AsyncMock(return_value=stored)):
                await config.load()
            self.assertEqual(config.credentials()["user_id"], "1")
            self.assertTrue(config.configured())

    async def test_conflicting_account_does_not_replace_cache(self):
        pool = SimpleNamespace(fetchval=AsyncMock(return_value=None))
        with patch.object(config, "_cache", {"user_id": "original", "access_token": "first"}), patch.object(config.db, "get_pool", AsyncMock(return_value=pool)), patch.object(secret_box, "encrypt", return_value="encrypted"):
            with self.assertRaisesRegex(ValueError, "Disconnect"):
                await config.save(access_token="second", user_id="different", username="two", connected_by="a", connected_at="now")
            self.assertEqual(config.credentials()["user_id"], "original")

    async def test_disconnect_clears_persistent_and_runtime_credentials(self):
        with patch.object(config, "_cache", {"user_id": "1", "access_token": "token"}), patch.object(config.db, "set_config", AsyncMock()) as save:
            await config.clear()
            save.assert_awaited_once_with("instagram", {})
            self.assertFalse(config.configured())

    def test_token_identifies_account_using_instagram_host(self):
        def respond(request):
            self.assertEqual(request.url.host, "graph.instagram.com")
            self.assertEqual(request.headers["authorization"], "Bearer private")
            self.assertNotIn("private", str(request.url))
            return httpx.Response(200, json={"user_id": "42", "username": "separate"})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(instagram_connect.httpx, "Client", return_value=client):
            self.assertEqual(instagram_connect.verify_token("private"), {"user_id": "42", "username": "separate"})

    def test_invalid_token_is_rejected_without_echoing_it(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(400, json={"error": {"message": "private-token"}})))
        with patch.object(instagram_connect.httpx, "Client", return_value=client):
            with self.assertRaises(ValueError) as error:
                instagram_connect.verify_token("private-token")
        self.assertNotIn("private-token", str(error.exception))


class PhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = orchestrator.CarouselOrchestrator(name="test_orchestrator")
        self.state = {K_PHASE: "review", "run_id": "test-run"}
        self.ctx = SimpleNamespace(session=SimpleNamespace(state=self.state), invocation_id="test", branch=None)
        self.holder = {"halted": False, "paused": False}
        self.record = AsyncMock()
        for patcher in (
            patch.object(config, "load", AsyncMock()),
            patch.object(config, "_cache", {}),
            patch.object(orchestrator, "ToolContext", return_value=SimpleNamespace(state=self.state)),
            patch.object(orchestrator.CarouselOrchestrator, "_record_phase_quietly", self.record),
            patch.object(orchestrator.db, "clear_pending_review", AsyncMock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def drive(self, handler):
        events = []
        async for event in handler(self.ctx, self.state, self.holder):
            self.state.update(event.actions.state_delta)
            events.append(event)
        return events

    async def test_disconnected_completes_without_review_or_publisher(self):
        with patch.object(telegram_delivery, "deliver", AsyncMock(return_value={"status": "delivered", "message_id": "m"})) as send, patch.object(orchestrator.CarouselOrchestrator, "_child", side_effect=AssertionError("No agent should run")):
            await self.drive(self.root._phase_review)
        send.assert_awaited_once()
        self.assertEqual(self.state[K_PHASE], "done")
        self.assertEqual(self.state["telegram_delivery"]["message_id"], "m")
        self.assertIsNone(self.state[K_VERDICT])

    async def test_delivery_failure_remains_resumable_without_approval(self):
        with patch.object(telegram_delivery, "deliver", AsyncMock(side_effect=RuntimeError("Telegram unavailable"))):
            await self.drive(self.root._phase_review)
        self.assertTrue(self.holder["halted"])
        self.assertEqual(self.state[K_PHASE], "review")
        self.assertEqual(self.state["telegram_delivery"]["status"], "error")
        self.assertEqual(self.record.call_args.kwargs["status"], "interrupted")

    async def test_completed_delivery_is_not_sent_twice(self):
        self.state["telegram_delivery"] = {"status": "delivered", "message_id": "m"}
        with patch.object(telegram_delivery, "deliver", AsyncMock()) as send:
            await self.drive(self.root._phase_review)
        send.assert_not_awaited()
        self.assertEqual(self.state[K_PHASE], "done")

    async def test_connecting_during_a_delivery_retry_does_not_enable_publishing(self):
        config._cache = {"user_id": "1", "access_token": "t"}
        self.state["delivery_mode"] = "telegram"
        with patch.object(telegram_delivery, "deliver", AsyncMock(return_value={"status": "delivered"})):
            await self.drive(self.root._phase_review)
        self.assertEqual(self.state[K_PHASE], "done")

    async def test_connected_run_waits_for_review(self):
        config._cache = {"user_id": "1", "access_token": "t"}
        self.state["instagram_account_id"] = "1"
        async def pause(root, child, ctx, holder):
            holder["paused"] = True
            if False:
                yield
        with patch.object(orchestrator.CarouselOrchestrator, "_drive", pause), patch.object(orchestrator.CarouselOrchestrator, "_child", return_value=object()), patch.object(telegram_delivery, "deliver", AsyncMock()) as send:
            await self.drive(self.root._phase_review)
        self.assertTrue(self.holder["paused"])
        self.assertEqual(self.state[K_PHASE], "review")
        send.assert_not_awaited()

    async def test_approved_connected_run_routes_to_publish(self):
        config._cache = {"user_id": "1", "access_token": "t"}
        self.state.update(instagram_account_id="1")
        self.state[K_VERDICT] = {"status": "approved"}
        await self.drive(self.root._phase_review)
        self.assertEqual(self.state[K_PHASE], "publish")

    async def test_rejected_connected_run_reworks_without_publishing(self):
        config._cache = {"user_id": "1", "access_token": "t"}
        self.state["instagram_account_id"] = "1"
        self.state[K_VERDICT] = {"status": "rejected", "feedback": "Fix the title"}
        await self.drive(self.root._phase_review)
        self.assertEqual(self.state[K_PHASE], "rework")

    async def test_receipt_survives_cleanup_failure(self):
        with patch.object(telegram_delivery, "deliver", AsyncMock(return_value={"status": "delivered", "message_id": "m"})), patch.object(orchestrator.db, "clear_pending_review", AsyncMock(side_effect=RuntimeError("database down"))):
            with self.assertRaisesRegex(RuntimeError, "database down"):
                await self.drive(self.root._phase_review)
        self.assertEqual(self.state["telegram_delivery"]["message_id"], "m")

    async def test_replacement_account_requires_new_approval(self):
        config._cache = {"user_id": "new", "access_token": "t"}
        self.state.update(phase="publish", instagram_account_id="old")
        self.state[K_VERDICT] = {"status": "approved"}
        with patch.object(orchestrator.CarouselOrchestrator, "_child", side_effect=AssertionError("Publisher must not run")):
            await self.drive(self.root._phase_publish)
        self.assertEqual(self.state[K_PHASE], "review")
        self.assertIsNone(self.state[K_VERDICT])

    async def test_qa_records_delivery_mode(self):
        self.state.update(phase="qa", qa_report={"passed": True})
        async def no_op(*args):
            if False:
                yield
        with patch.object(orchestrator.CarouselOrchestrator, "_drive", no_op), patch.object(orchestrator.CarouselOrchestrator, "_child", return_value=object()):
            await self.drive(self.root._phase_qa)
        self.assertEqual(self.state["delivery_mode"], "telegram")
        self.assertEqual(self.record.call_args.kwargs["status"], "running")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_contains_all_assets_and_caption(self):
        state = {"run_id": "r", "bundle": {
            "cover": {"video_artifact": "cover.mp4", "poster_artifact": "cover.png"},
            "slides": [{"index": 1, "artifact": "slide.png"}],
            "cta": {"cta_type": "follow", "artifact": "cta.png"},
            "caption": "Finished caption", "ordered_artifacts": ["cover.mp4", "slide.png", "cta.png"],
        }}
        ctx = SimpleNamespace(state=state, load_artifact=AsyncMock(return_value=SimpleNamespace(inline_data=SimpleNamespace(data=b"asset"))))
        def send(run_id, archive, title, previous=None):
            with ZipFile(archive) as zip_file:
                self.assertEqual(len(zip_file.namelist()), 5)
                self.assertEqual(zip_file.read("caption.txt"), b"Finished caption")
                self.assertIn("04-cover.png", zip_file.namelist())
            return {"message_id": "m"}
        with patch.object(telegram_delivery, "send_completed_carousel", send):
            self.assertEqual((await telegram_delivery.deliver(ctx))["status"], "delivered")

    def test_completion_upload_has_no_review_buttons(self):
        with patch.object(telegram_tools.telegram_config, "all_credentials", return_value=[{"bot_id": "1", "bot_token": "test", "chat_id": "chat"}]), patch.object(telegram_tools, "_request", return_value={"message_id": 7}) as request, patch.object(telegram_tools, "_api_base", return_value="https://api.telegram.org/bottest"), patch.object(telegram_tools, "_chat_id", return_value="chat"):
            telegram_tools.send_completed_carousel("r", io.BytesIO(b"zip"), "Title")
        self.assertEqual(request.call_args.args[1], "sendDocument")
        self.assertNotIn("reply_markup", request.call_args.kwargs["data"])
        self.assertNotIn("approv", request.call_args.kwargs["data"]["caption"].lower())


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    async def test_publisher_requires_a_real_verdict(self):
        with patch.object(config, "load", AsyncMock()), patch.object(config, "_cache", {"user_id": "1", "access_token": "t"}), patch.object(instagram_tools, "publish_carousel") as publish:
            result = await publisher.publish_approved_carousel(SimpleNamespace(state={"instagram_account_id": "1"}))
        self.assertEqual(result["status"], "error")
        publish.assert_not_called()

    def test_publishing_checks_account_and_uses_instagram_host(self):
        requests = []
        def respond(request):
            requests.append(request)
            self.assertEqual(request.url.host, "graph.instagram.com")
            self.assertEqual(request.headers["authorization"], "Bearer token")
            if request.url.path.endswith("/media_publish"):
                return httpx.Response(200, json={"id": "published"})
            if request.method == "POST":
                return httpx.Response(200, json={"id": "container"})
            return httpx.Response(200, json={"status_code": "FINISHED", "permalink": "https://instagram.com/p/1"})
        client = httpx.Client(transport=httpx.MockTransport(respond), headers={"Authorization": "Bearer token"})
        with patch.object(config, "_cache", {"user_id": "1", "access_token": "token"}), patch.object(instagram_tools.httpx, "Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "fresh approval"):
                instagram_tools.publish_carousel({}, ["https://x/a.png", "https://x/b.png"], expected_account_id="other")
            self.assertEqual(requests, [])
            result = instagram_tools.publish_carousel({}, ["https://x/a.png", "https://x/b.png"], expected_account_id="1")
        self.assertEqual(result["media_id"], "published")
