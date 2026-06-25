"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._scope import required_branch_binding_id, required_session_run_id

def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]

@dataclass
class SessionRunStartRequest:
    peer_token: str
    prompt: str
    session_hint: str | None = None
    client_request_id: str | None = None
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    locale: str | None = None
    mentions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "peer_token": self.peer_token,
            "prompt": self.prompt,
            "session_hint": self.session_hint,
        }
        if self.client_request_id is not None:
            payload["client_request_id"] = self.client_request_id
        if self.mode is not None:
            payload["mode"] = self.mode
        if self.workflow_mode is not None:
            payload["workflow_mode"] = self.workflow_mode
        if self.taskflow_id is not None:
            payload["taskflow_id"] = self.taskflow_id
        if self.provider_id is not None:
            payload["provider_id"] = self.provider_id
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.parameters:
            payload["parameters"] = dict(self.parameters)
        if self.locale is not None:
            payload["locale"] = self.locale
        if self.mentions:
            payload["mentions"] = list(self.mentions)
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunStartRequest":
        parameters = d.get("parameters")
        return cls(
            peer_token=d["peer_token"],
            prompt=d["prompt"],
            session_hint=d.get("session_hint"),
            client_request_id=d.get("client_request_id"),
            mode=d.get("mode"),
            workflow_mode=d.get("workflow_mode"),
            taskflow_id=d.get("taskflow_id"),
            provider_id=d.get("provider_id"),
            model_id=d.get("model_id"),
            parameters=parameters if isinstance(parameters, dict) else {},
            locale=d.get("locale"),
            mentions=_dict_list(d.get("mentions")),
        )

@dataclass
class SessionRunStartResponse:
    session_run_id: str
    session_id: str | None = None
    agent_run_id: str | None = None
    activation_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None
    agent_id: str | None = None
    workflow_mode: str | None = None
    runtime_state: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"session_run_id": self.session_run_id, "error": self.error}
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.activation_id is not None:
            payload["activation_id"] = self.activation_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        if self.agent_id is not None:
            payload["agent_id"] = self.agent_id
        if self.workflow_mode is not None:
            payload["workflow_mode"] = self.workflow_mode
        if self.runtime_state:
            payload["runtime_state"] = dict(self.runtime_state)
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunStartResponse":
        return cls(
            session_run_id=d.get("session_run_id", ""),
            session_id=d.get("session_id") if isinstance(d.get("session_id"), str) else None,
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            activation_id=d.get("activation_id") if isinstance(d.get("activation_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
            agent_id=d.get("agent_id") if isinstance(d.get("agent_id"), str) else None,
            workflow_mode=d.get("workflow_mode") if isinstance(d.get("workflow_mode"), str) else None,
            runtime_state=d.get("runtime_state") if isinstance(d.get("runtime_state"), dict) else {},
            error=d.get("error"),
        )

@dataclass
class ChatCommandDispatchRequest:
    peer_token: str
    text: str = ""
    command_id: str = ""
    trigger: str = ""
    args: str = ""
    session_hint: str | None = None
    client_request_id: str | None = None
    mentions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def command_text(self) -> str:
        text = str(self.text or "").strip()
        if text:
            return text
        trigger = str(self.trigger or "").strip()
        args = str(self.args or "").strip()
        return f"{trigger} {args}".strip()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "text": self.text,
            "command_id": self.command_id,
            "trigger": self.trigger,
            "args": self.args,
        }
        if self.session_hint is not None:
            payload["session_hint"] = self.session_hint
        if self.client_request_id is not None:
            payload["client_request_id"] = self.client_request_id
        if self.mentions:
            payload["mentions"] = list(self.mentions)
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatCommandDispatchRequest":
        return cls(
            peer_token=d["peer_token"],
            text=str(d.get("text") or ""),
            command_id=str(d.get("command_id") or ""),
            trigger=str(d.get("trigger") or ""),
            args=str(d.get("args") or ""),
            session_hint=d.get("session_hint"),
            client_request_id=d.get("client_request_id"),
            mentions=_dict_list(d.get("mentions")),
        )


@dataclass
class ChatCommandDispatchResponse:
    ok: bool
    action: str = "continue"
    session_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "events": list(self.events),
        }
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatCommandDispatchResponse":
        events = d.get("events")
        return cls(
            ok=bool(d.get("ok")),
            action=str(d.get("action") or "continue"),
            session_id=d.get("session_id") if isinstance(d.get("session_id"), str) else None,
            events=events if isinstance(events, list) else [],
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )

