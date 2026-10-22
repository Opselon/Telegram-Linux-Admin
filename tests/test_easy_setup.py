"""End-to-end tests for the one-command (easy) setup.

The suite proves the single-command contract without needing a systemd host:

* argument parsing / credential discovery,
* secure-by-default config (fail-closed whitelist, deny_public),
* the exact systemd unit that gets installed (paths, account, health probe),
* the full ``easy_setup`` pipeline in a scratch directory,
* a real bot process answering ``/healthz`` and ``/readyz``,
* the wizard ("pro") path still working when driven from stdin,
* ``install.sh`` argument handling.

Tests that need the project's dependencies are skipped, never silently
passed, when those dependencies are missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP = REPO_ROOT / "scripts" / "setup.py"
INSTALL_SH = REPO_ROOT / "install.sh"
TOKEN = "123456:ABC-DEF1234567890"

# install.sh is a POSIX script: Windows runners either lack bash or check it
# out with CRLF, which changes what the shell sees. The script itself is the
# product for Linux hosts, so its checks live where they are meaningful.
requires_posix_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="install.sh is POSIX shell and needs a real bash",
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def listening_socket(port: int) -> socket.socket:
    """A listener configured the way the fixture's own probe works."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    return listener


def clean_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TLA_")
    }
    env.update(
        {
            "TLA_CONFIG_FILE": str(tmp_path / "config.json"),
            "TLA_DB_FILE": str(tmp_path / "database.db"),
            "TLA_AUDIT_DIR": str(tmp_path / "var" / "log"),
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    env.update(extra)
    return env


def run_setup(tmp_path: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    # setup.py writes UTF-8 (including the banner); decoding with the Windows
    # locale code page would fail, so pin the pipe encoding explicitly.
    return subprocess.run(
        [sys.executable, str(SETUP), *args],
        cwd=str(tmp_path),
        env=clean_env(tmp_path),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


@pytest.fixture
def setup_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Loads ``scripts/setup.py`` as a module against a scratch directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TLA_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "database.db"))
    monkeypatch.setenv("TLA_AUDIT_DIR", str(tmp_path / "var" / "log"))
    for name in ("TLA_BOT_TOKEN", "TLA_ADMIN_ID", "TLA_ADMIN_IDS"):
        monkeypatch.delenv(name, raising=False)

    # ``src.config`` owns a module-level singleton that reads the environment
    # at import; pop it so every test gets a fresh Config() pointed at *its*
    # scratch directory instead of a previous test's paths.
    sys.modules.pop("src.config", None)

    sys.path.insert(0, str(REPO_ROOT))
    namespace: dict[str, object] = {
        "__file__": str(SETUP),
        "__name__": "tla_setup_under_test",
        "__package__": None,
    }
    try:
        exec(compile(SETUP.read_text(encoding="utf-8"), str(SETUP), "exec"), namespace)
    except ImportError:
        sys.path.remove(str(REPO_ROOT))
        pytest.skip("project dependencies not importable (run pip install -e .)")
    yield namespace
    sys.path.remove(str(REPO_ROOT))


@pytest.fixture
def make_args(setup_module):
    """Builds an ``argparse`` namespace from the real parser's defaults."""
    defaults = vars(setup_module["build_arg_parser"]().parse_args([]))

    def make(**overrides) -> argparse.Namespace:
        values = dict(defaults)
        values.update(overrides)
        return argparse.Namespace(**values)

    return make


# ---------------------------------------------------------------------------
# Argument parsing and credential discovery
# ---------------------------------------------------------------------------

class TestArgumentParsing:
    def test_defaults_to_interactive_wizard(self, setup_module):
        parsed = setup_module["build_arg_parser"]().parse_args([])
        assert parsed.easy is False
        assert parsed.token is None
        assert parsed.no_service is False
        assert parsed.access_mode == "whitelist"

    def test_admin_id_is_repeatable_and_comma_separated(self, setup_module):
        parsed = setup_module["build_arg_parser"]().parse_args(
            ["--admin-id", "1", "--admin-id", "2,3"]
        )
        assert setup_module["collect_admin_ids"](parsed.admin_ids) == ["1", "2", "3"]

    def test_alias_user_id_and_telegram_id(self, setup_module):
        parsed = setup_module["build_arg_parser"]().parse_args(
            ["--user-id", "42", "--telegram-id", "43"]
        )
        assert setup_module["collect_admin_ids"](parsed.admin_ids) == ["42", "43"]

    def test_collect_admin_ids_ignores_blanks(self, setup_module):
        assert setup_module["collect_admin_ids"]([None, "", " ", "7"]) == ["7"]

    def test_env_admin_ids(self, setup_module, monkeypatch):
        monkeypatch.setenv("TLA_ADMIN_ID", "11, 12 13")
        assert setup_module["env_admin_ids"]() == ["11", "12", "13"]
        monkeypatch.delenv("TLA_ADMIN_ID")
        monkeypatch.setenv("TLA_ADMIN_IDS", "21")
        assert setup_module["env_admin_ids"]() == ["21"]

    def test_unknown_access_mode_is_rejected(self, setup_module):
        with pytest.raises(SystemExit):
            setup_module["build_arg_parser"]().parse_args(["--access-mode", "solo"])


# ---------------------------------------------------------------------------
# Port handling
# ---------------------------------------------------------------------------

class TestPortSelection:
    def test_chooses_preferred_port_when_free(self, setup_module):
        port = free_port()
        assert setup_module["choose_health_port"](port) == port

    def test_walks_upward_when_busy(self, setup_module):
        port = free_port()
        with listening_socket(port):
            assert setup_module["choose_health_port"](port) == port + 1

    def test_returns_none_when_range_exhausted(self, setup_module):
        base = free_port()
        listeners = []
        try:
            for candidate in range(base, base + setup_module["EASY_PORT_ATTEMPTS"]):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", candidate))
                    sock.listen(1)
                except OSError:
                    sock.close()  # occupied externally: counts as busy too
                    continue
                listeners.append(sock)
            assert setup_module["choose_health_port"](base) is None
        finally:
            for listener in listeners:
                listener.close()

    def test_port_in_use_detects_listener(self, setup_module):
        port = free_port()
        with listening_socket(port):
            assert setup_module["port_in_use"](port) is True
        assert setup_module["port_in_use"](port) is False


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------

class TestReadinessProbe:
    def _serve(self, status: int, body: str) -> tuple[int, threading.Event]:
        stop = threading.Event()
        port = free_port()

        def worker() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", port))
                server.listen(4)
                server.settimeout(0.2)
                while not stop.is_set():
                    try:
                        client, _ = server.accept()
                    except (socket.timeout, OSError):
                        continue
                    with client:
                        try:
                            client.recv(4096)
                        except OSError:
                            continue
                        client.sendall(
                            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode()
                        )

        threading.Thread(target=worker, daemon=True).start()
        time.sleep(0.2)
        return port, stop

    def test_accepts_ready_response(self, setup_module):
        port, stop = self._serve(200, '{"status": "ready"}')
        try:
            assert setup_module["wait_for_readiness"](port, timeout=5.0)[0] is True
        finally:
            stop.set()

    def test_times_out_on_not_ready(self, setup_module):
        port, stop = self._serve(503, '{"status": "not_ready"}')
        try:
            ready, detail = setup_module["wait_for_readiness"](port, timeout=2.0)
        finally:
            stop.set()
        assert ready is False
        assert "not_ready" in detail

    def test_survives_connection_refused(self, setup_module):
        ready, _ = setup_module["wait_for_readiness"](free_port(), timeout=1.5)
        assert ready is False


# ---------------------------------------------------------------------------
# The easy pipeline
# ---------------------------------------------------------------------------

class TestEasySetup:
    ADMIN = 5094837833

    def test_writes_fail_closed_config(self, setup_module, make_args, tmp_path):
        exit_code = setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=[str(self.ADMIN)], no_service=True, no_cron=True)
        )
        assert exit_code == 0
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["telegram_token"] == TOKEN
        assert data["whitelisted_users"] == [self.ADMIN]
        assert data["access_mode"] == "whitelist"
        assert data["destination_policy"] == "deny_public"
        assert isinstance(data["health_port"], int)

    def test_config_is_mode_0600(self, setup_module, make_args, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX file modes are not implemented on Windows")
        setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=[str(self.ADMIN)], no_service=True, no_cron=True)
        )
        assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600

    def test_creates_database(self, setup_module, make_args, tmp_path):
        setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=[str(self.ADMIN)], no_service=True, no_cron=True)
        )
        assert (tmp_path / "database.db").exists()

    def test_keeps_allowed_destinations_for_allowlist(self, setup_module, make_args, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "telegram_token": TOKEN,
                    "whitelisted_users": [self.ADMIN],
                    "allowed_destinations": ["10.0.0.0/8"],
                }
            ),
            encoding="utf-8",
        )
        setup_module["easy_setup"](
            make_args(
                token=TOKEN,
                admin_ids=[str(self.ADMIN)],
                destination_policy="allowlist",
                no_service=True,
                no_cron=True,
            )
        )
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["destination_policy"] == "allowlist"
        assert data["allowed_destinations"] == ["10.0.0.0/8"]

    def test_merges_admins_and_keeps_token_across_runs(self, setup_module, make_args, tmp_path):
        setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=["111"], no_service=True, no_cron=True)
        )
        setup_module["easy_setup"](
            make_args(token="654321:NEW-SECRET-VALUE", admin_ids=["222"], no_service=True, no_cron=True)
        )
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["whitelisted_users"] == [111, 222]
        assert data["telegram_token"] == "654321:NEW-SECRET-VALUE"

    def test_json_summary_hides_the_token(self, setup_module, make_args, capsys):
        setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=["77"], no_service=True, no_cron=True, json=True)
        )
        captured = capsys.readouterr()
        lines = [line for line in captured.out.splitlines() if line.strip()]
        # stdout carries exactly one machine-readable line; the banner is on stderr
        assert len(lines) == 1, lines
        payload = json.loads(lines[0])
        assert payload["ok"] is True
        assert payload["admin_ids"] == ["77"]
        assert TOKEN not in json.dumps(payload)
        assert payload["readiness"] is False
        assert "🎉" not in captured.out

    def test_preserves_unknown_user_keys(self, setup_module, make_args, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "telegram_token": TOKEN,
                    "whitelisted_users": [self.ADMIN],
                    "plan": "premium",
                    "custom_note": "hands off",
                }
            ),
            encoding="utf-8",
        )
        setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=[str(self.ADMIN)], no_service=True, no_cron=True)
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["plan"] == "premium"
        assert data["custom_note"] == "hands off"

    def test_missing_admin_id_is_fatal(self, setup_module, make_args, tmp_path):
        assert setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=[], no_service=True, no_cron=True)
        ) == 2
        assert not (tmp_path / "config.json").exists()

    def test_malformed_token_is_fatal(self, setup_module, make_args, tmp_path):
        assert setup_module["easy_setup"](
            make_args(token="not-a-token", admin_ids=["12345"], no_service=True, no_cron=True)
        ) == 2
        assert not (tmp_path / "config.json").exists()

    def test_non_numeric_admin_id_is_fatal(self, setup_module, make_args):
        assert setup_module["easy_setup"](
            make_args(token=TOKEN, admin_ids=["abc"], no_service=True, no_cron=True)
        ) == 2

    def test_no_free_port_is_fatal(self, setup_module, make_args, monkeypatch):
        # Every port in the scan window is reported busy. Simulating that with
        # real listeners is racy (anything else on the host can take one of the
        # 20 consecutive ports), so the probe itself is stubbed.
        monkeypatch.setitem(setup_module, "port_in_use", lambda port: True)
        exit_code = setup_module["easy_setup"](
            make_args(
                token=TOKEN,
                admin_ids=["12345"],
                health_port=free_port(),
                no_service=True,
                no_cron=True,
            )
        )
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Systemd unit
# ---------------------------------------------------------------------------

