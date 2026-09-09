"""Credentials the console stores are encrypted, and there is no way around it.

The Telegram bot token is a bearer credential: whoever holds it can post as
your bot. It lives in ``app_config``, an ordinary Postgres table, so a backup
file or a support session would otherwise hand it over in the clear.

Fernet rather than a bare HMAC, deliberately: an HMAC is one-way and the
dispatcher has to send the REAL token to Telegram on every review. Fernet is
AES-128-CBC for confidentiality with an HMAC-SHA256 over the ciphertext for
integrity - encryption and an HMAC, from one key.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import secret_box

KEY_A = "P0K_U2E4P9x9LvHuQd0jceOVdJaURoBM2m56y7-4O7I="


def _with_key(key: str):
    return patch.object(secret_box, "settings", SimpleNamespace(secrets_key=key))


class RoundTripTests(unittest.TestCase):
    def test_a_value_survives_encryption(self) -> None:
        with _with_key(KEY_A):
            self.assertEqual(secret_box.decrypt(secret_box.encrypt("hello")), "hello")

    def test_the_ciphertext_does_not_contain_the_secret(self) -> None:
        """The whole point: a database dump must not read as the token."""
        with _with_key(KEY_A):
            stored = secret_box.encrypt("111111111:sensitive-part")
        self.assertNotIn("sensitive-part", stored)
        self.assertTrue(stored.startswith(secret_box.PREFIX))

    def test_encrypting_twice_gives_different_ciphertext(self) -> None:
        """Fernet carries a random IV, so equal tokens are not equal at rest."""
        with _with_key(KEY_A):
            self.assertNotEqual(secret_box.encrypt("same"), secret_box.encrypt("same"))

    def test_empty_in_empty_out(self) -> None:
        with _with_key(KEY_A):
            self.assertEqual(secret_box.encrypt(""), "")
            self.assertEqual(secret_box.decrypt(""), "")
            self.assertEqual(secret_box.decrypt(None), "")


class KeyHandlingTests(unittest.TestCase):
    def test_a_different_key_cannot_read_it(self) -> None:
        with _with_key(KEY_A):
            stored = secret_box.encrypt("token")
        with _with_key(secret_box.generate_key()):
            # Not an exception: a credential that cannot be read is
            # functionally absent, and every page that merely asks "is
            # Telegram set up?" must keep working.
            self.assertEqual(secret_box.decrypt(stored), "")

    def test_no_key_means_nothing_can_be_stored(self) -> None:
        """Refuse, rather than quietly writing plaintext instead."""
        with _with_key(""):
            self.assertFalse(secret_box.configured())
            with self.assertRaises(secret_box.SecretsNotConfigured) as ctx:
                secret_box.encrypt("token")
        self.assertIn("SECRETS_KEY", str(ctx.exception))

    def test_a_junk_key_is_rejected_with_an_explanation(self) -> None:
        with _with_key("not-a-real-fernet-key"):
            with self.assertRaises(secret_box.SecretsNotConfigured) as ctx:
                secret_box.encrypt("token")
        self.assertIn("Fernet", str(ctx.exception))

    def test_generated_keys_work_and_differ(self) -> None:
        first, second = secret_box.generate_key(), secret_box.generate_key()
        self.assertNotEqual(first, second)
        with _with_key(first):
            self.assertEqual(secret_box.decrypt(secret_box.encrypt("x")), "x")


class UnencryptedValueTests(unittest.TestCase):
    def test_a_plaintext_value_is_ignored_not_trusted(self) -> None:
        """A hand-edited row must not become a working credential.

        Without the prefix check, someone pasting a raw token into app_config
        would get a system that works - and silently keeps a plaintext bearer
        credential in the database, which is exactly what this removed.
        """
        with _with_key(KEY_A):
            self.assertEqual(secret_box.decrypt("111111111:raw-token"), "")


class NoEnvironmentFallbackTests(unittest.TestCase):
    """The console is the only source of Telegram credentials."""

    def test_settings_has_no_telegram_token(self) -> None:
        from app.config import settings

        self.assertFalse(hasattr(settings, "telegram_bot_token"))
        self.assertFalse(hasattr(settings, "telegram_chat_id"))

    def test_credentials_are_empty_when_nothing_is_connected(self) -> None:
        from app.services import telegram_config

        with patch.object(telegram_config, "_cache", None):
            creds = telegram_config.credentials()
            self.assertEqual(creds["bot_token"], "")
            self.assertEqual(creds["chat_id"], "")
            self.assertFalse(telegram_config.configured())
            self.assertEqual(telegram_config.source(), "unset")

    def test_the_tool_names_the_console_not_a_dotenv_variable(self) -> None:
        from app.tools import telegram_tools

        with patch.object(telegram_config_module(), "_cache", None):
            with self.assertRaises(RuntimeError) as ctx:
                telegram_tools._api_base()
        message = str(ctx.exception)
        self.assertIn("Profile", message)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", message)


def telegram_config_module():
    from app.services import telegram_config

    return telegram_config


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
