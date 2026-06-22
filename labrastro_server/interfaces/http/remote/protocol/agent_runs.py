"""AgentRun remote protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._scope import required_branch_binding_id, required_session_run_id


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


@dataclass
class AgentRunEventsQuery:
    peer_token: str = ""
    after_seq: int = 0
    timeout_sec: float = 0.0
    limit: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "after_seq": self.after_seq,
            "timeout_sec": self.timeout_sec,
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunEventsQuery":
        return cls(
            peer_token=str(d.get("peer_token") or ""),
            after_seq=int(d.get("after_seq") or 0),
            timeout_sec=float(d.get("timeout_sec") or 0),
            limit=int(d.get("limit") or 200),
        )


@dataclass
class AgentRunEventsResponse:
    ok: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 0
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "events": list(self.events),
            "next_seq": self.next_seq,
            "has_more": self.has_more,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunEventsResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            events=_dict_list(d.get("events")),
            next_seq=int(d.get("next_seq") or 0),
            has_more=bool(d.get("has_more", False)),
        )


@dataclass
class AgentRunActivationClaimRequest:
    peer_token: str
    worker_id: str | None = None
    worker_kind: str | None = None
    executors: list[str] = field(default_factory=list)
    wait_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "executors": list(self.executors),
            "wait_sec": self.wait_sec,
        }
        if self.worker_id is not None:
            payload["worker_id"] = self.worker_id
        if self.worker_kind is not None:
            payload["worker_kind"] = self.worker_kind
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationClaimRequest":
        return cls(
            peer_token=d["peer_token"],
            worker_id=d.get("worker_id") if isinstance(d.get("worker_id"), str) else None,
            worker_kind=d.get("worker_kind")
            if isinstance(d.get("worker_kind"), str)
            else None,
            executors=_str_list(d.get("executors")),
            wait_sec=float(d.get("wait_sec") or 0),
        )


@dataclass
class AgentRunActivationClaimResponse:
    claim: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"claim": dict(self.claim) if isinstance(self.claim, dict) else None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationClaimResponse":
        claim = d.get("claim")
        return cls(claim=dict(claim) if isinstance(claim, dict) else None)


@dataclass
class AgentRunActivationHeartbeatRequest:
    peer_token: str
    request_id: str
    agent_run_id: str
    activation_id: str
    worker_id: str
    lease_sec: int | None = None
    delivered_steer_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "request_id": self.request_id,
            "agent_run_id": self.agent_run_id,
            "activation_id": self.activation_id,
            "worker_id": self.worker_id,
            "delivered_steer_ids": list(self.delivered_steer_ids),
        }
        if self.lease_sec is not None:
            payload["lease_sec"] = self.lease_sec
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationHeartbeatRequest":
        return cls(
            peer_token=d["peer_token"],
            request_id=d["request_id"],
            agent_run_id=d["agent_run_id"],
            activation_id=d["activation_id"],
            worker_id=d["worker_id"],
            lease_sec=int(d["lease_sec"]) if d.get("lease_sec") is not None else None,
            delivered_steer_ids=_str_list(d.get("delivered_steer_ids")),
        )


@dataclass
class AgentRunActivationHeartbeatResponse:
    ok: bool
    cancel_requested: bool = False
    reason: str | None = None
    lease_sec: int | None = None
    activation_steers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "cancel_requested": self.cancel_requested,
            "activation_steers": list(self.activation_steers),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.lease_sec is not None:
            payload["lease_sec"] = self.lease_sec
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationHeartbeatResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            cancel_requested=bool(d.get("cancel_requested", False)),
            reason=d.get("reason") if isinstance(d.get("reason"), str) else None,
            lease_sec=int(d["lease_sec"]) if d.get("lease_sec") is not None else None,
            activation_steers=_dict_list(d.get("activation_steers")),
        )


@dataclass
class AgentRunActivationSessionPinRequest:
    peer_token: str
    request_id: str
    agent_run_id: str
    activation_id: str
    worker_id: str
    workdir: str | None = None
    branch: str | None = None
    executor_session_id: str | None = None
    repo_url: str | None = None
    cache_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "peer_token": self.peer_token,
            "request_id": self.request_id,
            "agent_run_id": self.agent_run_id,
            "activation_id": self.activation_id,
            "worker_id": self.worker_id,
        }
        for key in ("workdir", "branch", "executor_session_id", "repo_url", "cache_path"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationSessionPinRequest":
        return cls(
            peer_token=d["peer_token"],
            request_id=d["request_id"],
            agent_run_id=d["agent_run_id"],
            activation_id=d["activation_id"],
            worker_id=d["worker_id"],
            workdir=d.get("workdir") if isinstance(d.get("workdir"), str) else None,
            branch=d.get("branch") if isinstance(d.get("branch"), str) else None,
            executor_session_id=d.get("executor_session_id")
            if isinstance(d.get("executor_session_id"), str)
            else None,
            repo_url=d.get("repo_url") if isinstance(d.get("repo_url"), str) else None,
            cache_path=d.get("cache_path") if isinstance(d.get("cache_path"), str) else None,
        )


@dataclass
class AgentRunActivationSessionPinResponse:
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationSessionPinResponse":
        return cls(ok=bool(d.get("ok", False)), error=d.get("error"))


@dataclass
class AgentRunActivationEventRequest:
    peer_token: str
    request_id: str
    agent_run_id: str
    activation_id: str
    worker_id: str
    type: str
    text: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "request_id": self.request_id,
            "agent_run_id": self.agent_run_id,
            "activation_id": self.activation_id,
            "worker_id": self.worker_id,
            "type": self.type,
            "data": dict(self.data),
        }
        if self.text is not None:
            payload["text"] = self.text
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationEventRequest":
        return cls(
            peer_token=d["peer_token"],
            request_id=d["request_id"],
            agent_run_id=d["agent_run_id"],
            activation_id=d["activation_id"],
            worker_id=d["worker_id"],
            type=d["type"],
            text=d.get("text") if isinstance(d.get("text"), str) else None,
            data=_dict(d.get("data")),
        )


@dataclass
class AgentRunActivationCompleteEvent:
    type: str
    text: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "data": dict(self.data)}
        if self.text is not None:
            payload["text"] = self.text
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationCompleteEvent":
        return cls(
            type=str(d.get("type") or "status"),
            text=d.get("text") if isinstance(d.get("text"), str) else None,
            data=_dict(d.get("data")),
        )


@dataclass
class AgentRunActivationModelRequest:
    peer_token: str
    request_id: str
    agent_run_id: str
    activation_id: str
    worker_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    stream: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "request_id": self.request_id,
            "agent_run_id": self.agent_run_id,
            "activation_id": self.activation_id,
            "worker_id": self.worker_id,
            "messages": list(self.messages),
            "stream": self.stream,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationModelRequest":
        return cls(
            peer_token=d["peer_token"],
            request_id=d["request_id"],
            agent_run_id=d["agent_run_id"],
            activation_id=d["activation_id"],
            worker_id=d["worker_id"],
            messages=_dict_list(d.get("messages")),
            stream=d.get("stream") is not False,
            metadata=_dict(d.get("metadata")),
        )


@dataclass
class AgentRunActivationModelResponse:
    ok: bool
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"ok": self.ok, "response": dict(self.response)}
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationModelResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            response=_dict(d.get("response")),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


@dataclass
class AgentRunActivationCompleteRequest:
    peer_token: str
    request_id: str
    agent_run_id: str
    activation_id: str
    worker_id: str
    status: str
    output: str = ""
    error: str | None = None
    executor_session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    events: list[AgentRunActivationCompleteEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "request_id": self.request_id,
            "agent_run_id": self.agent_run_id,
            "activation_id": self.activation_id,
            "worker_id": self.worker_id,
            "status": self.status,
            "output": self.output,
            "usage": dict(self.usage),
            "artifacts": list(self.artifacts),
            "events": [event.to_dict() for event in self.events],
        }
        if self.error is not None:
            payload["error"] = self.error
        if self.executor_session_id is not None:
            payload["executor_session_id"] = self.executor_session_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationCompleteRequest":
        return cls(
            peer_token=d["peer_token"],
            request_id=d["request_id"],
            agent_run_id=d["agent_run_id"],
            activation_id=d["activation_id"],
            worker_id=d["worker_id"],
            status=d["status"],
            output=str(d.get("output") or ""),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
            executor_session_id=d.get("executor_session_id")
            if isinstance(d.get("executor_session_id"), str)
            else None,
            usage=_dict(d.get("usage")),
            artifacts=_dict_list(d.get("artifacts")),
            events=[
                AgentRunActivationCompleteEvent.from_dict(item)
                for item in _dict_list(d.get("events"))
            ],
        )


@dataclass
class AgentRunActivationCompleteResponse:
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunActivationCompleteResponse":
        return cls(ok=bool(d.get("ok", False)), error=d.get("error"))


@dataclass
class AgentRunRequest:
    agent_id: str = "default"
    prompt: str = ""
    source: str = "manual"
    agent_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "agent_id": self.agent_id,
            "prompt": self.prompt,
            "source": self.source,
            "metadata": dict(self.metadata),
        }
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunRequest":
        return cls(
            agent_id=str(d.get("agent_id") or "default"),
            prompt=str(d.get("prompt") or ""),
            source=str(d.get("source") or "manual"),
            agent_run_id=d.get("agent_run_id")
            if isinstance(d.get("agent_run_id"), str)
            else None,
            metadata=_dict(d.get("metadata")),
        )


@dataclass
class AgentRunResponse:
    ok: bool
    agent_run: dict[str, Any] = field(default_factory=dict)
    branch_binding_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"ok": self.ok, "agent_run": dict(self.agent_run)}
        if self.branch_binding_id is not None:
            payload["branch_binding_id"] = self.branch_binding_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            agent_run=_dict(d.get("agent_run")),
            branch_binding_id=d.get("branch_binding_id")
            if isinstance(d.get("branch_binding_id"), str)
            else None,
        )


@dataclass
class AgentRunAdminEventsRequest:
    agent_run_id: str
    after_seq: int = 0
    limit: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.agent_run_id,
            "after_seq": self.after_seq,
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunAdminEventsRequest":
        return cls(
            agent_run_id=d["agent_run_id"],
            after_seq=int(d.get("after_seq") or 0),
            limit=int(d.get("limit") or 200),
        )


@dataclass
class AgentRunCancelRequest:
    agent_run_id: str
    reason: str = "user_cancelled"

    def to_dict(self) -> dict[str, Any]:
        return {"agent_run_id": self.agent_run_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunCancelRequest":
        return cls(
            agent_run_id=d["agent_run_id"],
            reason=str(d.get("reason") or "user_cancelled"),
        )


@dataclass
class AgentRunCancelResponse:
    ok: bool
    agent_run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "agent_run_id": self.agent_run_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunCancelResponse":
        return cls(ok=bool(d.get("ok", False)), agent_run_id=str(d.get("agent_run_id") or ""))


@dataclass
class AgentRunRetryRequest:
    agent_run_id: str
    resume_session: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"agent_run_id": self.agent_run_id, "resume_session": self.resume_session}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunRetryRequest":
        return cls(
            agent_run_id=d["agent_run_id"],
            resume_session=bool(d.get("resume_session", False)),
        )


@dataclass
class AgentRunBranchRequest:
    source_agent_run_id: str
    base_session_item_id: str
    runtime_root: str
    prompt: str = ""
    agent_run_id: str | None = None
    branch_binding_id: str = ""
    select_branch: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_agent_run_id": self.source_agent_run_id,
            "base_session_item_id": self.base_session_item_id,
            "runtime_root": self.runtime_root,
            "prompt": self.prompt,
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "metadata": dict(self.metadata),
        }
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        payload["select_branch"] = self.select_branch
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunBranchRequest":
        return cls(
            source_agent_run_id=d["source_agent_run_id"],
            base_session_item_id=d["base_session_item_id"],
            runtime_root=d["runtime_root"],
            prompt=str(d.get("prompt") or ""),
            agent_run_id=d.get("agent_run_id")
            if isinstance(d.get("agent_run_id"), str)
            else None,
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            select_branch=bool(d.get("select_branch", True)),
            metadata=_dict(d.get("metadata")),
        )


@dataclass
class AgentRunForkRequest:
    source_agent_run_id: str
    base_session_item_id: str
    fork_workspace_ref: str
    target_owner_session_run_id: str
    prompt: str = ""
    agent_run_id: str | None = None
    branch_binding_id: str = ""
    select_branch: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_agent_run_id": self.source_agent_run_id,
            "base_session_item_id": self.base_session_item_id,
            "fork_workspace_ref": self.fork_workspace_ref,
            "target_owner_session_run_id": self.target_owner_session_run_id,
            "prompt": self.prompt,
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
            "metadata": dict(self.metadata),
        }
        if self.agent_run_id is not None:
            payload["agent_run_id"] = self.agent_run_id
        payload["select_branch"] = self.select_branch
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunForkRequest":
        return cls(
            source_agent_run_id=d["source_agent_run_id"],
            base_session_item_id=d["base_session_item_id"],
            fork_workspace_ref=d["fork_workspace_ref"],
            target_owner_session_run_id=d["target_owner_session_run_id"],
            prompt=str(d.get("prompt") or ""),
            agent_run_id=d.get("agent_run_id")
            if isinstance(d.get("agent_run_id"), str)
            else None,
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            select_branch=bool(d.get("select_branch", True)),
            metadata=_dict(d.get("metadata")),
        )


@dataclass
class AgentRunSteerRequest:
    payload: dict[str, Any]
    agent_run_id: str = ""
    source: str = "user"
    peer_token: str | None = None
    session_run_id: str | None = None
    branch_binding_id: str | None = None
    activation_id: str | None = None
    idempotency_key: str | None = None
    client_steer_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "agent_run_id": self.agent_run_id,
            "payload": dict(self.payload),
            "source": self.source,
            "metadata": dict(self.metadata),
        }
        if self.peer_token is not None:
            result["peer_token"] = self.peer_token
        if self.session_run_id is not None:
            result["session_run_id"] = self.session_run_id
        if self.branch_binding_id is not None:
            result["branch_binding_id"] = self.branch_binding_id
        if self.activation_id is not None:
            result["activation_id"] = self.activation_id
        if self.idempotency_key is not None:
            result["idempotency_key"] = self.idempotency_key
        if self.client_steer_id is not None:
            result["client_steer_id"] = self.client_steer_id
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunSteerRequest":
        return cls(
            agent_run_id=str(d.get("agent_run_id") or ""),
            payload=_dict(d.get("payload")),
            source=str(d.get("source") or "user"),
            peer_token=d.get("peer_token") if isinstance(d.get("peer_token"), str) else None,
            session_run_id=d.get("session_run_id")
            if isinstance(d.get("session_run_id"), str)
            else None,
            branch_binding_id=d.get("branch_binding_id")
            if isinstance(d.get("branch_binding_id"), str)
            else None,
            activation_id=d.get("activation_id")
            if isinstance(d.get("activation_id"), str)
            else None,
            idempotency_key=d.get("idempotency_key")
            if isinstance(d.get("idempotency_key"), str)
            else None,
            client_steer_id=d.get("client_steer_id")
            if isinstance(d.get("client_steer_id"), str)
            else None,
            metadata=_dict(d.get("metadata")),
        )


@dataclass
class SessionRunAgentRunSteerRequest:
    payload: dict[str, Any]
    session_run_id: str
    branch_binding_id: str
    agent_run_id: str = ""
    source: str = "user"
    peer_token: str | None = None
    activation_id: str | None = None
    idempotency_key: str | None = None
    client_steer_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.session_run_id = required_session_run_id(self.session_run_id)
        self.branch_binding_id = required_branch_binding_id(self.branch_binding_id)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "agent_run_id": self.agent_run_id,
            "payload": dict(self.payload),
            "source": self.source,
            "metadata": dict(self.metadata),
            "session_run_id": required_session_run_id(self.session_run_id),
            "branch_binding_id": required_branch_binding_id(self.branch_binding_id),
        }
        if self.peer_token is not None:
            result["peer_token"] = self.peer_token
        if self.activation_id is not None:
            result["activation_id"] = self.activation_id
        if self.idempotency_key is not None:
            result["idempotency_key"] = self.idempotency_key
        if self.client_steer_id is not None:
            result["client_steer_id"] = self.client_steer_id
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRunAgentRunSteerRequest":
        return cls(
            agent_run_id=str(d.get("agent_run_id") or ""),
            payload=_dict(d.get("payload")),
            source=str(d.get("source") or "user"),
            peer_token=d.get("peer_token") if isinstance(d.get("peer_token"), str) else None,
            session_run_id=required_session_run_id(d.get("session_run_id")),
            branch_binding_id=required_branch_binding_id(d.get("branch_binding_id")),
            activation_id=d.get("activation_id")
            if isinstance(d.get("activation_id"), str)
            else None,
            idempotency_key=d.get("idempotency_key")
            if isinstance(d.get("idempotency_key"), str)
            else None,
            client_steer_id=d.get("client_steer_id")
            if isinstance(d.get("client_steer_id"), str)
            else None,
            metadata=_dict(d.get("metadata")),
        )


@dataclass
class AgentRunSteerResponse:
    ok: bool
    status: str = ""
    activation_steer: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "status": self.status,
            "activation_steer": dict(self.activation_steer),
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunSteerResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            status=str(d.get("status") or ""),
            activation_steer=_dict(d.get("activation_steer")),
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


@dataclass
class AgentRunListRequest:
    status: str | None = None
    agent_id: str | None = None
    limit: int = 50
    after_created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "agent_id": self.agent_id,
            "limit": self.limit,
            "after_created_at": self.after_created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunListRequest":
        return cls(
            status=d.get("status") if isinstance(d.get("status"), str) else None,
            agent_id=d.get("agent_id") if isinstance(d.get("agent_id"), str) else None,
            limit=int(d.get("limit") or 50),
            after_created_at=d.get("after_created_at")
            if isinstance(d.get("after_created_at"), str)
            else None,
        )


@dataclass
class AgentRunListResponse:
    ok: bool
    agent_runs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "agent_runs": list(self.agent_runs)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunListResponse":
        return cls(ok=bool(d.get("ok", False)), agent_runs=_dict_list(d.get("agent_runs")))


@dataclass
class AgentRunLoadRequest:
    agent_run_id: str
    event_limit: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {"agent_run_id": self.agent_run_id, "event_limit": self.event_limit}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunLoadRequest":
        return cls(agent_run_id=d["agent_run_id"], event_limit=int(d.get("event_limit") or 100))


@dataclass
class AgentRunDetail:
    ok: bool
    agent_run: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "agent_run": dict(self.agent_run),
            "events": list(self.events),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRunDetail":
        return cls(
            ok=bool(d.get("ok", False)),
            agent_run=_dict(d.get("agent_run")),
            events=_dict_list(d.get("events")),
        )


__all__ = [
    "AgentRunActivationClaimRequest",
    "AgentRunActivationClaimResponse",
    "AgentRunActivationCompleteEvent",
    "AgentRunActivationCompleteRequest",
    "AgentRunActivationCompleteResponse",
    "AgentRunActivationEventRequest",
    "AgentRunActivationHeartbeatRequest",
    "AgentRunActivationHeartbeatResponse",
    "AgentRunActivationModelRequest",
    "AgentRunActivationModelResponse",
    "AgentRunActivationSessionPinRequest",
    "AgentRunActivationSessionPinResponse",
    "AgentRunAdminEventsRequest",
    "AgentRunBranchRequest",
    "AgentRunCancelRequest",
    "AgentRunCancelResponse",
    "AgentRunDetail",
    "AgentRunEventsQuery",
    "AgentRunEventsResponse",
    "AgentRunForkRequest",
    "AgentRunListRequest",
    "AgentRunListResponse",
    "AgentRunLoadRequest",
    "AgentRunRequest",
    "AgentRunResponse",
    "AgentRunRetryRequest",
    "AgentRunSteerRequest",
    "AgentRunSteerResponse",
]