@dataclass
class SessionRunEventsRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    cursor: int = 0
    timeout_sec: float = 30.0

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "cursor": self.cursor,
            "timeout_sec": self.timeout_sec,
        }
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunEventsRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            cursor=int(d.get("cursor", 0)),
            timeout_sec=float(d.get("timeout_sec", 30.0)),
        )

@dataclass
class SessionRunEventsBatch:
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    next_cursor: int = 0
    error: str | None = None
    branches: list[dict[str, Any]] = field(default_factory=list)
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "events": self.events,
            "done": self.done,
            "next_cursor": self.next_cursor,
            "error": self.error,
            "branches": _dict_list(self.branches),
        }
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunEventsBatch":
        return cls(
            events=list(d.get("events", [])),
            done=bool(d.get("done", False)),
            next_cursor=int(d.get("next_cursor", 0)),
            error=d.get("error"),
            branches=_dict_list(d.get("branches")),
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
        )

@dataclass
class SessionRunStatusRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    cursor: int = 0

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "cursor": self.cursor,
        }
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunStatusRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            cursor=int(d.get("cursor", 0)),
        )

@dataclass
class SessionRunBranchSelectRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    cursor: int = 0

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "cursor": self.cursor,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunBranchSelectRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            cursor=int(d.get("cursor", 0)),
        )

@dataclass
class SessionRunStatusResponse:
    ok: bool
    session_run_id: str
    status: str
    running: bool
    done: bool
    reconnectable: bool
    cursor: int
    next_cursor: int
    first_available_seq: int
    latest_seq: int
    dropped_count: int = 0
    peer_id: str | None = None
    session_id: str | None = None
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_id: str | None = None
    agent_id: str | None = None
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None
    runtime_state: dict[str, Any] = field(default_factory=dict)
    created_at: float | None = None
    last_activity_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    recovery: dict[str, Any] | None = None
    approvals: list[dict[str, Any]] = field(default_factory=list)
    user_inputs: list[dict[str, Any]] = field(default_factory=list)
    branches: list[dict[str, Any]] = field(default_factory=list)
    terminal: bool = False
    bindingStatus: str = "active"
    recoverable: bool = False
    eventStreamAllowed: bool = False
    projectionState: str = "live"
    mainlineState: str = "none"
    agentRunState: str = "none"
    activationState: str = "none"
    working: bool = False
    continuable: bool = False
    closedReason: str | None = None
    transportState: str = "disconnected"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_run_id": self.session_run_id,
            "peer_id": self.peer_id,
            "status": self.status,
            "running": self.running,
            "done": self.done,
            "reconnectable": self.reconnectable,
            "terminal": self.terminal,
            "bindingStatus": self.bindingStatus,
            "recoverable": self.recoverable,
            "eventStreamAllowed": self.eventStreamAllowed,
            "projectionState": self.projectionState,
            "mainlineState": self.mainlineState,
            "agentRunState": self.agentRunState,
            "activationState": self.activationState,
            "working": self.working,
            "continuable": self.continuable,
            "closedReason": self.closedReason,
            "transportState": self.transportState,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "first_available_seq": self.first_available_seq,
            "latest_seq": self.latest_seq,
            "dropped_count": self.dropped_count,
            "session_id": self.session_id,
            "mode": self.mode,
            "workflow_mode": self.workflow_mode,
            "taskflow_id": self.taskflow_id,
            "agent_id": self.agent_id,
            "agent_run_id": self.agent_run_id,
            "branch_binding_id": self.branch_binding_id,
            "scope_id": self.scope_id,
            "selected": self.selected,
            "runtime_state": dict(self.runtime_state),
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "recovery": dict(self.recovery) if isinstance(self.recovery, dict) else None,
            "approvals": _dict_list(self.approvals),
            "user_inputs": _dict_list(self.user_inputs),
            "branches": _dict_list(self.branches),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunStatusResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            session_run_id=str(d.get("session_run_id", "")),
            peer_id=d.get("peer_id") if isinstance(d.get("peer_id"), str) else None,
            status=str(d.get("status", "")),
            running=bool(d.get("running", False)),
            done=bool(d.get("done", False)),
            reconnectable=bool(d.get("reconnectable", False)),
            terminal=bool(d.get("terminal", False)),
            bindingStatus=str(d.get("bindingStatus") or "active"),
            recoverable=bool(d.get("recoverable", False)),
            eventStreamAllowed=bool(d.get("eventStreamAllowed", False)),
            projectionState=str(d.get("projectionState") or "live"),
            mainlineState=str(d.get("mainlineState") or "none"),
            agentRunState=str(d.get("agentRunState") or "none"),
            activationState=str(d.get("activationState") or "none"),
            working=bool(d.get("working", False)),
            continuable=bool(d.get("continuable", False)),
            closedReason=d.get("closedReason") if isinstance(d.get("closedReason"), str) else None,
            transportState=str(d.get("transportState") or "disconnected"),
            cursor=int(d.get("cursor", 0)),
            next_cursor=int(d.get("next_cursor", 0)),
            first_available_seq=int(d.get("first_available_seq", 0)),
            latest_seq=int(d.get("latest_seq", 0)),
            dropped_count=int(d.get("dropped_count", 0)),
            session_id=d.get("session_id") if isinstance(d.get("session_id"), str) else None,
            mode=d.get("mode") if isinstance(d.get("mode"), str) else None,
            workflow_mode=d.get("workflow_mode") if isinstance(d.get("workflow_mode"), str) else None,
            taskflow_id=d.get("taskflow_id") if isinstance(d.get("taskflow_id"), str) else None,
            agent_id=d.get("agent_id") if isinstance(d.get("agent_id"), str) else None,
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
            runtime_state=d.get("runtime_state") if isinstance(d.get("runtime_state"), dict) else {},
            created_at=float(d["created_at"]) if d.get("created_at") is not None else None,
            last_activity_at=float(d["last_activity_at"])
            if d.get("last_activity_at") is not None
            else None,
            finished_at=float(d["finished_at"]) if d.get("finished_at") is not None else None,
            error=d.get("error") if isinstance(d.get("error"), str) else None,
            recovery=d.get("recovery") if isinstance(d.get("recovery"), dict) else None,
            approvals=_dict_list(d.get("approvals")),
            user_inputs=_dict_list(d.get("user_inputs")),
            branches=_dict_list(d.get("branches")),
        )

