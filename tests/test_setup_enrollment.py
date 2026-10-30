# PURPOSE: Prove the one-time admin enrollment (setup PIN) behaves as
#          documented: single-use, constant-time compared, throttled, and
#          disabled when no PIN is configured.
# OWNER: security lane, 3.5.
# CONSUMES: src.access_control.SetupEnrollment.
# PROVIDES: tests/test_setup_enrollment.py.
# INVARIANTS: a wrong PIN never raises; the 5th failure locks the user out;
#          a successful claim burns the PIN for everyone; reset() clears
#          lockouts.
# EXTEND: add enrollment edge cases here.

from __future__ import annotations

import pytest

from src.access_control import SetupEnrollment


class TestSetupEnrollment:
    def test_disabled_when_pin_empty(self) -> None:
        enrollment = SetupEnrollment("")
        assert enrollment.enabled is False
        ok, key = enrollment.verify(42, "1234")
        assert ok is False
        assert key == "setup_disabled"

    def test_correct_pin_enrolls_and_burns(self) -> None:
        enrollment = SetupEnrollment("7355608")
        assert enrollment.enabled is True
        ok, key = enrollment.verify(42, "7355608")
        assert ok is True
        assert key == "setup_ok"
        # PIN is single-use: the same (or any) PIN no longer works.
        ok_again, key_again = enrollment.verify(43, "7355608")
        assert ok_again is False
        assert key_again == "setup_disabled"
        assert enrollment.enabled is False

    def test_wrong_pin_reports_wrong_key(self) -> None:
        enrollment = SetupEnrollment("123456")
        ok, key = enrollment.verify(1, "654321")
        assert ok is False
        assert key == "setup_wrong_pin"

    def test_five_failures_lock_out(self) -> None:
        enrollment = SetupEnrollment("123456")
        for _ in range(SetupEnrollment.MAX_ATTEMPTS):
            ok, key = enrollment.verify(9, "wrong")
            assert ok is False
        ok, key = enrollment.verify(9, "123456")
        assert ok is False
        assert key == "setup_locked_out"

    def test_lockout_is_per_user(self) -> None:
        enrollment = SetupEnrollment("123456")
        for _ in range(SetupEnrollment.MAX_ATTEMPTS):
            enrollment.verify(9, "wrong")
        # A different user is not affected by user 9's lockout.
        ok, key = enrollment.verify(10, "123456")
        assert ok is True
        assert key == "setup_ok"

    def test_whitespace_padded_pin_is_accepted(self) -> None:
        enrollment = SetupEnrollment("  424242  ")
        ok, _ = enrollment.verify(5, "424242")
        assert ok is True

    def test_reset_clears_lockout(self) -> None:
        enrollment = SetupEnrollment("123456")
        for _ in range(SetupEnrollment.MAX_ATTEMPTS):
            enrollment.verify(9, "wrong")
        enrollment.reset()
        ok, key = enrollment.verify(9, "123456")
        assert ok is True
        assert key == "setup_ok"

    def test_none_user_is_rejected(self) -> None:
        enrollment = SetupEnrollment("123456")
        ok, key = enrollment.verify(None, "123456")
        assert ok is False
        assert key == "setup_not_available"

    @pytest.mark.parametrize("pin", ["1234", "0", "9" * 64])
    def test_varied_pin_lengths_work(self, pin: str) -> None:
        enrollment = SetupEnrollment(pin)
        ok, _ = enrollment.verify(7, pin)
        assert ok is True
