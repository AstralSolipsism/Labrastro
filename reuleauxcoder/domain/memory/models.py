"""Provider-contract models for agent-scoped memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _clean(value: Any) -> str:
    return str(value or "").strip()


class MemoryProviderError(RuntimeError):
    """Base error raised by memory provider adapters or runtime policy."""


class MemoryProviderConfigurationError(MemoryProviderError):
    """Raised when configured memory providers cannot be resolved."""


class MemoryProviderUnavailable(MemoryProviderError):
    """Raised when a resolved memory provider cannot serve a request."""


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Private memory partition and task/project coordinates.

    `owner_agent_id` is the isolation boundary. Project and workspace are scoped
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


@dataclass(frozen=True, slots=True)
class MemoryProviderCapabilities:
    provide: bool = False
    capture: bool = False
    remember: bool = False
    forget: bool = False
    session_lifecycle: bool = False
    streaming_events: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryProviderCapabilities":
        raw = data if isinstance(data, dict) else {}
        return cls(
            provide=bool(raw.get("provide", False)),
            capture=bool(raw.get("capture", False)),
            remember=bool(raw.get("remember", False)),
            forget=bool(raw.get("forget", False)),
            session_lifecycle=bool(raw.get("session_lifecycle", False)),
            streaming_events=bool(raw.get("streaming_events", False)),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "provide": self.provide,
            "capture": self.capture,
            "remember": self.remember,
            "forget": self.forget,
            "session_lifecycle": self.session_lifecycle,
            "streaming_events": self.streaming_events,
        }


@dataclass(frozen=True, slots=True)
class MemoryProviderStatus:
    provider_id: str
    adapter: str = ""
    available: bool = True
    message: str = ""
    capabilities: MemoryProviderCapabilities = field(
        default_factory=MemoryProviderCapabilities
    )
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryProviderDiagnostic:
    provider_id: str
    severity: str
    message: str
    code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "severity": self.severity,
            "message": self.message,
            "code": self.code,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MemoryProvideRequest:
    query: str = ""
    token_budget: int = 800
    limit: int = 8
    source_kind_filter: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryBundleFragment:
    id: str
    text: str
    source_provider: str
    source_kind: str
    trust_tier: str
    score: float = 0.0
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean(self.id))
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "source_provider", _clean(self.source_provider))
        object.__setattr__(self, "source_kind", _clean(self.source_kind) or "memory")
        object.__setattr__(self, "trust_tier", _clean(self.trust_tier) or "external")
        object.__setattr__(self, "score", float(self.score or 0.0))
        token_estimate = int(self.token_estimate or 0)
        if token_estimate < 1:
            token_estimate = max(1, len(self.text) // 4) if self.text else 0
        object.__setattr__(self, "token_estimate", token_estimate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "source_provider": self.source_provider,
            "source_kind": self.source_kind,
            "trust_tier": self.trust_tier,
            "score": self.score,
            "token_estimate": self.token_estimate,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MemoryBundle:
    scope: MemoryScope
    fragments: list[MemoryBundleFragment] = field(default_factory=list)
    token_estimate: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[MemoryProviderDiagnostic] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "fragments": [fragment.to_dict() for fragment in self.fragments],
            "token_estimate": self.token_estimate,
            "provenance": dict(self.provenance),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class MemoryCaptureEvent:
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    created_at: str = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class MemoryCaptureReceipt:
    provider_id: str
    accepted: bool
    status: str = "accepted"
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[MemoryProviderDiagnostic] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryRememberItem:
    text: str
    source_kind: str = "manual"
    trust_tier: str = "user"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryForgetSelector:
    id: str = ""
    query: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryMutationResult:
    provider_id: str
    accepted: bool
    status: str = "accepted"
    item_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[MemoryProviderDiagnostic] = field(default_factory=list)
