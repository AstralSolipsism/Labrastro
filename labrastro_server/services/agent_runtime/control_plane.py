"""Server-side control plane for queued Agent runtime tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
import threading
import time
import uuid

from reuleauxcoder.domain.agent_runtime.models import (
    ArtifactStatus,
    ArtifactType,
    ExecutionLocation,
    ExecutorType,
    TaskArtifact,
    TaskRecord,
    TaskSessionRef,
    TaskStatus,
    TriggerMode,
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
from labrastro_server.services.agent_runtime.prompt_renderer import (
    CanonicalAgentContext,
    ExecutorPromptRenderer,
)
from labrastro_server.services.agent_runtime.runtime_store import (
    DEFAULT_RUNTIME_EVENT_LIMIT,
    RuntimeStore,
    clamp_event_limit,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


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


def _task_to_dict(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "issue_id": task.issue_id,
        "agent_id": task.agent_id,
        "trigger_mode": task.trigger_mode.value,
        "status": task.status.value,
        "prompt": task.prompt,
        "runtime_profile_id": task.runtime_profile_id,
        "executor": task.executor.value if task.executor else None,
        "execution_location": (
            task.execution_location.value if task.execution_location else None
        ),
        "output": task.output,
        "parent_task_id": task.parent_task_id,
        "trigger_comment_id": task.trigger_comment_id,
        "branch_name": task.branch_name,
        "pr_url": task.pr_url,
        "worker_id": task.worker_id,
        "executor_session_id": task.executor_session_id,
        "workdir": task.workdir,
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


def _string_list_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _workspace_key(value: str | None) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").lower()


def _can_resume_from_parent(request: "RuntimeTaskRequest", parent: TaskRecord) -> bool:
    if not parent.executor_session_id:
        return False
    return (
        request.agent_id == parent.agent_id
        and request.runtime_profile_id == parent.runtime_profile_id
        and request.executor == parent.executor
        and request.execution_location == parent.execution_location
        and _workspace_key(request.workdir) == _workspace_key(parent.workdir)
        and str(request.branch_name or "") == str(parent.branch_name or "")
    )


@dataclass
class RuntimeTaskRequest:
    """Request accepted by the Labrastro runtime control plane."""

    issue_id: str
    agent_id: str
    prompt: str
    executor: ExecutorType | str | None = None
    execution_location: ExecutionLocation | str | None = None
    trigger_mode: TriggerMode | str = TriggerMode.ISSUE_TASK
    runtime_profile_id: str | None = None
    parent_task_id: str | None = None
    trigger_comment_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    workdir: str | None = None
    executor_session_id: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.executor = _optional_executor(self.executor)
        self.execution_location = _optional_location(self.execution_location)
        if not isinstance(self.trigger_mode, TriggerMode):
            self.trigger_mode = TriggerMode(str(self.trigger_mode))


@dataclass
class RuntimeTaskEvent:
    """Ordered task event stored by the control plane."""

    task_id: str
    seq: int
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "seq": self.seq,
            "type": self.type,
            "payload": dict(self.payload),
        }


@dataclass
class RuntimeTaskClaim:
    """Task payload returned to a worker after a successful claim."""

    request_id: str
    worker_id: str
    task: TaskRecord
    executor_request: ExecutorRunRequest
    runtime_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "task": _task_to_dict(self.task),
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

    def create_or_update(self, task: TaskRecord, *, diff: str = "") -> PRArtifactResult:
        """Create or update a pull request for task output."""


class InMemoryPRFlow:
    """Deterministic PR flow used by tests and local dry runs."""

    def __init__(self, base_url: str = "https://example.invalid/pr") -> None:
        self.base_url = base_url.rstrip("/")

    def create_or_update(self, task: TaskRecord, *, diff: str = "") -> PRArtifactResult:
        branch = task.branch_name or f"agent/{task.agent_id}/{task.id[:12]}"
        return PRArtifactResult(
            branch_name=branch,
            pr_url=f"{self.base_url}/{task.id}",
            metadata={"diff_bytes": len(diff.encode("utf-8"))},
        )


class AgentRuntimeControlPlane:
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
        store: RuntimeStore | None = None,
    ) -> None:
        self.max_running_tasks = max(1, int(max_running_tasks or 1))
        self.runtime_snapshot = dict(runtime_snapshot or {})
        self.pr_flow = pr_flow or InMemoryPRFlow()
        self._store = store
        self._lock = threading.RLock()
        self._states: dict[str, TaskLifecycleState] = {}
        self._sessions: dict[str, TaskSessionRef] = {}
        self._events: dict[str, list[RuntimeTaskEvent]] = {}
        self._claims: dict[str, RuntimeTaskClaim] = {}
        self._claim_leases: dict[str, dict[str, Any]] = {}
        self._cancel_requests: dict[str, str] = {}
        self._wakeup = threading.Condition()

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

    def submit_task(
        self, request: RuntimeTaskRequest, *, task_id: str | None = None
    ) -> TaskRecord:
        if self._store is not None:
            task = self._store.submit_task(request, task_id=task_id)
            self.notify_task_available()
            return task
        with self._lock:
            request = self._resolve_request_locked(request)
            metadata = dict(request.metadata)
            if request.model is not None:
                metadata.setdefault("model", request.model)
            task = TaskRecord(
                id=task_id or _new_id("task"),
                issue_id=request.issue_id,
                agent_id=request.agent_id,
                trigger_mode=request.trigger_mode,
                status=TaskStatus.QUEUED,
                prompt=request.prompt,
                runtime_profile_id=request.runtime_profile_id,
                executor=request.executor,
                execution_location=request.execution_location,
                parent_task_id=request.parent_task_id,
                trigger_comment_id=request.trigger_comment_id,
                branch_name=request.branch_name,
                pr_url=request.pr_url,
                executor_session_id=request.executor_session_id,
                workdir=request.workdir,
                metadata=metadata,
            )
            self._states[task.id] = TaskLifecycleState(task=task)
            self._events[task.id] = []
            self._append_event_locked(task.id, "queued", {"task": _task_to_dict(task)})
        self.notify_task_available()
        return task

    def notify_task_available(self) -> None:
        """Wake workers waiting for queued runtime tasks or event changes."""

        with self._wakeup:
            self._wakeup.notify_all()

    def _resolve_request_locked(self, request: RuntimeTaskRequest) -> RuntimeTaskRequest:
        parent = self._states.get(request.parent_task_id).task if request.parent_task_id in self._states else None
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

        agent_profile_id = str(raw_agent.get("runtime_profile") or "").strip()
        profile_id = str(request.runtime_profile_id or agent_profile_id).strip()
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
        if request.model is None and raw_profile.get("model") is not None:
            request.model = str(raw_profile["model"])
        if (
            parent is not None
            and request.executor_session_id is None
            and _can_resume_from_parent(request, parent)
        ):
            request.executor_session_id = parent.executor_session_id
        return request

    def claim_task(
        self,
        *,
        worker_id: str,
        executors: list[ExecutorType | str] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
        wait_sec: float = 0.0,
    ) -> RuntimeTaskClaim | None:
        deadline = time.time() + max(0.0, float(wait_sec or 0.0))
        while True:
            claim = self._claim_task_once(
                worker_id=worker_id,
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
        executors: list[ExecutorType | str] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> RuntimeTaskClaim | None:
        if self._store is not None:
            return self._store.claim_task(
                worker_id=worker_id,
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
        with self._lock:
            self.recover_stale_tasks()
            if self._running_count_locked() >= self.max_running_tasks:
                return None
            for state in self._states.values():
                task = state.task
                if task.status != TaskStatus.QUEUED:
                    continue
                if allowed and task.executor not in allowed:
                    continue
                if not self._worker_matches_task_locked(
                    task, features=features, workspace_root=workspace_root
                ):
                    continue
                task.status = TaskStatus.DISPATCHED
                task.worker_id = worker_id
                metadata = self._executor_metadata(task)
                claim = RuntimeTaskClaim(
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
                        "request_id": claim.request_id,
                        "lease_sec": max(1, int(lease_sec or 15)),
                    },
                )
                return claim
            return None

    def heartbeat_task(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
        lease_sec: int | None = None,
    ) -> dict[str, Any]:
        if self._store is not None:
            result = self._store.heartbeat_task(
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
                    "reason": "task_not_found",
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

    def recover_stale_tasks(self, *, now: float | None = None) -> list[str]:
        if self._store is not None:
            recovered = self._store.recover_stale_tasks(now=now)
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

    def _executor_metadata(self, task: TaskRecord) -> dict[str, Any]:
        metadata = dict(task.metadata)
        executor = task.executor or ExecutorType.REULEAUXCODER
        rendered = self._render_prompt_for_task(task, executor)
        if rendered is not None:
            metadata.setdefault("prompt_files", rendered.files)
            metadata.setdefault("prompt_metadata", rendered.metadata)
            if rendered.metadata.get("system_prompt"):
                metadata.setdefault("system_prompt", rendered.metadata["system_prompt"])
        return metadata

    def _worker_matches_task_locked(
        self,
        task: TaskRecord,
        *,
        features: set[str] | None,
        workspace_root: str | None,
    ) -> bool:
        location = task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
        if features is None:
            return True
        if location == ExecutionLocation.LOCAL_WORKSPACE:
            if (
                "agent_runtime" not in features
                and "agent_runtime.local_workspace" not in features
            ):
                return False
            bound_workspace = str(task.metadata.get("workspace_root") or "").strip()
            if bound_workspace:
                return bool(workspace_root) and _workspace_key(
                    bound_workspace
                ) == _workspace_key(workspace_root)
            return True
        location_feature = f"agent_runtime.{location.value}"
        if location_feature in features:
            return True
        return "agent_runtime" in features

    def _render_prompt_for_task(
        self, task: TaskRecord, executor: ExecutorType
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
                self._append_event_locked(
                    task_id,
                    "blocked",
                    {"error": expansion.policy_error},
                )
            if event.type.value == "status":
                status = str(event.data.get("status", ""))
                if status == "waiting_approval":
                    task.status = TaskStatus.WAITING_APPROVAL
                elif status == "running":
                    task.status = TaskStatus.RUNNING
                elif status == "blocked":
                    task.status = TaskStatus.BLOCKED
            return True, ""

    def complete_claimed_task(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str, TaskRecord | None]:
        if self._store is not None:
            result_value = self._store.complete_claimed_task(
                task_id,
                result,
                request_id=request_id,
                worker_id=worker_id,
                peer_id=peer_id,
                artifacts=artifacts,
            )
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
            return True, "", self.complete_task(task_id, result, artifacts=artifacts)

    def complete_task(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> TaskRecord:
        if self._store is not None:
            task = self._store.complete_task(task_id, result, artifacts=artifacts)
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            policy_error = str(
                task.metadata.get("environment_policy_violation") or ""
            ).strip()
            if result.succeeded and not policy_error:
                self._states[task_id].complete_task(output=result.output)
            elif policy_error:
                task.status = TaskStatus.BLOCKED
                task.output = policy_error
            elif result.status == "cancelled":
                task.status = TaskStatus.CANCELLED
                task.output = result.output
            elif result.status == "blocked":
                task.status = TaskStatus.BLOCKED
                task.output = result.output or result.error
            else:
                task.status = TaskStatus.FAILED
                task.output = result.output
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
                {"result": result.to_dict(), "task": _task_to_dict(task)},
            )
            self._clear_task_claims_locked(task_id)
            self._cancel_requests.pop(task_id, None)
            return task

    def retry_task(
        self,
        task_id: str,
        *,
        new_task_id: str | None = None,
        resume_session: bool = False,
    ) -> TaskRecord:
        if self._store is not None:
            task = self._store.retry_task(
                task_id,
                new_task_id=new_task_id,
                resume_session=resume_session,
            )
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            if not task.is_terminal:
                raise ValueError("only terminal runtime tasks can be retried")
            metadata = dict(task.metadata)
            metadata["retry_of"] = task.id
            retry = RuntimeTaskRequest(
                issue_id=task.issue_id,
                agent_id=task.agent_id,
                prompt=task.prompt,
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
                workdir=task.workdir,
                executor_session_id=task.executor_session_id
                if resume_session
                else None,
                model=str(task.metadata.get("model"))
                if task.metadata.get("model") is not None
                else None,
                metadata=metadata,
            )
            return self.submit_task(retry, task_id=new_task_id)

    def fail_task(self, task_id: str, *, error: str) -> TaskRecord:
        if self._store is not None:
            task = self._store.fail_task(task_id, error=error)
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            task.status = TaskStatus.FAILED
            task.output = error
            self._append_event_locked(task_id, "failed", {"error": error})
            self._clear_task_claims_locked(task_id)
            self._cancel_requests.pop(task_id, None)
            return task

    def cancel_task(self, task_id: str, *, reason: str = "user_cancelled") -> bool:
        if self._store is not None:
            ok = self._store.cancel_task(task_id, reason=reason)
            self.notify_task_available()
            return ok
        with self._lock:
            task = self._task_locked(task_id)
            if task.is_terminal:
                return False
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
    ) -> list[RuntimeTaskEvent]:
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
    ) -> list[RuntimeTaskEvent]:
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

    def get_task(self, task_id: str) -> TaskRecord:
        if self._store is not None:
            return self._store.get_task(task_id)
        with self._lock:
            return self._task_locked(task_id)

    def task_to_dict(self, task_id: str) -> dict[str, Any]:
        if self._store is not None:
            return self._store.task_to_dict(task_id)
        return _task_to_dict(self.get_task(task_id))

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.artifacts_to_dict(task_id)
        return [_artifact_to_dict(artifact) for artifact in self.list_artifacts(task_id)]

    def list_tasks(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        issue_id: str | None = None,
        limit: int = 50,
        after_created_at: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.list_tasks(
                status=status,
                agent_id=agent_id,
                issue_id=issue_id,
                limit=limit,
                after_created_at=after_created_at,
            )
        with self._lock:
            tasks = [_task_to_dict(state.task) for state in self._states.values()]
            if status:
                tasks = [task for task in tasks if task.get("status") == status]
            if agent_id:
                tasks = [task for task in tasks if task.get("agent_id") == agent_id]
            if issue_id:
                tasks = [task for task in tasks if task.get("issue_id") == issue_id]
            return tasks[-max(1, int(limit or 50)) :]

    def load_task_detail(
        self,
        task_id: str,
        *,
        event_limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> dict[str, Any]:
        event_limit = clamp_event_limit(event_limit)
        if self._store is not None:
            return self._store.load_task_detail(task_id, event_limit=event_limit)
        task = self.task_to_dict(task_id)
        with self._lock:
            raw_events = list(self._events.get(task_id, []))[-event_limit:]
        events = [event.to_dict() for event in raw_events]
        session = self._sessions.get(task_id)
        return {
            "task": task,
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
            }
            if session is not None
            else None,
            "claim": None,
            "events": events,
        }

    def _running_count_locked(self) -> int:
        return sum(
            1
            for state in self._states.values()
            if state.task.status
            in {TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
        )

    def _task_locked(self, task_id: str) -> TaskRecord:
        state = self._states.get(task_id)
        if state is None:
            raise KeyError(f"runtime task not found: {task_id}")
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
            return False, "task_not_found"
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
    ) -> RuntimeTaskEvent:
        events = self._events.setdefault(task_id, [])
        event = RuntimeTaskEvent(
            task_id=task_id,
            seq=len(events) + 1,
            type=event_type,
            payload=payload,
        )
        events.append(event)
        self.notify_task_available()
        return event


__all__ = [
    "AgentRuntimeControlPlane",
    "InMemoryPRFlow",
    "PRArtifactResult",
    "PRFlow",
    "RuntimeTaskClaim",
    "RuntimeTaskEvent",
    "RuntimeTaskRequest",
]
