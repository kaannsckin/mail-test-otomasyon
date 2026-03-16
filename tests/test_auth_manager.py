"""
test_auth_manager.py — Auth/MFA yöneticisi unit testleri.
"""

import threading
import time
import pytest
from auth_manager import (
    generate_totp,
    totp_remaining_seconds,
    MFAChallenge,
    MFAManager,
)


# ── TOTP testleri ──────────────────────────────────────────────
class TestTOTP:
    """generate_totp() ve totp_remaining_seconds() testleri."""

    # RFC 6238 test vector — secret = "12345678901234567890" base32 = GEZDGNBVGY3TQOJQ
    KNOWN_SECRET = "GEZDGNBVGY3TQOJQ"

    def test_generates_six_digit_code(self):
        code = generate_totp(self.KNOWN_SECRET)
        assert len(code) == 6
        assert code.isdigit()

    def test_same_secret_same_code_within_interval(self):
        code1 = generate_totp(self.KNOWN_SECRET)
        code2 = generate_totp(self.KNOWN_SECRET)
        assert code1 == code2

    def test_handles_spaces_in_secret(self):
        code = generate_totp("GEZD GNBV GY3T QOJQ")
        assert len(code) == 6

    def test_handles_lowercase_secret(self):
        code = generate_totp("gezdgnbvgy3tqojq")
        assert len(code) == 6

    def test_invalid_secret_returns_empty(self):
        code = generate_totp("!!!INVALID!!!")
        assert code == ""

    def test_remaining_seconds_in_range(self):
        remaining = totp_remaining_seconds()
        assert 0 <= remaining <= 30


# ── MFAChallenge dataclass ─────────────────────────────────────
class TestMFAChallenge:
    def test_defaults(self):
        c = MFAChallenge(
            server_key="ems",
            server_label="EMS",
            method="totp",
            prompt="Enter code",
        )
        assert c.code is None
        assert c.resolved is False
        assert c.cancelled is False


# ── MFAManager ─────────────────────────────────────────────────
class TestMFAManager:
    """MFAManager thread-safe akış testleri."""

    def test_no_pending_initially(self):
        mgr = MFAManager()
        assert mgr.get_pending() is None

    def test_auto_totp_returns_code(self):
        mgr = MFAManager()
        code = mgr.mfa_challenge("ems", "EMS", method="totp", totp_secret="GEZDGNBVGY3TQOJQ")
        assert code is not None
        assert len(code) == 6

    def test_submit_code_resolves_challenge(self):
        mgr = MFAManager()
        result = [None]

        def requester():
            result[0] = mgr.mfa_challenge("ems", "EMS", method="sms", totp_secret="")

        t = threading.Thread(target=requester)
        t.start()

        # Challenge oluşmasını bekle
        for _ in range(50):
            if mgr.get_pending() is not None:
                break
            time.sleep(0.05)

        pending = mgr.get_pending()
        assert pending is not None
        assert pending["server_key"] == "ems"
        assert pending["method"] == "sms"

        mgr.submit_code("123456")
        t.join(timeout=5)

        assert result[0] == "123456"

    def test_cancel_returns_none(self):
        mgr = MFAManager()
        result = [None]

        def requester():
            result[0] = mgr.mfa_challenge("gmail", "Gmail", method="totp", totp_secret="")

        t = threading.Thread(target=requester)
        t.start()

        for _ in range(50):
            if mgr.get_pending() is not None:
                break
            time.sleep(0.05)

        mgr.cancel()
        t.join(timeout=5)

        assert result[0] is None

    def test_submit_without_challenge_returns_false(self):
        mgr = MFAManager()
        assert mgr.submit_code("123456") is False

    def test_cancel_without_challenge_returns_false(self):
        mgr = MFAManager()
        assert mgr.cancel() is False
