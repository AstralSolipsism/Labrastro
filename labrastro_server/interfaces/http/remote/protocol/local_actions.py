"""Local peer action protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


LOCAL_ACTION_SCOPES = frozenset(
    {"activation_scoped", "run_scoped", "admin_task_scoped"}
)
LOCAL_ACTION_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "timed_out"}
)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _action_list(value: Any) -> list["LocalActionRecord"]:
    if not isinstance(value, list):
        return []
    return [
        LocalActionRecord.from_dict(item)
        for item in value
        if isinstance(item, dict)
    ]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _required_text(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(code)
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


@dataclass
class LocalActionRecord:
    scope: str
    local_action_id: str
    action_kind: str
    status: str = "waiting_peer"
    agent_run_id: str | None = None
    activation_id: str | None = None
    session_run_id: str | None = None
    branch_binding_id: str | None = None
    admin_task_id: str | None = None
    requested_by: str | None = None
    peer_id: str | None = None
    workspace_root: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    lease_id: str | None = None
    lease_expires_at: float | None = None
    created_at: float | None = None
    updated_at: float | None = None

    def __post_init__(self) -> None:
        self.scope = _required_text(self.scope, "local_action_scope_required")
        if self.scope not in LOCAL_ACTION_SCOPES:
            raise ValueError("invalid_local_action_scope")
        self.local_action_id = _required_text(
            self.local_action_id,
            "local_action_id_required",
        )
        self.action_kind = _required_text(self.action_kind, "action_kind_required")
        self.status = _required_text(self.status, "local_action_status_required")
        self.agent_run_id = _optional_text(self.agent_run_id)
        self.activation_id = _optional_text(self.activation_id)
        self.session_run_id = _optional_text(self.session_run_id)
        self.branch_binding_id = _optional_text(self.branch_binding_id)
        self.admin_task_id = _optional_text(self.admin_task_id)
        self.requested_by = _optional_text(self.requested_by)
        self.peer_id = _optional_text(self.peer_id)
        self.workspace_root = _optional_text(self.workspace_root)
        self.payload = _dict(self.payload)
        self.progress = _dict(self.progress)
        self.result = _dict(self.result)
        self.error = _optional_text(self.error)
        self.lease_id = _optional_text(self.lease_id)
        self.lease_expires_at = _optional_float(self.lease_expires_at)
        self.created_at = _optional_float(self.created_at)
        self.updated_at = _optional_float(self.updated_at)
        self._validate_scope()

    def _validate_scope(self) -> None:
        if self.scope == "activation_scoped":
            if not self.agent_run_id:
                raise ValueError("agent_run_id_required")
            if not self.activation_id:
                raise ValueError("activation_id_required")
            self._validate_visible_session_scope()
            return
        if self.scope == "run_scoped":
            if not self.agent_run_id:
                raise ValueError("agent_run_id_required")
            self._validate_visible_session_scope()
            return
        if self.scope == "admin_task_scoped":
            if not self.admin_task_id:
                raise ValueError("admin_task_id_required")
            if not self.requested_by:
                raise ValueError("requested_by_required")

    def _validate_visible_session_scope(self) -> None:
        has_session = bool(self.session_run_id)
        has_branch = bool(self.branch_binding_id)
        if has_session != has_branch:
            raise ValueError("session_run_branch_binding_required")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": self.scope,
            "local_action_id": self.local_action_id,
            "action_kind": self.action_kind,
            "status": self.status,
            "payload": dict(self.payload),
        }
        for key in (
            "agent_run_id",
            "activation_id",
            "session_run_id",
            "branch_binding_id",
            "admin_task_id",
            "requested_by",
            "peer_id",
            "workspace_root",
            "error",
            "lease_id",
            "lease_expires_at",
            "created_at",
            "updated_at",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.progress:
            payload["progress"] = dict(self.progress)
        if self.result:
            payload["result"] = dict(self.result)
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionRecord":
        return cls(
            scope=str(d.get("scope") or ""),
            local_action_id=str(d.get("local_action_id") or ""),
            action_kind=str(d.get("action_kind") or ""),
            status=str(d.get("status") or "waiting_peer"),
            agent_run_id=d.get("agent_run_id"),
            activation_id=d.get("activation_id"),
            session_run_id=d.get("session_run_id"),
            branch_binding_id=d.get("branch_binding_id"),
            admin_task_id=d.get("admin_task_id"),
            requested_by=d.get("requested_by"),
            peer_id=d.get("peer_id"),
            workspace_root=d.get("workspace_root"),
            payload=_dict(d.get("payload")),
            progress=_dict(d.get("progress")),
            result=_dict(d.get("result")),
            error=d.get("error"),
            lease_id=d.get("lease_id"),
            lease_expires_at=d.get("lease_expires_at"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


@dataclass
class LocalActionClaimRequest:
    peer_token: str
    peer_id: str
    worker_kind: str
    features: list[str] = field(default_factory=list)
    workspace_root: str | None = None
    max_actions: int = 1

    def __post_init__(self) -> None:
        self.peer_token = _required_text(self.peer_token, "peer_token_required")
        self.peer_id = _required_text(self.peer_id, "peer_id_required")
        self.worker_kind = _required_text(self.worker_kind, "worker_kind_required")
        self.features = _str_list(self.features)
        self.workspace_root = _optional_text(self.workspace_root)
        self.max_actions = max(1, int(self.max_actions or 1))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "peer_id": self.peer_id,
            "worker_kind": self.worker_kind,
            "features": list(self.features),
            "max_actions": self.max_actions,
        }
        if self.workspace_root is not None:
            payload["workspace_root"] = self.workspace_root
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionClaimRequest":
        return cls(
            peer_token=str(d.get("peer_token") or ""),
            peer_id=str(d.get("peer_id") or ""),
            worker_kind=str(d.get("worker_kind") or ""),
            features=_str_list(d.get("features")),
            workspace_root=d.get("workspace_root"),
            max_actions=int(d.get("max_actions") or 1),
        )


@dataclass
class LocalActionClaimResponse:
    actions: list[LocalActionRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"actions": [action.to_dict() for action in self.actions]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionClaimResponse":
        return cls(actions=_action_list(d.get("actions")))


@dataclass
class LocalActionProgressRequest:
    local_action_id: str
    status: str = "progress"
    peer_token: str = ""
    lease_id: str = ""
    progress: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "local_action_id": self.local_action_id,
            "status": self.status,
            "progress": dict(self.progress),
        }
        if self.peer_token:
            payload["peer_token"] = self.peer_token
        if self.lease_id:
            payload["lease_id"] = self.lease_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionProgressRequest":
        return cls(
            local_action_id=str(d.get("local_action_id") or ""),
            status=str(d.get("status") or "progress"),
            peer_token=str(d.get("peer_token") or ""),
            lease_id=str(d.get("lease_id") or ""),
            progress=_dict(d.get("progress")),
        )


@dataclass
class LocalActionCompleteRequest:
    local_action_id: str
    status: str
    peer_token: str = ""
    lease_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "local_action_id": self.local_action_id,
            "status": self.status,
            "result": dict(self.result),
        }
        if self.peer_token:
            payload["peer_token"] = self.peer_token
        if self.lease_id:
            payload["lease_id"] = self.lease_id
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionCompleteRequest":
        return cls(
            local_action_id=str(d.get("local_action_id") or ""),
            status=str(d.get("status") or ""),
            peer_token=str(d.get("peer_token") or ""),
            lease_id=str(d.get("lease_id") or ""),
            result=_dict(d.get("result")),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


@dataclass
class LocalActionCompleteResponse:
    ok: bool
    action: LocalActionRecord | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.action is not None:
            payload["action"] = self.action.to_dict()
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionCompleteResponse":
        action = d.get("action")
        return cls(
            ok=bool(d.get("ok", False)),
            action=(
                LocalActionRecord.from_dict(action)
                if isinstance(action, dict)
                else None
            ),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


@dataclass
class LocalActionProgressResponse:
    ok: bool
    action: LocalActionRecord | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.action is not None:
            payload["action"] = self.action.to_dict()
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionProgressResponse":
        action = d.get("action")
        return cls(
            ok=bool(d.get("ok", False)),
            action=(
                LocalActionRecord.from_dict(action)
                if isinstance(action, dict)
                else None
            ),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


@dataclass
class LocalActionCancelRequest:
    local_action_id: str
    peer_token: str = ""
    lease_id: str = ""
    reason: str = "cancelled"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "local_action_id": self.local_action_id,
            "reason": self.reason,
        }
        if self.peer_token:
            payload["peer_token"] = self.peer_token
        if self.lease_id:
            payload["lease_id"] = self.lease_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionCancelRequest":
        return cls(
            local_action_id=str(d.get("local_action_id") or ""),
            peer_token=str(d.get("peer_token") or ""),
            lease_id=str(d.get("lease_id") or ""),
            reason=str(d.get("reason") or "cancelled"),
        )


@dataclass
class LocalActionCancelResponse:
    ok: bool
    action: LocalActionRecord | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.action is not None:
            payload["action"] = self.action.to_dict()
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalActionCancelResponse":
        action = d.get("action")
        return cls(
            ok=bool(d.get("ok", False)),
            action=(
                LocalActionRecord.from_dict(action)
                if isinstance(action, dict)
                else None
            ),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


__all__ = [
    "LOCAL_ACTION_SCOPES",
    "LOCAL_ACTION_TERMINAL_STATUSES",
    "LocalActionCancelRequest",
    "LocalActionCancelResponse",
    "LocalActionClaimRequest",
    "LocalActionClaimResponse",
    "LocalActionCompleteRequest",
    "LocalActionCompleteResponse",
    "LocalActionProgressRequest",
    "LocalActionProgressResponse",
    "LocalActionRecord",
]
