"""Auth domain models."""

from __future__ import annotations

from dataclasses import dataclass


AUTH_ROLES = {"superadmin", "admin", "user"}
AUTH_SCOPES = {
    "self:read",
    "self:password",
    "devices:read",
    "devices:revoke",
    "peer:bootstrap",
    "admin:read",
    "admin:write",
    "users:manage",
    "audit:read",
}

ROLE_DEFAULT_SCOPES: dict[str, tuple[str, ...]] = {
    "user": (
        "self:read",
        "self:password",
        "devices:read",
        "devices:revoke",
        "peer:bootstrap",
    ),
    "admin": (
        "self:read",
        "self:password",
        "devices:read",
        "devices:revoke",
        "peer:bootstrap",
        "admin:read",
        "admin:write",
    ),
    "superadmin": tuple(sorted(AUTH_SCOPES)),
}


def normalize_scopes(scopes: object) -> tuple[str, ...]:
    if not isinstance(scopes, (list, tuple, set)):
        return ()
    return tuple(
        sorted(
            {
                str(scope).strip()
                for scope in scopes
                if str(scope).strip() in AUTH_SCOPES
            }
        )
    )


def default_scopes_for_role(role: str) -> tuple[str, ...]:
    return ROLE_DEFAULT_SCOPES.get(role, ROLE_DEFAULT_SCOPES["user"])


def effective_scopes(role: str, scopes: object = ()) -> tuple[str, ...]:
    if role == "superadmin":
        return ROLE_DEFAULT_SCOPES["superadmin"]
    return tuple(sorted(set(default_scopes_for_role(role)) | set(normalize_scopes(scopes))))


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    password_hash: str
    role: str
    enabled: bool
    created_at: float
    updated_at: float
    last_login_at: float | None = None
    configured: bool = False
    scopes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "password_hash": self.password_hash,
            "role": self.role,
            "scopes": list(self.scopes),
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
            "configured": self.configured,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuthUser":
        return cls(
            id=str(data["id"]),
            username=str(data["username"]),
            password_hash=str(data["password_hash"]),
            role=str(data.get("role") or "user"),
            scopes=normalize_scopes(data.get("scopes", [])),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at", 0) or 0),
            updated_at=float(data.get("updated_at", 0) or 0),
            last_login_at=(
                float(data["last_login_at"])
                if data.get("last_login_at") is not None
                else None
            ),
            configured=bool(data.get("configured", False)),
        )


@dataclass(frozen=True)
class AuthDevice:
    id: str
    user_id: str
    label: str
    created_at: float
    last_seen_at: float | None = None
    revoked_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "label": self.label,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuthDevice":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            label=str(data.get("label") or ""),
            created_at=float(data.get("created_at", 0) or 0),
            last_seen_at=(
                float(data["last_seen_at"]) if data.get("last_seen_at") is not None else None
            ),
            revoked_at=(
                float(data["revoked_at"]) if data.get("revoked_at") is not None else None
            ),
        )


@dataclass(frozen=True)
class RefreshTokenRecord:
    id: str
    user_id: str
    device_id: str
    token_hash: str
    expires_at: float
    created_at: float
    revoked_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "token_hash": self.token_hash,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RefreshTokenRecord":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            device_id=str(data["device_id"]),
            token_hash=str(data["token_hash"]),
            expires_at=float(data.get("expires_at", 0) or 0),
            created_at=float(data.get("created_at", 0) or 0),
            revoked_at=(
                float(data["revoked_at"]) if data.get("revoked_at") is not None else None
            ),
        )


@dataclass(frozen=True)
class AccessTokenRecord:
    id: str
    user_id: str
    device_id: str
    token_hash: str
    expires_at: float
    created_at: float
    revoked_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "token_hash": self.token_hash,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AccessTokenRecord":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            device_id=str(data["device_id"]),
            token_hash=str(data["token_hash"]),
            expires_at=float(data.get("expires_at", 0) or 0),
            created_at=float(data.get("created_at", 0) or 0),
            revoked_at=(
                float(data["revoked_at"]) if data.get("revoked_at") is not None else None
            ),
        )


@dataclass(frozen=True)
class LoginFailureRecord:
    id: str
    username: str
    source: str
    failed_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "source": self.source,
            "failed_at": self.failed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoginFailureRecord":
        return cls(
            id=str(data["id"]),
            username=str(data["username"]),
            source=str(data["source"]),
            failed_at=float(data.get("failed_at", 0) or 0),
        )


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: str
    username: str
    role: str
    device_id: str | None = None
    scopes: tuple[str, ...] = ()

    def public_user(self) -> dict:
        return {
            "id": self.user_id,
            "username": self.username,
            "role": self.role,
            "scopes": list(self.effective_scopes()),
        }

    def effective_scopes(self) -> tuple[str, ...]:
        return effective_scopes(self.role, self.scopes)

    def has_scope(self, scope: str) -> bool:
        return scope in self.effective_scopes()


@dataclass(frozen=True)
class AuthSession:
    access_token: str
    access_expires_at: float
    refresh_token: str
    refresh_expires_at: float
    principal: AuthPrincipal
    device: AuthDevice

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "access_token": self.access_token,
            "access_expires_at": self.access_expires_at,
            "refresh_token": self.refresh_token,
            "refresh_expires_at": self.refresh_expires_at,
            "user": self.principal.public_user(),
            "device": {
                "id": self.device.id,
                "label": self.device.label,
            },
        }
