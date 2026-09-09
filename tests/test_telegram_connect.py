"""Connecting a bot: a plain program, tested against a stubbed Bot API."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.services import telegram_connect as tc


def _client(handler) -> object:
    """Patch httpx.Client so no real request leaves the machine."""
    transport = httpx.MockTransport(handler)
    real = httpx.Client
    return patch.object(
        tc.httpx, "Client", lambda *a, **k: real(transport=transport, timeout=5)
    )


class VerifyTokenTests(unittest.TestCase):
    def test_an_empty_token_is_refused_before_any_request(self) -> None:
        with self.assertRaises(tc.ConnectError) as ctx:
            tc.verify_token("   ")
        self.assertEqual(ctx.exception.code, "missing_token")

    def test_a_valid_token_returns_the_bot_username(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/getMe", request.url.path)
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 7, "username": "carousel_bot",
                                             "first_name": "Carousel"}},
            )

        with _client(handler):
            bot = tc.verify_token("123:abc")
        self.assertEqual(bot["username"], "carousel_bot")

    def test_telegram_rejecting_the_token_says_so_plainly(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

        with _client(handler):
            with self.assertRaises(tc.ConnectError) as ctx:
                tc.verify_token("nope")
        self.assertEqual(ctx.exception.code, "invalid_token")
        self.assertIn("BotFather", ctx.exception.message)


class DiscoverChatTests(unittest.TestCase):
    """Telegram only names a chat once a human has messaged the bot."""

    def test_no_updates_means_no_chat_yet(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": []})

        with _client(handler):
            self.assertIsNone(tc.discover_chat("123:abc"))

    def test_the_most_recent_chat_wins(self) -> None:
        """Whoever just pressed Start while setting this up is the one meant."""
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"message": {"chat": {"id": 111}}},
                        {"message": {"chat": {"id": 222}}},
                    ],
                },
            )

        with _client(handler):
            self.assertEqual(tc.discover_chat("123:abc"), "222")

    def test_a_channel_post_counts_too(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ok": True, "result": [{"channel_post": {"chat": {"id": -100}}}]}
            )

        with _client(handler):
            self.assertEqual(tc.discover_chat("123:abc"), "-100")


class SendWelcomeTests(unittest.TestCase):
    def test_it_sends_the_confirmation_text(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        with _client(handler):
            tc.send_welcome("123:abc", "555")
        self.assertIn("successfully+connected", seen["body"].replace("%20", "+"))
        self.assertIn("555", seen["body"])

    def test_a_refused_delivery_is_reported(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"ok": False, "description": "chat not found"}
            )

        with _client(handler):
            with self.assertRaises(tc.ConnectError) as ctx:
                tc.send_welcome("123:abc", "555")
        self.assertEqual(ctx.exception.code, "send_failed")
        self.assertIn("chat not found", ctx.exception.message)


class NoAgentTests(unittest.TestCase):
    """Connecting a bot is three fixed calls; a model would only add risk."""

    def test_the_module_imports_no_agent_machinery(self) -> None:
        source = (tc.__doc__ or "") + str(tc.__dict__.keys())
        self.assertNotIn("LlmAgent", source)
        self.assertNotIn("google.adk", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
