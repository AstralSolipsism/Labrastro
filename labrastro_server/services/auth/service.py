"""Authentication service for Labrastro remote host."""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import replace
from typing import Any, Callable

from labrastro_server.services.auth.crypto import (
    hash_password,
    hash_token,
    verify_password,
)
from labrastro_server.services.auth.models import (
    AccessTokenRecord,
    AUTH_ROLES,
    AUTH_SCOPES,
    AuthDevice,
    AuthPrincipal,
    AuthSession,
    AuthUser,
    LoginFailureRecord,
    RefreshTokenRecord,
    effective_scopes,
    normalize_scopes,
)
from labrastro_server.services.auth.store import AuthStore
from reuleauxcoder.domain.config.models import AuthConfig


SENSITIVE_AUDIT_KEYS = {
    "password",
    "new_password",
    "current_password",
    "access_token",
    "refresh_token",
    "bootstrap_token",
    "token",
    "provider_key",
    "password_hash",
}


class AuthError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class AuthService:
    def __init__(
        self,
        config: AuthConfig,
        store: AuthStore,
        *,
        issue_bootstrap_token: Callable[[int], str],
    ) -> None:
        self.config = config
        self.store = store
        self.issue_bootstrap_token = issue_bootstrap_token
        self._sync_configured_superadmins()

    def state(self) -> dict[str, Any]:
        return {
            "ok": True,
            "auth_enabled": self.config.enabled,
            "login_required": True,
            "scopes": sorted(AUTH_SCOPES),
        }

    def login(
        self,
        username: str,
        password: str,
        device_label: str,
        *,
        source_ip: str = "",
    ) -> AuthSession:
        username = username.strip()
        source_key = source_ip or "unknown"
        self._check_login_rate_limit(username, source_key)
        user = self.store.get_user_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            self._record_failed_login(username, source_key)
            self._audit("login_failed", username=username, source_ip=source_ip)
            raise AuthError("invalid_credentials")
        if not user.enabled:
            self._record_failed_login(username, source_key)
            self._audit(
                "login_disabled",
                user_id=user.id,
                username=user.username,
                source_ip=source_ip,
            )
            raise AuthError("user_disabled")
        self._clear_failed_login(username, source_key)
        now = time.time()
        user = self.store.update_user(replace(user, last_login_at=now, updated_at=now))
        device = AuthDevice(
            id="dev_" + uuid.uuid4().hex,
            user_id=user.id,
            label=device_label.strip() or "VS Code",
            created_at=now,
            last_seen_at=now,
        )
        self.store.create_device(device)
        session = self._create_session(user, device)
        self._audit(
            "login_success",
            user_id=user.id,
            username=user.username,
            device_id=device.id,
            source_ip=source_ip,
        )
        return session

    def refresh(self, refresh_token: str) -> AuthSession:
        now = time.time()
        token_hash = hash_token(refresh_token, self.config.token_secret)
        record = self.store.get_refresh_token_by_hash(token_hash)
        if record is None or record.revoked_at is not None or record.expires_at <= now:
            raise AuthError("invalid_refresh_token")
        user = self.store.get_user_by_id(record.user_id)
        device = self.store.get_device(record.device_id)
        if user is None or not user.enabled:
            raise AuthError("invalid_refresh_token")
        if device is None or device.revoked_at is not None:
            raise AuthError("invalid_refresh_token")
        self.store.update_refresh_token(replace(record, revoked_at=now))
        device = self.store.update_device(replace(device, last_seen_at=now))
        session = self._create_session(user, device)
        self._audit(
            "refresh",
            user_id=user.id,
            username=user.username,
            device_id=device.id,
        )
        return session

    def logout(self, refresh_token: str) -> None:
        token_hash = hash_token(refresh_token, self.config.token_secret)
        record = self.store.get_refresh_token_by_hash(token_hash)
        if record is not None and record.revoked_at is None:
            now = time.time()
            self.store.update_refresh_token(replace(record, revoked_at=now))
            self._revoke_access_tokens(record.user_id, record.device_id)
            self._audit("logout", user_id=record.user_id, device_id=record.device_id)

    def authenticate_access_token(self, token: str) -> AuthPrincipal | None:
        if not token:
            return None
        token_hash = hash_token(token, self.config.token_secret)
        record = self.store.get_access_token_by_hash(token_hash)
        if record is None:
            return None
        now = time.time()
        if record.revoked_at is not None:
            return None
        if record.expires_at <= now:
            self.store.update_access_token(replace(record, revoked_at=now))
            return None
        user = self.store.get_user_by_id(record.user_id)
        if user is None or not user.enabled:
            self.store.update_access_token(replace(record, revoked_at=now))
            return None
        device = self.store.get_device(record.device_id)
        if device is None or device.revoked_at is not None:
            self.store.update_access_token(replace(record, revoked_at=now))
            return None
        return self._principal_for(user, record.device_id)

    def require_roles(
        self, principal: AuthPrincipal | None, roles: set[str]
    ) -> AuthPrincipal:
        if principal is None:
            raise AuthError("unauthorized")
        if principal.role not in roles:
            raise AuthError("forbidden")
        return principal

    def require_scopes(
        self, principal: AuthPrincipal | None, scopes: set[str]
    ) -> AuthPrincipal:
        if principal is None:
            raise AuthError("unauthorized")
        missing = sorted(scope for scope in scopes if not principal.has_scope(scope))
        if missing:
            raise AuthError("forbidden")
        return principal

    def me(self, principal: AuthPrincipal) -> dict[str, Any]:
        self.require_scopes(principal, {"self:read"})
        device = (
            self.store.get_device(principal.device_id)
            if principal.device_id is not None
            else None
        )
        return {
            "ok": True,
            "user": principal.public_user(),
            "device": (
                {"id": device.id, "label": device.label}
                if device is not None
                else None
            ),
        }

    def bootstrap_token(self, principal: AuthPrincipal, ttl_sec: int) -> dict[str, Any]:
        self.require_scopes(principal, {"peer:bootstrap"})
        token = self.issue_bootstrap_token(ttl_sec)
        self._audit(
            "bootstrap_token_issued",
            user_id=principal.user_id,
            username=principal.username,
            device_id=principal.device_id,
        )
        return {"ok": True, "bootstrap_token": token, "expires_in": ttl_sec}

    def change_password(
        self,
        principal: AuthPrincipal,
        *,
        current_password: str,
        new_password: str,
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"self:password"})
        user = self._require_user(principal.user_id)
        if user.configured:
            raise AuthError(
                "configured_user_immutable",
                "configured superadmin password must be changed in server config",
            )
        if not verify_password(current_password, user.password_hash):
            raise AuthError("invalid_credentials")
        self._validate_password(new_password)
        now = time.time()
        self.store.update_user(
            replace(
                user,
                password_hash=hash_password(
                    new_password,
                    iterations=self.config.password_hash_iterations,
                ),
                updated_at=now,
            )
        )
        revoked = self._revoke_other_device_sessions(user.id, principal.device_id, now)
        self._audit(
            "password_changed",
            user_id=user.id,
            username=user.username,
            device_id=principal.device_id,
            revoked_sessions=revoked,
        )
        return {"ok": True, "revoked_sessions": revoked}

    def list_users(self, principal: AuthPrincipal) -> dict[str, Any]:
        self.require_scopes(principal, {"users:manage"})
        return {"ok": True, "users": [self._public_user(user) for user in self.store.list_users()]}

    def create_user(
        self,
        principal: AuthPrincipal,
        *,
        username: str,
        password: str,
        role: str = "user",
        scopes: object = (),
        enabled: bool = True,
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"users:manage"})
        username = username.strip()
        if not username:
            raise AuthError("username_required")
        if self.store.get_user_by_username(username) is not None:
            raise AuthError("username_exists")
        role = self._validate_role(role)
        normalized_scopes = self._validate_requested_scopes(scopes)
        self._validate_password(password)
        now = time.time()
        user = self.store.upsert_user(
            AuthUser(
                id="usr_" + uuid.uuid4().hex,
                username=username,
                password_hash=hash_password(
                    password,
                    iterations=self.config.password_hash_iterations,
                ),
                role=role,
                scopes=normalized_scopes,
                enabled=bool(enabled),
                created_at=now,
                updated_at=now,
                configured=False,
            )
        )
        self._audit(
            "user_created",
            user_id=principal.user_id,
            username=principal.username,
            target_user_id=user.id,
            target_username=user.username,
            role=user.role,
        )
        return {"ok": True, "user": self._public_user(user)}

    def update_user(
        self,
        principal: AuthPrincipal,
        *,
        user_id: str,
        role: str | None = None,
        scopes: object | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"users:manage"})
        user = self._require_user(user_id)
        if user.configured and (
            (role is not None and role != "superadmin")
            or enabled is False
            or scopes is not None
        ):
            raise AuthError("configured_user_immutable")
        next_role = user.role if role is None else self._validate_role(role)
        next_scopes = user.scopes if scopes is None else self._validate_requested_scopes(scopes)
        next_enabled = user.enabled if enabled is None else bool(enabled)
        if user.configured:
            next_role = "superadmin"
            next_enabled = True
            next_scopes = ()
        updated = self.store.update_user(
            replace(
                user,
                role=next_role,
                scopes=next_scopes,
                enabled=next_enabled,
                updated_at=time.time(),
            )
        )
        if not updated.enabled or updated.role != user.role or updated.scopes != user.scopes:
            self._revoke_user_sessions(updated.id)
        self._audit(
            "user_updated",
            user_id=principal.user_id,
            username=principal.username,
            target_user_id=updated.id,
            target_username=updated.username,
            role=updated.role,
            enabled=updated.enabled,
        )
        return {"ok": True, "user": self._public_user(updated)}

    def disable_user(self, principal: AuthPrincipal, *, user_id: str) -> dict[str, Any]:
        self.require_scopes(principal, {"users:manage"})
        user = self._require_user(user_id)
        if user.configured:
            raise AuthError("configured_user_immutable")
        updated = self.store.update_user(
            replace(user, enabled=False, updated_at=time.time())
        )
        revoked = self._revoke_user_sessions(user.id)
        self._audit(
            "user_disabled",
            user_id=principal.user_id,
            username=principal.username,
            target_user_id=user.id,
            target_username=user.username,
            revoked_sessions=revoked,
        )
        return {"ok": True, "user": self._public_user(updated), "revoked_sessions": revoked}

    def reset_password(
        self, principal: AuthPrincipal, *, user_id: str, password: str
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"users:manage"})
        user = self._require_user(user_id)
        if user.configured:
            raise AuthError("configured_user_immutable")
        self._validate_password(password)
        updated = self.store.update_user(
            replace(
                user,
                password_hash=hash_password(
                    password,
                    iterations=self.config.password_hash_iterations,
                ),
                updated_at=time.time(),
            )
        )
        revoked = self._revoke_user_sessions(updated.id)
        self._audit(
            "password_reset",
            user_id=principal.user_id,
            username=principal.username,
            target_user_id=updated.id,
            target_username=updated.username,
            revoked_sessions=revoked,
        )
        return {"ok": True, "user": self._public_user(updated), "revoked_sessions": revoked}

    def list_devices(
        self, principal: AuthPrincipal, *, user_id: str | None = None
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"devices:read"})
        target_user_id = user_id.strip() if isinstance(user_id, str) else ""
        if principal.role != "superadmin" or not target_user_id:
            target_user_id = principal.user_id
        devices = [
            self._public_device(device)
            for device in self.store.list_devices(user_id=target_user_id)
        ]
        return {"ok": True, "devices": devices}

    def revoke_device(
        self, principal: AuthPrincipal, *, device_id: str
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"devices:revoke"})
        device = self.store.get_device(device_id)
        if device is None:
            raise AuthError("device_not_found")
        if principal.role != "superadmin" and device.user_id != principal.user_id:
            raise AuthError("forbidden")
        now = time.time()
        updated = self.store.update_device(replace(device, revoked_at=now))
        revoked = self.store.revoke_refresh_tokens(
            device_id=device.id,
            revoked_at=now,
        )
        self._revoke_access_tokens(device.user_id, device.id)
        self._audit(
            "device_revoked",
            user_id=principal.user_id,
            username=principal.username,
            device_id=principal.device_id,
            target_device_id=device.id,
            target_user_id=device.user_id,
            revoked_sessions=revoked,
        )
        return {
            "ok": True,
            "device": self._public_device(updated),
            "revoked_sessions": revoked,
        }

    def list_audit_events(
        self,
        principal: AuthPrincipal,
        *,
        limit: int = 100,
        after_created_at: float | None = None,
        event_type: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.require_scopes(principal, {"audit:read"})
        events = self.store.list_audit_events(
            limit=limit,
            after_created_at=after_created_at,
            event_type=event_type,
            user_id=user_id,
        )
        return {"ok": True, "events": events}

    def _create_session(self, user: AuthUser, device: AuthDevice) -> AuthSession:
        now = time.time()
        access_token = "at_" + secrets.token_urlsafe(32)
        access_expires_at = now + self.config.access_token_ttl_sec
        refresh_token = "rt_" + secrets.token_urlsafe(32)
        refresh_expires_at = now + self.config.refresh_token_ttl_sec
        principal = self._principal_for(user, device.id)
        self.store.record_access_token(
            AccessTokenRecord(
                id="at_" + uuid.uuid4().hex,
                user_id=user.id,
                device_id=device.id,
                token_hash=hash_token(access_token, self.config.token_secret),
                expires_at=access_expires_at,
                created_at=now,
            )
        )
        self.store.record_refresh_token(
            RefreshTokenRecord(
                id="rt_" + uuid.uuid4().hex,
                user_id=user.id,
                device_id=device.id,
                token_hash=hash_token(refresh_token, self.config.token_secret),
                expires_at=refresh_expires_at,
                created_at=now,
            )
        )
        return AuthSession(
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
            principal=principal,
            device=device,
        )

    def _sync_configured_superadmins(self) -> None:
        now = time.time()
        for item in self.config.superadmins:
            username = item.username.strip()
            if not username:
                continue
            existing = self.store.get_user_by_username(username)
            user_id = existing.id if existing is not None else "usr_" + uuid.uuid4().hex
            created_at = existing.created_at if existing is not None else now
            last_login_at = existing.last_login_at if existing is not None else None
            self.store.upsert_user(
                AuthUser(
                    id=user_id,
                    username=username,
                    password_hash=item.password_hash,
                    role="superadmin",
                    scopes=(),
                    enabled=True,
                    created_at=created_at,
                    updated_at=now,
                    last_login_at=last_login_at,
                    configured=True,
                )
            )

    def _principal_for(self, user: AuthUser, device_id: str | None) -> AuthPrincipal:
        return AuthPrincipal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            device_id=device_id,
            scopes=effective_scopes(user.role, user.scopes),
        )

    def _public_user(self, user: AuthUser) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "scopes": list(effective_scopes(user.role, user.scopes)),
            "enabled": user.enabled,
            "configured": user.configured,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "last_login_at": user.last_login_at,
        }

    def _public_device(self, device: AuthDevice) -> dict[str, Any]:
        user = self.store.get_user_by_id(device.user_id)
        return {
            "id": device.id,
            "user_id": device.user_id,
            "username": user.username if user is not None else "",
            "label": device.label,
            "created_at": device.created_at,
            "last_seen_at": device.last_seen_at,
            "revoked_at": device.revoked_at,
        }

    def _require_user(self, user_id: str) -> AuthUser:
        user = self.store.get_user_by_id(str(user_id or ""))
        if user is None:
            raise AuthError("user_not_found")
        return user

    def _validate_role(self, role: str) -> str:
        role = str(role or "user").strip()
        if role not in AUTH_ROLES:
            raise AuthError("invalid_role")
        return role

    def _validate_requested_scopes(self, scopes: object) -> tuple[str, ...]:
        if scopes is None:
            return ()
        raw = scopes if isinstance(scopes, (list, tuple, set)) else []
        requested = {str(scope).strip() for scope in raw if str(scope).strip()}
        invalid = sorted(scope for scope in requested if scope not in AUTH_SCOPES)
        if invalid:
            raise AuthError("invalid_scope")
        return normalize_scopes(list(requested))

    def _validate_password(self, password: str) -> None:
        min_length = int(getattr(self.config, "password_min_length", 10) or 10)
        max_length = int(getattr(self.config, "password_max_length", 256) or 256)
        if not isinstance(password, str) or len(password) < min_length:
            raise AuthError("password_policy_failed")
        if len(password) > max_length:
            raise AuthError("password_policy_failed")

    def _check_login_rate_limit(self, username: str, source: str) -> None:
        limit = int(getattr(self.config, "login_rate_limit_count", 5) or 5)
        window = int(getattr(self.config, "login_rate_limit_window_sec", 900) or 900)
        now = time.time()
        self.store.delete_old_login_failures(before=now - window)
        recent = self.store.count_login_failures(
            username=username.strip().lower(),
            source=source,
            since=now - window,
        )
        if recent >= limit:
            self._audit("login_rate_limited", username=username, source_ip=source)
            raise AuthError("rate_limited")

    def _record_failed_login(self, username: str, source: str) -> None:
        self.store.record_login_failure(
            LoginFailureRecord(
                id="lf_" + uuid.uuid4().hex,
                username=username.strip().lower(),
                source=source,
                failed_at=time.time(),
            )
        )

    def _clear_failed_login(self, username: str, source: str) -> None:
        self.store.clear_login_failures(
            username=username.strip().lower(),
            source=source,
        )

    def _revoke_access_tokens(self, user_id: str, device_id: str | None) -> None:
        self.store.revoke_access_tokens(
            user_id=user_id,
            device_id=device_id,
            revoked_at=time.time(),
        )

    def _revoke_other_device_sessions(
        self, user_id: str, current_device_id: str | None, revoked_at: float
    ) -> int:
        revoked = 0
        for token in self.store.list_refresh_tokens(user_id=user_id):
            if token.device_id == current_device_id or token.revoked_at is not None:
                continue
            self.store.update_refresh_token(replace(token, revoked_at=revoked_at))
            revoked += 1
        self.store.revoke_access_tokens(
            user_id=user_id,
            exclude_device_id=current_device_id,
            revoked_at=revoked_at,
        )
        return revoked

    def _revoke_user_sessions(self, user_id: str) -> int:
        now = time.time()
        revoked = self.store.revoke_refresh_tokens(user_id=user_id, revoked_at=now)
        self.store.revoke_access_tokens(user_id=user_id, revoked_at=now)
        return revoked

    def _audit(self, event_type: str, **payload: Any) -> None:
        clean_payload = self._sanitize_payload(payload)
        self.store.append_audit_event(
            {
                "id": "evt_" + uuid.uuid4().hex,
                "type": event_type,
                "created_at": time.time(),
                "user_id": str(clean_payload.pop("user_id", "") or ""),
                "username": str(clean_payload.pop("username", "") or ""),
                "device_id": str(clean_payload.pop("device_id", "") or ""),
                "source_ip": str(clean_payload.pop("source_ip", "") or ""),
                "payload": clean_payload,
            }
        )

    def _sanitize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if key in SENSITIVE_AUDIT_KEYS:
                continue
            if isinstance(value, dict):
                cleaned[key] = self._sanitize_payload(value)
            elif isinstance(value, (list, tuple)):
                cleaned[key] = [
                    self._sanitize_payload(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                cleaned[key] = value
        return cleaned


def validate_auth_config(config: AuthConfig) -> list[str]:
    errors: list[str] = []
    if not config.enabled:
        return errors
    if not config.token_secret:
        errors.append("auth.token_secret is required when auth.enabled is true")
    if config.access_token_ttl_sec < 1:
        errors.append("auth.access_token_ttl_sec must be positive")
    if config.refresh_token_ttl_sec < 1:
        errors.append("auth.refresh_token_ttl_sec must be positive")
    if config.password_hash_iterations < 1:
        errors.append("auth.password_hash_iterations must be positive")
    store_backend = str(getattr(config, "store_backend", "auto") or "auto")
    if store_backend not in {"auto", "file", "postgres"}:
        errors.append("auth.store_backend must be one of auto, file, postgres")
    password_min = int(getattr(config, "password_min_length", 10) or 10)
    password_max = int(getattr(config, "password_max_length", 256) or 256)
    if password_min < 1:
        errors.append("auth.password_min_length must be positive")
    if password_max < password_min:
        errors.append("auth.password_max_length must be >= auth.password_min_length")
    if int(getattr(config, "login_rate_limit_count", 5) or 5) < 1:
        errors.append("auth.login_rate_limit_count must be positive")
    if int(getattr(config, "login_rate_limit_window_sec", 900) or 900) < 1:
        errors.append("auth.login_rate_limit_window_sec must be positive")
    if not config.superadmins:
        errors.append("auth.superadmins must contain at least one superadmin")
    for item in config.superadmins:
        if not item.username.strip():
            errors.append("auth.superadmins.username is required")
        if not item.password_hash.strip():
            errors.append("auth.superadmins.password_hash is required")
        if item.role not in AUTH_ROLES:
            errors.append("auth.superadmins.role must be superadmin")
        if item.role != "superadmin":
            errors.append("auth.superadmins entries must use role superadmin")
    return errors
