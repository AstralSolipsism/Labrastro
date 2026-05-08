from __future__ import annotations

from http import HTTPStatus
from typing import Any

from labrastro_server.services.auth.service import AuthError


class RemoteAuthRoutes:
    def _handle_auth_get(self, path: str) -> bool:
        if path == "/remote/auth/state":
            self._send_json(HTTPStatus.OK, self.service.auth_service.state())
            return True
        if path == "/remote/auth/me":
            principal = self._require_auth()
            if principal is None:
                return True
            self._send_json(HTTPStatus.OK, self.service.auth_service.me(principal))
            return True
        return False

    def _handle_auth_post(self, path: str) -> bool:
        payload = self._read_json()
        try:
            if path == "/remote/auth/login":
                session = self.service.auth_service.login(
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                    str(payload.get("device_label") or "VS Code"),
                    source_ip=str(getattr(self, "client_address", [""])[0] or ""),
                )
                self._send_json(HTTPStatus.OK, session.to_dict())
                return True
            if path == "/remote/auth/refresh":
                session = self.service.auth_service.refresh(
                    str(payload.get("refresh_token") or "")
                )
                self._send_json(HTTPStatus.OK, session.to_dict())
                return True
            if path == "/remote/auth/logout":
                self.service.auth_service.logout(str(payload.get("refresh_token") or ""))
                self._send_json(HTTPStatus.OK, {"ok": True})
                return True
            if path == "/remote/auth/bootstrap-token":
                principal = self._require_auth_scopes({"peer:bootstrap"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.bootstrap_token(
                        principal,
                        self.service.bootstrap_token_ttl_sec,
                    ),
                )
                return True
            if path == "/remote/auth/password/change":
                principal = self._require_auth()
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.change_password(
                        principal,
                        current_password=str(payload.get("current_password") or ""),
                        new_password=str(payload.get("new_password") or ""),
                    ),
                )
                return True
            if path == "/remote/auth/users/list":
                principal = self._require_auth_scopes({"users:manage"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.list_users(principal),
                )
                return True
            if path == "/remote/auth/users/create":
                principal = self._require_auth_scopes({"users:manage"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.create_user(
                        principal,
                        username=str(payload.get("username") or ""),
                        password=str(payload.get("password") or ""),
                        role=str(payload.get("role") or "user"),
                        scopes=payload.get("scopes", []),
                        enabled=bool(payload.get("enabled", True)),
                    ),
                )
                return True
            if path == "/remote/auth/users/update":
                principal = self._require_auth_scopes({"users:manage"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.update_user(
                        principal,
                        user_id=str(payload.get("user_id") or ""),
                        role=(
                            str(payload["role"])
                            if payload.get("role") is not None
                            else None
                        ),
                        scopes=payload.get("scopes") if "scopes" in payload else None,
                        enabled=(
                            bool(payload["enabled"])
                            if payload.get("enabled") is not None
                            else None
                        ),
                    ),
                )
                return True
            if path == "/remote/auth/users/disable":
                principal = self._require_auth_scopes({"users:manage"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.disable_user(
                        principal,
                        user_id=str(payload.get("user_id") or ""),
                    ),
                )
                return True
            if path == "/remote/auth/users/reset-password":
                principal = self._require_auth_scopes({"users:manage"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.reset_password(
                        principal,
                        user_id=str(payload.get("user_id") or ""),
                        password=str(payload.get("password") or ""),
                    ),
                )
                return True
            if path == "/remote/auth/devices/list":
                principal = self._require_auth_scopes({"devices:read"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.list_devices(
                        principal,
                        user_id=(
                            str(payload["user_id"])
                            if payload.get("user_id") is not None
                            else None
                        ),
                    ),
                )
                return True
            if path == "/remote/auth/devices/revoke":
                principal = self._require_auth_scopes({"devices:revoke"})
                if principal is None:
                    return True
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.revoke_device(
                        principal,
                        device_id=str(payload.get("device_id") or ""),
                    ),
                )
                return True
            if path == "/remote/auth/audit/list":
                principal = self._require_auth_scopes({"audit:read"})
                if principal is None:
                    return True
                after_created_at = payload.get("after_created_at")
                self._send_json(
                    HTTPStatus.OK,
                    self.service.auth_service.list_audit_events(
                        principal,
                        limit=int(payload.get("limit") or 100),
                        after_created_at=(
                            float(after_created_at)
                            if after_created_at is not None
                            else None
                        ),
                        event_type=(
                            str(payload["event_type"])
                            if payload.get("event_type") is not None
                            else None
                        ),
                        user_id=(
                            str(payload["user_id"])
                            if payload.get("user_id") is not None
                            else None
                        ),
                    ),
                )
                return True
        except AuthError as exc:
            self._send_auth_error(exc)
            return True
        return False

    def _send_auth_error(self, error: AuthError) -> None:
        status = HTTPStatus.BAD_REQUEST
        if error.code in {"invalid_credentials", "invalid_refresh_token", "unauthorized"}:
            status = HTTPStatus.UNAUTHORIZED
        elif error.code in {"user_disabled", "forbidden"}:
            status = HTTPStatus.FORBIDDEN
        elif error.code == "rate_limited":
            status = HTTPStatus.TOO_MANY_REQUESTS
        elif error.code in {"configured_user_immutable"}:
            status = HTTPStatus.FORBIDDEN
        self._send_error(status, error.code, str(error))
