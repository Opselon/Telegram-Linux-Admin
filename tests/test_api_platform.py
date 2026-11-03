"""End-to-end API tests over the real FastAPI app (TestClient).

Covers: setup/bootstrap, login lockout, CSRF enforcement, RBAC isolation,
command policy (blocked/confirm/replay), jobs, backups, Bale link codes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.services import store
from src.services.migrations import run_migrations


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "api.db"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "keys.key"))
    monkeypatch.setenv("TLA_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TLA_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("TLA_ACCESS_MODE", "open")
    monkeypatch.setenv("TLA_SETUP_TOKEN", "test-setup-token")
    from src import database

    database.close_db_connection()
    run_migrations(snapshot=False)
    with TestClient(create_app()) as c:
        yield c
    database.close_db_connection()


def _setup_owner(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "root", "password": "S3curePass!2026",
              "setup_token": "test-setup-token"},
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": client.cookies.get("tla_csrf")}


def _create_server(client: TestClient, headers: dict, alias: str = "srv1") -> int:
    response = client.post(
        "/api/v1/servers",
        json={"alias": alias, "hostname": "example.invalid", "user": "root", "password": "pw"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- bootstrap / auth ----------------------------------------------------------

def test_setup_requires_token_when_configured(client):
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "root", "password": "S3curePass!2026", "setup_token": "wrong"},
    )
    assert response.status_code == 403


def test_setup_is_one_time(client):
    _setup_owner(client)
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "other", "password": "S3curePass!2026", "setup_token": "test-setup-token"},
    )
    assert response.status_code == 403


def test_weak_password_rejected_at_setup(client):
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "root", "password": "short", "setup_token": "test-setup-token"},
    )
    # 422 (schema min length) or 400 (strength check) both refuse the password.
    assert response.status_code in (400, 422)


def test_login_logout_roundtrip(client):
    _setup_owner(client)
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get("tla_csrf")})
    response = client.post("/api/v1/auth/login",
                           json={"username": "root", "password": "S3curePass!2026"})
    assert response.status_code == 200
    assert response.json()["role"] == "owner"
    assert client.get("/api/v1/auth/me").status_code == 200
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get("tla_csrf")})
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_wrong_password_is_uniform(client):
    _setup_owner(client)
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get("tla_csrf")})
    for username in ("root", "no-such-user"):
        response = client.post("/api/v1/auth/login",
                               json={"username": username, "password": "wrong"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_lockout_after_repeated_failures(client):
    _setup_owner(client)
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get("tla_csrf")})
    code = None
    for _ in range(5):
        response = client.post("/api/v1/auth/login",
                               json={"username": "root", "password": "wrong"})
        code = response.json()["error"]["code"]
    assert code == "login_locked_out"
    # Even the correct password is refused while locked out.
    response = client.post("/api/v1/auth/login",
                           json={"username": "root", "password": "S3curePass!2026"})
    assert response.status_code == 429


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/v1/servers").status_code == 401
    assert client.get("/api/v1/jobs").status_code == 401


# --- CSRF ----------------------------------------------------------------------

def test_mutations_require_csrf(client):
    headers = _setup_owner(client)
    del headers["X-CSRF-Token"]
    assert client.post("/api/v1/servers", json={"alias": "x", "hostname": "h", "user": "u"}).status_code == 403
    assert client.delete("/api/v1/servers/1").status_code == 403
    # GETs are exempt.
    assert client.get("/api/v1/servers").status_code == 200


def test_csrf_token_mismatch_rejected(client):
    _setup_owner(client)
    response = client.post("/api/v1/servers", json={"alias": "x", "hostname": "h", "user": "u"},
                           headers={"X-CSRF-Token": "forged"})
    assert response.status_code == 403


# --- servers + RBAC ----------------------------------------------------------------

def test_server_crud_and_ownership(client):
    headers = _setup_owner(client)
    server_id = _create_server(client, headers)
    # Owner flag present, secrets absent.
    body = client.get(f"/api/v1/servers/{server_id}").json()
    assert body["owner"] is True
    assert "password" not in body
    # Rename, update, delete.
    assert client.post(f"/api/v1/servers/{server_id}/rename", json={"alias": "renamed"},
                       headers=headers).status_code == 200
    assert client.patch(f"/api/v1/servers/{server_id}", json={"port": 2222},
                        headers=headers).status_code == 200
    assert client.get(f"/api/v1/servers/{server_id}").json()["port"] == 2222
    assert client.delete(f"/api/v1/servers/{server_id}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/servers/{server_id}").status_code == 404


def test_low_privilege_user_is_isolated(client, tmp_path):
    headers = _setup_owner(client)
    server_id = _create_server(client, headers)
    # Owner creates an operator account.
    response = client.post("/api/v1/users", json={
        "username": "op", "password": "OperatorPass!1", "role": "operator"},
        headers=headers)
    assert response.status_code == 201
    # Log in as the operator in a *separate* client (fresh cookies).
    from fastapi.testclient import TestClient

    with TestClient(client.app) as op_client:
        login = op_client.post("/api/v1/auth/login",
                               json={"username": "op", "password": "OperatorPass!1"})
        assert login.status_code == 200
        assert login.json()["role"] == "operator"
        op_headers = {"X-CSRF-Token": op_client.cookies.get("tla_csrf")}
        # Operator cannot see the owner's server; delete is refused (404
        # ownership mask or 403 permission — both fail closed).
        assert op_client.get(f"/api/v1/servers/{server_id}").status_code == 404
        assert op_client.delete(f"/api/v1/servers/{server_id}", headers=op_headers).status_code in (403, 404)
        # Operator cannot manage users.
        assert op_client.get("/api/v1/users").status_code == 403
        # Operator can create their own server.
        assert op_client.post("/api/v1/servers",
                              json={"alias": "op1", "hostname": "h", "user": "u"},
                              headers=op_headers).status_code == 201


def test_server_validation_rejects_bad_input(client):
    headers = _setup_owner(client)
    assert client.post("/api/v1/servers", json={"alias": "bad alias!", "hostname": "h", "user": "u"},
                       headers=headers).status_code == 422
    assert client.post("/api/v1/servers", json={"alias": "ok", "hostname": "h", "user": "u", "port": 70000},
                       headers=headers).status_code == 422


# --- command policy over HTTP ---------------------------------------------------------

def test_blocked_command_returns_403(client):
    headers = _setup_owner(client)
    server_id = _create_server(client, headers)
    response = client.post("/api/v1/commands",
                           json={"server_id": server_id, "command": "mkfs.ext4 /dev/sda1"},
                           headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "command_denied"


def test_safe_command_returns_job_handle(client):
    headers = _setup_owner(client)
    server_id = _create_server(client, headers)
    response = client.post("/api/v1/commands",
                           json={"server_id": server_id, "command": "echo hello"},
                           headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "executed"
    assert body["job"]["id"].startswith("job_")
    assert body["job"]["state"] in ("queued", "running", "done")


def test_confirm_flow_over_http(client):
    headers = _setup_owner(client)
    server_id = _create_server(client, headers)
    response = client.post("/api/v1/commands",
                           json={"server_id": server_id, "command": "reboot now"},
                           headers=headers)
    body = response.json()
    assert body["kind"] == "confirm"
    token = body["pending"]["token"]
    ok = client.post("/api/v1/commands/confirm", json={"token": token}, headers=headers)
    assert ok.status_code == 200
    replay = client.post("/api/v1/commands/confirm", json={"token": token}, headers=headers)
    assert replay.status_code == 400


def test_commands_to_foreign_server_404(client, tmp_path):
    headers = _setup_owner(client)
    server_id = _create_server(client, headers)
    client.post("/api/v1/users", json={"username": "op", "password": "OperatorPass!1", "role": "operator"},
                headers=headers)
    from fastapi.testclient import TestClient

    with TestClient(client.app) as op_client:
        op_client.post("/api/v1/auth/login", json={"username": "op", "password": "OperatorPass!1"})
        op_headers = {"X-CSRF-Token": op_client.cookies.get("tla_csrf")}
        response = op_client.post("/api/v1/commands",
                                  json={"server_id": server_id, "command": "ls"},
                                  headers=op_headers)
        assert response.status_code in (403, 404)


# --- backups -----------------------------------------------------------------------

def test_backup_create_verify_restore_gate(client):
    headers = _setup_owner(client)
    created = client.post("/api/v1/backups", headers=headers)
    assert created.status_code == 201
    backup_id = created.json()["backup_id"]
    assert client.post(f"/api/v1/backups/{backup_id}/verify", headers=headers).json()["ok"] is True
    # Restore requires explicit confirmation.
    assert client.post(f"/api/v1/backups/{backup_id}/restore", json={}, headers=headers).status_code == 400
    listing = client.get("/api/v1/backups").json()
    assert any(b["backup_id"] == backup_id for b in listing)


# --- Bale link codes --------------------------------------------------------------

def test_link_code_created_and_consumed(client):
    headers = _setup_owner(client)
    response = client.post("/api/v1/bale/link-code", json={}, headers=headers)
    assert response.status_code == 200
    code = response.json()["code"]
    assert store.consume_link_code(code) is not None
    assert store.consume_link_code(code) is None  # single-use


# --- misc ---------------------------------------------------------------------------

def test_health_and_ready_endpoints(client):
    assert client.get("/api/health").json()["ok"] is True
    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] is True


def test_error_envelope_shape(client):
    response = client.get("/api/v1/servers/99999")
    # 401 (unauthenticated) also uses the envelope.
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_security_headers_present(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
