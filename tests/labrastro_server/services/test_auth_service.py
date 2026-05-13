from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import time
import uuid

import pytest

from labrastro_server.services.auth.crypto import hash_password, hash_token, verify_password
from labrastro_server.services.auth.file_store import FileAuthStore
from labrastro_server.services.auth.models import (
    AccessTokenRecord,
    AuthDevice,
    AuthUser,
    LoginFailureRecord,
    RefreshTokenRecord,
)
from labrastro_server.services.auth.postgres_store import PostgresAuthStore
from labrastro_server.services.auth.service import AuthError, AuthService
from reuleauxcoder.domain.config.models import AuthConfig, AuthSuperadminConfig
from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.migration import run_migrations


TEST_PASSWORD = "admin-password"


def _config(path: Path, *, access_ttl: int = 900) -> AuthConfig:
    return AuthConfig(
        enabled=True,
        token_secret="test-token-secret",
        access_token_ttl_sec=access_ttl,
        refresh_token_ttl_sec=3600,
        password_hash_iterations=1000,
        store_path=str(path),
        superadmins=[
            AuthSuperadminConfig(
                username="admin",
                password_hash=hash_password(TEST_PASSWORD, iterations=1000),
            )
        ],
    )


def _service(tmp_path: Path, *, access_ttl: int = 900) -> tuple[AuthService, FileAuthStore]:
    store = FileAuthStore(tmp_path / "auth.json")
    service = AuthService(
        _config(tmp_path / "auth.json", access_ttl=access_ttl),
        store,
        issue_bootstrap_token=lambda ttl: f"bt_test_{ttl}",
    )
    return service, store


