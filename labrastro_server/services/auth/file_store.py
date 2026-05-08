"""File-backed auth store for single-host deployments."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from labrastro_server.services.auth.models import (
    AccessTokenRecord,
    AuthDevice,
    AuthUser,
    LoginFailureRecord,
    RefreshTokenRecord,
)


class FileAuthStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def get_user_by_username(self, username: str) -> AuthUser | None:
        data = self._read()
        normalized = username.strip().lower()
        for item in data["users"]:
            user = AuthUser.from_dict(item)
            if user.username.lower() == normalized:
                return user
        return None

    def get_user_by_id(self, user_id: str) -> AuthUser | None:
        data = self._read()
        for item in data["users"]:
            user = AuthUser.from_dict(item)
            if user.id == user_id:
                return user
        return None

    def list_users(self) -> list[AuthUser]:
        data = self._read()
        users = [AuthUser.from_dict(item) for item in data["users"]]
        return sorted(users, key=lambda item: item.username.lower())

    def upsert_user(self, user: AuthUser) -> AuthUser:
        with self._lock:
            data = self._read_unlocked()
            users = [AuthUser.from_dict(item) for item in data["users"]]
            replaced = False
            next_users: list[dict[str, Any]] = []
            for existing in users:
                if existing.username.lower() == user.username.lower():
                    next_users.append(user.to_dict())
                    replaced = True
                else:
                    next_users.append(existing.to_dict())
            if not replaced:
                next_users.append(user.to_dict())
            data["users"] = next_users
            self._write_unlocked(data)
        return user

    def update_user(self, user: AuthUser) -> AuthUser:
        return self.upsert_user(user)

    def create_device(self, device: AuthDevice) -> AuthDevice:
        with self._lock:
            data = self._read_unlocked()
            data["devices"] = [
                item for item in data["devices"] if str(item.get("id")) != device.id
            ]
            data["devices"].append(device.to_dict())
            self._write_unlocked(data)
        return device

    def get_device(self, device_id: str) -> AuthDevice | None:
        data = self._read()
        for item in data["devices"]:
            device = AuthDevice.from_dict(item)
            if device.id == device_id:
                return device
        return None

    def list_devices(self, *, user_id: str | None = None) -> list[AuthDevice]:
        data = self._read()
        devices = [AuthDevice.from_dict(item) for item in data["devices"]]
        if user_id is not None:
            devices = [device for device in devices if device.user_id == user_id]
        return sorted(devices, key=lambda item: item.created_at, reverse=True)

    def update_device(self, device: AuthDevice) -> AuthDevice:
        with self._lock:
            data = self._read_unlocked()
            data["devices"] = [
                item for item in data["devices"] if str(item.get("id")) != device.id
            ]
            data["devices"].append(device.to_dict())
            self._write_unlocked(data)
        return device

    def record_refresh_token(self, token: RefreshTokenRecord) -> None:
        with self._lock:
            data = self._read_unlocked()
            data["refresh_tokens"] = [
                item
                for item in data["refresh_tokens"]
                if str(item.get("id")) != token.id
                and str(item.get("token_hash")) != token.token_hash
            ]
            data["refresh_tokens"].append(token.to_dict())
            self._write_unlocked(data)

    def get_refresh_token_by_hash(self, token_hash: str) -> RefreshTokenRecord | None:
        data = self._read()
        for item in data["refresh_tokens"]:
            token = RefreshTokenRecord.from_dict(item)
            if token.token_hash == token_hash:
                return token
        return None

    def update_refresh_token(self, token: RefreshTokenRecord) -> None:
        self.record_refresh_token(token)

    def list_refresh_tokens(
        self, *, user_id: str | None = None, device_id: str | None = None
    ) -> list[RefreshTokenRecord]:
        data = self._read()
        tokens = [
            RefreshTokenRecord.from_dict(item) for item in data["refresh_tokens"]
        ]
        if user_id is not None:
            tokens = [token for token in tokens if token.user_id == user_id]
        if device_id is not None:
            tokens = [token for token in tokens if token.device_id == device_id]
        return sorted(tokens, key=lambda item: item.created_at, reverse=True)

    def revoke_refresh_tokens(
        self,
        *,
        user_id: str | None = None,
        device_id: str | None = None,
        revoked_at: float,
    ) -> int:
        revoked = 0
        with self._lock:
            data = self._read_unlocked()
            tokens = [
                RefreshTokenRecord.from_dict(item)
                for item in data["refresh_tokens"]
            ]
            next_tokens: list[dict[str, Any]] = []
            for token in tokens:
                matches_user = user_id is None or token.user_id == user_id
                matches_device = device_id is None or token.device_id == device_id
                if matches_user and matches_device and token.revoked_at is None:
                    token = RefreshTokenRecord(
                        id=token.id,
                        user_id=token.user_id,
                        device_id=token.device_id,
                        token_hash=token.token_hash,
                        expires_at=token.expires_at,
                        created_at=token.created_at,
                        revoked_at=revoked_at,
                    )
                    revoked += 1
                next_tokens.append(token.to_dict())
            data["refresh_tokens"] = next_tokens
            self._write_unlocked(data)
        return revoked

    def record_access_token(self, token: AccessTokenRecord) -> None:
        with self._lock:
            data = self._read_unlocked()
            data["access_tokens"] = [
                item
                for item in data["access_tokens"]
                if str(item.get("id")) != token.id
                and str(item.get("token_hash")) != token.token_hash
            ]
            data["access_tokens"].append(token.to_dict())
            self._write_unlocked(data)

    def get_access_token_by_hash(self, token_hash: str) -> AccessTokenRecord | None:
        data = self._read()
        for item in data["access_tokens"]:
            token = AccessTokenRecord.from_dict(item)
            if token.token_hash == token_hash:
                return token
        return None

    def update_access_token(self, token: AccessTokenRecord) -> None:
        self.record_access_token(token)

    def revoke_access_tokens(
        self,
        *,
        user_id: str | None = None,
        device_id: str | None = None,
        exclude_device_id: str | None = None,
        revoked_at: float,
    ) -> int:
        revoked = 0
        with self._lock:
            data = self._read_unlocked()
            tokens = [
                AccessTokenRecord.from_dict(item)
                for item in data["access_tokens"]
            ]
            next_tokens: list[dict[str, Any]] = []
            for token in tokens:
                matches_user = user_id is None or token.user_id == user_id
                matches_device = device_id is None or token.device_id == device_id
                excluded = (
                    exclude_device_id is not None
                    and token.device_id == exclude_device_id
                )
                if (
                    matches_user
                    and matches_device
                    and not excluded
                    and token.revoked_at is None
                ):
                    token = AccessTokenRecord(
                        id=token.id,
                        user_id=token.user_id,
                        device_id=token.device_id,
                        token_hash=token.token_hash,
                        expires_at=token.expires_at,
                        created_at=token.created_at,
                        revoked_at=revoked_at,
                    )
                    revoked += 1
                next_tokens.append(token.to_dict())
            data["access_tokens"] = next_tokens
            self._write_unlocked(data)
        return revoked

    def delete_expired_access_tokens(self, *, now: float) -> int:
        deleted = 0
        with self._lock:
            data = self._read_unlocked()
            next_tokens: list[dict[str, Any]] = []
            for item in data["access_tokens"]:
                token = AccessTokenRecord.from_dict(item)
                if token.expires_at <= now:
                    deleted += 1
                    continue
                next_tokens.append(token.to_dict())
            data["access_tokens"] = next_tokens
            self._write_unlocked(data)
        return deleted

    def record_login_failure(self, failure: LoginFailureRecord) -> None:
        with self._lock:
            data = self._read_unlocked()
            data["login_failures"] = [
                item
                for item in data["login_failures"]
                if str(item.get("id")) != failure.id
            ]
            data["login_failures"].append(failure.to_dict())
            self._write_unlocked(data)

    def count_login_failures(
        self, *, username: str, source: str, since: float
    ) -> int:
        normalized = username.strip().lower()
        data = self._read()
        return sum(
            1
            for item in data["login_failures"]
            if str(item.get("username") or "").strip().lower() == normalized
            and str(item.get("source") or "") == source
            and float(item.get("failed_at", 0) or 0) >= since
        )

    def clear_login_failures(self, *, username: str, source: str) -> int:
        normalized = username.strip().lower()
        cleared = 0
        with self._lock:
            data = self._read_unlocked()
            next_failures: list[dict[str, Any]] = []
            for item in data["login_failures"]:
                if (
                    str(item.get("username") or "").strip().lower() == normalized
                    and str(item.get("source") or "") == source
                ):
                    cleared += 1
                    continue
                next_failures.append(dict(item))
            data["login_failures"] = next_failures
            self._write_unlocked(data)
        return cleared

    def delete_old_login_failures(self, *, before: float) -> int:
        deleted = 0
        with self._lock:
            data = self._read_unlocked()
            next_failures: list[dict[str, Any]] = []
            for item in data["login_failures"]:
                if float(item.get("failed_at", 0) or 0) < before:
                    deleted += 1
                    continue
                next_failures.append(dict(item))
            data["login_failures"] = next_failures
            self._write_unlocked(data)
        return deleted

    def append_audit_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            data = self._read_unlocked()
            data["audit_events"].append(dict(event))
            data["audit_events"] = data["audit_events"][-1000:]
            self._write_unlocked(data)

    def list_audit_events(
        self,
        *,
        limit: int = 100,
        after_created_at: float | None = None,
        event_type: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        data = self._read()
        events = [dict(item) for item in data["audit_events"] if isinstance(item, dict)]
        if after_created_at is not None:
            events = [
                event
                for event in events
                if float(event.get("created_at", 0) or 0) > after_created_at
            ]
        if event_type:
            events = [
                event
                for event in events
                if str(event.get("type") or "") == event_type
            ]
        if user_id:
            events = [
                event
                for event in events
                if str(event.get("user_id") or "") == user_id
                or str(
                    (
                        event.get("payload")
                        if isinstance(event.get("payload"), dict)
                        else {}
                    ).get("user_id")
                    or ""
                )
                == user_id
            ]
        events = sorted(
            events,
            key=lambda item: float(item.get("created_at", 0) or 0),
            reverse=True,
        )
        return events[: max(1, min(int(limit or 100), 500))]

    def _read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty()
        data = self._empty()
        if isinstance(raw, dict):
            for key in data:
                value = raw.get(key)
                if isinstance(value, list):
                    data[key] = value
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "users": [],
            "devices": [],
            "refresh_tokens": [],
            "access_tokens": [],
            "login_failures": [],
            "audit_events": [],
        }