@dataclass
class SessionRunCancelRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    reason: str | None = None

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunCancelRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            reason=d.get("reason"),
        )

@dataclass
class SessionRunCancelResponse:
    ok: bool
    session_run_id: str | None = None
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "error": self.error}
        if self.session_run_id is not None:
            payload["session_run_id"] = self.session_run_id
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunCancelResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            session_run_id=d.get("session_run_id") if isinstance(d.get("session_run_id"), str) else None,
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
            error=d.get("error"),
        )


@dataclass
class SessionRunStopRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    reason: str | None = None

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunStopRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            reason=d.get("reason"),
        )


@dataclass
class SessionRunStopResponse:
    ok: bool
    session_run_id: str | None = None
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "error": self.error}
        if self.session_run_id is not None:
            payload["session_run_id"] = self.session_run_id
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunStopResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            session_run_id=d.get("session_run_id") if isinstance(d.get("session_run_id"), str) else None,
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
            error=d.get("error"),
        )


@dataclass
class SessionRunContinueRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    prompt: str
    client_request_id: str | None = None
    locale: str | None = None
    mentions: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "prompt": self.prompt,
        }
        if self.client_request_id is not None:
            payload["client_request_id"] = self.client_request_id
        if self.locale is not None:
            payload["locale"] = self.locale
        if self.mentions:
            payload["mentions"] = list(self.mentions)
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunContinueRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            prompt=d["prompt"],
            client_request_id=d.get("client_request_id"),
            locale=d.get("locale"),
            mentions=_dict_list(d.get("mentions")),
        )


@dataclass
class SessionRunContinueResponse:
    ok: bool
    session_run_id: str
    activation_id: str | None = None
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "session_run_id": self.session_run_id,
            "error": self.error,
        }
        if self.activation_id is not None:
            payload["activation_id"] = self.activation_id
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunContinueResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            session_run_id=str(d.get("session_run_id") or ""),
            activation_id=d.get("activation_id")
            if isinstance(d.get("activation_id"), str)
            else None,
            agent_run_id=d.get("agent_run_id")
            if isinstance(d.get("agent_run_id"), str)
            else None,
            branch_binding_id=d.get("branch_binding_id")
            if isinstance(d.get("branch_binding_id"), str)
            else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