def test_bootstrap_token_passes_principal_claims(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def issue_bootstrap_token(ttl: int, claims: dict | None = None) -> str:
        captured["ttl"] = ttl
        captured["claims"] = dict(claims or {})
        return f"bt_claims_{ttl}"

    store = FileAuthStore(tmp_path / "auth.json")
    service = AuthService(
        _config(tmp_path / "auth.json"),
        store,
        issue_bootstrap_token=issue_bootstrap_token,
    )
    session = service.login("admin", TEST_PASSWORD, "VS Code")

    result = service.bootstrap_token(session.principal, 123)

    assert result["bootstrap_token"] == "bt_claims_123"
    assert captured["ttl"] == 123
    assert captured["claims"] == {
        "user_id": session.principal.user_id,
        "username": "admin",
        "role": "superadmin",
        "device_id": session.principal.device_id,
        "scopes": list(session.principal.effective_scopes()),
    }


def test_password_hash_and_token_hash_are_verifiable_without_raw_secret() -> None:
    password_hash = hash_password(TEST_PASSWORD, iterations=1000)

    assert verify_password(TEST_PASSWORD, password_hash) is True
    assert verify_password("wrong", password_hash) is False
    assert password_hash != TEST_PASSWORD
    assert hash_token("rt_raw", "secret") == hash_token("rt_raw", "secret")
    assert hash_token("rt_raw", "secret") != "rt_raw"


def test_file_auth_store_contract_lists_revokes_and_filters_audit(tmp_path: Path) -> None:
    store = FileAuthStore(tmp_path / "auth.json")
    now = time.time()
    user = store.upsert_user(
        AuthUser(
            id="usr_contract",
            username="contract",
            password_hash=hash_password("contract-password", iterations=1000),
            role="user",
            enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    device = store.create_device(
        AuthDevice(
            id="dev_contract",
            user_id=user.id,
            label="pytest",
            created_at=now,
        )
    )
    store.record_refresh_token(
        RefreshTokenRecord(
            id="rt_contract",
            user_id=user.id,
            device_id=device.id,
            token_hash="hashed",
            expires_at=now + 3600,
            created_at=now,
        )
    )
    store.record_access_token(
        AccessTokenRecord(
            id="at_contract",
            user_id=user.id,
            device_id=device.id,
            token_hash="access_hashed",
            expires_at=now + 900,
            created_at=now,
        )
    )
    store.record_login_failure(
        LoginFailureRecord(
            id="lf_contract",
            username="contract",
            source="127.0.0.1",
            failed_at=now,
        )
    )
    store.append_audit_event(
        {
            "id": "evt_contract",
            "type": "contract",
            "created_at": now,
            "user_id": user.id,
            "payload": {"ok": True},
        }
    )

    assert store.list_users()[0].username == "contract"
    assert store.list_devices(user_id=user.id)[0].id == device.id
    assert store.revoke_refresh_tokens(user_id=user.id, revoked_at=now + 1) == 1
    assert store.list_refresh_tokens(user_id=user.id)[0].revoked_at == now + 1
    assert store.get_access_token_by_hash("access_hashed") is not None
    assert store.revoke_access_tokens(device_id=device.id, revoked_at=now + 2) == 1
    assert store.get_access_token_by_hash("access_hashed").revoked_at == now + 2
    assert store.count_login_failures(
        username="contract",
        source="127.0.0.1",
        since=now - 60,
    ) == 1
    assert store.clear_login_failures(
        username="contract",
        source="127.0.0.1",
    ) == 1
    assert store.list_audit_events(event_type="contract", user_id=user.id)[0]["id"] == "evt_contract"


@pytest.mark.skipif(
    not (os.environ.get("LABRASTRO_TEST_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")),
    reason="Postgres auth store contract requires LABRASTRO_TEST_DATABASE_URL or TEST_DATABASE_URL",
)
def test_postgres_auth_store_contract() -> None:
    database_url = os.environ.get("LABRASTRO_TEST_DATABASE_URL") or os.environ["TEST_DATABASE_URL"]
    run_migrations(database_url)
    store = PostgresAuthStore(create_postgres_engine(database_url))
    suffix = uuid.uuid4().hex
    now = time.time()
    user = store.upsert_user(
        AuthUser(
            id=f"usr_{suffix}",
            username=f"contract_{suffix}",
            password_hash=hash_password("contract-password", iterations=1000),
            role="admin",
            enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    device = store.create_device(
        AuthDevice(
            id=f"dev_{suffix}",
            user_id=user.id,
            label="pytest",
            created_at=now,
        )
    )
    store.record_refresh_token(
        RefreshTokenRecord(
            id=f"rt_{suffix}",
            user_id=user.id,
            device_id=device.id,
            token_hash=f"hash_{suffix}",
            expires_at=now + 3600,
            created_at=now,
        )
    )
    store.record_access_token(
        AccessTokenRecord(
            id=f"at_{suffix}",
            user_id=user.id,
            device_id=device.id,
            token_hash=f"access_hash_{suffix}",
            expires_at=now + 900,
            created_at=now,
        )
    )
    store.record_login_failure(
        LoginFailureRecord(
            id=f"lf_{suffix}",
            username=user.username,
            source="127.0.0.1",
            failed_at=now,
        )
    )
    store.append_audit_event(
        {
            "id": f"evt_{suffix}",
            "type": "contract",
            "created_at": now,
            "user_id": user.id,
            "payload": {"ok": True},
        }
    )

    assert store.get_user_by_username(user.username).id == user.id
    assert store.list_devices(user_id=user.id)[0].id == device.id
    assert store.revoke_refresh_tokens(device_id=device.id, revoked_at=now + 2) == 1
    assert store.get_access_token_by_hash(f"access_hash_{suffix}") is not None
    assert store.revoke_access_tokens(user_id=user.id, revoked_at=now + 3) == 1
    assert store.count_login_failures(
        username=user.username,
        source="127.0.0.1",
        since=now - 60,
    ) == 1
    assert store.list_audit_events(event_type="contract", user_id=user.id)[0]["id"] == f"evt_{suffix}"


def test_login_refresh_rotation_logout_and_hashed_refresh_storage(tmp_path: Path) -> None:
    service, store = _service(tmp_path)

    session = service.login("admin", TEST_PASSWORD, "pytest")
    assert session.access_token.startswith("at_")
    assert session.refresh_token.startswith("rt_")
    principal = service.authenticate_access_token(session.access_token)
    assert principal is not None
    assert principal.username == "admin"
    assert "users:manage" in principal.effective_scopes()

    token_record = store.get_refresh_token_by_hash(
        hash_token(session.refresh_token, service.config.token_secret)
    )
    assert token_record is not None
    assert token_record.token_hash != session.refresh_token

    rotated = service.refresh(session.refresh_token)
    assert rotated.refresh_token != session.refresh_token
    with pytest.raises(AuthError) as old_refresh_error:
        service.refresh(session.refresh_token)
    assert old_refresh_error.value.code == "invalid_refresh_token"

    assert service.authenticate_access_token(rotated.access_token) is not None
    service.logout(rotated.refresh_token)
    assert service.authenticate_access_token(rotated.access_token) is None
    assert service.authenticate_access_token(session.access_token) is None
    with pytest.raises(AuthError) as revoked_error:
        service.refresh(rotated.refresh_token)
    assert revoked_error.value.code == "invalid_refresh_token"


def test_access_token_expires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service, _store = _service(tmp_path, access_ttl=1)
    session = service.login("admin", TEST_PASSWORD, "pytest")
    assert service.authenticate_access_token(session.access_token) is not None

    monkeypatch.setattr(
        "labrastro_server.services.auth.service.time.time",
        lambda: session.access_expires_at + 1,
    )

    assert service.authenticate_access_token(session.access_token) is None


def test_access_token_survives_auth_service_restart(tmp_path: Path) -> None:
    service, store = _service(tmp_path)

    session = service.login("admin", TEST_PASSWORD, "pytest")
    restarted = AuthService(
        service.config,
        store,
        issue_bootstrap_token=lambda ttl: f"bt_restarted_{ttl}",
    )

    principal = restarted.authenticate_access_token(session.access_token)
    assert principal is not None
    assert principal.username == "admin"


def test_login_rate_limit_survives_auth_service_restart(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    source_ip = "127.0.0.1"

    for _index in range(5):
        with pytest.raises(AuthError) as bad_login:
            service.login("missing", "bad-password", "pytest", source_ip=source_ip)
        assert bad_login.value.code == "invalid_credentials"

    restarted = AuthService(
        service.config,
        store,
        issue_bootstrap_token=lambda ttl: f"bt_restarted_{ttl}",
    )
    with pytest.raises(AuthError) as rate_limited:
        restarted.login("missing", "bad-password", "pytest", source_ip=source_ip)

    assert rate_limited.value.code == "rate_limited"


def test_disabled_configured_user_cannot_login(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    user = store.get_user_by_username("admin")
    assert user is not None
    store.update_user(replace(user, enabled=False))

    with pytest.raises(AuthError) as excinfo:
        service.login("admin", TEST_PASSWORD, "pytest")

    assert excinfo.value.code == "user_disabled"


def test_user_management_scopes_and_configured_superadmin_protection(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    admin_session = service.login("admin", TEST_PASSWORD, "pytest")
    admin = service.authenticate_access_token(admin_session.access_token)
    assert admin is not None

    created = service.create_user(
        admin,
        username="operator",
        password="operator-password",
        role="admin",
        scopes=[],
    )
    assert created["user"]["role"] == "admin"
    assert "admin:write" in created["user"]["scopes"]

    operator_session = service.login("operator", "operator-password", "pytest")
    operator = service.authenticate_access_token(operator_session.access_token)
    assert operator is not None
    with pytest.raises(AuthError) as manage_error:
        service.list_users(operator)
    assert manage_error.value.code == "forbidden"

    users = service.list_users(admin)["users"]
    configured = next(user for user in users if user["username"] == "admin")
    with pytest.raises(AuthError) as disable_error:
        service.disable_user(admin, user_id=configured["id"])
    assert disable_error.value.code == "configured_user_immutable"


def test_password_policy_rate_limit_and_session_revocation(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    admin_session = service.login("admin", TEST_PASSWORD, "pytest")
    admin = service.authenticate_access_token(admin_session.access_token)
    assert admin is not None

    with pytest.raises(AuthError) as policy_error:
        service.create_user(admin, username="shorty", password="short", role="user")
    assert policy_error.value.code == "password_policy_failed"

    six_char = service.create_user(
        admin,
        username="sixchr",
        password="abc123",
        role="user",
    )
    assert six_char["user"]["username"] == "sixchr"

    for _index in range(5):
        with pytest.raises(AuthError) as bad_login:
            service.login("missing", "bad-password", "pytest", source_ip="127.0.0.1")
        assert bad_login.value.code == "invalid_credentials"
    with pytest.raises(AuthError) as rate_limited:
        service.login("missing", "bad-password", "pytest", source_ip="127.0.0.1")
    assert rate_limited.value.code == "rate_limited"

    created = service.create_user(
        admin,
        username="viewer",
        password="viewer-password",
        role="user",
    )
    viewer_session = service.login("viewer", "viewer-password", "pytest")
    viewer = service.authenticate_access_token(viewer_session.access_token)
    assert viewer is not None
    devices = service.list_devices(viewer)["devices"]
    assert devices
    service.revoke_device(viewer, device_id=devices[0]["id"])
    assert service.list_devices(viewer)["devices"] == []
    assert service.authenticate_access_token(viewer_session.access_token) is None
    with pytest.raises(AuthError) as revoked_refresh:
        service.refresh(viewer_session.refresh_token)
    assert revoked_refresh.value.code == "invalid_refresh_token"

    disabled = service.disable_user(admin, user_id=created["user"]["id"])
    assert disabled["user"]["enabled"] is False
    audit = service.list_audit_events(admin, limit=20)["events"]
    assert any(event["type"] == "device_revoked" for event in audit)
