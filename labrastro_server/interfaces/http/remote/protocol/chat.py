"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class ChatRequest:
    peer_token: str
    prompt: str
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"peer_token": self.peer_token, "prompt": self.prompt}
        if self.mode is not None:
            payload["mode"] = self.mode
        if self.workflow_mode is not None:
            payload["workflow_mode"] = self.workflow_mode
        if self.taskflow_id is not None:
            payload["taskflow_id"] = self.taskflow_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatRequest":
        return cls(
            peer_token=d["peer_token"],
            prompt=d["prompt"],
            mode=d.get("mode"),
            workflow_mode=d.get("workflow_mode"),
            taskflow_id=d.get("taskflow_id"),
        )

@dataclass
class ChatResponse:
    response: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"response": self.response, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatResponse":
        return cls(response=d.get("response", ""), error=d.get("error"))

@dataclass
class ChatStartRequest:
    peer_token: str
    prompt: str
    session_hint: str | None = None
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "peer_token": self.peer_token,
            "prompt": self.prompt,
            "session_hint": self.session_hint,
        }
        if self.mode is not None:
            payload["mode"] = self.mode
        if self.workflow_mode is not None:
            payload["workflow_mode"] = self.workflow_mode
        if self.taskflow_id is not None:
            payload["taskflow_id"] = self.taskflow_id
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStartRequest":
        return cls(
            peer_token=d["peer_token"],
            prompt=d["prompt"],
            session_hint=d.get("session_hint"),
            mode=d.get("mode"),
            workflow_mode=d.get("workflow_mode"),
            taskflow_id=d.get("taskflow_id"),
        )

@dataclass
class ChatStartResponse:
    chat_id: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStartResponse":
        return cls(chat_id=d.get("chat_id", ""), error=d.get("error"))

@dataclass
class ChatStreamRequest:
    peer_token: str
    chat_id: str
    cursor: int = 0
    timeout_sec: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "cursor": self.cursor,
            "timeout_sec": self.timeout_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStreamRequest":
        return cls(
            peer_token=d["peer_token"],
            chat_id=d["chat_id"],
            cursor=int(d.get("cursor", 0)),
            timeout_sec=float(d.get("timeout_sec", 30.0)),
        )

@dataclass
class ChatStreamResponse:
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    next_cursor: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "done": self.done,
            "next_cursor": self.next_cursor,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStreamResponse":
        return cls(
            events=list(d.get("events", [])),
            done=bool(d.get("done", False)),
            next_cursor=int(d.get("next_cursor", 0)),
            error=d.get("error"),
        )

@dataclass
class ChatStatusRequest:
    peer_token: str
    chat_id: str
    cursor: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "cursor": self.cursor,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStatusRequest":
        return cls(
            peer_token=d["peer_token"],
            chat_id=d["chat_id"],
            cursor=int(d.get("cursor", 0)),
        )

@dataclass
class ChatStatusResponse:
    ok: bool
    chat_id: str
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
    created_at: float | None = None
    last_activity_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chat_id": self.chat_id,
            "peer_id": self.peer_id,
            "status": self.status,
            "running": self.running,
            "done": self.done,
            "reconnectable": self.reconnectable,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "first_available_seq": self.first_available_seq,
            "latest_seq": self.latest_seq,
            "dropped_count": self.dropped_count,
            "session_id": self.session_id,
            "mode": self.mode,
            "workflow_mode": self.workflow_mode,
            "taskflow_id": self.taskflow_id,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStatusResponse":
        return cls(
            ok=bool(d.get("ok", False)),
            chat_id=str(d.get("chat_id", "")),
            peer_id=d.get("peer_id") if isinstance(d.get("peer_id"), str) else None,
            status=str(d.get("status", "")),
            running=bool(d.get("running", False)),
            done=bool(d.get("done", False)),
            reconnectable=bool(d.get("reconnectable", False)),
            cursor=int(d.get("cursor", 0)),
            next_cursor=int(d.get("next_cursor", 0)),
            first_available_seq=int(d.get("first_available_seq", 0)),
            latest_seq=int(d.get("latest_seq", 0)),
            dropped_count=int(d.get("dropped_count", 0)),
            session_id=d.get("session_id") if isinstance(d.get("session_id"), str) else None,
            mode=d.get("mode") if isinstance(d.get("mode"), str) else None,
            workflow_mode=d.get("workflow_mode") if isinstance(d.get("workflow_mode"), str) else None,
            taskflow_id=d.get("taskflow_id") if isinstance(d.get("taskflow_id"), str) else None,
            created_at=float(d["created_at"]) if d.get("created_at") is not None else None,
            last_activity_at=float(d["last_activity_at"])
            if d.get("last_activity_at") is not None
            else None,
            finished_at=float(d["finished_at"]) if d.get("finished_at") is not None else None,
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )

@dataclass
class ChatCancelRequest:
    peer_token: str
    chat_id: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatCancelRequest":
        return cls(
            peer_token=d["peer_token"],
            chat_id=d["chat_id"],
            reason=d.get("reason"),
        )

@dataclass
class ChatCancelResponse:
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatCancelResponse":
        return cls(ok=bool(d.get("ok", False)), error=d.get("error"))

@dataclass
class ApprovalReplyRequest:
    peer_token: str
    chat_id: str
    approval_id: str
    decision: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "approval_id": self.approval_id,
            "decision": self.decision,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalReplyRequest":
        return cls(
            peer_token=d["peer_token"],
            chat_id=d["chat_id"],
            approval_id=d["approval_id"],
            decision=d["decision"],
            reason=d.get("reason"),
        )

@dataclass
class ApprovalReplyResponse:
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalReplyResponse":
        return cls(ok=bool(d.get("ok", False)), error=d.get("error"))


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ChatStartRequest",
    "ChatStartResponse",
    "ChatStreamRequest",
    "ChatStreamResponse",
    "ChatStatusRequest",
    "ChatStatusResponse",
    "ChatCancelRequest",
    "ChatCancelResponse",
    "ApprovalReplyRequest",
    "ApprovalReplyResponse",
]
