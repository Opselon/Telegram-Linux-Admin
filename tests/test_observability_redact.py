"""Tests for :func:`src.observability.redact`.

Each secret shape that reaches a log (Telegram bot token, ``password=``
pairs, ``Authorization``/``Bearer`` headers, private-key blocks) must be
scrubbed, while short ordinary command output must survive byte-identical —
over-redaction would destroy the audit trail.
"""

from __future__ import annotations

import pytest

from src.observability import redact

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # obvious fake token


class TestBotTokens:
    def test_plain_bot_token_is_scrubbed(self):
        assert redact(f"bot {BOT_TOKEN} online") == "bot 123456789:[REDACTED] online"

    def test_token_secret_part_fully_removed(self):
        scrubbed = redact(BOT_TOKEN)
        assert scrubbed.startswith("123456789:")
        assert scrubbed.endswith("[REDACTED]")
        assert "AAHdq" not in scrubbed

    def test_10_digit_token_prefix(self):
        scrubbed = redact("sleep token 9876543210:TESTTOKENx12yz4567890123456 = ok")
        assert "TESTTOKEN" not in scrubbed

    def test_token_embedded_in_url(self):
        scrubbed = redact(
            "https://api.example.com/123456789:TESTTOKENabcdefghij/sendMessage"
        )
        assert "TESTTOKEN" not in scrubbed

    def test_six_digit_prefixed_pair_without_value_is_left_alone(self):
        # ``12:34`` style short text must not be corrupted.
        assert redact("ratio 12:34 seconds 45:60") == "ratio 12:34 seconds 45:60"


class TestPasswordAssignments:
    def test_password_equals(self):
        assert "hunter2" not in redact("login --password=hunter2")

    def test_password_colon_quoted(self):
        assert redact("password: 's3cr3t!'") == "password: [REDACTED]"

    def test_password_double_quoted(self):
        assert "'hawk wings'" not in redact('password="hawk wings"')

    def test_db_password(self):
        assert "hunter2" not in redact("PGPASSWORD=hunter2 psql -h example.com")
        assert "pg_pass" not in redact("--pg-pass=hunter2")

    def test_user_and_password_alive(self):
        scrubbed = redact("user=bob password=hunter2 host=example.com")
        assert "bob" in scrubbed
        assert "example.com" in scrubbed
        assert "hunter2" not in scrubbed


class TestAuthHeaders:
    def test_authorization_bearer(self):
        scrubbed = redact("Authorization: Bearer abcDEF1234567890abcDEF1234")
        assert "abcDEF1234567890abcDEF1234" not in scrubbed
        assert "Authorization" in scrubbed

    def test_authorization_basic_quoted(self):
        scrubbed = redact("-H 'Authorization: Basic dXNlcjpwYXNz'")
        assert "dXNlcjpwYXNz" not in scrubbed
        assert "Basic" in scrubbed

    def test_authorization_header_in_json(self):
        scrubbed = redact('{"Authorization": "Bearer abcDEF1234567890abcDEF"}')
        assert "abcDEF1234567890abcDEF" not in scrubbed

    def test_standalone_bearer_token(self):
        scrubbed = redact("curl -H bearer Zx89KkQ2TOKENPAIR11 https://example.com")
        assert "Zx89KkQ2TOKENPAIR11" not in scrubbed

    def test_authorization_keep_url_intact(self):
        scrubbed = redact("-H 'Authorization: Basic dXNlcjpwYXNz' https://example.com")
        assert "https://example.com" in scrubbed


class TestKeyBlocks:
    def test_rsa_private_key_block(self):
        blob = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA4CzW\nT0KlZqX\n"
            "-----END RSA PRIVATE KEY-----"
        )
        scrubbed = redact(f"key material:\n{blob}\nend")
        assert "MIIEow" not in scrubbed
        assert "-----BEGIN PRIVATE KEY-----[REDACTED]" in scrubbed

    def test_pem_and_openssh_blocks(self):
        for header in ("PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY BLOCK"):
            blob = (
                f"-----BEGIN {header}-----\nA\nB\n"
                f"-----END {header}-----"
            )
            assert "A\nB\n" not in redact(blob)


class TestOrdinaryTextUntouched:
    @pytest.mark.parametrize(
        "value",
        [
            "systemctl restart nginx",
            "ls -la /var/log",
            "uptime: 12 days",
            "docker ps -a",
            "x",
            "",
            "ratio 3:2 45:60",
            "drwxr-xr-x 2 root root 4096 .",
            "fps=60:7 stalled",
        ],
    )
    def test_ordinary_output_survives(self, value):
        assert redact(value) == value

    def test_short_words_with_colons_not_touched(self):
        assert redact("a:b c:d") == "a:b c:d"


class TestIdempotenceAndCompounds:
    def test_redaction_is_idempotent(self):
        once = redact(f"password=hunter2 token {BOT_TOKEN}")
        twice = redact(once)
        assert once == twice
        assert "hunter2" not in twice

    def test_multiple_secrets_in_one_line(self):
        scrubbed = redact(f"{BOT_TOKEN} password=hunter2 Bearer xyzTOKENvv1/Pairs0")
        assert "AAHdq" not in scrubbed
        assert "hunter2" not in scrubbed
        assert "xyzTOKENvv1" not in scrubbed

    def test_case_insensitive_matching(self):
        scrubbed = redact("PASSWORD=hunter2 TokenFmt")
        assert "hunter2" not in scrubbed
