"""When our own session cookie stops working.

``CLOCK_SKEW_LEEWAY_S`` exists for a real incident: this machine ran 2.5
seconds behind internet time, and PyJWT rejected freshly minted SUPABASE
tokens with "not yet valid (iat)" - the right component, the wrong clock, and
a message pointing at neither.

That allowance is correct where it was earned, in ``SupabaseJWTVerifier``,
which verifies a token minted on Supabase's machines against our clock.

It was also applied to ``read_session_token``, which verifies a token THIS
process minted moments earlier on THIS clock. There is no skew between a
machine and itself, so the only thing the allowance bought there was sixty
seconds of extra life for every expired session.

The two directions are not equally bad. Refusing slightly early logs someone
in again; accepting past expiry means a session outliving its own deadline.
So `exp` is now exact, while `iat`/`nbf` keep the tolerance - a clock that
jumps backwards mid-session must not log everybody out.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import jwt

from web_api.auth import (
    CLOCK_SKEW_LEEWAY_S,
    Identity,
    issue_session_token,
    read_session_token,
)

SECRET = "t" * 48
ALLOWED = Identity(email="a@b.co", subject="a@b.co", role="reviewer")


class ExpiryIsExactTests(unittest.TestCase):
    def test_a_live_token_is_accepted(self) -> None:
        token = issue_session_token(ALLOWED, ttl_s=3600, secret=SECRET)
        identity = read_session_token(token, secret=SECRET)
        assert identity is not None
        self.assertEqual(identity.email, "a@b.co")

    def test_a_token_expired_by_one_second_is_refused(self) -> None:
        token = issue_session_token(ALLOWED, ttl_s=-1, secret=SECRET)
        self.assertIsNone(read_session_token(token, secret=SECRET))

    def test_a_token_expired_inside_the_old_leeway_is_refused(self) -> None:
        """The exact regression: -10s used to be accepted, -61s refused."""
        token = issue_session_token(ALLOWED, ttl_s=-10, secret=SECRET)
        self.assertIsNone(read_session_token(token, secret=SECRET))

    def test_nothing_inside_the_old_window_survives(self) -> None:
        for ttl in (-1, -10, -30, -59, -60, -61):
            with self.subTest(expired_seconds_ago=abs(ttl)):
                token = issue_session_token(ALLOWED, ttl_s=ttl, secret=SECRET)
                self.assertIsNone(read_session_token(token, secret=SECRET))


class ClockDriftIsStillToleratedTests(unittest.TestCase):
    """The reason the leeway was introduced must keep working."""

    def test_a_token_issued_slightly_in_the_future_is_still_accepted(self) -> None:
        """A clock that jumps backwards must not log everyone out."""
        future = time.time() + (CLOCK_SKEW_LEEWAY_S / 2)
        with patch.object(__import__("web_api.auth", fromlist=["time"]).time,
                          "time", return_value=future):
            token = issue_session_token(ALLOWED, ttl_s=3600, secret=SECRET)
        self.assertIsNotNone(read_session_token(token, secret=SECRET))

    def test_a_forged_token_is_still_refused(self) -> None:
        token = issue_session_token(ALLOWED, ttl_s=3600, secret=SECRET)
        self.assertIsNone(read_session_token(token, secret="w" * 48))

    def test_a_tampered_token_is_still_refused(self) -> None:
        token = issue_session_token(ALLOWED, ttl_s=3600, secret=SECRET)
        self.assertIsNone(read_session_token(token + "x", secret=SECRET))

    def test_a_token_with_no_expiry_claim_is_refused(self) -> None:
        """An unbounded session must never be readable."""
        forever = jwt.encode(
            {"sub": "a@b.co", "email": "a@b.co", "role": "admin"},
            SECRET,
            algorithm="HS256",
        )
        self.assertIsNone(read_session_token(forever, secret=SECRET))


class SupabaseVerifierKeepsItsLeewayTests(unittest.TestCase):
    def test_the_constant_is_unchanged_for_the_cross_machine_path(self) -> None:
        """Removing it there would reintroduce the original sign-in failure."""
        self.assertEqual(CLOCK_SKEW_LEEWAY_S, 60)


if __name__ == "__main__":
    unittest.main()
