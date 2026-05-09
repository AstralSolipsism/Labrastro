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
    "ChatCancelRequest",
    "ChatCancelResponse",
    "ApprovalReplyRequest",
    "ApprovalReplyResponse",
]
