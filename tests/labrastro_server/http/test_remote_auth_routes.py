from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

from labrastro_server.interfaces.http.remote.service import RemoteRelayHTTPService
from labrastro_server.relay.server import RelayServer
from labrastro_server.services.auth.crypto import hash_password
from labrastro_server.services.auth.file_store import FileAuthStore
from labrastro_server.services.auth.models import AuthUser
from labrastro_server.services.auth.service import AuthService
from reuleauxcoder.domain.config.models import AuthConfig, AuthSuperadminConfig


_URLOPEN = request.build_opener(request.ProxyHandler({})).open
TEST_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _assert_error_shape(body: dict, code: str) -> None:
    assert body["ok"] is False
    assert body["error"] == code
    assert isinstance(body["message"], str)
    assert isinstance(body["details"], dict)
    assert isinstance(body["request_id"], str)


def _auth_service(tmp_path: Path, relay: RelayServer) -> AuthService:
    store_path = tmp_path / "auth.json"
    store = FileAuthStore(store_path)
    service = AuthService(
        AuthConfig(
            enabled=True,
            token_secret="test-token-secret",
            access_token_ttl_sec=900,
            refresh_token_ttl_sec=3600,
            password_hash_iterations=1000,
            store_path=str(store_path),
            superadmins=[
                AuthSuperadminConfig(
                    username="admin",
                    password=TEST_PASSWORD,
                )
            ],
        ),
        store,
        issue_bootstrap_token=relay.issue_bootstrap_token,
    )
    store.upsert_user(
        AuthUser(
            id="usr_user",
            username="viewer",
            password_hash=hash_password(USER_PASSWORD, iterations=1000),
            role="user",
            enabled=True,
            created_at=time.time(),
            updated_at=time.time(),
            configured=False,
        )
    )
    return service


def _service(
    tmp_path: Path, **service_kwargs: object
) -> tuple[RemoteRelayHTTPService, RelayServer]:
    relay = RelayServer()
    relay.start()
    service = RemoteRelayHTTPService(
        relay_server=relay,
        bind=f"127.0.0.1:{_free_port()}",
        auth_service=_auth_service(tmp_path, relay),
        bootstrap_token_ttl_sec=60,
        **service_kwargs,
    )
    service.start()
    return service, relay


def test_auth_state_login_me_refresh_and_logout(tmp_path: Path) -> None:
    service, relay = _service(tmp_path)
    try:
        _, state = _json_request("GET", f"{service.base_url}/remote/auth/state")
        assert state["ok"] is True
        assert state["auth_enabled"] is True
        assert state["login_required"] is True
        assert "users:manage" in state["scopes"]

        _, login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "admin", "password": TEST_PASSWORD, "device_label": "pytest"},
        )
        assert login["access_token"].startswith("at_")
        assert login["refresh_token"].startswith("rt_")
        assert login["user"]["username"] == "admin"
        assert login["user"]["role"] == "superadmin"
        assert "audit:read" in login["user"]["scopes"]

        _, me = _json_request(
            "GET",
            f"{service.base_url}/remote/auth/me",
            headers=_auth_headers(login["access_token"]),
        )
        assert me["user"]["username"] == "admin"
        assert me["device"]["label"] == "pytest"

        _, refreshed = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/refresh",
            {"refresh_token": login["refresh_token"]},
        )
        assert refreshed["access_token"] != login["access_token"]
        assert refreshed["refresh_token"] != login["refresh_token"]

        try:
            _json_request(
                "POST",
                f"{service.base_url}/remote/auth/refresh",
                {"refresh_token": login["refresh_token"]},
            )
            raise AssertionError("old refresh token should be revoked")
        except HTTPError as exc:
            assert exc.code == 401
            assert json.loads(exc.read().decode("utf-8"))["error"] == "invalid_refresh_token"

        _, logout = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/logout",
            {"refresh_token": refreshed["refresh_token"]},
        )
        assert logout == {"ok": True}
        try:
            _json_request(
                "POST",
                f"{service.base_url}/remote/auth/refresh",
                {"refresh_token": refreshed["refresh_token"]},
            )
            raise AssertionError("logged-out refresh token should be revoked")
        except HTTPError as exc:
            assert exc.code == 401
    finally:
        service.stop()
        relay.stop()


def test_admin_requires_bearer_and_rejects_plain_user(tmp_path: Path) -> None:
    service, relay = _service(tmp_path)
    try:
        try:
            _json_request("POST", f"{service.base_url}/remote/admin/status", {})
            raise AssertionError("admin status should require auth")
        except HTTPError as exc:
            assert exc.code == 401

        _, user_login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "viewer", "password": USER_PASSWORD},
        )
        try:
            _json_request(
                "POST",
                f"{service.base_url}/remote/admin/status",
                {},
                headers=_auth_headers(user_login["access_token"]),
            )
            raise AssertionError("plain user should not access admin routes")
        except HTTPError as exc:
            assert exc.code == 403

        _, admin_login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "admin", "password": TEST_PASSWORD},
        )
        _, status = _json_request(
            "POST",
            f"{service.base_url}/remote/admin/status",
            {},
            headers=_auth_headers(admin_login["access_token"]),
        )
        assert status["ok"] is True
    finally:
        service.stop()
        relay.stop()


