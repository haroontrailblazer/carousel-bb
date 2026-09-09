"""Telegram review-channel tools.

Covers the parts that can be wrong without anyone noticing until a review is
sitting unsent: preview resolution, the album cap, the Approve/Reject links,
and the error envelope Telegram returns with a 200-looking body.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.tools import telegram_tools as tg


def _settings(
    token: str = "bot-token",
    chat: str = "12345",
    public_base_url: str = "https://console.example",
):
    """Stand-in for the frozen Settings singleton (it cannot be mutated)."""
    # NOTE: no telegram_bot_token / telegram_chat_id. Credentials come from
    # telegram_config now (console-set, encrypted at rest); settings only
    # carries the public base URL the review button is built from.
    del token, chat
    return SimpleNamespace(public_base_url=public_base_url)


def _creds(token: str = "bot-token", chat: str = "12345") -> dict:
    """What telegram_config.credentials() returns."""
    return {
        "bot_token": token,
        "chat_id": chat,
        "bot_username": "",
        "connected_by": "",
        "connected_at": "",
    }


class ConfigGuardTests(unittest.TestCase):
    """Credentials come from telegram_config, and from NOWHERE else.

    They used to be read from the frozen Settings singleton, which meant a
    bearer token sitting in plaintext in .env and in every shell that
    inherited it - and a second, invisible source of truth that could override
    what the console displayed. The bot is connected on the profile page now
    and the token is stored encrypted.
    """

    def test_missing_token_points_at_the_console(self) -> None:
        with patch.object(tg.telegram_config, "credentials", lambda: _creds(token="")):
            with self.assertRaises(RuntimeError) as ctx:
                tg._api_base()
        message = str(ctx.exception)
        self.assertIn("Profile", message)
        # The .env variable must NOT be suggested - it is no longer read.
        self.assertNotIn("TELEGRAM_BOT_TOKEN", message)

    def test_missing_chat_id_says_it_is_discovered_for_you(self) -> None:
        """The old message told people to read getUpdates by hand."""
        with patch.object(tg.telegram_config, "credentials", lambda: _creds(chat="")):
            with self.assertRaises(RuntimeError) as ctx:
                tg._chat_id()
        self.assertIn("discovers", str(ctx.exception))

    def test_api_base_embeds_the_token(self) -> None:
        with patch.object(tg.telegram_config, "credentials", lambda: _creds()):
            self.assertEqual(tg._api_base(), "https://api.telegram.org/botbot-token")

    def test_there_is_no_environment_fallback(self) -> None:
        """With nothing connected, nothing is configured - full stop."""
        from app.services import telegram_config

        with patch.object(telegram_config, "_cache", None):
            creds = telegram_config.credentials()
            self.assertEqual(creds["bot_token"], "")
            self.assertEqual(creds["chat_id"], "")
            self.assertFalse(telegram_config.configured())


class ReviewUrlTests(unittest.TestCase):
    def test_a_trailing_slash_does_not_double_up(self) -> None:
        with patch.object(tg, "settings", _settings(public_base_url="https://c.example/")):
            self.assertEqual(
                tg._console_review_url("run-abc"),
                "https://c.example/tasks/run-abc?tab=review",
            )


class PreviewPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _make(self, name: str) -> str:
        path = self.root / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return str(path)

    def test_mapping_shape_puts_poster_first(self) -> None:
        poster, s1, s2 = self._make("p.png"), self._make("s1.png"), self._make("s2.png")
        paths = tg._preview_paths(
            {"preview_paths": {"poster": poster, "slides": [s1, s2]}}
        )
        self.assertEqual([p.name for p in paths], ["p.png", "s1.png", "s2.png"])

    def test_list_shape_is_accepted(self) -> None:
        paths = tg._preview_paths(
            {"preview_paths": [self._make("a.png"), self._make("b.png")]}
        )
        self.assertEqual([p.name for p in paths], ["a.png", "b.png"])

    def test_cover_is_an_accepted_alias_for_poster(self) -> None:
        paths = tg._preview_paths({"preview_paths": {"cover": self._make("c.png")}})
        self.assertEqual([p.name for p in paths], ["c.png"])

    def test_missing_files_are_skipped_not_fatal(self) -> None:
        """A half-materialized preview should still send what exists."""
        paths = tg._preview_paths(
            {
                "preview_paths": {
                    "poster": str(self.root / "gone.png"),
                    "slides": [self._make("s1.png")],
                }
            }
        )
        self.assertEqual([p.name for p in paths], ["s1.png"])

    def test_no_previews_is_empty_not_an_error(self) -> None:
        self.assertEqual(tg._preview_paths({}), [])


class RequestEnvelopeTests(unittest.TestCase):
    """Telegram reports failure in the body, so the body must be checked."""

    def _client(self, handler) -> httpx.Client:
        return httpx.Client(
            base_url="https://api.telegram.org/botX",
            transport=httpx.MockTransport(handler),
        )

    def test_ok_false_raises_with_the_description(self) -> None:
        def handler(_request):
            return httpx.Response(
                400,
                json={"ok": False, "description": "chat not found", "error_code": 400},
            )

        with self._client(handler) as client:
            with self.assertRaises(RuntimeError) as ctx:
                tg._request(client, "sendMessage", data={})
        self.assertIn("chat not found", str(ctx.exception))
        self.assertIn("error_code=400", str(ctx.exception))

    def test_non_json_body_raises(self) -> None:
        def handler(_request):
            return httpx.Response(200, text="<html>gateway</html>")

        with self._client(handler) as client:
            with self.assertRaises(RuntimeError) as ctx:
                tg._request(client, "sendMessage", data={})
        self.assertIn("non-JSON", str(ctx.exception))

    def test_success_returns_the_result_object(self) -> None:
        def handler(_request):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

        with self._client(handler) as client:
            result = tg._request(client, "sendMessage", data={})
        self.assertEqual(result["message_id"], 42)


class SendReviewMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls: list[tuple[str, dict]] = []

    def _previews(self, count: int) -> list[str]:
        made = []
        for i in range(count):
            path = self.root / f"s{i}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            made.append(str(path))
        return made

    def _run(self, bundle: dict, round_no: int = 1) -> dict:
        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append((method, {"content_len": len(request.content)}))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def fake_client(*_args, **kwargs):
            return real_client(
                base_url=kwargs.get("base_url", ""), transport=transport
            )

        with patch.object(tg, "settings", _settings()), patch.object(
            tg.telegram_config, "credentials", lambda: _creds()
        ), patch.object(tg.httpx, "Client", fake_client):
            return tg.send_review_message("run-abc", bundle, round_no)

    def test_album_then_message(self) -> None:
        bundle = {
            "news_title": "A headline",
            "caption": "some caption",
            "preview_paths": self._previews(3),
        }
        result = self._run(bundle)
        self.assertEqual([m for m, _ in self.calls], ["sendMediaGroup", "sendMessage"])
        self.assertEqual(result["previews_sent"], 3)
        self.assertEqual(result["message_id"], "7")

    def test_album_is_capped_at_the_telegram_limit(self) -> None:
        """A full carousel exceeds the cap; it must trim, not fail."""
        bundle = {"news_title": "T", "preview_paths": self._previews(14)}
        result = self._run(bundle)
        self.assertEqual(result["previews_sent"], tg.MEDIA_GROUP_LIMIT)

    def test_no_previews_still_sends_the_decision_message(self) -> None:
        """Previews are a convenience; the Approve/Reject links are the point."""
        result = self._run({"news_title": "T", "preview_paths": []})
        self.assertEqual([m for m, _ in self.calls], ["sendMessage"])
        self.assertEqual(result["previews_sent"], 0)

    def test_title_falls_back_to_the_cover_when_news_title_is_absent(self) -> None:
        body = self._capture_message({"cover": {"title": "Cover title"}}, round_no=2)
        self.assertIn("Cover title", body["text"][0])
        self.assertIn("round 2", body["text"][0])

    def test_keyboard_is_one_button_to_the_console(self) -> None:
        """One button, not two.

        Approve and Reject both have to land on the same screen now that the
        decision is made in the console, and two buttons opening the identical
        page would imply the choice was made by tapping one of them.
        """
        body = self._capture_message({"news_title": "T"})
        keyboard = json.loads(body["reply_markup"][0])
        urls = [row[0]["url"] for row in keyboard["inline_keyboard"]]
        self.assertEqual(urls, ["https://console.example/tasks/run-abc?tab=review"])
        self.assertEqual(len(keyboard["inline_keyboard"]), 1)

    def _capture_message(self, bundle: dict, round_no: int = 1) -> dict:
        """Run a send and return the parsed form body of the sendMessage call."""
        from urllib.parse import parse_qs

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("sendMessage"):
                captured.update(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with patch.object(tg, "settings", _settings()), patch.object(
            tg.telegram_config, "credentials", lambda: _creds()
        ), patch.object(
            tg.httpx,
            "Client",
            lambda *a, **k: real_client(
                base_url="https://api.telegram.org/botX", transport=transport
            ),
        ):
            tg.send_review_message("run-abc", bundle, round_no)
        return captured


class ButtonFallbackTests(unittest.TestCase):
    """Telegram refuses non-public URLs in buttons; that must not fail the send.

    Discovered the hard way: with a localhost PUBLIC_BASE_URL the API answers
    "Bad Request: ... Wrong HTTP URL" and the whole review message fails -
    which would leave a paused run with nobody notified.
    """

    def test_classifies_local_and_public_urls(self) -> None:
        for url in (
            "http://localhost:8080/x",
            "http://127.0.0.1:8080/x",
            "http://[::1]:8080/x",
            "ftp://example.com/x",
            "",
        ):
            self.assertFalse(tg._buttons_supported(url), url)
        for url in ("https://abc.ngrok-free.app/x", "https://review.example/x"):
            self.assertTrue(tg._buttons_supported(url), url)

    def _send_with_base(self, base_url: str) -> dict:
        from urllib.parse import parse_qs

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("sendMessage"):
                captured.update(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        stub = SimpleNamespace(public_base_url=base_url)
        with patch.object(tg, "settings", stub), patch.object(
            tg.telegram_config, "credentials", lambda: _creds()
        ), patch.object(
            tg.httpx,
            "Client",
            lambda *a, **k: real_client(
                base_url="https://api.telegram.org/botX", transport=transport
            ),
        ):
            tg.send_review_message("run-abc", {"news_title": "T"}, 1)
        return captured

    def test_local_base_url_sends_the_link_in_the_body_not_a_button(self) -> None:
        body = self._send_with_base("http://localhost:8080")
        self.assertNotIn("reply_markup", body)
        self.assertIn(
            "http://localhost:8080/tasks/run-abc?tab=review", body["text"][0]
        )

    def test_public_base_url_still_uses_a_button(self) -> None:
        body = self._send_with_base("https://console.example")
        self.assertIn("reply_markup", body)
        keyboard = json.loads(body["reply_markup"][0])
        self.assertEqual(
            [row[0]["text"] for row in keyboard["inline_keyboard"]],
            ["REVIEW CAROUSEL"],
        )


class ReviewLinkTests(unittest.TestCase):
    """Where the review button sends people, and what that implies."""

    def test_it_points_at_the_console_review_screen(self) -> None:
        with patch.object(tg, "settings", _settings()):
            url = tg._console_review_url("run-1")
        self.assertEqual(url, "https://console.example/tasks/run-1?tab=review")

    def test_it_does_not_point_at_the_open_review_api(self) -> None:
        """The whole reason for the change.

        /review-api/review/<id>/approve needed no credentials, and approving
        auto-publishes to Instagram - so anyone who ever saw that URL could
        post as the brand. The button must not lead there any more.
        """
        with patch.object(tg, "settings", _settings()):
            url = tg._console_review_url("run-1")
        self.assertNotIn("/review-api", url)
        self.assertNotIn("/approve", url)
        self.assertNotIn("/reject", url)

    def test_without_public_base_url_there_is_no_tappable_button(self) -> None:
        """Better a visible plain link than one that looks configured.

        The old fallback guessed an origin from REVIEW_API_BASE_URL - a
        setting that named a service which no longer exists.
        """
        with patch.object(tg, "settings", _settings(public_base_url="")):
            url = tg._console_review_url("run-1")
        self.assertEqual(url, "/tasks/run-1?tab=review")
        self.assertFalse(tg._buttons_supported(url))

    def test_the_review_tab_is_requested_explicitly(self) -> None:
        """Landing on the trace would be one tap short of the decision."""
        with patch.object(tg, "settings", _settings()):
            self.assertIn("tab=review", tg._console_review_url("run-1"))


if __name__ == "__main__":
    unittest.main()