@dataclass
class SessionRunRecoverRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    action: str = "continue"

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "action": self.action,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunRecoverRequest":
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            action=str(d.get("action") or "continue"),
        )

@dataclass
class SessionRunRecoverResponse:
    ok: bool
    session_run_id: str
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None
    state: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "session_run_id": self.session_run_id,
            "error": self.error,
        }
        if self.state is not None:
            payload["state"] = self.state
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunRecoverResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            session_run_id=str(d.get("session_run_id") or ""),
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
            state=d.get("state") if isinstance(d.get("state"), str) else None,
            error=d.get("error"),
        )

@dataclass
class SessionRunUserInputReplyRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    input_id: str
    action: str
    content: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "input_id": self.input_id,
            "action": self.action,
            "content": dict(self.content),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunUserInputReplyRequest":
        content = d.get("content")
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            input_id=d.get("input_id"),
            action=str(d.get("action") or "decline"),
            content=content if isinstance(content, dict) else {},
            reason=d.get("reason") if isinstance(d.get("reason"), str) else None,
        )


@dataclass
class SessionRunUserInputReplyResponse:
    ok: bool
    error: str | None = None
    state: str | None = None
    session_run_id: str | None = None
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "error": self.error, "state": self.state}
        if self.session_run_id is not None:
            payload["session_run_id"] = self.session_run_id
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunUserInputReplyResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            error=d.get("error"),
            state=d.get("state") if isinstance(d.get("state"), str) else None,
            session_run_id=d.get("session_run_id") if isinstance(d.get("session_run_id"), str) else None,
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
        )


@dataclass
class ApprovalReplyRequest:
    peer_token: str
    session_run_id: str
    branch_binding_id: str
    approval_id: str
    decision: str
    reason: str | None = None
    approved_save_candidate: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "peer_token": self.peer_token,
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "approval_id": self.approval_id,
            "decision": self.decision,
            "reason": self.reason,
        }
        if self.approved_save_candidate:
            payload["approved_save_candidate"] = dict(self.approved_save_candidate)
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalReplyRequest":
        approved_save_candidate = d.get("approved_save_candidate")
        return cls(
            peer_token=d["peer_token"],
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            approval_id=d["approval_id"],
            decision=d["decision"],
            reason=d.get("reason"),
            approved_save_candidate=(
                dict(approved_save_candidate)
                if isinstance(approved_save_candidate, dict)
                else {}
            ),
        )

@dataclass
class ApprovalReplyResponse:
    ok: bool
    error: str | None = None
    state: str | None = None
    session_run_id: str | None = None
    agent_run_id: str | None = None
    branch_binding_id: str | None = None
    scope_id: str | None = None
    selected: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "error": self.error, "state": self.state}
        if self.session_run_id is not None:
            payload["session_run_id"] = self.session_run_id
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        if self.scope_id is not None:
            payload["scope_id"] = self.scope_id
        if self.selected is not None:
            payload["selected"] = self.selected
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalReplyResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            error=d.get("error"),
            state=d.get("state") if isinstance(d.get("state"), str) else None,
            session_run_id=d.get("session_run_id") if isinstance(d.get("session_run_id"), str) else None,
            agent_run_id=d.get("agent_run_id") if isinstance(d.get("agent_run_id"), str) else None,
            branch_binding_id=d.get("branch_binding_id") if isinstance(d.get("branch_binding_id"), str) else None,
            scope_id=d.get("scope_id") if isinstance(d.get("scope_id"), str) else None,
            selected=bool(d.get("selected")) if "selected" in d else None,
        )


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

__all__ = [
    "SessionRunStartRequest",
    "SessionRunStartResponse",
    "SessionRunEventsRequest",
    "SessionRunEventsBatch",
    "SessionRunStatusRequest",
    "SessionRunStatusResponse",
    "SessionRunCancelRequest",
    "SessionRunCancelResponse",
    "SessionRunStopRequest",
    "SessionRunStopResponse",
    "SessionRunContinueRequest",
    "SessionRunContinueResponse",
    "SessionRunRecoverRequest",
    "SessionRunRecoverResponse",
    "SessionRunUserInputReplyRequest",
    "SessionRunUserInputReplyResponse",
    "ApprovalReplyRequest",
    "ApprovalReplyResponse",
]
