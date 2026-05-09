"""Long-lived project state for Taskflow compilation.

This module implements the stable ProjectState side of the architecture
defined in ``docs/文档.md`` Sections 1, 2, 3, 5.7, 5.8, and 5.9.
ProjectState is the cross-goal source of truth: it stores reusable knowledge,
normalized WorkItems, GoalWorkLinks, TaskRuns, and traceability links.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from labrastro_server.taskflow.domain.time import utc_now


def _enum_value(value: Enum | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


class ProjectStatus(str, Enum):
    """Lifecycle status for the long-lived Project boundary."""

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class GoalLifecycleStatus(str, Enum):
    """Goal lifecycle from ``docs/文档.md`` Section 2.2."""

    DRAFT = "draft"
    CLARIFYING = "clarifying"
    CONFIRMED = "confirmed"
    COMPILED = "compiled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkItemType(str, Enum):
    """Reusable work definition type from ``docs/文档.md`` Section 2.3."""

    IMPLEMENTATION = "implementation"
    RESEARCH = "research"
    DESIGN = "design"
    REVIEW = "review"
    TEST = "test"
    MIGRATION = "migration"
    DOCUMENTATION = "documentation"
    OPS = "ops"
    SHARED_ENABLER = "shared_enabler"


class WorkItemStatus(str, Enum):
    """Status for reusable WorkItems from ``docs/文档.md`` Section 2.3."""

    CANDIDATE = "candidate"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskRunStatus(str, Enum):
    """Execution-instance status from ``docs/文档.md`` Section 2.4."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRunExecutor(str, Enum):
    """Executor category for a TaskRun."""

    AGENT = "agent"
    HUMAN = "human"
    MIXED = "mixed"


class GoalWorkRelationType(str, Enum):
    """Goal-to-WorkItem relation from ``docs/文档.md`` Section 3.1."""

    DIRECT_DELIVERY = "direct_delivery"
    PARTIAL_CONTRIBUTION = "partial_contribution"
    DEPENDENCY = "dependency"
    SHARED_ENABLER = "shared_enabler"
    FOLLOW_UP = "follow_up"
    DERIVED_FROM = "derived_from"


class TraceEntityType(str, Enum):
    """Traceable node types from ``docs/文档.md`` Section 3.2."""

    GOAL = "goal"
    DECISION = "decision"
    ACCEPTANCE_EXAMPLE = "acceptance_example"
    WORK_ITEM = "work_item"
    TASK_RUN = "task_run"
    ARTIFACT = "artifact"
    ISSUE = "issue"
    PR = "pr"


class TraceRelationType(str, Enum):
    """Trace relation types from ``docs/文档.md`` Section 3.2."""

    IMPLEMENTS = "implements"
    VALIDATES = "validates"
    BLOCKS = "blocks"
    DEPENDS_ON = "depends_on"
    DERIVED_FROM = "derived_from"
    EXPLAINS = "explains"
    SUPERSEDES = "supersedes"
    PRODUCES = "produces"


