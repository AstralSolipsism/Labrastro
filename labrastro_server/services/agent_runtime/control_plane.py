"""Server-side control plane for queued AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
import threading
import time
import uuid

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AgentRunSource,
    ArtifactStatus,
    ArtifactType,
    ExecutionLocation,
    ExecutorType,
    PublishPolicy,
    TaskArtifact,
    AgentRunRecord,
    TaskSessionRef,
    TaskStatus,
    TriggerMode,
    ModelRequestOrigin,
    WorkerKind,
    WorktreeRole,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunRequest,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.environment_events import (
    environment_summary_event,
    expand_environment_executor_event,
)
from labrastro_server.services.agent_runtime.lifecycle import IssueStatus, TaskLifecycleState
from labrastro_server.services.agent_runtime.permission_events import (
    blocked_review_event_payload,
    should_block_waiting_approval,
)
from labrastro_server.services.agent_runtime.prompt_renderer import (
    CanonicalAgentContext,
    ExecutorPromptRenderer,
)
from labrastro_server.services.agent_runtime.runtime_store import (
    DEFAULT_RUNTIME_EVENT_LIMIT,
    AgentRunStore,
    clamp_event_limit,
    runtime_slots_allow_agent_run_claim,
)
from labrastro_server.services.agent_runtime.runtime_policy import (
    model_request_origin_for_runtime,
    optional_model_request_origin,
    optional_publish_policy,
    optional_worker_kind,
    optional_worktree_role,
    system_flow_for_source,
    validate_agent_run_runtime_policy,
    worker_kind_for_runtime,
    worker_matches_agent_run,
    workspace_key,
)
from labrastro_server.services.sandbox.provider import SandboxProfile, SandboxProvider


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _default_executor_session_id(task_id: str) -> str:
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "-"
        for ch in str(task_id or "").strip()
    ).strip("-")
    return f"labrastro-agent-run-{safe or uuid.uuid4().hex}"


def _ensure_reuleauxcoder_executor_session(
    request: "AgentRunRequest",
    task_id: str,
) -> None:
    if request.executor == ExecutorType.REULEAUXCODER and not request.executor_session_id:
        request.executor_session_id = _default_executor_session_id(task_id)


def _coerce_executor(value: ExecutorType | str | None) -> ExecutorType:
    if isinstance(value, ExecutorType):
        return value
    if value is None or str(value).strip() == "":
        return ExecutorType.REULEAUXCODER
    return ExecutorType(str(value))


def _coerce_location(value: ExecutionLocation | str | None) -> ExecutionLocation:
    if isinstance(value, ExecutionLocation):
        return value
    if value is None or str(value).strip() == "":
        return ExecutionLocation.LOCAL_WORKSPACE
    return ExecutionLocation(str(value))


def _optional_executor(value: ExecutorType | str | None) -> ExecutorType | None:
    if isinstance(value, ExecutorType):
        return value
    if value is None or str(value).strip() == "":
        return None
    return ExecutorType(str(value))


def _optional_location(
    value: ExecutionLocation | str | None,
) -> ExecutionLocation | None:
    if isinstance(value, ExecutionLocation):
        return value
    if value is None or str(value).strip() == "":
        return None
    return ExecutionLocation(str(value))


def _agent_run_to_dict(task: AgentRunRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "agent_run_id": task.id,
        "issue_id": task.issue_id,
        "agent_id": task.agent_id,
        "source": task.source.value,
        "trigger_mode": task.trigger_mode.value,
        "status": task.status.value,
        "prompt": task.prompt,
        "runtime_profile_id": task.runtime_profile_id,
        "executor": task.executor.value if task.executor else None,
        "execution_location": (
            task.execution_location.value if task.execution_location else None
        ),
        "worktree_role": task.worktree_role.value if task.worktree_role else None,
        "publish_policy": task.publish_policy.value if task.publish_policy else None,
        "output": task.output,
        "parent_task_id": task.parent_task_id,
        "trigger_comment_id": task.trigger_comment_id,
        "branch_name": task.branch_name,
        "pr_url": task.pr_url,
        "worker_id": task.worker_id,
        "executor_session_id": task.executor_session_id,
        "workdir": task.workdir,
        "sandbox_id": task.sandbox_id,
        "sandbox_session_id": task.sandbox_session_id,
        "workspace_ref": task.workspace_ref,
        "delegated_by_run_id": task.delegated_by_run_id,
        "parent_run_id": task.parent_run_id,
        "failure_reason": task.failure_reason,
        "cancel_reason": task.cancel_reason,
        "metadata": dict(task.metadata),
    }


def _artifact_to_dict(artifact: TaskArtifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "task_id": artifact.task_id,
        "type": artifact.type.value,
        "status": artifact.status.value,
        "branch_name": artifact.branch_name,
        "pr_url": artifact.pr_url,
        "content": artifact.content,
        "path": artifact.path,
        "metadata": dict(artifact.metadata),
        "merge_status": artifact.merge_status.value if artifact.merge_status else None,
        "merged_by": artifact.merged_by,
    }


def _dict_from(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _agent_model_binding(raw_agent: dict[str, Any]) -> dict[str, Any]:
    raw_model = _dict_from(raw_agent.get("model"))
    provider = str(raw_model.get("provider") or "").strip()
    model = str(raw_model.get("model") or "").strip()
    if not provider or not model:
        return {}
    binding: dict[str, Any] = {"provider": provider, "model": model}
    if raw_model.get("display_name"):
        binding["display_name"] = str(raw_model["display_name"])
    parameters = _dict_from(raw_model.get("parameters"))
    if parameters:
        binding["parameters"] = parameters
    return binding


def _string_list_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _can_resume_from_parent(request: "AgentRunRequest", parent: AgentRunRecord) -> bool:
    if not parent.executor_session_id:
        return False
    return (
        request.agent_id == parent.agent_id
        and request.runtime_profile_id == parent.runtime_profile_id
        and request.executor == parent.executor
        and request.execution_location == parent.execution_location
        and workspace_key(request.workdir) == workspace_key(parent.workdir)
        and str(request.branch_name or "") == str(parent.branch_name or "")
    )


def _coerce_source(value: AgentRunSource | str | None) -> AgentRunSource:
    if isinstance(value, AgentRunSource):
        return value
    if value is None or str(value).strip() == "":
        return AgentRunSource.MANUAL
    return AgentRunSource(str(value))


@dataclass
class AgentRunRequest:
    """Request accepted by the AgentRun control plane."""

    issue_id: str
    agent_id: str
    prompt: str
    source: AgentRunSource | str = AgentRunSource.MANUAL
    executor: ExecutorType | str | None = None
    execution_location: ExecutionLocation | str | None = None
    worker_kind: WorkerKind | str | None = None
    model_request_origin: ModelRequestOrigin | str | None = None
    worktree_role: WorktreeRole | str | None = None
    publish_policy: PublishPolicy | str | None = None
    trigger_mode: TriggerMode | str = TriggerMode.ISSUE_TASK
    runtime_profile_id: str | None = None
    parent_task_id: str | None = None
    trigger_comment_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    workdir: str | None = None
    executor_session_id: str | None = None
    model: str | None = None
    sandbox_id: str | None = None
    sandbox_session_id: str | None = None
    workspace_ref: str | None = None
    delegated_by_run_id: str | None = None
    parent_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source = _coerce_source(self.source)
        self.executor = _optional_executor(self.executor)
        self.execution_location = _optional_location(self.execution_location)
        self.worker_kind = optional_worker_kind(self.worker_kind)
        self.model_request_origin = optional_model_request_origin(
            self.model_request_origin
        )
        self.worktree_role = optional_worktree_role(self.worktree_role)
        self.publish_policy = optional_publish_policy(self.publish_policy)
        if not isinstance(self.trigger_mode, TriggerMode):
            self.trigger_mode = TriggerMode(str(self.trigger_mode))

@dataclass
class AgentRunEvent:
    """Ordered AgentRun event stored by the control plane."""

    task_id: str
    seq: int
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.task_id,
            "seq": self.seq,
            "type": self.type,
            "payload": dict(self.payload),
        }

@dataclass
class AgentRunClaim:
    """AgentRun payload returned to a worker after a successful claim."""

    request_id: str
    worker_id: str
    task: AgentRunRecord
    executor_request: ExecutorRunRequest
    runtime_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "agent_run": _agent_run_to_dict(self.task),
            "executor_request": self.executor_request.to_dict(),
            "runtime_snapshot": dict(self.runtime_snapshot),
        }

@dataclass
class PRArtifactResult:
    """Result returned by a PR flow implementation."""

    branch_name: str
    pr_url: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PRFlow(Protocol):
    """Protocol for creating or updating a task pull request artifact."""

    def create_or_update(self, task: AgentRunRecord, *, diff: str = "") -> PRArtifactResult:
        """Create or update a pull request for task output."""


class InMemoryPRFlow:
    """Deterministic PR flow used by tests and local dry runs."""

    def __init__(self, base_url: str = "https://example.invalid/pr") -> None:
        self.base_url = base_url.rstrip("/")

    def create_or_update(self, task: AgentRunRecord, *, diff: str = "") -> PRArtifactResult:
        branch = task.branch_name or f"agent/{task.agent_id}/{task.id[:12]}"
        return PRArtifactResult(
            branch_name=branch,
            pr_url=f"{self.base_url}/{task.id}",
            metadata={"diff_bytes": len(diff.encode("utf-8"))},
        )


class AgentRunControlPlane:
    """In-memory runtime control plane for tasks, worker claims and artifacts.

    The service is deliberately storage-agnostic. The public methods are the
    contract that a persistent implementation and HTTP relay endpoints can keep.
    """

    def __init__(
        self,
        *,
        max_running_tasks: int = 4,
        runtime_snapshot: dict[str, Any] | None = None,
        pr_flow: PRFlow | None = None,
        store: AgentRunStore | None = None,
        sandbox_provider: SandboxProvider | None = None,
        sandbox_profile: SandboxProfile | None = None,
    ) -> None:
        self.max_running_tasks = max(1, int(max_running_tasks or 1))
        self.runtime_snapshot = dict(runtime_snapshot or {})
        self.pr_flow = pr_flow or InMemoryPRFlow()
        self._store = store
        self._sandbox_provider = sandbox_provider
        self._sandbox_profile = sandbox_profile
        self._lock = threading.RLock()
        self._states: dict[str, TaskLifecycleState] = {}
        self._sessions: dict[str, TaskSessionRef] = {}
        self._events: dict[str, list[AgentRunEvent]] = {}
        self._claims: dict[str, AgentRunClaim] = {}
        self._claim_leases: dict[str, dict[str, Any]] = {}
        self._cancel_requests: dict[str, str] = {}
        self._wakeup = threading.Condition()

    def configure_sandbox_provider(
        self,
        provider: SandboxProvider | None,
        profile: SandboxProfile | None = None,
    ) -> None:
        """Attach or replace the execution-room provider for new AgentRuns."""

        with self._lock:
            self._sandbox_provider = provider
            self._sandbox_profile = profile

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Refresh runtime config without dropping queued/running task state."""

        with self._lock:
            if self._store is not None:
                self._store.configure(
                    max_running_tasks=max_running_tasks,
                    runtime_snapshot=runtime_snapshot,
                )
                self.max_running_tasks = self._store.max_running_tasks
                self.runtime_snapshot = dict(self._store.runtime_snapshot)
                return
            if max_running_tasks is not None:
                self.max_running_tasks = max(1, int(max_running_tasks or 1))
            if runtime_snapshot is not None:
                self.runtime_snapshot = dict(runtime_snapshot)

    def submit_agent_run(
        self, request: AgentRunRequest, *, task_id: str | None = None
    ) -> AgentRunRecord:
        task_id = task_id or _new_id("task")
        if self._store is not None:
            parent = None
            if request.parent_task_id:
                try:
                    parent = self._store.get_agent_run(request.parent_task_id)
                except KeyError:
                    parent = None
            request = self._resolve_request_against_snapshot(request, parent=parent)
            _ensure_reuleauxcoder_executor_session(request, task_id)
            metadata = dict(request.metadata)
            if request.worktree_role is not None:
                metadata.setdefault("worktree_role", request.worktree_role.value)
            if request.publish_policy is not None:
                metadata.setdefault("publish_policy", request.publish_policy.value)
            request.metadata = self._metadata_with_snapshot_capabilities(
                metadata,
                agent_id=request.agent_id,
                source=request.source,
                runtime_profile_id=request.runtime_profile_id,
            )
            sandbox_error = self._prepare_sandbox_session(request, task_id)
            task = self._store.submit_agent_run(request, task_id=task_id)
            if sandbox_error:
                task = self._store.fail_agent_run(task.id, error=sandbox_error)
            self.notify_task_available()
            return task
        with self._lock:
            request = self._resolve_request_locked(request)
            _ensure_reuleauxcoder_executor_session(request, task_id)
        sandbox_error = self._prepare_sandbox_session(request, task_id)
        with self._lock:
            metadata = dict(request.metadata)
            metadata.setdefault("agent_run_source", request.source.value)
            if request.sandbox_id:
                metadata.setdefault("sandbox_id", request.sandbox_id)
            if request.sandbox_session_id:
                metadata.setdefault("sandbox_session_id", request.sandbox_session_id)
            if request.workspace_ref:
                metadata.setdefault("workspace_ref", request.workspace_ref)
            if request.delegated_by_run_id:
                metadata.setdefault("delegated_by_run_id", request.delegated_by_run_id)
            if request.parent_run_id:
                metadata.setdefault("parent_run_id", request.parent_run_id)
            if request.model is not None:
                metadata.setdefault("model", request.model)
            if request.worker_kind is not None:
                metadata.setdefault("worker_kind", request.worker_kind.value)
            if request.model_request_origin is not None:
                metadata.setdefault(
                    "model_request_origin",
                    request.model_request_origin.value,
                )
            if request.worktree_role is not None:
                metadata.setdefault("worktree_role", request.worktree_role.value)
            if request.publish_policy is not None:
                metadata.setdefault("publish_policy", request.publish_policy.value)
            metadata = self._metadata_with_snapshot_capabilities(
                metadata,
                agent_id=request.agent_id,
                source=request.source,
                runtime_profile_id=request.runtime_profile_id,
            )
            task = AgentRunRecord(
                id=task_id,
                issue_id=request.issue_id,
                agent_id=request.agent_id,
                source=request.source,
                trigger_mode=request.trigger_mode,
                status=TaskStatus.QUEUED,
                prompt=request.prompt,
                runtime_profile_id=request.runtime_profile_id,
                executor=request.executor,
                execution_location=request.execution_location,
                worktree_role=request.worktree_role,
                publish_policy=request.publish_policy,
                parent_task_id=request.parent_task_id,
                trigger_comment_id=request.trigger_comment_id,
                branch_name=request.branch_name,
                pr_url=request.pr_url,
                executor_session_id=request.executor_session_id,
                workdir=request.workdir,
                sandbox_id=request.sandbox_id,
                sandbox_session_id=request.sandbox_session_id,
                workspace_ref=request.workspace_ref,
                delegated_by_run_id=request.delegated_by_run_id,
                parent_run_id=request.parent_run_id or request.parent_task_id,
                metadata=metadata,
            )
            self._states[task.id] = TaskLifecycleState(task=task)
            self._events[task.id] = []
            self._append_event_locked(task.id, "queued", {"agent_run": _agent_run_to_dict(task)})
            if task.sandbox_session_id:
                self._append_event_locked(
                    task.id,
                    "sandbox_session_started",
                    {
                        "sandbox_id": task.sandbox_id,
                        "sandbox_session_id": task.sandbox_session_id,
                        "workspace_ref": task.workspace_ref,
                        "workdir": task.workdir,
                    },
                )
            if sandbox_error:
                task.status = TaskStatus.FAILED
                task.output = sandbox_error
                task.failure_reason = sandbox_error
                task.cancel_reason = None
                self._append_event_locked(task.id, "failed", {"error": sandbox_error})
        self.notify_task_available()
        return task

    def notify_task_available(self) -> None:
        """Wake workers waiting for queued AgentRuns or event changes."""

        with self._wakeup:
            self._wakeup.notify_all()

    def _resolve_request_against_snapshot(
        self,
        request: AgentRunRequest,
        *,
        parent: AgentRunRecord | None = None,
    ) -> AgentRunRequest:
        if parent is not None:
            if request.runtime_profile_id is None:
                request.runtime_profile_id = parent.runtime_profile_id
            if request.executor is None:
                request.executor = parent.executor
            if request.execution_location is None:
                request.execution_location = parent.execution_location
            if request.workdir is None:
                request.workdir = parent.workdir
            if request.branch_name is None:
                request.branch_name = parent.branch_name
            if request.pr_url is None:
                request.pr_url = parent.pr_url
        snapshot = self.runtime_snapshot
        agents = _dict_from(snapshot.get("agents"))
        profiles = _dict_from(snapshot.get("runtime_profiles"))
        raw_agent = _dict_from(agents.get(request.agent_id))
        agent_config: AgentConfig | None = None
        if raw_agent:
            agent_config = AgentConfig.from_dict(request.agent_id, raw_agent)
            if agent_config.visibility != "user":
                flow = system_flow_for_source(request.source)
                if not agent_config.allows_system_flow(flow):
                    raise ValueError(
                        "agent is restricted to system flows: "
                        f"{request.agent_id} does not allow {flow}"
                    )

        agent_profile_id = str(raw_agent.get("runtime_profile") or "").strip()
        profile_id = str(request.runtime_profile_id or agent_profile_id).strip()
        if raw_agent and not profile_id:
            raise ValueError(
                f"agent {request.agent_id} requires a runtime_profile"
            )
        raw_profile = _dict_from(profiles.get(profile_id)) if profile_id else {}
        if profile_id and not raw_profile:
            raise ValueError(f"runtime profile not found: {profile_id}")

        request.runtime_profile_id = profile_id or None
        request.executor = (
            request.executor
            or _optional_executor(raw_profile.get("executor"))
            or ExecutorType.REULEAUXCODER
        )
        request.execution_location = (
            request.execution_location
            or _optional_location(raw_profile.get("execution_location"))
            or ExecutionLocation.LOCAL_WORKSPACE
        )
        request.worker_kind = request.worker_kind or worker_kind_for_runtime(
            raw_profile,
            request.execution_location,
        )
        request.model_request_origin = (
            request.model_request_origin
            or model_request_origin_for_runtime(
                raw_profile,
                executor=request.executor,
                worker_kind=request.worker_kind,
            )
        )
        request.worktree_role = (
            request.worktree_role
            or optional_worktree_role(raw_profile.get("worktree_role"))
            or WorktreeRole.TARGET
        )
        request.publish_policy = (
            request.publish_policy
            or optional_publish_policy(raw_profile.get("publish_policy"))
            or PublishPolicy.NEVER
        )
        model_binding = _agent_model_binding(raw_agent)
        if model_binding:
            request.metadata.setdefault("model_binding", model_binding)
            if request.model is None:
                request.model = str(model_binding["model"])
        self._validate_runtime_policy(
            request,
            agent_config=agent_config,
        )
        if request.model is None and raw_profile.get("model") is not None:
            request.model = str(raw_profile["model"])
        if (
            parent is not None
            and request.executor_session_id is None
            and _can_resume_from_parent(request, parent)
        ):
            request.executor_session_id = parent.executor_session_id
        return request

    def _validate_runtime_policy(
        self,
        request: AgentRunRequest,
        *,
        agent_config: AgentConfig | None,
    ) -> None:
        validate_agent_run_runtime_policy(request, agent_config=agent_config)

    def _resolve_request_locked(self, request: AgentRunRequest) -> AgentRunRequest:
        parent = (
            self._states.get(request.parent_task_id).task
            if request.parent_task_id in self._states
            else None
        )
        return self._resolve_request_against_snapshot(request, parent=parent)

    def _runtime_profile_for_request(self, request: AgentRunRequest) -> dict[str, Any]:
        profiles = _dict_from(self.runtime_snapshot.get("runtime_profiles"))
        return _dict_from(profiles.get(request.runtime_profile_id or ""))

    def _sandbox_profile_for_runtime(
        self,
        runtime_profile: dict[str, Any],
    ) -> SandboxProfile:
        base = self._sandbox_profile or SandboxProfile(image="labrastro-host:test")
        sandbox = _dict_from(runtime_profile.get("sandbox"))
        return SandboxProfile(
            image=str(
                sandbox.get("image")
                or runtime_profile.get("worker_image")
                or base.image
            ),
            cpu_limit=str(sandbox.get("cpu_limit") or base.cpu_limit),
            memory_limit=str(sandbox.get("memory_limit") or base.memory_limit),
            network=str(sandbox.get("network") or base.network),
            workspace_volume_prefix=str(
                sandbox.get("workspace_volume_prefix")
                or base.workspace_volume_prefix
            ),
            idle_ttl_seconds=int(
                sandbox.get("idle_ttl_seconds") or base.idle_ttl_seconds
            ),
            env={
                **base.env,
                **{
                    str(k): str(v)
                    for k, v in _dict_from(sandbox.get("env")).items()
                },
            },
        )

    def _prepare_sandbox_session(
        self,
        request: AgentRunRequest,
        task_id: str,
    ) -> str | None:
        provider = self._sandbox_provider
        if provider is None:
            return None
        if request.sandbox_session_id:
            return None
        if request.metadata.get("skip_sandbox") is True:
            return None
        if request.worker_kind != WorkerKind.SANDBOX_WORKER:
            return None
        metadata = dict(request.metadata)
        runtime_profile = self._runtime_profile_for_request(request)
        profile = self._sandbox_profile_for_runtime(runtime_profile)
        workspace_ref = str(
            request.workspace_ref
            or metadata.get("workspace_ref")
            or metadata.get("workspace_root")
            or request.issue_id
            or task_id
        ).strip()
        if not workspace_ref:
            workspace_ref = task_id
        try:
            sandbox = provider.ensure_sandbox(
                workspace_ref,
                profile,
                {
                    "agent_run_id": task_id,
                    "agent_id": request.agent_id,
                    "source": request.source.value,
                },
            )
            runtime_profile_for_session = dict(runtime_profile)
            runtime_profile_for_session["sandbox"] = profile.__dict__.copy()
            session = provider.start_session(
                sandbox.id,
                runtime_profile_for_session,
                task_id,
            )
            mount = provider.prepare_workspace(
                session.id,
                {
                    "source": workspace_ref,
                    "agent_run_id": task_id,
                    "source_workspace_root": metadata.get("workspace_root"),
                },
            )
            provider.exec_agent_run(
                session.id,
                {
                    "agent_run_id": task_id,
                    "agent_id": request.agent_id,
                    "runtime_profile_id": request.runtime_profile_id,
                    "source": request.source.value,
                },
            )
        except Exception as exc:
            metadata["sandbox_error"] = str(exc)
            request.metadata = metadata
            return f"sandbox provider failed to start session: {exc}"

        request.sandbox_id = sandbox.id
        request.sandbox_session_id = session.id
        request.workspace_ref = workspace_ref
        request.workdir = request.workdir or mount.path
        if metadata.get("workspace_root") is not None:
            metadata.setdefault("source_workspace_root", str(metadata["workspace_root"]))
        metadata["workspace_root"] = mount.path
        metadata["sandbox_id"] = sandbox.id
        metadata["sandbox_session_id"] = session.id
        metadata["workspace_ref"] = workspace_ref
        metadata["workspace_mount"] = mount.path
        metadata["sandbox_container_id"] = session.container_id
        request.metadata = metadata
        return None

    def _stop_sandbox_for_task(
        self,
        task: AgentRunRecord,
        *,
        cancel: bool = False,
    ) -> None:
        provider = self._sandbox_provider
        if provider is None or not task.sandbox_session_id:
            return
        try:
            if cancel:
                provider.cancel(task.sandbox_session_id)
            else:
                provider.stop_session(task.sandbox_session_id)
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            task.metadata["sandbox_stop_error"] = str(exc)

    def claim_agent_run(
        self,
        *,
        worker_id: str,
        worker_kind: WorkerKind | str | None = None,
        executors: list[ExecutorType | str] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
        wait_sec: float = 0.0,
    ) -> AgentRunClaim | None:
        deadline = time.time() + max(0.0, float(wait_sec or 0.0))
        while True:
            claim = self._claim_task_once(
                worker_id=worker_id,
                worker_kind=worker_kind,
                executors=executors,
                peer_id=peer_id,
                peer_features=peer_features,
                workspace_root=workspace_root,
                lease_sec=lease_sec,
            )
            if claim is not None or wait_sec <= 0:
                return claim
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            with self._wakeup:
                self._wakeup.wait(timeout=min(remaining, 1.0))

    def _claim_task_once(
        self,
        *,
        worker_id: str,
        worker_kind: WorkerKind | str | None = None,
        executors: list[ExecutorType | str] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> AgentRunClaim | None:
        if self._store is not None:
            return self._store.claim_agent_run(
                worker_id=worker_id,
                worker_kind=worker_kind,
                executors=executors,
                peer_id=peer_id,
                peer_features=peer_features,
                workspace_root=workspace_root,
                lease_sec=lease_sec,
            )
        allowed = {_coerce_executor(executor) for executor in executors or []}
        features = (
            {str(feature) for feature in peer_features}
            if peer_features is not None
            else None
        )
        worker_kind_value = optional_worker_kind(worker_kind)
        with self._lock:
            self.recover_stale_agent_runs()
            running_tasks = self._running_tasks_locked()
            for state in self._states.values():
                task = state.task
                if task.status != TaskStatus.QUEUED:
                    continue
                if allowed and task.executor not in allowed:
                    continue
                if not self._worker_matches_task_locked(
                    task,
                    worker_kind=worker_kind_value,
                    features=features,
                    workspace_root=workspace_root,
                ):
                    continue
                if not runtime_slots_allow_agent_run_claim(
                    running_tasks,
                    task,
                    self.runtime_snapshot,
                    max_running_tasks=self.max_running_tasks,
                ):
                    continue
                task.status = TaskStatus.DISPATCHED
                task.worker_id = worker_id
                metadata = self._executor_metadata(task)
                claim = AgentRunClaim(
                    request_id=_new_id("claim"),
                    worker_id=worker_id,
                    task=task,
                    executor_request=ExecutorRunRequest(
                        task_id=task.id,
                        agent_id=task.agent_id,
                        executor=task.executor or ExecutorType.REULEAUXCODER,
                        prompt=task.prompt,
                        execution_location=(
                            task.execution_location
                            or ExecutionLocation.LOCAL_WORKSPACE
                        ),
                        issue_id=task.issue_id,
                        runtime_profile_id=task.runtime_profile_id,
                        worker_kind=metadata.get("worker_kind"),
                        model_request_origin=metadata.get("model_request_origin"),
                        worktree_role=task.worktree_role,
                        publish_policy=task.publish_policy,
                        workdir=task.workdir,
                        branch=task.branch_name,
                        model=str(task.metadata.get("model"))
                        if task.metadata.get("model") is not None
                        else None,
                        executor_session_id=task.executor_session_id,
                        metadata=metadata,
                    ),
                    runtime_snapshot=dict(self.runtime_snapshot),
                )
                self._claims[claim.request_id] = claim
                now = time.time()
                self._claim_leases[claim.request_id] = {
                    "task_id": task.id,
                    "worker_id": worker_id,
                    "peer_id": peer_id or "",
                    "last_heartbeat_at": now,
                    "lease_deadline": now + max(1, int(lease_sec or 15)),
                    "lease_sec": max(1, int(lease_sec or 15)),
                }
                self._append_event_locked(
                    task.id,
                    "claimed",
                    {
                        "worker_id": worker_id,
                        "peer_id": peer_id,
                        "worker_kind": worker_kind_value.value
                        if worker_kind_value is not None
                        else None,
                        "request_id": claim.request_id,
                        "lease_sec": max(1, int(lease_sec or 15)),
                    },
                )
                return claim
            return None

    def heartbeat_agent_run(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
        lease_sec: int | None = None,
    ) -> dict[str, Any]:
        if self._store is not None:
            result = self._store.heartbeat_agent_run(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
                lease_sec=lease_sec,
            )
            self.notify_task_available()
            return result
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return {
                    "ok": False,
                    "cancel_requested": True,
                    "reason": "agent_run_not_found",
                    "lease_sec": 0,
                }
            task = state.task
            lease = self._claim_leases.get(request_id)
            if lease is None:
                return {
                    "ok": False,
                    "cancel_requested": task_id in self._cancel_requests,
                    "reason": self._cancel_requests.get(task_id, "claim_not_found"),
                    "lease_sec": 0,
                }
            ok, reason = self._validate_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            if not ok:
                return {
                    "ok": False,
                    "cancel_requested": True,
                    "reason": reason,
                    "lease_sec": 0,
                }
            effective_lease_sec = max(1, int(lease_sec or lease.get("lease_sec") or 15))
            now = time.time()
            lease["last_heartbeat_at"] = now
            lease["lease_deadline"] = now + effective_lease_sec
            lease["lease_sec"] = effective_lease_sec
            reason = self._cancel_requests.get(task_id, "")
            if task.status == TaskStatus.DISPATCHED:
                task.status = TaskStatus.RUNNING
            return {
                "ok": True,
                "cancel_requested": bool(reason),
                "reason": reason,
                "lease_sec": effective_lease_sec,
            }

    def validate_claim_owner(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        if self._store is not None:
            return self._store.validate_claim_owner(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
        with self._lock:
            return self._validate_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )

    def recover_stale_agent_runs(self, *, now: float | None = None) -> list[str]:
        if self._store is not None:
            recovered = self._store.recover_stale_agent_runs(now=now)
            if recovered:
                self.notify_task_available()
            return recovered
        current = time.time() if now is None else now
        recovered: list[str] = []
        with self._lock:
            for request_id, lease in list(self._claim_leases.items()):
                deadline = float(lease.get("lease_deadline") or 0)
                if deadline > current:
                    continue
                task_id = str(lease.get("task_id") or "")
                state = self._states.get(task_id)
                if state is None:
                    self._claim_leases.pop(request_id, None)
                    self._claims.pop(request_id, None)
                    continue
                task = state.task
                if task.status in {
                    TaskStatus.DISPATCHED,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING_APPROVAL,
                }:
                    task.status = TaskStatus.QUEUED
                    task.worker_id = None
                    self._cancel_requests.pop(task_id, None)
                    recovered.append(task_id)
                    self._append_event_locked(
                        task_id,
                        "lease_expired",
                        {
                            "request_id": request_id,
                            "worker_id": lease.get("worker_id"),
                            "peer_id": lease.get("peer_id"),
                        },
                    )
                self._claim_leases.pop(request_id, None)
                self._claims.pop(request_id, None)
        return recovered

    def _executor_metadata(self, task: AgentRunRecord) -> dict[str, Any]:
        metadata = dict(task.metadata)
        executor = task.executor or ExecutorType.REULEAUXCODER
        if task.worktree_role is not None:
            metadata.setdefault("worktree_role", task.worktree_role.value)
        if task.publish_policy is not None:
            metadata.setdefault("publish_policy", task.publish_policy.value)
        worker_kind = str(metadata.get("worker_kind") or "").strip()
        model_request_origin = str(metadata.get("model_request_origin") or "").strip()
        worktree_role = str(metadata.get("worktree_role") or "").strip()
        publish_policy = str(metadata.get("publish_policy") or "").strip()
        if worker_kind:
            metadata.setdefault("worker_kind", worker_kind)
        if model_request_origin:
            metadata.setdefault("model_request_origin", model_request_origin)
        if worktree_role:
            metadata.setdefault("worktree_role", worktree_role)
        if publish_policy:
            metadata.setdefault("publish_policy", publish_policy)
        rendered = self._render_prompt_for_task(task, executor)
        if rendered is not None:
            metadata.setdefault("prompt_files", rendered.files)
            metadata.setdefault("prompt_metadata", rendered.metadata)
            if rendered.metadata.get("system_prompt"):
                metadata.setdefault("system_prompt", rendered.metadata["system_prompt"])
        return self._metadata_with_snapshot_capabilities(
            metadata,
            agent_id=task.agent_id,
            source=task.source,
            runtime_profile_id=task.runtime_profile_id,
        )

    def _metadata_with_snapshot_capabilities(
        self,
        metadata: dict[str, Any],
        *,
        agent_id: str,
        source: AgentRunSource,
        runtime_profile_id: str | None,
    ) -> dict[str, Any]:
        snapshot = self.runtime_snapshot
        raw_agent = _dict_from(_dict_from(snapshot.get("agents")).get(agent_id))
        resolved = _dict_from(raw_agent.get("resolved_capabilities"))
        effective = _dict_from(raw_agent.get("effective_capabilities"))
        overlay = _dict_from(resolved.get("capability_overlay"))
        if overlay:
            metadata.setdefault("capability_overlay", overlay)
        if effective:
            metadata.setdefault("effective_capabilities", effective)
            metadata.setdefault(
                "execution_policies",
                effective.get("execution_policies", []),
            )
        metadata.setdefault(
            "permission_context",
            {
                "agent_id": agent_id,
                "source": source.value,
                "interactive": source == AgentRunSource.CHAT,
                "runtime_profile_id": runtime_profile_id
                or str(raw_agent.get("runtime_profile") or ""),
                "effective_capabilities": effective,
            },
        )
        return metadata

    def _session_metadata(
        self,
        task: AgentRunRecord,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_metadata = self._executor_metadata(task)
        session_metadata.update(dict(metadata or {}))
        return session_metadata

    def _worker_matches_task_locked(
        self,
        task: AgentRunRecord,
        *,
        worker_kind: WorkerKind | None,
        features: set[str] | None,
        workspace_root: str | None,
    ) -> bool:
        return worker_matches_agent_run(
            task,
            worker_kind=worker_kind,
            features=features,
            workspace_root=workspace_root,
        )

    def _render_prompt_for_task(
        self, task: AgentRunRecord, executor: ExecutorType
    ) -> Any | None:
        snapshot = self.runtime_snapshot
        agents = _dict_from(snapshot.get("agents"))
        profiles = _dict_from(snapshot.get("runtime_profiles"))
        raw_agent = _dict_from(agents.get(task.agent_id))
        profile_id = task.runtime_profile_id or str(raw_agent.get("runtime_profile") or "")
        raw_profile = _dict_from(profiles.get(profile_id))
        prompt = _dict_from(raw_agent.get("prompt"))
        profile_mcp = _dict_from(raw_profile.get("mcp"))
        resolved = _dict_from(raw_agent.get("resolved_capabilities"))
        credential_refs = {
            **{
                str(key): str(val)
                for key, val in _dict_from(raw_profile.get("credential_refs")).items()
            },
            **{
                str(key): str(val)
                for key, val in _dict_from(raw_agent.get("credential_refs")).items()
            },
        }
        servers = []
        for source in (profile_mcp.get("servers"), resolved.get("mcp_servers")):
            servers.extend(_string_list_from(source))
        context = CanonicalAgentContext(
            agent_id=task.agent_id,
            agent_name=str(raw_agent.get("name") or ""),
            agent_md=(
                str(prompt["agent_md"]) if prompt.get("agent_md") is not None else None
            ),
            system_append=str(prompt.get("system_append") or ""),
            dispatch=_dict_from(raw_agent.get("dispatch")),
            capability_refs=_string_list_from(raw_agent.get("capability_refs")),
            resolved_capabilities=resolved,
            mcp_servers=servers,
            credential_refs=credential_refs,
        )
        return ExecutorPromptRenderer().render(executor.value, context)

    def pin_session(self, task_id: str, session: TaskSessionRef) -> None:
        if self._store is not None:
            self._store.pin_session(task_id, session)
            self.notify_task_available()
            return
        with self._lock:
            task = self._task_locked(task_id)
            task.status = TaskStatus.RUNNING
            if session.executor_session_id is not None:
                task.executor_session_id = session.executor_session_id
            if session.workdir is not None:
                task.workdir = session.workdir
            if session.branch is not None:
                task.branch_name = session.branch
            pinned = TaskSessionRef(
                agent_id=session.agent_id,
                executor=session.executor,
                execution_location=session.execution_location,
                issue_id=session.issue_id,
                task_id=session.task_id,
                workdir=task.workdir,
                branch=task.branch_name,
                executor_session_id=task.executor_session_id,
                metadata=self._session_metadata(task, session.metadata),
            )
            self._sessions[task_id] = pinned
            self._append_event_locked(
                task_id,
                "session_pinned",
                {
                    "executor_session_id": task.executor_session_id,
                    "workdir": task.workdir,
                    "branch": task.branch_name,
                },
            )

    def pin_claimed_session(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
        workdir: str | None = None,
        branch: str | None = None,
        executor_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if self._store is not None:
            result = self._store.pin_claimed_session(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
                workdir=workdir,
                branch=branch,
                executor_session_id=executor_session_id,
                metadata=metadata,
            )
            self.notify_task_available()
            return result
        with self._lock:
            ok, reason = self._validate_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            if not ok:
                return False, reason
            task = self._task_locked(task_id)
            session = TaskSessionRef(
                agent_id=task.agent_id,
                executor=task.executor or ExecutorType.REULEAUXCODER,
                execution_location=(
                    task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                ),
                issue_id=task.issue_id,
                task_id=task_id,
                workdir=workdir if workdir else None,
                branch=branch if branch else None,
                executor_session_id=(
                    executor_session_id if executor_session_id else None
                ),
                metadata=self._session_metadata(task, metadata),
            )
            self.pin_session(task_id, session)
            if metadata:
                self._append_event_locked(
                    task_id,
                    "session_metadata",
                    {"request_id": request_id, "worker_id": worker_id, **metadata},
                )
            return True, ""

    def append_executor_event(
        self,
        task_id: str,
        event: ExecutorEvent,
        *,
        request_id: str | None = None,
        worker_id: str | None = None,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        if self._store is not None:
            result = self._store.append_executor_event(
                task_id,
                event,
                request_id=request_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            self.notify_task_available()
            return result
        with self._lock:
            if request_id or worker_id or peer_id:
                ok, reason = self._validate_claim_owner_locked(
                    request_id=request_id or "",
                    task_id=task_id,
                    worker_id=worker_id or "",
                    peer_id=peer_id,
                )
                if not ok:
                    return False, reason
            task = self._task_locked(task_id)
            self._append_event_locked(task_id, event.type.value, event.to_dict())
            expansion = expand_environment_executor_event(task.metadata, event)
            for event_type, payload in expansion.events:
                self._append_event_locked(task_id, event_type, payload)
            if expansion.policy_error:
                task.metadata["environment_policy_violation"] = expansion.policy_error
                task.status = TaskStatus.BLOCKED
                task.failure_reason = expansion.policy_error
                task.cancel_reason = None
                self._append_event_locked(
                    task_id,
                    "blocked",
                    {"error": expansion.policy_error},
                )
            if event.type.value == "status":
                status = str(event.data.get("status", ""))
                if status == "waiting_approval":
                    if should_block_waiting_approval(task.source):
                        task.status = TaskStatus.BLOCKED
                        task.failure_reason = str(
                            event.data.get("reason")
                            or event.data.get("message")
                            or "approval_required"
                        )
                        task.cancel_reason = None
                        self._append_event_locked(
                            task_id,
                            "permission.blocked_review",
                            blocked_review_event_payload(event.data),
                        )
                    else:
                        task.status = TaskStatus.WAITING_APPROVAL
                elif status == "running":
                    task.status = TaskStatus.RUNNING
                elif status == "blocked":
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = str(
                        event.data.get("reason")
                        or event.data.get("message")
                        or "blocked"
                    )
                    task.cancel_reason = None
            return True, ""

    def complete_claimed_agent_run(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str, AgentRunRecord | None]:
        if self._store is not None:
            result_value = self._store.complete_claimed_agent_run(
                task_id,
                result,
                request_id=request_id,
                worker_id=worker_id,
                peer_id=peer_id,
                artifacts=artifacts,
            )
            if result_value[2] is not None:
                self._stop_sandbox_for_task(result_value[2])
            self.notify_task_available()
            return result_value
        with self._lock:
            ok, reason = self._validate_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            if not ok:
                return False, reason, None
            return True, "", self.complete_agent_run(task_id, result, artifacts=artifacts)

    def complete_agent_run(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentRunRecord:
        if self._store is not None:
            task = self._store.complete_agent_run(task_id, result, artifacts=artifacts)
            self._stop_sandbox_for_task(task)
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            policy_error = str(
                task.metadata.get("environment_policy_violation") or ""
            ).strip()
            requested_cancel_reason = str(
                self._cancel_requests.get(task_id) or ""
            ).strip()
            if result.succeeded and not policy_error:
                self._states[task_id].complete_agent_run(output=result.output)
                task.failure_reason = None
                task.cancel_reason = None
            elif policy_error:
                task.status = TaskStatus.BLOCKED
                task.output = policy_error
                task.failure_reason = policy_error
                task.cancel_reason = None
            elif result.status == "cancelled":
                cancel_reason = (
                    result.output
                    or requested_cancel_reason
                    or result.error
                    or "cancelled"
                )
                task.status = TaskStatus.CANCELLED
                task.output = result.output or cancel_reason
                task.failure_reason = "cancelled"
                task.cancel_reason = cancel_reason
            elif result.status == "blocked":
                task.status = TaskStatus.BLOCKED
                task.output = result.output or result.error
                task.failure_reason = result.error or result.output or "blocked"
                task.cancel_reason = None
            else:
                task.status = TaskStatus.FAILED
                task.output = result.output
                task.failure_reason = result.error or "agent_error"
                task.cancel_reason = None
            if result.executor_session_id:
                task.executor_session_id = result.executor_session_id
                self._sessions[task_id] = TaskSessionRef(
                    agent_id=task.agent_id,
                    executor=task.executor or ExecutorType.REULEAUXCODER,
                    execution_location=(
                        task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                    ),
                    issue_id=task.issue_id,
                    task_id=task_id,
                    workdir=task.workdir,
                    branch=task.branch_name,
                    executor_session_id=task.executor_session_id,
                    metadata=self._session_metadata(task),
                )
            for event in result.events:
                self._append_event_locked(task_id, event.type.value, event.to_dict())
                expansion = expand_environment_executor_event(task.metadata, event)
                for event_type, payload in expansion.events:
                    self._append_event_locked(task_id, event_type, payload)
                if expansion.policy_error and not policy_error:
                    policy_error = expansion.policy_error
                    task.metadata["environment_policy_violation"] = policy_error
                    task.status = TaskStatus.BLOCKED
                    task.output = policy_error
                    task.failure_reason = policy_error
                    task.cancel_reason = None
                    self._append_event_locked(
                        task_id,
                        "blocked",
                        {"error": policy_error},
                    )
            for artifact in artifacts or []:
                self.attach_artifact(task_id, **artifact)
            summary = environment_summary_event(
                task.metadata,
                task.status.value,
                output=task.output or result.output,
                error=policy_error or result.error or "",
            )
            if summary is not None:
                self._append_event_locked(task_id, summary[0], summary[1])
            self._append_event_locked(
                task_id,
                task.status.value if policy_error else result.status,
                {"result": result.to_dict(), "agent_run": _agent_run_to_dict(task)},
            )
            self._clear_task_claims_locked(task_id)
            self._cancel_requests.pop(task_id, None)
            self._stop_sandbox_for_task(task)
            return task

    def retry_agent_run(
        self,
        task_id: str,
        *,
        new_agent_run_id: str | None = None,
        resume_session: bool = False,
    ) -> AgentRunRecord:
        if self._store is not None:
            task = self._store.retry_agent_run(
                task_id,
                new_agent_run_id=new_agent_run_id,
                resume_session=resume_session,
            )
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            if not task.is_terminal:
                raise ValueError("only terminal AgentRuns can be retried")
            metadata = dict(task.metadata)
            metadata["retry_of"] = task.id
            retry = AgentRunRequest(
                issue_id=task.issue_id,
                agent_id=task.agent_id,
                prompt=task.prompt,
                source=task.source,
                executor=task.executor or ExecutorType.REULEAUXCODER,
                execution_location=(
                    task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                ),
                trigger_mode=task.trigger_mode,
                runtime_profile_id=task.runtime_profile_id,
                parent_task_id=task.parent_task_id,
                trigger_comment_id=task.trigger_comment_id,
                branch_name=task.branch_name,
                pr_url=task.pr_url,
                worktree_role=task.worktree_role,
                publish_policy=task.publish_policy,
                workdir=task.workdir,
                sandbox_id=task.sandbox_id,
                sandbox_session_id=task.sandbox_session_id,
                workspace_ref=task.workspace_ref,
                delegated_by_run_id=task.delegated_by_run_id,
                parent_run_id=task.parent_run_id,
                executor_session_id=task.executor_session_id
                if resume_session
                else None,
                model=str(task.metadata.get("model"))
                if task.metadata.get("model") is not None
                else None,
                metadata=metadata,
            )
            return self.submit_agent_run(retry, task_id=new_agent_run_id)

    def fail_agent_run(self, task_id: str, *, error: str) -> AgentRunRecord:
        if self._store is not None:
            task = self._store.fail_agent_run(task_id, error=error)
            self._stop_sandbox_for_task(task)
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            task.status = TaskStatus.FAILED
            task.output = error
            task.failure_reason = error
            task.cancel_reason = None
            self._append_event_locked(task_id, "failed", {"error": error})
            self._clear_task_claims_locked(task_id)
            self._cancel_requests.pop(task_id, None)
            self._stop_sandbox_for_task(task)
            return task

    def cancel_agent_run(self, task_id: str, *, reason: str = "user_cancelled") -> bool:
        if self._store is not None:
            task_before = self._store.get_agent_run(task_id)
            ok = self._store.cancel_agent_run(task_id, reason=reason)
            if ok and task_before.sandbox_session_id:
                self._stop_sandbox_for_task(task_before, cancel=True)
            self.notify_task_available()
            return ok
        with self._lock:
            task = self._task_locked(task_id)
            if task.is_terminal:
                return False
            if task.sandbox_session_id:
                self._stop_sandbox_for_task(task, cancel=True)
                task.status = TaskStatus.CANCELLED
                task.output = reason
                task.failure_reason = "cancelled"
                task.cancel_reason = reason
                self._append_event_locked(task_id, "cancelled", {"reason": reason})
                self._clear_task_claims_locked(task_id)
                self._cancel_requests.pop(task_id, None)
                return True
            if task.status in {
                TaskStatus.DISPATCHED,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
            }:
                self._cancel_requests[task_id] = reason
                self._append_event_locked(
                    task_id,
                    "cancel_requested",
                    {"reason": reason, "worker_id": task.worker_id},
                )
                return True
            task.status = TaskStatus.CANCELLED
            task.output = reason
            task.failure_reason = "cancelled"
            task.cancel_reason = reason
            self._append_event_locked(task_id, "cancelled", {"reason": reason})
            self._clear_task_claims_locked(task_id)
            return True

    def attach_artifact(
        self,
        task_id: str,
        *,
        type: str,
        status: str = "generated",
        artifact_id: str | None = None,
        branch_name: str | None = None,
        pr_url: str | None = None,
        content: str | None = None,
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskArtifact:
        if self._store is not None:
            artifact = self._store.attach_artifact(
                task_id,
                type=type,
                status=status,
                artifact_id=artifact_id,
                branch_name=branch_name,
                pr_url=pr_url,
                content=content,
                path=path,
                metadata=metadata,
            )
            self.notify_task_available()
            return artifact
        with self._lock:
            state = self._states[task_id]
            artifact = state.attach_artifact(
                artifact_id=artifact_id or _new_id("artifact"),
                type=type,
                status=status,
                branch_name=branch_name,
                pr_url=pr_url,
                content=content,
                path=path,
                metadata=metadata,
            )
            if artifact.branch_name:
                state.task.branch_name = artifact.branch_name
            if artifact.pr_url:
                state.task.pr_url = artifact.pr_url
            if artifact.type == ArtifactType.PULL_REQUEST:
                state.issue_status = IssueStatus.IN_REVIEW
            self._append_event_locked(
                task_id,
                "artifact_attached",
                {"artifact": _artifact_to_dict(artifact)},
            )
            return artifact

    def create_or_update_pr(self, task_id: str, *, diff: str = "") -> TaskArtifact:
        if self._store is not None:
            artifact = self._store.create_or_update_pr(task_id, diff=diff)
            self.notify_task_available()
            return artifact
        with self._lock:
            task = self._task_locked(task_id)
            pr = self.pr_flow.create_or_update(task, diff=diff)
            task.branch_name = pr.branch_name
            task.pr_url = pr.pr_url
            return self.attach_artifact(
                task_id,
                type=ArtifactType.PULL_REQUEST.value,
                status=ArtifactStatus.PR_CREATED.value,
                branch_name=pr.branch_name,
                pr_url=pr.pr_url,
                content=diff,
                metadata=pr.metadata,
            )

    def list_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> list[AgentRunEvent]:
        limit = clamp_event_limit(limit)
        if self._store is not None:
            return self._store.list_events(task_id, after_seq=after_seq, limit=limit)
        with self._lock:
            return [
                event
                for event in list(self._events.get(task_id, []))
                if event.seq > after_seq
            ][:limit]

    def wait_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        timeout_sec: float = 0.0,
        limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> list[AgentRunEvent]:
        limit = clamp_event_limit(limit)
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while True:
            events = self.list_events(task_id, after_seq=after_seq, limit=limit)
            if events or timeout_sec <= 0:
                return events
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            with self._wakeup:
                self._wakeup.wait(timeout=min(remaining, 1.0))

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        if self._store is not None:
            return self._store.list_artifacts(task_id)
        with self._lock:
            return list(self._states[task_id].artifacts.values())

    def get_agent_run(self, task_id: str) -> AgentRunRecord:
        if self._store is not None:
            return self._store.get_agent_run(task_id)
        with self._lock:
            return self._task_locked(task_id)

    def agent_run_to_dict(self, task_id: str) -> dict[str, Any]:
        if self._store is not None:
            return self._store.agent_run_to_dict(task_id)
        return _agent_run_to_dict(self.get_agent_run(task_id))

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.artifacts_to_dict(task_id)
        return [_artifact_to_dict(artifact) for artifact in self.list_artifacts(task_id)]

    def list_agent_runs(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        issue_id: str | None = None,
        limit: int = 50,
        after_created_at: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.list_agent_runs(
                status=status,
                agent_id=agent_id,
                issue_id=issue_id,
                limit=limit,
                after_created_at=after_created_at,
            )
        with self._lock:
            tasks = [_agent_run_to_dict(state.task) for state in self._states.values()]
            if status:
                tasks = [task for task in tasks if task.get("status") == status]
            if agent_id:
                tasks = [task for task in tasks if task.get("agent_id") == agent_id]
            if issue_id:
                tasks = [task for task in tasks if task.get("issue_id") == issue_id]
            return tasks[-max(1, int(limit or 50)) :]

    def load_agent_run_detail(
        self,
        task_id: str,
        *,
        event_limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> dict[str, Any]:
        event_limit = clamp_event_limit(event_limit)
        if self._store is not None:
            return self._store.load_agent_run_detail(task_id, event_limit=event_limit)
        task = self.agent_run_to_dict(task_id)
        with self._lock:
            raw_events = list(self._events.get(task_id, []))[-event_limit:]
        events = [event.to_dict() for event in raw_events]
        session = self._sessions.get(task_id)
        return {
            "agent_run": task,
            "artifacts": self.artifacts_to_dict(task_id),
            "session": {
                "agent_id": session.agent_id,
                "executor": session.executor.value,
                "execution_location": session.execution_location.value,
                "issue_id": session.issue_id,
                "task_id": session.task_id,
                "workdir": session.workdir,
                "branch": session.branch,
                "executor_session_id": session.executor_session_id,
                "metadata": dict(session.metadata),
            }
            if session is not None
            else None,
            "claim": None,
            "events": events,
        }

    def _running_count_locked(self) -> int:
        return len(self._running_tasks_locked())

    def _running_tasks_locked(self) -> list[AgentRunRecord]:
        return [
            state.task
            for state in self._states.values()
            if state.task.status
            in {TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
        ]

    def _task_locked(self, task_id: str) -> AgentRunRecord:
        state = self._states.get(task_id)
        if state is None:
            raise KeyError(f"AgentRun not found: {task_id}")
        return state.task

    def _validate_claim_owner_locked(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        if task_id not in self._states:
            return False, "agent_run_not_found"
        lease = self._claim_leases.get(request_id)
        if lease is None:
            return False, "claim_not_found"
        if str(lease.get("task_id") or "") != task_id:
            return False, "task_mismatch"
        if str(lease.get("worker_id") or "") != worker_id:
            return False, "worker_mismatch"
        expected_peer = str(lease.get("peer_id") or "")
        if peer_id and expected_peer and expected_peer != peer_id:
            return False, "peer_mismatch"
        return True, ""

    def _clear_task_claims_locked(self, task_id: str) -> None:
        for request_id, claim in list(self._claims.items()):
            if claim.task.id == task_id:
                self._claims.pop(request_id, None)
                self._claim_leases.pop(request_id, None)

    def _append_event_locked(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> AgentRunEvent:
        events = self._events.setdefault(task_id, [])
        event = AgentRunEvent(
            task_id=task_id,
            seq=len(events) + 1,
            type=event_type,
            payload=payload,
        )
        events.append(event)
        self.notify_task_available()
        return event


__all__ = [
    "AgentRunControlPlane",
    "AgentRunClaim",
    "AgentRunEvent",
    "AgentRunRequest",
    "InMemoryPRFlow",
    "PRArtifactResult",
    "PRFlow",
    "AgentRunClaim",
    "AgentRunEvent",
    "AgentRunRequest",
]