def test_bootstrap_token_is_bearer_protected_and_one_time(tmp_path: Path) -> None:
    service, relay = _service(tmp_path)
    try:
        try:
            _json_request("POST", f"{service.base_url}/remote/auth/bootstrap-token", {})
            raise AssertionError("bootstrap token should require auth")
        except HTTPError as exc:
            assert exc.code == 401

        _, login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "admin", "password": TEST_PASSWORD},
        )
        _, bootstrap = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/bootstrap-token",
            {},
            headers=_auth_headers(login["access_token"]),
        )
        bootstrap_token = bootstrap["bootstrap_token"]
        assert bootstrap_token.startswith("bt_")

        _, register = _json_request(
            "POST",
            f"{service.base_url}/remote/register",
            {"bootstrap_token": bootstrap_token, "cwd": str(tmp_path)},
        )
        assert register["type"] == "register_ok"
        assert register["payload"]["peer_token"].startswith("pt_")

        try:
            _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {"bootstrap_token": bootstrap_token, "cwd": str(tmp_path)},
            )
            raise AssertionError("bootstrap token should be one-time")
        except HTTPError as exc:
            assert exc.code == 403
            body = json.loads(exc.read().decode("utf-8"))
            assert body["ok"] is False
            assert body["error"] == "register_rejected"
            assert body["details"]["reason"]
    finally:
        service.stop()
        relay.stop()


def test_auth_management_devices_audit_and_scope_boundaries(tmp_path: Path) -> None:
    service, relay = _service(tmp_path)
    try:
        _, admin_login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "admin", "password": TEST_PASSWORD},
        )
        admin_headers = _auth_headers(admin_login["access_token"])
        _, users = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/users/list",
            {},
            headers=admin_headers,
        )
        assert any(user["username"] == "viewer" for user in users["users"])

        _, created = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/users/create",
            {
                "username": "operator",
                "password": "operator-password",
                "role": "admin",
            },
            headers=admin_headers,
        )
        assert created["user"]["role"] == "admin"

        _, operator_login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "operator", "password": "operator-password"},
        )
        operator_headers = _auth_headers(operator_login["access_token"])
        _, status = _json_request(
            "POST",
            f"{service.base_url}/remote/admin/status",
            {},
            headers=operator_headers,
        )
        assert status["ok"] is True
        try:
            _json_request(
                "POST",
                f"{service.base_url}/remote/auth/users/list",
                {},
                headers=operator_headers,
            )
            raise AssertionError("admin role should not manage users")
        except HTTPError as exc:
            assert exc.code == 403

        _, viewer_login = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/login",
            {"username": "viewer", "password": USER_PASSWORD},
        )
        viewer_headers = _auth_headers(viewer_login["access_token"])
        _, devices = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/devices/list",
            {},
            headers=viewer_headers,
        )
        assert devices["devices"][0]["username"] == "viewer"
        _, revoked = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/devices/revoke",
            {"device_id": devices["devices"][0]["id"]},
            headers=viewer_headers,
        )
        assert revoked["device"]["revoked_at"] is not None
        _, after_revoke_devices = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/devices/list",
            {"user_id": viewer_login["user"]["id"]},
            headers=admin_headers,
        )
        assert after_revoke_devices["devices"] == []

        _, audit = _json_request(
            "POST",
            f"{service.base_url}/remote/auth/audit/list",
            {"limit": 20},
            headers=admin_headers,
        )
        assert any(event["type"] == "user_created" for event in audit["events"])
    finally:
        service.stop()
        relay.stop()


def test_auth_login_rate_limit_returns_429(tmp_path: Path) -> None:
    service, relay = _service(tmp_path)
    try:
        for _index in range(5):
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/auth/login",
                    {"username": "missing", "password": "bad-password"},
                )
                raise AssertionError("bad login should fail")
            except HTTPError as exc:
                assert exc.code == 401
        try:
            _json_request(
                "POST",
                f"{service.base_url}/remote/auth/login",
                {"username": "missing", "password": "bad-password"},
            )
            raise AssertionError("rate-limited login should fail")
        except HTTPError as exc:
            assert exc.code == 429
    finally:
        service.stop()
        relay.stop()


def test_invalid_json_unknown_route_and_body_limit_return_error_shape(
    tmp_path: Path,
) -> None:
    service, relay = _service(tmp_path, max_request_body_bytes=32)
    try:
        invalid_req = request.Request(
            f"{service.base_url}/remote/auth/login",
            data=b"{invalid-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            _URLOPEN(invalid_req, timeout=5)
            raise AssertionError("invalid JSON should fail")
        except HTTPError as exc:
            assert exc.code == 400
            _assert_error_shape(
                json.loads(exc.read().decode("utf-8")),
                "invalid_json",
            )

        oversized_req = request.Request(
            f"{service.base_url}/remote/auth/login",
            data=json.dumps({"username": "x" * 64}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            _URLOPEN(oversized_req, timeout=5)
            raise AssertionError("oversized JSON should fail")
        except HTTPError as exc:
            assert exc.code == 413
            _assert_error_shape(
                json.loads(exc.read().decode("utf-8")),
                "request_body_too_large",
            )

        try:
            _json_request("GET", f"{service.base_url}/remote/missing")
            raise AssertionError("unknown route should fail")
        except HTTPError as exc:
            assert exc.code == 404
            _assert_error_shape(
                json.loads(exc.read().decode("utf-8")),
                "not_found",
            )
    finally:
        service.stop()
        relay.stop()
