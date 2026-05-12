"""Core models for agent-scoped private memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _clean(value: Any) -> str:
    return str(value or "").strip()


class MemoryBackendUnavailable(RuntimeError):
    """Raised when the memory backend cannot serve a non-identity request."""


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Private memory partition and task/project coordinates.

    `owner_agent_id` is the security boundary. Project and workspace are scoped
    coordinates inside that owner, not substitutes for the owner.
    """

    owner_agent_id: str
    memory_namespace: str = ""
    project_id: str = ""
    workspace_id: str = ""
    repo_id: str = ""
    goal_id: str = ""
    task_id: str = ""
    session_id: str = ""
    sensitivity: str = ""

    def __post_init__(self) -> None:
        owner = _clean(self.owner_agent_id)
        if not owner:
            raise ValueError("memory scope requires owner_agent_id")
        namespace = _clean(self.memory_namespace) or owner
        object.__setattr__(self, "owner_agent_id", owner)
        object.__setattr__(self, "memory_namespace", namespace)
        for field_name in (
            "project_id",
            "workspace_id",
            "repo_id",
            "goal_id",
            "task_id",
            "session_id",
            "sensitivity",
        ):
            object.__setattr__(self, field_name, _clean(getattr(self, field_name)))

    @classmethod
    def from_metadata(
        cls,
        metadata: dict[str, Any] | None,
        *,
        default_agent_id: str = "",
        default_namespace: str = "",
    ) -> "MemoryScope":
        payload = dict(metadata or {})
        owner = (
            payload.get("owner_agent_id")
            or payload.get("memory_owner_agent_id")
            or default_agent_id
        )
        namespace = (
            payload.get("memory_namespace")
            or payload.get("owner_memory_namespace")
            or default_namespace
            or owner
        )
        return cls(
            owner_agent_id=_clean(owner),
            memory_namespace=_clean(namespace),
            project_id=payload.get("project_id") or payload.get("memory_project_id") or "",
            workspace_id=payload.get("workspace_id")
            or payload.get("memory_workspace_id")
            or "",
            repo_id=payload.get("repo_id") or payload.get("memory_repo_id") or "",
            goal_id=payload.get("goal_id") or payload.get("memory_goal_id") or "",
            task_id=payload.get("task_id") or payload.get("memory_task_id") or "",
            session_id=payload.get("session_id") or payload.get("memory_session_id") or "",
            sensitivity=payload.get("sensitivity")
            or payload.get("memory_sensitivity")
            or "",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "owner_agent_id": self.owner_agent_id,
            "memory_namespace": self.memory_namespace,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "repo_id": self.repo_id,
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "sensitivity": self.sensitivity,
        }

    def cache_key(self) -> tuple[str, ...]:
        return (
            self.owner_agent_id,
            self.memory_namespace,
            self.project_id,
            self.workspace_id,
            self.repo_id,
            self.goal_id,
            self.task_id,
            self.sensitivity,
        )


@dataclass(slots=True)
class MemoryItem:
    """One private memory item owned by exactly one agent namespace."""

    id: str
    owner_agent_id: str
    memory_namespace: str
    type: str
    content: str
    abstract: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0
    version: int = 1
    status: str = "active"
    project_id: str = ""
    workspace_id: str = ""
    repo_id: str = ""
    goal_id: str = ""
    task_id: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def create(
        cls,
        *,
        scope: MemoryScope,
        type: str,
        content: str,
        abstract: str = "",
        fields: dict[str, Any] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        confidence: float = 1.0,
    ) -> "MemoryItem":
        return cls(
            id=f"mem_{uuid.uuid4().hex}",
            owner_agent_id=scope.owner_agent_id,
            memory_namespace=scope.memory_namespace,
            type=_clean(type) or "note",
            content=str(content or ""),
            abstract=str(abstract or ""),
            fields=dict(fields or {}),
            source_refs=[dict(ref) for ref in source_refs or []],
            confidence=float(confidence),
            project_id=scope.project_id,
            workspace_id=scope.workspace_id,
            repo_id=scope.repo_id,
            goal_id=scope.goal_id,
            task_id=scope.task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_agent_id": self.owner_agent_id,
            "memory_namespace": self.memory_namespace,
            "type": self.type,
            "content": self.content,
            "abstract": self.abstract,
            "fields": dict(self.fields),
            "source_refs": [dict(ref) for ref in self.source_refs],
            "confidence": self.confidence,
            "version": self.version,
            "status": self.status,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "repo_id": self.repo_id,
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    query: str = ""
    limit: int = 8
    type_filter: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryProvideRequest:
    query: str = ""
    token_budget: int = 800
    limit: int = 8
    type_filter: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryBundle:
    scope: MemoryScope
    items: list[MemoryItem] = field(default_factory=list)
    token_estimate: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryCaptureEvent:
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    created_at: str = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class MemoryCaptureReceipt:
    job_id: str
    enqueued: bool
    scope_version: int


@dataclass(frozen=True, slots=True)
class MemoryCaptureJob:
    job_id: str
    owner_agent_id: str
    memory_namespace: str
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    status: str = "queued"
    created_at: str = ""