@dataclass(slots=True)
class Stakeholder:
    """Person or role affected by a project-level decision or goal."""

    id: str
    name: str = ""
    role: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Stakeholder":
        return cls(
            id=str(data.get("id") or data.get("name") or ""),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class Constraint:
    """Reusable project or goal constraint."""

    id: str
    statement: str
    source: str = ""
    severity: str = "medium"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Constraint":
        return cls(
            id=str(data.get("id") or ""),
            statement=str(data.get("statement") or data.get("description") or ""),
            source=str(data.get("source") or ""),
            severity=str(data.get("severity") or "medium"),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "source": self.source,
            "severity": self.severity,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class RepositoryRef:
    """Repository reference used by Project, WorkItem, and TaskRun."""

    id: str
    name: str = ""
    url: str = ""
    default_branch: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepositoryRef":
        return cls(
            id=str(data.get("id") or data.get("url") or data.get("name") or ""),
            name=str(data.get("name") or ""),
            url=str(data.get("url") or ""),
            default_branch=str(data.get("default_branch") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "default_branch": self.default_branch,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class WorkspaceRef:
    """Workspace reference used during dispatch."""

    id: str
    path: str = ""
    repo_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceRef":
        return cls(
            id=str(data.get("id") or data.get("path") or ""),
            path=str(data.get("path") or ""),
            repo_ref=str(data.get("repo_ref") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "repo_ref": self.repo_ref,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ArtifactRef:
    """Reference to a generated or external artifact."""

    id: str
    type: str
    title: str = ""
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRef":
        return cls(
            id=str(data.get("id") or data.get("uri") or ""),
            type=str(data.get("type") or "artifact"),
            title=str(data.get("title") or ""),
            uri=str(data.get("uri") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "uri": self.uri,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class BranchRef:
    """Branch reference projected from execution state."""

    id: str
    name: str
    repo_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BranchRef":
        return cls(
            id=str(data.get("id") or data.get("name") or ""),
            name=str(data.get("name") or ""),
            repo_ref=str(data.get("repo_ref") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "repo_ref": self.repo_ref,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class PullRequestRef:
    """Pull request projection stored in ProjectState."""

    id: str
    number: int | None = None
    url: str = ""
    title: str = ""
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PullRequestRef":
        number = data.get("number")
        return cls(
            id=str(data.get("id") or data.get("url") or number or ""),
            number=int(number) if number is not None else None,
            url=str(data.get("url") or ""),
            title=str(data.get("title") or ""),
            status=str(data.get("status") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "url": self.url,
            "title": self.title,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class Project:
    """Long-lived project boundary from ``docs/文档.md`` Section 2.1."""

    id: str
    name: str
    status: ProjectStatus | str = ProjectStatus.ACTIVE
    background: str = ""
    stakeholders: list[Stakeholder] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    repositories: list[RepositoryRef] = field(default_factory=list)
    workspaces: list[WorkspaceRef] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = ProjectStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ProjectStatus.ACTIVE.value),
            background=str(data.get("background") or ""),
            stakeholders=[
                Stakeholder.from_dict(_dict(item))
                for item in _list(data.get("stakeholders"))
            ],
            constraints=[
                Constraint.from_dict(_dict(item))
                for item in _list(data.get("constraints"))
            ],
            repositories=[
                RepositoryRef.from_dict(_dict(item))
                for item in _list(data.get("repositories"))
            ],
            workspaces=[
                WorkspaceRef.from_dict(_dict(item))
                for item in _list(data.get("workspaces"))
            ],
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "background": self.background,
            "stakeholders": [item.to_dict() for item in self.stakeholders],
            "constraints": [item.to_dict() for item in self.constraints],
            "repositories": [item.to_dict() for item in self.repositories],
            "workspaces": [item.to_dict() for item in self.workspaces],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class Goal:
    """Confirmed outcome hypothesis from ``docs/文档.md`` Section 2.2."""

    id: str
    project_id: str
    statement: str
    status: GoalLifecycleStatus | str = GoalLifecycleStatus.DRAFT
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    constraints_delta: list[Constraint] = field(default_factory=list)
    created_from_taskflow_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = GoalLifecycleStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        return cls(
            id=str(data.get("id") or ""),
            project_id=str(data.get("project_id") or ""),
            statement=str(data.get("statement") or ""),
            status=str(data.get("status") or GoalLifecycleStatus.DRAFT.value),
            scope_in=_string_list(data.get("scope_in")),
            scope_out=_string_list(data.get("scope_out")),
            success_criteria=_string_list(data.get("success_criteria")),
            constraints_delta=[
                Constraint.from_dict(_dict(item))
                for item in _list(data.get("constraints_delta"))
            ],
            created_from_taskflow_id=(
                str(data["created_from_taskflow_id"])
                if data.get("created_from_taskflow_id") is not None
                else None
            ),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "statement": self.statement,
            "status": self.status.value,
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "success_criteria": list(self.success_criteria),
            "constraints_delta": [item.to_dict() for item in self.constraints_delta],
            "created_from_taskflow_id": self.created_from_taskflow_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class WorkItem:
    """Reusable normalized work definition from ``docs/文档.md`` Section 2.3."""

    id: str
    project_id: str
    title: str
    description: str
    type: WorkItemType | str
    status: WorkItemStatus | str = WorkItemStatus.CANDIDATE
    acceptance_refs: list[str] = field(default_factory=list)
    decision_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    dedupe_key: str | None = None
    derived_from: str | None = None
    depends_on: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = WorkItemType(_enum_value(self.type))
        self.status = WorkItemStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkItem":
        return cls(
            id=str(data.get("id") or ""),
            project_id=str(data.get("project_id") or ""),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            type=str(data.get("type") or WorkItemType.IMPLEMENTATION.value),
            status=str(data.get("status") or WorkItemStatus.CANDIDATE.value),
            acceptance_refs=_string_list(data.get("acceptance_refs")),
            decision_refs=_string_list(data.get("decision_refs")),
            artifact_refs=_string_list(data.get("artifact_refs")),
            dedupe_key=(
                str(data["dedupe_key"]) if data.get("dedupe_key") is not None else None
            ),
            derived_from=(
                str(data["derived_from"])
                if data.get("derived_from") is not None
                else None
            ),
            depends_on=_string_list(data.get("depends_on")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "type": self.type.value,
            "status": self.status.value,
            "acceptance_refs": list(self.acceptance_refs),
            "decision_refs": list(self.decision_refs),
            "artifact_refs": list(self.artifact_refs),
            "dedupe_key": self.dedupe_key,
            "derived_from": self.derived_from,
            "depends_on": list(self.depends_on),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class TaskRun:
    """Concrete execution instance from ``docs/文档.md`` Section 2.4."""

    id: str
    project_id: str
    work_item_id: str
    goal_id: str | None = None
    runtime_task_id: str | None = None
    status: TaskRunStatus | str = TaskRunStatus.PENDING
    executor: TaskRunExecutor | str = TaskRunExecutor.AGENT
    repo_ref: RepositoryRef | None = None
    workspace_ref: WorkspaceRef | None = None
    branch_ref: BranchRef | None = None
    pr_ref: PullRequestRef | None = None
    dispatch_ref_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = TaskRunStatus(_enum_value(self.status))
        self.executor = TaskRunExecutor(_enum_value(self.executor))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRun":
        return cls(
            id=str(data.get("id") or ""),
            project_id=str(data.get("project_id") or ""),
            goal_id=str(data["goal_id"]) if data.get("goal_id") is not None else None,
            work_item_id=str(data.get("work_item_id") or ""),
            runtime_task_id=(
                str(data["runtime_task_id"])
                if data.get("runtime_task_id") is not None
                else None
            ),
            status=str(data.get("status") or TaskRunStatus.PENDING.value),
            executor=str(data.get("executor") or TaskRunExecutor.AGENT.value),
            repo_ref=(
                RepositoryRef.from_dict(_dict(data.get("repo_ref")))
                if data.get("repo_ref") is not None
                else None
            ),
            workspace_ref=(
                WorkspaceRef.from_dict(_dict(data.get("workspace_ref")))
                if data.get("workspace_ref") is not None
                else None
            ),
            branch_ref=(
                BranchRef.from_dict(_dict(data.get("branch_ref")))
                if data.get("branch_ref") is not None
                else None
            ),
            pr_ref=(
                PullRequestRef.from_dict(_dict(data.get("pr_ref")))
                if data.get("pr_ref") is not None
                else None
            ),
            dispatch_ref_id=(
                str(data["dispatch_ref_id"])
                if data.get("dispatch_ref_id") is not None
                else None
            ),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "work_item_id": self.work_item_id,
            "runtime_task_id": self.runtime_task_id,
            "status": self.status.value,
            "executor": self.executor.value,
            "repo_ref": self.repo_ref.to_dict() if self.repo_ref else None,
            "workspace_ref": (
                self.workspace_ref.to_dict() if self.workspace_ref else None
            ),
            "branch_ref": self.branch_ref.to_dict() if self.branch_ref else None,
            "pr_ref": self.pr_ref.to_dict() if self.pr_ref else None,
            "dispatch_ref_id": self.dispatch_ref_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class GoalWorkLink:
    """Many-to-many Goal <-> WorkItem link from ``docs/文档.md`` Section 3.1."""

    id: str
    project_id: str
    goal_id: str
    work_item_id: str
    relation_type: GoalWorkRelationType | str
    rationale: str = ""
    contribution_weight: float | None = None
    hard_blocker: bool = False
    acceptance_refs: list[str] = field(default_factory=list)
    decision_refs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.relation_type = GoalWorkRelationType(_enum_value(self.relation_type))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalWorkLink":
        return cls(
            id=str(data.get("id") or ""),
            project_id=str(data.get("project_id") or ""),
            goal_id=str(data.get("goal_id") or ""),
            work_item_id=str(data.get("work_item_id") or ""),
            relation_type=str(
                data.get("relation_type") or GoalWorkRelationType.DIRECT_DELIVERY.value
            ),
            rationale=str(data.get("rationale") or ""),
            contribution_weight=(
                float(data["contribution_weight"])
                if data.get("contribution_weight") is not None
                else None
            ),
            hard_blocker=bool(data.get("hard_blocker", False)),
            acceptance_refs=_string_list(data.get("acceptance_refs")),
            decision_refs=_string_list(data.get("decision_refs")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "work_item_id": self.work_item_id,
            "relation_type": self.relation_type.value,
            "rationale": self.rationale,
            "contribution_weight": self.contribution_weight,
            "hard_blocker": self.hard_blocker,
            "acceptance_refs": list(self.acceptance_refs),
            "decision_refs": list(self.decision_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class TraceLink:
    """Traceability edge from ``docs/文档.md`` Section 3.2."""

    id: str
    project_id: str
    source_type: TraceEntityType | str
    source_id: str
    target_type: TraceEntityType | str
    target_id: str
    relation_type: TraceRelationType | str
    rationale: str = ""
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.source_type = TraceEntityType(_enum_value(self.source_type))
        self.target_type = TraceEntityType(_enum_value(self.target_type))
        self.relation_type = TraceRelationType(_enum_value(self.relation_type))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceLink":
        return cls(
            id=str(data.get("id") or ""),
            project_id=str(data.get("project_id") or ""),
            source_type=str(data.get("source_type") or TraceEntityType.GOAL.value),
            source_id=str(data.get("source_id") or ""),
            target_type=str(data.get("target_type") or TraceEntityType.WORK_ITEM.value),
            target_id=str(data.get("target_id") or ""),
            relation_type=str(
                data.get("relation_type") or TraceRelationType.IMPLEMENTS.value
            ),
            rationale=str(data.get("rationale") or ""),
            created_at=str(data.get("created_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "target_type": self.target_type.value,
            "target_id": self.target_id,
            "relation_type": self.relation_type.value,
            "rationale": self.rationale,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ProjectProfile:
    """Project-level profile block from the ProjectState schema."""

    name: str = ""
    status: ProjectStatus | str = ProjectStatus.ACTIVE
    background: str = ""
    stakeholders: list[Stakeholder] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    repositories: list[RepositoryRef] = field(default_factory=list)
    workspaces: list[WorkspaceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = ProjectStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectProfile":
        return cls(
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ProjectStatus.ACTIVE.value),
            background=str(data.get("background") or ""),
            stakeholders=[
                Stakeholder.from_dict(_dict(item))
                for item in _list(data.get("stakeholders"))
            ],
            constraints=[
                Constraint.from_dict(_dict(item))
                for item in _list(data.get("constraints"))
            ],
            repositories=[
                RepositoryRef.from_dict(_dict(item))
                for item in _list(data.get("repositories"))
            ],
            workspaces=[
                WorkspaceRef.from_dict(_dict(item))
                for item in _list(data.get("workspaces"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "background": self.background,
            "stakeholders": [item.to_dict() for item in self.stakeholders],
            "constraints": [item.to_dict() for item in self.constraints],
            "repositories": [item.to_dict() for item in self.repositories],
            "workspaces": [item.to_dict() for item in self.workspaces],
        }


@dataclass(slots=True)
class KnowledgeBase:
    """Reusable project knowledge from the ProjectState schema."""

    domain_model: dict[str, Any] = field(default_factory=dict)
    ubiquitous_language: dict[str, str] = field(default_factory=dict)
    architecture_notes: list[str] = field(default_factory=list)
    reusable_context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeBase":
        return cls(
            domain_model=_dict(data.get("domain_model")),
            ubiquitous_language={
                str(key): str(value)
                for key, value in _dict(data.get("ubiquitous_language")).items()
            },
            architecture_notes=_string_list(data.get("architecture_notes")),
            reusable_context=_dict(data.get("reusable_context")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_model": dict(self.domain_model),
            "ubiquitous_language": dict(self.ubiquitous_language),
            "architecture_notes": list(self.architecture_notes),
            "reusable_context": dict(self.reusable_context),
        }


@dataclass(slots=True)
class ProjectDecision:
    """Project-level decision memory independent of one Taskflow session."""

    id: str
    topic: str
    rationale: str = ""
    status: str = "confirmed_by_user"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectDecision":
        return cls(
            id=str(data.get("id") or ""),
            topic=str(data.get("topic") or ""),
            rationale=str(data.get("rationale") or ""),
            status=str(data.get("status") or data.get("state") or "confirmed_by_user"),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "rationale": self.rationale,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ProjectDecisions:
    """Decision buckets from the ProjectState schema."""

    project_decisions: list[ProjectDecision] = field(default_factory=list)
    architecture_decisions: list[ProjectDecision] = field(default_factory=list)
    policy_decisions: list[ProjectDecision] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectDecisions":
        return cls(
            project_decisions=[
                ProjectDecision.from_dict(_dict(item))
                for item in _list(data.get("project_decisions"))
            ],
            architecture_decisions=[
                ProjectDecision.from_dict(_dict(item))
                for item in _list(data.get("architecture_decisions"))
            ],
            policy_decisions=[
                ProjectDecision.from_dict(_dict(item))
                for item in _list(data.get("policy_decisions"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_decisions": [_serialize(item) for item in self.project_decisions],
            "architecture_decisions": [
                _serialize(item) for item in self.architecture_decisions
            ],
            "policy_decisions": [_serialize(item) for item in self.policy_decisions],
        }


@dataclass(slots=True)
class WorkItemBuckets:
    """WorkItem groups from the ProjectState schema."""

    active_work_items: list[WorkItem] = field(default_factory=list)
    reusable_work_items: list[WorkItem] = field(default_factory=list)
    shared_enablers: list[WorkItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkItemBuckets":
        return cls(
            active_work_items=[
                WorkItem.from_dict(_dict(item))
                for item in _list(data.get("active_work_items"))
            ],
            reusable_work_items=[
                WorkItem.from_dict(_dict(item))
                for item in _list(data.get("reusable_work_items"))
            ],
            shared_enablers=[
                WorkItem.from_dict(_dict(item))
                for item in _list(data.get("shared_enablers"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_work_items": [item.to_dict() for item in self.active_work_items],
            "reusable_work_items": [
                item.to_dict() for item in self.reusable_work_items
            ],
            "shared_enablers": [item.to_dict() for item in self.shared_enablers],
        }

    def all_items(self) -> list[WorkItem]:
        """Return all known WorkItems across active, reusable, and enabler buckets."""

        seen: set[str] = set()
        items: list[WorkItem] = []
        for item in [
            *self.active_work_items,
            *self.reusable_work_items,
            *self.shared_enablers,
        ]:
            if item.id not in seen:
                seen.add(item.id)
                items.append(item)
        return items


@dataclass(slots=True)
class Traceability:
    """Project trace graph from the ProjectState schema."""

    goal_links: list[GoalWorkLink] = field(default_factory=list)
    decision_links: list[TraceLink] = field(default_factory=list)
    artifact_links: list[TraceLink] = field(default_factory=list)
    task_run_links: list[TraceLink] = field(default_factory=list)
    task_runs: list[TaskRun] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Traceability":
        return cls(
            goal_links=[
                GoalWorkLink.from_dict(_dict(item))
                for item in _list(data.get("goal_links"))
            ],
            decision_links=[
                TraceLink.from_dict(_dict(item))
                for item in _list(data.get("decision_links"))
            ],
            artifact_links=[
                TraceLink.from_dict(_dict(item))
                for item in _list(data.get("artifact_links"))
            ],
            task_run_links=[
                TraceLink.from_dict(_dict(item))
                for item in _list(data.get("task_run_links"))
            ],
            task_runs=[
                TaskRun.from_dict(_dict(item)) for item in _list(data.get("task_runs"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_links": [item.to_dict() for item in self.goal_links],
            "decision_links": [item.to_dict() for item in self.decision_links],
            "artifact_links": [item.to_dict() for item in self.artifact_links],
            "task_run_links": [item.to_dict() for item in self.task_run_links],
            "task_runs": [item.to_dict() for item in self.task_runs],
        }


@dataclass(slots=True)
class Projections:
    """Projected artifacts and execution surfaces from ProjectState."""

    artifacts: list[ArtifactRef] = field(default_factory=list)
    branches: list[BranchRef] = field(default_factory=list)
    PRs: list[PullRequestRef] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    runbooks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Projections":
        return cls(
            artifacts=[
                ArtifactRef.from_dict(_dict(item))
                for item in _list(data.get("artifacts"))
            ],
            branches=[
                BranchRef.from_dict(_dict(item))
                for item in _list(data.get("branches"))
            ],
            PRs=[
                PullRequestRef.from_dict(_dict(item))
                for item in _list(data.get("PRs") or data.get("prs"))
            ],
            reviews=[_dict(item) for item in _list(data.get("reviews"))],
            runbooks=[_dict(item) for item in _list(data.get("runbooks"))],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": [item.to_dict() for item in self.artifacts],
            "branches": [item.to_dict() for item in self.branches],
            "PRs": [item.to_dict() for item in self.PRs],
            "reviews": [dict(item) for item in self.reviews],
            "runbooks": [dict(item) for item in self.runbooks],
        }


@dataclass(slots=True)
class ProjectState:
    """Long-lived source of truth for project knowledge and work.

    Source: ``docs/文档.md`` Section 1, "ProjectState 的职责".
    This object intentionally stores cross-goal knowledge only. A single
    conversation belongs in ``TaskflowState`` and should reference this state
    through project/work/decision/artifact refs instead of copying it wholesale.
    """

    project_id: str
    project_profile: ProjectProfile = field(default_factory=ProjectProfile)
    knowledge_base: KnowledgeBase = field(default_factory=KnowledgeBase)
    decisions: ProjectDecisions = field(default_factory=ProjectDecisions)
    work_items: WorkItemBuckets = field(default_factory=WorkItemBuckets)
    traceability: Traceability = field(default_factory=Traceability)
    projections: Projections = field(default_factory=Projections)
    schema_version: str = "taskflow.project_state.v1"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def new(cls, *, project_id: str, name: str = "") -> "ProjectState":
        """Create an empty ProjectState for a new long-lived project."""

        return cls(
            project_id=project_id,
            project_profile=ProjectProfile(name=name),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectState":
        return cls(
            project_id=str(data.get("project_id") or ""),
            project_profile=ProjectProfile.from_dict(_dict(data.get("project_profile"))),
            knowledge_base=KnowledgeBase.from_dict(_dict(data.get("knowledge_base"))),
            decisions=ProjectDecisions.from_dict(_dict(data.get("decisions"))),
            work_items=WorkItemBuckets.from_dict(_dict(data.get("work_items"))),
            traceability=Traceability.from_dict(_dict(data.get("traceability"))),
            projections=Projections.from_dict(_dict(data.get("projections"))),
            schema_version=str(
                data.get("schema_version") or "taskflow.project_state.v1"
            ),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_profile": self.project_profile.to_dict(),
            "knowledge_base": self.knowledge_base.to_dict(),
            "decisions": self.decisions.to_dict(),
            "work_items": self.work_items.to_dict(),
            "traceability": self.traceability.to_dict(),
            "projections": self.projections.to_dict(),
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def touch(self) -> None:
        """Refresh the update timestamp after a state mutation."""

        self.updated_at = utc_now()

    def list_work_items(self) -> list[WorkItem]:
        """Return every known WorkItem exactly once."""

        return self.work_items.all_items()

    def find_work_item_by_dedupe_key(self, dedupe_key: str | None) -> WorkItem | None:
        """Find a reusable WorkItem by the compiler dedupe key.

        Logic from ``docs/文档.md`` Section 5.6: exact dedupe-key equality is
        the highest confidence reuse signal.
        """

        if not dedupe_key:
            return None
        for item in self.list_work_items():
            if item.dedupe_key == dedupe_key:
                return item
        return None

    def find_similar_work_items(self, *, title: str, type: str) -> list[WorkItem]:
        """Return simple title/type matches for compiler similarity hints."""

        normalized_title = " ".join(title.lower().split())
        normalized_type = str(type)
        return [
            item
            for item in self.list_work_items()
            if item.type.value == normalized_type
            and " ".join(item.title.lower().split()) == normalized_title
        ]

    def upsert_work_item(self, item: WorkItem) -> WorkItem:
        """Insert or replace a WorkItem in the appropriate ProjectState bucket."""

        buckets: Iterable[list[WorkItem]] = (
            self.work_items.active_work_items,
            self.work_items.reusable_work_items,
            self.work_items.shared_enablers,
        )
        for bucket in buckets:
            for index, existing in enumerate(bucket):
                if existing.id == item.id:
                    item.updated_at = utc_now()
                    bucket[index] = item
                    self.touch()
                    return item
        if item.type == WorkItemType.SHARED_ENABLER:
            self.work_items.shared_enablers.append(item)
        elif item.status in {WorkItemStatus.READY, WorkItemStatus.ACTIVE}:
            self.work_items.active_work_items.append(item)
        else:
            self.work_items.reusable_work_items.append(item)
        self.touch()
        return item

    def add_goal_work_link(self, link: GoalWorkLink) -> GoalWorkLink:
        """Add or replace a GoalWorkLink in ProjectState traceability."""

        for index, existing in enumerate(self.traceability.goal_links):
            if existing.id == link.id:
                self.traceability.goal_links[index] = link
                self.touch()
                return link
        self.traceability.goal_links.append(link)
        self.touch()
        return link

    def add_trace_link(self, link: TraceLink) -> TraceLink:
        """Add a trace link to its section-specific projection."""

        target = self.traceability.decision_links
        if link.source_type == TraceEntityType.ARTIFACT or link.target_type == TraceEntityType.ARTIFACT:
            target = self.traceability.artifact_links
        if link.source_type == TraceEntityType.TASK_RUN or link.target_type == TraceEntityType.TASK_RUN:
            target = self.traceability.task_run_links
        if not any(existing.id == link.id for existing in target):
            target.append(link)
            self.touch()
        return link

    def add_task_run(self, run: TaskRun) -> TaskRun:
        """Record a concrete execution instance without reusing prior TaskRuns."""

        for index, existing in enumerate(self.traceability.task_runs):
            if existing.id == run.id:
                self.traceability.task_runs[index] = run
                self.touch()
                return run
        self.traceability.task_runs.append(run)
        self.touch()
        return run


__all__ = [
    "ArtifactRef",
    "BranchRef",
    "Constraint",
    "Goal",
    "GoalLifecycleStatus",
    "GoalWorkLink",
    "GoalWorkRelationType",
    "KnowledgeBase",
    "Project",
    "ProjectDecision",
    "ProjectDecisions",
    "ProjectProfile",
    "ProjectState",
    "ProjectStatus",
    "PullRequestRef",
    "RepositoryRef",
    "Stakeholder",
    "TaskRun",
    "TaskRunExecutor",
    "TaskRunStatus",
    "TraceEntityType",
    "TraceLink",
    "TraceRelationType",
    "Traceability",
    "WorkItem",
    "WorkItemBuckets",
    "WorkItemStatus",
    "WorkItemType",
    "WorkspaceRef",
]
