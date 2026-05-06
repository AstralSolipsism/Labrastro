"""Postgres-backed auth store."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from labrastro_server.services.auth.models import (
    AuthDevice,
    AuthUser,
    RefreshTokenRecord,
)

try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


def _require_sqlalchemy() -> None:
    if text is None:
        raise RuntimeError("Postgres auth store requires sqlalchemy and psycopg.")


def _json(value: Any, fallback: Any) -> str:
    return json.dumps(value if value is not None else fallback, ensure_ascii=False)


def _row_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: _row_value(value) for key, value in dict(row).items()}


class PostgresAuthStore:
    def __init__(self, engine: Any) -> None:
        _require_sqlalchemy()
        self.engine = engine

    def get_user_by_username(self, username: str) -> AuthUser | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_auth_users
                    WHERE lower(username)=lower(:username)
                    """
                ),
                {"username": username.strip()},
            ).mappings().first()
        return AuthUser.from_dict(_row_dict(row)) if row else None

    def get_user_by_id(self, user_id: str) -> AuthUser | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_auth_users WHERE id=:user_id"),
                {"user_id": user_id},
            ).mappings().first()
        return AuthUser.from_dict(_row_dict(row)) if row else None

    def list_users(self) -> list[AuthUser]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text("SELECT * FROM labrastro_auth_users ORDER BY lower(username) ASC")
            ).mappings()
            return [AuthUser.from_dict(_row_dict(row)) for row in rows]

    def upsert_user(self, user: AuthUser) -> AuthUser:
        existing = self.get_user_by_username(user.username)
        if existing is not None and existing.id != user.id:
            user = replace(user, id=existing.id, created_at=existing.created_at)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_auth_users (
                        id, username, password_hash, role, scopes, enabled,
                        configured, created_at, updated_at, last_login_at
                    ) VALUES (
                        :id, :username, :password_hash, :role,
                        CAST(:scopes AS JSONB), :enabled, :configured,
                        :created_at, :updated_at, :last_login_at
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        username=EXCLUDED.username,
                        password_hash=EXCLUDED.password_hash,
                        role=EXCLUDED.role,
                        scopes=EXCLUDED.scopes,
                        enabled=EXCLUDED.enabled,
                        configured=EXCLUDED.configured,
                        updated_at=EXCLUDED.updated_at,
                        last_login_at=EXCLUDED.last_login_at
                    """
                ),
                self._user_params(user),
            )
        return self.get_user_by_id(user.id) or user

    def update_user(self, user: AuthUser) -> AuthUser:
        return self.upsert_user(user)

    def create_device(self, device: AuthDevice) -> AuthDevice:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_auth_devices (
                        id, user_id, label, created_at, last_seen_at, revoked_at
                    ) VALUES (
                        :id, :user_id, :label, :created_at, :last_seen_at, :revoked_at
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        user_id=EXCLUDED.user_id,
                        label=EXCLUDED.label,
                        last_seen_at=EXCLUDED.last_seen_at,
                        revoked_at=EXCLUDED.revoked_at
                    """
                ),
                device.to_dict(),
            )
        return self.get_device(device.id) or device

    def get_device(self, device_id: str) -> AuthDevice | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_auth_devices WHERE id=:device_id"),
                {"device_id": device_id},
            ).mappings().first()
        return AuthDevice.from_dict(_row_dict(row)) if row else None

    def list_devices(self, *, user_id: str | None = None) -> list[AuthDevice]:
        params: dict[str, Any] = {}
        sql = "SELECT * FROM labrastro_auth_devices"
        if user_id is not None:
            sql += " WHERE user_id=:user_id"
            params["user_id"] = user_id
        sql += " ORDER BY created_at DESC"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings()
            return [AuthDevice.from_dict(_row_dict(row)) for row in rows]

    def update_device(self, device: AuthDevice) -> AuthDevice:
        return self.create_device(device)

    def record_refresh_token(self, token_record: RefreshTokenRecord) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_auth_refresh_tokens (
                        id, user_id, device_id, token_hash, expires_at,
                        created_at, revoked_at
                    ) VALUES (
                        :id, :user_id, :device_id, :token_hash, :expires_at,
                        :created_at, :revoked_at
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        user_id=EXCLUDED.user_id,
                        device_id=EXCLUDED.device_id,
                        token_hash=EXCLUDED.token_hash,
                        expires_at=EXCLUDED.expires_at,
                        revoked_at=EXCLUDED.revoked_at
                    """
                ),
                token_record.to_dict(),
            )

    def get_refresh_token_by_hash(self, token_hash: str) -> RefreshTokenRecord | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_auth_refresh_tokens
                    WHERE token_hash=:token_hash
                    """
                ),
                {"token_hash": token_hash},
            ).mappings().first()
        return RefreshTokenRecord.from_dict(_row_dict(row)) if row else None

    def update_refresh_token(self, token_record: RefreshTokenRecord) -> None:
        self.record_refresh_token(token_record)

    def list_refresh_tokens(
        self, *, user_id: str | None = None, device_id: str | None = None
    ) -> list[RefreshTokenRecord]:
        filters: list[str] = []
        params: dict[str, Any] = {}
        if user_id is not None:
            filters.append("user_id=:user_id")
            params["user_id"] = user_id
        if device_id is not None:
            filters.append("device_id=:device_id")
            params["device_id"] = device_id
        sql = "SELECT * FROM labrastro_auth_refresh_tokens"
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY created_at DESC"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings()
            return [RefreshTokenRecord.from_dict(_row_dict(row)) for row in rows]

    def revoke_refresh_tokens(
        self,
        *,
        user_id: str | None = None,
        device_id: str | None = None,
        revoked_at: float,
    ) -> int:
        filters = ["revoked_at IS NULL"]
        params: dict[str, Any] = {"revoked_at": revoked_at}
        if user_id is not None:
            filters.append("user_id=:user_id")
            params["user_id"] = user_id
        if device_id is not None:
            filters.append("device_id=:device_id")
            params["device_id"] = device_id
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_auth_refresh_tokens
                    SET revoked_at=:revoked_at
                    WHERE """ + " AND ".join(filters)
                ),
                params,
            )
        return int(result.rowcount or 0)

    def append_audit_event(self, event: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_auth_audit_events (
                        id, type, created_at, user_id, username, device_id,
                        source_ip, payload
                    ) VALUES (
                        :id, :type, :created_at, :user_id, :username, :device_id,
                        :source_ip, CAST(:payload AS JSONB)
                    )
                    """
                ),
                {
                    "id": event.get("id"),
                    "type": event.get("type"),
                    "created_at": float(event.get("created_at", 0) or 0),
                    "user_id": str(event.get("user_id") or ""),
                    "username": str(event.get("username") or ""),
                    "device_id": str(event.get("device_id") or ""),
                    "source_ip": str(event.get("source_ip") or ""),
                    "payload": _json(event.get("payload"), {}),
                },
            )

    def list_audit_events(
        self,
        *,
        limit: int = 100,
        after_created_at: float | None = None,
        event_type: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 500))}
        if after_created_at is not None:
            filters.append("created_at>:after_created_at")
            params["after_created_at"] = float(after_created_at)
        if event_type:
            filters.append("type=:event_type")
            params["event_type"] = event_type
        if user_id:
            filters.append("user_id=:user_id")
            params["user_id"] = user_id
        sql = "SELECT * FROM labrastro_auth_audit_events"
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY created_at DESC LIMIT :limit"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings()
            return [_row_dict(row) for row in rows]

    def _user_params(self, user: AuthUser) -> dict[str, Any]:
        return {
            **user.to_dict(),
            "scopes": _json(list(user.scopes), []),
        }