class TestServiceUnit:
    def test_unit_pins_paths_and_health_endpoint(self, setup_module, tmp_path):
        unit = setup_module["build_service_unit"](health_port=8081)
        assert "ExecStart=" in unit
        assert f'Environment="TLA_CONFIG_FILE={tmp_path / "config.json"}"' in unit
        assert f'Environment="TLA_DB_FILE={tmp_path / "database.db"}"' in unit
        assert 'Environment="TLA_HEALTH_PORT=8081"' in unit
        assert 'Environment="TLA_HEALTH_HOST=127.0.0.1"' in unit
        assert "Restart=always" in unit
        assert "WantedBy=multi-user.target" in unit

    def test_unit_runs_as_sudo_user(self, setup_module, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        unit = setup_module["build_service_unit"](health_port=8081)
        assert re.search(r"^User=alice$", unit, re.M)
        assert re.search(r"^Group=alice$", unit, re.M)

    def test_unit_without_health_port_has_no_endpoint(self, setup_module):
        assert "TLA_HEALTH_PORT" not in setup_module["build_service_unit"](health_port=None)

    def test_unit_escapes_percent_signs(self, setup_module, monkeypatch, tmp_path):
        odd = tmp_path / "100% complete"
        odd.mkdir()
        monkeypatch.chdir(odd)
        assert "100%% complete" in setup_module["build_service_unit"](health_port=None)


# ---------------------------------------------------------------------------
# CLI contract (the exact path install.sh and curl users run)
# ---------------------------------------------------------------------------

class TestCli:
    def test_easy_cli_exits_zero_and_writes_config(self, tmp_path):
        result = run_setup(
            tmp_path,
            "--easy", "--no-service", "--no-cron", "--no-start", "--json",
            "--token", TOKEN, "--admin-id", "5094837833",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(
            [line for line in result.stdout.splitlines() if line.startswith("{")][-1]
        )
        assert payload["ok"] is True
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["telegram_token"] == TOKEN
        assert data["whitelisted_users"] == [5094837833]

    def test_cli_credentials_can_come_from_environment(self, tmp_path):
        env = clean_env(tmp_path, TLA_BOT_TOKEN=TOKEN, TLA_ADMIN_ID="4242")
        result = subprocess.run(
            [sys.executable, str(SETUP), "--easy", "--no-service", "--no-cron", "--no-start"],
            cwd=str(tmp_path), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["whitelisted_users"] == [4242]

    def test_cli_rejects_missing_admin_with_exit_2(self, tmp_path):
        result = run_setup(
            tmp_path, "--easy", "--no-service", "--no-cron", "--token", TOKEN
        )
        assert result.returncode == 2
        assert "admin" in (result.stdout + result.stderr).lower()

    def test_interactive_wizard_still_configures(self, tmp_path):
        """The pro path: the original guided wizard, driven from stdin."""
        result = run_setup(
            tmp_path,
            stdin=f"{TOKEN}\n777\nn\nn\n\n",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["whitelisted_users"] == [777]
        assert data["telegram_token"] == TOKEN


@requires_posix_bash
class TestInstallScript:
    def test_bash_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_help_documents_the_one_liner(self):
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--help"], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "--token" in result.stdout
        assert "--admin-id" in result.stdout
        assert "curl" in result.stdout

    def test_unknown_option_fails(self):
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--nonsense"], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "unknown option" in result.stderr

    def test_option_without_value_fails(self):
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--token"], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "requires a value" in result.stderr


# ---------------------------------------------------------------------------
# Real bot process
# ---------------------------------------------------------------------------

class TestRealBotProcess:
    """The real entry point, the real readiness payload, the real HTTP server.

    Needs a live bot token (``TLA_E2E_TOKEN``): Telegram rejects the fake
    tokens used elsewhere, so this case is skipped rather than faked. The
    runner script ``scripts/e2e_setup.sh`` sets the variable.
    """

    ADMIN = int(os.environ.get("TLA_E2E_ADMIN", "123456789"))

    def test_bot_answers_readyz(self, tmp_path):
        live_token = os.environ.get("TLA_E2E_TOKEN", "").strip()
        if not live_token:
            pytest.skip("set TLA_E2E_TOKEN to exercise the live bot process")
        try:
            subprocess.run(
                [sys.executable, "-c", "import telegram, asyncssh, cryptography"],
                check=True, capture_output=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            pytest.skip("dependencies unavailable (run pip install -e .)")

        port = free_port()
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "telegram_token": live_token,
                    "whitelisted_users": [self.ADMIN],
                    "access_mode": "whitelist",
                    "destination_policy": "deny_public",
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "var" / "log").mkdir(parents=True, exist_ok=True)
        env = clean_env(
            tmp_path,
            TLA_HEALTH_PORT=str(port),
            TLA_HEALTH_HOST="127.0.0.1",
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "src.main"],
            cwd=str(tmp_path),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            deadline = time.monotonic() + 60
            ready_body = ""
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    tail = process.stdout.read() if process.stdout else ""
                    pytest.fail(f"bot exited early:\n{tail}")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/readyz", timeout=3
                    ) as response:
                        ready_body = response.read().decode()
                        break
                except Exception:
                    time.sleep(1)
            else:
                pytest.fail("bot never answered /readyz")

            assert '"database": true' in ready_body
            assert '"token_configured": true' in ready_body
            assert '"audit_writable": true' in ready_body

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=3
            ) as response:
                assert response.status == 200
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


# ---------------------------------------------------------------------------
# Backwards compatibility of the wizard entry point
# ---------------------------------------------------------------------------

class TestWizardRouting:
    def test_no_credentials_routes_to_wizard(self, setup_module, tmp_path):
        (tmp_path / "config.json").unlink(missing_ok=True)
        database = mock.Mock()
        wizard = mock.Mock()
        menu = mock.Mock()
        with mock.patch.dict(setup_module, {
            "initialize_database": database,
            "first_time_setup": wizard,
            "management_menu": menu,
        }):
            assert setup_module["main"]([]) == 0
        database.assert_called_once()
        wizard.assert_called_once()
        menu.assert_not_called()

    def test_existing_config_routes_to_management_menu(self, setup_module, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"telegram_token": TOKEN, "whitelisted_users": [12345]}),
            encoding="utf-8",
        )
        setup_module["config"].load_config()  # the CLI reads it at import time
        wizard = mock.Mock()
        menu = mock.Mock()
        with mock.patch.dict(setup_module, {
            "first_time_setup": wizard,
            "management_menu": menu,
        }):
            assert setup_module["main"]([]) == 0
        wizard.assert_not_called()
        menu.assert_called_once()
