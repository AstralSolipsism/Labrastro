"""Domain models for Issue Assignment and Mention Agent control state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum_value(value: Enum | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


class IssueStatus(str, Enum):
    OPEN = "open"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class AssignmentStatus(str, Enum):
    READY = "ready"
    NEEDS_ASSIGNMENT = "needs_assignment"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"


class MentionStatus(str, Enum):
    PARSED = "parsed"
    READY = "ready"
    NEEDS_ASSIGNMENT = "needs_assignment"
    CANCELLED = "cancelled"


@dataclass
class IssueAssignmentEvent:
    scope: str
    scope_id: str
    seq: int
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IssueAssignmentEvent":
        return cls(
            scope=str(data.get("scope") or ""),
            scope_id=str(data.get("scope_id") or ""),
            seq=int(data.get("seq") or 0),
            type=str(data.get("type") or ""),
            payload=_dict(data.get("payload")),
            created_at=str(data.get("created_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "seq": self.seq,
            "type": self.type,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass
class IssueRecord:
    id: str
    title: str
    description: str = ""
    status: IssueStatus | str = IssueStatus.OPEN
    peer_id: str | None = None
    source: str = "manual"
    taskflow_id: str | None = None
    work_item_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = IssueStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IssueRecord":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            status=str(data.get("status") or IssueStatus.OPEN.value),
            peer_id=str(data["peer_id"]) if data.get("peer_id") is not None else None,
            source=str(data.get("source") or "manual"),
            taskflow_id=(
                str(data["taskflow_id"])
                if data.get("taskflow_id") is not None
                else None
            ),
            work_item_id=(
                str(data["work_item_id"])
                if data.get("work_item_id") is not None
                else None
            ),
            metadata=_dict(data.get("metadata")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "peer_id": self.peer_id,
            "source": self.source,
            "taskflow_id": self.taskflow_id,
            "work_item_id": self.work_item_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AssignmentRecord:
    id: str
    issue_id: str
    status: AssignmentStatus | str = AssignmentStatus.READY
    target_agent_id: str | None = None
    source: str = "manual"
    reason: str = ""
    work_item_id: str | None = None
    task_run_id: str | None = None
    runtime_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = AssignmentStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssignmentRecord":
        return cls(
            id=str(data.get("id") or ""),
            issue_id=str(data.get("issue_id") or ""),
            status=str(data.get("status") or AssignmentStatus.READY.value),
            target_agent_id=(
                str(data["target_agent_id"])
                if data.get("target_agent_id") is not None
                else None
            ),
            source=str(data.get("source") or "manual"),
            reason=str(data.get("reason") or ""),
            work_item_id=(
                str(data["work_item_id"])
                if data.get("work_item_id") is not None
                else None
            ),
            task_run_id=(
                str(data["task_run_id"])
                if data.get("task_run_id") is not None
                else None
            ),
            runtime_task_id=(
                str(data["runtime_task_id"])
                if data.get("runtime_task_id") is not None
                else None
            ),
            metadata=_dict(data.get("metadata")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "issue_id": self.issue_id,
            "status": self.status.value,
            "target_agent_id": self.target_agent_id,
            "source": self.source,
            "reason": self.reason,
            "work_item_id": self.work_item_id,
            "task_run_id": self.task_run_id,
            "runtime_task_id": self.runtime_task_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class MentionRecord:
    id: str
    raw_text: str
    status: MentionStatus | str = MentionStatus.PARSED
    peer_id: str | None = None
    issue_id: str | None = None
    assignment_id: str | None = None
    context_type: str = "chat"
    context_id: str | None = None
    agent_ref: str = ""
    resolved_agent_id: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    source: str = "manual"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = MentionStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MentionRecord":
        return cls(
            id=str(data.get("id") or ""),
            raw_text=str(data.get("raw_text") or ""),
            status=str(data.get("status") or MentionStatus.PARSED.value),
            peer_id=str(data["peer_id"]) if data.get("peer_id") is not None else None,
            issue_id=str(data["issue_id"]) if data.get("issue_id") is not None else None,
            assignment_id=(
                str(data["assignment_id"])
                if data.get("assignment_id") is not None
                else None
            ),
            context_type=str(data.get("context_type") or "chat"),
            context_id=(
                str(data["context_id"]) if data.get("context_id") is not None else None
            ),
            agent_ref=str(data.get("agent_ref") or ""),
            resolved_agent_id=(
                str(data["resolved_agent_id"])
                if data.get("resolved_agent_id") is not None
                else None
            ),
            candidates=_list_of_dicts(data.get("candidates")),
            reason=str(data.get("reason") or ""),
            source=str(data.get("source") or "manual"),
            metadata=_dict(data.get("metadata")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "raw_text": self.raw_text,
            "status": self.status.value,
            "peer_id": self.peer_id,
            "issue_id": self.issue_id,
            "assignment_id": self.assignment_id,
            "context_type": self.context_type,
            "context_id": self.context_id,
            "agent_ref": self.agent_ref,
            "resolved_agent_id": self.resolved_agent_id,
            "candidates": [dict(item) for item in self.candidates],
            "reason": self.reason,
            "source": self.source,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
