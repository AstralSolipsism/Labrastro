"""Executor backend abstraction for AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Callable, Protocol

from reuleauxcoder.domain.agent_runtime.models import (
    ExecutionLocation,
    ExecutorType,
    ModelRequestOrigin,
    PublishPolicy,
    TaskSessionRef,
    WorkerKind,
    WorktreeRole,
)
from labrastro_server.services.agent_runtime.runtime_policy import (
    optional_publish_policy,
    optional_worktree_role,
)
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent.runtime_budget import normalize_runtime_budget
from reuleauxcoder.domain.memory.runtime import bind_memory_scope_to_agent


class ExecutorEventType(str, Enum):
    """Normalized executor output event types."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    STATUS = "status"
    ERROR = "error"
    LOG = "log"
    USAGE = "usage"
    RESULT = "result"
    LIFECYCLE_HOOK = "lifecycle_hook"
    SESSION_RUN_START = "session_run_start"
    SESSION_RUN_END = "session_run_end"


def _coerce_executor(value: ExecutorType | str) -> ExecutorType:
    if isinstance(value, ExecutorType):
        return value
    return ExecutorType(str(value))


def _coerce_execution_location(
    value: ExecutionLocation | str,
) -> ExecutionLocation:
    if isinstance(value, ExecutionLocation):
        return value
    return ExecutionLocation(str(value))


def _coerce_event_type(value: ExecutorEventType | str) -> ExecutorEventType:
    if isinstance(value, ExecutorEventType):
        return value
    text = str(value).replace("-", "_")
    return ExecutorEventType(text)


@dataclass
class ExecutorRunRequest:
    """Executor-neutral request to start or resume an AgentRun."""

    task_id: str
    agent_id: str
    executor: ExecutorType | str
    prompt: str
    execution_location: ExecutionLocation | str = ExecutionLocation.LOCAL_WORKSPACE
    issue_id: str | None = None
    runtime_profile_id: str | None = None
    worker_kind: WorkerKind | str | None = None
    model_request_origin: ModelRequestOrigin | str | None = None
    worktree_role: WorktreeRole | str | None = None
    publish_policy: PublishPolicy | str | None = None
    workdir: str | None = None
    branch: str | None = None
    model: str | None = None
    executor_session_id: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.executor = _coerce_executor(self.executor)
        self.execution_location = _coerce_execution_location(self.execution_location)
        if self.worker_kind is not None and not isinstance(self.worker_kind, WorkerKind):
            self.worker_kind = WorkerKind(str(self.worker_kind))
        if self.model_request_origin is not None and not isinstance(
            self.model_request_origin,
            ModelRequestOrigin,
        ):
            self.model_request_origin = ModelRequestOrigin(str(self.model_request_origin))
        self.worktree_role = optional_worktree_role(self.worktree_role)
        self.publish_policy = optional_publish_policy(self.publish_policy)
        self.budget = dict(self.budget) if isinstance(self.budget, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.task_id,
            "agent_id": self.agent_id,
            "executor": self.executor.value,
            "prompt": self.prompt,
            "execution_location": self.execution_location.value,
            "issue_id": self.issue_id,
            "runtime_profile_id": self.runtime_profile_id,
            "worker_kind": self.worker_kind.value if self.worker_kind else None,
            "model_request_origin": (
                self.model_request_origin.value if self.model_request_origin else None
            ),
            "worktree_role": self.worktree_role.value if self.worktree_role else None,
            "publish_policy": self.publish_policy.value if self.publish_policy else None,
            "workdir": self.workdir,
            "branch": self.branch,
            "model": self.model,
            "executor_session_id": self.executor_session_id,
            "budget": dict(self.budget),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutorRunRequest":
        return cls(
            task_id=str(data["agent_run_id"]),
            agent_id=str(data["agent_id"]),
            executor=str(data["executor"]),
            prompt=str(data.get("prompt", "") or ""),
            execution_location=str(data.get("execution_location", "local_workspace")),
            issue_id=(
                str(data["issue_id"]) if data.get("issue_id") is not None else None
            ),
            runtime_profile_id=(
                str(data["runtime_profile_id"])
                if data.get("runtime_profile_id") is not None
                else None
            ),
            worker_kind=(
                str(data["worker_kind"]) if data.get("worker_kind") is not None else None
            ),
            model_request_origin=(
                str(data["model_request_origin"])
                if data.get("model_request_origin") is not None
                else None
            ),
            worktree_role=(
                str(data["worktree_role"])
                if data.get("worktree_role") is not None
                else None
            ),
            publish_policy=(
                str(data["publish_policy"])
                if data.get("publish_policy") is not None
                else None
            ),
            workdir=str(data["workdir"]) if data.get("workdir") is not None else None,
            branch=str(data["branch"]) if data.get("branch") is not None else None,
            model=str(data["model"]) if data.get("model") is not None else None,
            executor_session_id=(
                str(data["executor_session_id"])
                if data.get("executor_session_id") is not None
                else None
            ),
            budget=dict(data.get("budget", {}))
            if isinstance(data.get("budget"), dict)
            else {},
            metadata=dict(data.get("metadata", {}))
            if isinstance(data.get("metadata"), dict)
            else {},
        )


@dataclass
class ExecutorEvent:
    """Normalized stream event emitted by any executor backend."""

    type: ExecutorEventType | str
    text: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = _coerce_event_type(self.type)

    @classmethod
    def text_event(cls, text: str) -> "ExecutorEvent":
        return cls(type=ExecutorEventType.TEXT, text=text)

    @classmethod
    def status(cls, status: str, **data: Any) -> "ExecutorEvent":
        return cls(type=ExecutorEventType.STATUS, data={"status": status, **data})

    @classmethod
    def error(cls, message: str, **data: Any) -> "ExecutorEvent":
        return cls(type=ExecutorEventType.ERROR, text=message, data=data)

    @classmethod
    def usage(cls, **data: Any) -> "ExecutorEvent":
        return cls(type=ExecutorEventType.USAGE, data=data)

    @classmethod
    def log(cls, message: str, *, level: str = "info", **data: Any) -> "ExecutorEvent":
        return cls(type=ExecutorEventType.LOG, text=message, data={"level": level, **data})

    @classmethod
    def session_run_start(cls, prompt: str, **data: Any) -> "ExecutorEvent":
        return cls(
            type=ExecutorEventType.SESSION_RUN_START,
            data={"prompt": prompt, **data},
        )

    @classmethod
    def session_run_end(
        cls,
        response: str,
        *,
        response_rendered: bool = True,
        **data: Any,
    ) -> "ExecutorEvent":
        return cls(
            type=ExecutorEventType.SESSION_RUN_END,
            data={
                "response": response,
                "response_rendered": response_rendered,
                **data,
            },
        )

    @classmethod
    def tool_use(
        cls,
        *,
        tool_name: str,
        tool_call_id: str | None = None,
        tool_args: dict[str, Any] | None = None,
        **data: Any,
    ) -> "ExecutorEvent":
        return cls(
            type=ExecutorEventType.TOOL_USE,
            data={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id or "",
                "input": dict(tool_args or {}),
                **data,
            },
        )

    @classmethod
    def tool_result(
        cls,
        *,
        tool_name: str,
        output: str,
        tool_call_id: str | None = None,
        **data: Any,
    ) -> "ExecutorEvent":
        return cls(
            type=ExecutorEventType.TOOL_RESULT,
            data={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id or "",
                "output": output,
                **data,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "text": self.text,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutorEvent":
        return cls(
            type=str(data.get("type", "status")),
            text=str(data["text"]) if data.get("text") is not None else None,
            data=dict(data.get("data", {}))
            if isinstance(data.get("data"), dict)
            else {},
        )


@dataclass
class ExecutorRunResult:
    """Final result of one executor run attempt."""

    task_id: str
    status: str
    output: str
    executor_session_id: str | None = None
    events: list[ExecutorEvent] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.task_id,
            "status": self.status,
            "output": self.output,
            "executor_session_id": self.executor_session_id,
            "events": [event.to_dict() for event in self.events],
            "usage": dict(self.usage),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutorRunResult":
        raw_events = data.get("events", [])
        return cls(
            task_id=str(data["agent_run_id"]),
            status=str(data.get("status", "")),
            output=str(data.get("output", "") or ""),
            executor_session_id=(
                str(data["executor_session_id"])
                if data.get("executor_session_id") is not None
                else None
            ),
            events=[
                ExecutorEvent.from_dict(event)
                for event in raw_events
                if isinstance(event, dict)
            ],
            usage=dict(data.get("usage", {}))
            if isinstance(data.get("usage"), dict)
            else {},
            artifacts=[
                dict(artifact)
                for artifact in data.get("artifacts", [])
                if isinstance(artifact, dict)
            ],
            error=str(data["error"]) if data.get("error") is not None else None,
        )


class AgentExecutorBackend(Protocol):
    """Protocol all Agent executor backends must implement."""

    executor: ExecutorType

    def start(self, request: ExecutorRunRequest) -> ExecutorRunResult:
        """Start a fresh task execution."""

    def resume(self, session: TaskSessionRef, prompt: str) -> ExecutorRunResult:
        """Resume a task in the same executor family."""

    def cancel(self, task_id: str, reason: str = "user_cancelled") -> bool:
        """Request cancellation for a running task."""


class ExecutorBackendRegistry:
    """In-memory registry for executor backends keyed by executor type."""

    def __init__(self) -> None:
        self._backends: dict[ExecutorType, AgentExecutorBackend] = {}

    def register(self, backend: AgentExecutorBackend) -> None:
        self._backends[_coerce_executor(backend.executor)] = backend

    def get(self, executor: ExecutorType | str) -> AgentExecutorBackend:
        executor_type = _coerce_executor(executor)
        backend = self._backends.get(executor_type)
        if backend is None:
            raise KeyError(f"executor backend not registered: {executor_type.value}")
        return backend

    def start(self, request: ExecutorRunRequest) -> ExecutorRunResult:
        return self.get(request.executor).start(request)

    def resume(self, session: TaskSessionRef, *, prompt: str) -> ExecutorRunResult:
        return self.get(session.executor).resume(session, prompt)

    def cancel(
        self,
        executor: ExecutorType | str,
        task_id: str,
        *,
        reason: str = "user_cancelled",
    ) -> bool:
        return self.get(executor).cancel(task_id, reason)


class ReuleauxCoderExecutorBackend:
    """Adapter that exposes the existing in-process ReuleauxCoder agent as a backend."""

    executor = ExecutorType.REULEAUXCODER

    def __init__(self, *, create_agent: Callable[[ExecutorRunRequest], Any]) -> None:
        self._create_agent = create_agent
        self._active_agents: dict[str, Any] = {}

    def start(self, request: ExecutorRunRequest) -> ExecutorRunResult:
        agent = self._create_agent(request)
        self._bind_memory_scope(request, agent)
        self._bind_permission_context(request, agent)
        self._active_agents[request.task_id] = agent
        return self._run_agent(request, agent)

    def resume(self, session: TaskSessionRef, prompt: str) -> ExecutorRunResult:
        request = ExecutorRunRequest(
            task_id=session.task_id,
            agent_id=session.agent_id,
            executor=session.executor,
            execution_location=session.execution_location,
            issue_id=session.issue_id,
            workdir=session.workdir,
            branch=session.branch,
            executor_session_id=session.executor_session_id,
            worktree_role=optional_worktree_role(session.metadata.get("worktree_role")),
            publish_policy=optional_publish_policy(session.metadata.get("publish_policy")),
            prompt=prompt,
            budget=dict(session.metadata.get("budget"))
            if isinstance(session.metadata.get("budget"), dict)
            else {},
            metadata=dict(session.metadata),
        )
        agent = self._create_agent(request)
        self._bind_memory_scope(request, agent)
        self._bind_permission_context(request, agent)
        if request.executor_session_id:
            setattr(agent, "current_session_id", request.executor_session_id)
        self._active_agents[request.task_id] = agent
        return self._run_agent(request, agent)

    def cancel(self, task_id: str, reason: str = "user_cancelled") -> bool:
        agent = self._active_agents.get(task_id)
        if agent is None:
            return False
        request_stop = getattr(agent, "request_stop", None)
        if not callable(request_stop):
            return False
        try:
            request_stop(reason)
        except TypeError:
            request_stop()
        return True

    def _run_agent(self, request: ExecutorRunRequest, agent: Any) -> ExecutorRunResult:
        events = [ExecutorEvent.status("running", task_id=request.task_id)]
        runtime_artifacts: list[dict[str, Any]] = []
        lifecycle_handler = self._attach_lifecycle_event_capture(
            agent,
            events,
            runtime_artifacts,
        )
        try:
            output = self._agent_chat(agent, request.prompt)
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            message = str(exc)
            events.append(ExecutorEvent.error(message))
            events.append(ExecutorEvent.status("failed", task_id=request.task_id))
            return ExecutorRunResult(
                task_id=request.task_id,
                status="failed",
                output="",
                executor_session_id=getattr(agent, "current_session_id", None),
                events=events,
                artifacts=runtime_artifacts,
                error=message,
            )
        finally:
            if lifecycle_handler is not None:
                self._detach_agent_event_handler(agent, lifecycle_handler)
            self._active_agents.pop(request.task_id, None)

        result_status, result_error = self._result_status_from_session_run_end(
            events,
            output,
        )
        events.append(ExecutorEvent.text_event(output))
        events.append(ExecutorEvent.status(result_status, task_id=request.task_id))
        return ExecutorRunResult(
            task_id=request.task_id,
            status=result_status,
            output=output,
            executor_session_id=getattr(agent, "current_session_id", None),
            events=events,
            artifacts=runtime_artifacts,
            error=result_error,
        )

    @staticmethod
    def _result_status_from_session_run_end(
        events: list[ExecutorEvent],
        output: str,
    ) -> tuple[str, str | None]:
        for event in reversed(events):
            if event.type != ExecutorEventType.SESSION_RUN_END:
                continue
            status = str(event.data.get("status") or "").strip()
            if not status or status == "done":
                return "completed", None
            message = str(event.data.get("error") or output or status)
            if status in {"blocked", "denied", "budget_exceeded"}:
                return "blocked", message
            if status == "cancelled":
                return "cancelled", message
            return "failed", message
        return "completed", None

    @staticmethod
    def _attach_lifecycle_event_capture(
        agent: Any,
        events: list[ExecutorEvent],
        runtime_artifacts: list[dict[str, Any]],
    ) -> Callable[[Any], None] | None:
        add_event_handler = getattr(agent, "add_event_handler", None)
        if not callable(add_event_handler):
            return None

        def _on_agent_event(event: Any) -> None:
            event_type = getattr(event, "event_type", None)
            event_type_value = str(getattr(event_type, "value", event_type) or "")
            data = getattr(event, "data", {})
            payload = dict(data) if isinstance(data, dict) else {}
            if event_type_value == AgentEventType.LIFECYCLE_HOOK.value:
                for artifact in getattr(event, "runtime_artifacts", []) or []:
                    if isinstance(artifact, dict):
                        runtime_artifacts.append(dict(artifact))
                events.append(
                    ExecutorEvent(
                        type=ExecutorEventType.LIFECYCLE_HOOK,
                        data=payload,
                    )
                )
                return
            if event_type_value == AgentEventType.SESSION_RUN_START.value:
                prompt = str(payload.get("user_input") or payload.get("prompt") or "")
                events.append(ExecutorEvent.session_run_start(prompt))
                return
            if event_type_value == AgentEventType.SESSION_RUN_END.value:
                response = str(payload.get("response") or "")
                response_rendered = payload.get("response_rendered", True)
                events.append(
                    ExecutorEvent.session_run_end(
                        response,
                        response_rendered=bool(response_rendered),
                        **{
                            key: value
                            for key, value in payload.items()
                            if key
                            not in {
                                "response",
                                "response_rendered",
                                "render_response",
                            }
                        },
                    )
                )
                return
            if event_type_value == AgentEventType.TOOL_CALL_START.value:
                events.append(
                    ExecutorEvent.tool_use(
                        tool_name=str(getattr(event, "tool_name", "") or ""),
                        tool_call_id=getattr(event, "tool_call_id", None),
                        tool_args=getattr(event, "tool_args", None),
                        **{
                            key: value
                            for key, value in payload.items()
                            if key not in {"tool_name", "tool_call_id", "tool_args"}
                        },
                    )
                )
                return
            if event_type_value == AgentEventType.TOOL_CALL_END.value:
                events.append(
                    ExecutorEvent.tool_result(
                        tool_name=str(getattr(event, "tool_name", "") or ""),
                        tool_call_id=getattr(event, "tool_call_id", None),
                        output=str(getattr(event, "tool_result", "") or ""),
                        **{
                            key: value
                            for key, value in payload.items()
                            if key not in {
                                "tool_name",
                                "tool_call_id",
                                "tool_result",
                                "output",
                            }
                        },
                    )
                )
                return
            if event_type_value == AgentEventType.USAGE_UPDATE.value:
                events.append(ExecutorEvent.usage(**payload))

        add_event_handler(_on_agent_event)
        return _on_agent_event

    @staticmethod
    def _detach_agent_event_handler(agent: Any, handler: Callable[[Any], None]) -> None:
        handlers = getattr(agent, "_event_handlers", None)
        if not isinstance(handlers, list):
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return

    @staticmethod
    def _bind_memory_scope(request: ExecutorRunRequest, agent: Any) -> None:
        metadata = dict(request.metadata or {})
        bind_memory_scope_to_agent(
            agent,
            owner_agent_id=request.agent_id,
            memory_namespace=request.agent_id,
            project_id=metadata.get("project_id"),
            workspace_id=metadata.get("workspace_id") or request.workdir,
            repo_id=metadata.get("repo_id"),
            goal_id=metadata.get("goal_id"),
            task_id=request.task_id,
            taskflow_id=metadata.get("taskflow_id"),
            issue_id=request.issue_id,
        )

    @staticmethod
    def _bind_permission_context(request: ExecutorRunRequest, agent: Any) -> None:
        metadata = dict(request.metadata or {})
        context = metadata.get("permission_context")
        permission_context = context if isinstance(context, dict) else {}
        agent_id = str(permission_context.get("agent_id") or request.agent_id or "")
        source = str(permission_context.get("source") or "taskflow")
        runtime_profile_id = str(
            permission_context.get("runtime_profile_id")
            or request.runtime_profile_id
            or ""
        )
        effective = permission_context.get("effective_capabilities")
        if not isinstance(effective, dict):
            effective = metadata.get("effective_capabilities")
        resolved = permission_context.get("resolved_capabilities")
        if not isinstance(resolved, dict):
            resolved = metadata.get("resolved_capabilities")
        budget = normalize_runtime_budget(request.budget)

        if agent_id:
            setattr(agent, "agent_config_id", agent_id)
        setattr(agent, "runtime_agent_run_id", request.task_id)
        setattr(agent, "runtime_task_id", request.task_id)
        setattr(agent, "runtime_budget", budget)
        setattr(agent, "runtime_tool_call_count", 0)
        if "max_turns" in budget:
            setattr(agent, "max_rounds", budget["max_turns"])
        if "timeout_sec" in budget:
            setattr(agent, "runtime_timeout_sec", budget["timeout_sec"])
            setattr(agent, "runtime_deadline", time.monotonic() + budget["timeout_sec"])
        if "token_budget" in budget:
            setattr(agent, "runtime_token_budget", budget["token_budget"])
        if budget:
            enforcement = {
                "max_tool_calls": "tool_executor",
                "max_turns": "agent_loop",
                "timeout_sec": "agent_loop_and_tool_executor",
                "token_budget": "agent_loop_and_tool_executor",
            }
            setattr(
                agent,
                "runtime_budget_enforcement",
                {
                    field_name: enforcement[field_name]
                    for field_name in budget
                    if field_name in enforcement
                },
            )
        if request.workdir:
            setattr(agent, "runtime_workspace_root", request.workdir)
            setattr(agent, "runtime_working_directory", request.workdir)
        setattr(agent, "permission_trigger_source", source)
        setattr(agent, "permission_interactive", permission_context.get("interactive") is True)
        if runtime_profile_id:
            setattr(agent, "runtime_profile_id", runtime_profile_id)
        if isinstance(effective, dict):
            setattr(agent, "effective_capabilities", effective)
            setattr(agent, "enforce_effective_capabilities", True)
        if isinstance(resolved, dict):
            setattr(agent, "resolved_capabilities", resolved)

    @staticmethod
    def _agent_chat(agent: Any, prompt: str) -> str:
        try:
            return str(agent.chat(prompt, clear_stop_request=True))
        except TypeError:
            return str(agent.chat(prompt))


__all__ = [
    "AgentExecutorBackend",
    "ExecutionLocation",
    "ExecutorBackendRegistry",
    "ExecutorEvent",
    "ExecutorEventType",
    "ExecutorRunRequest",
    "ExecutorRunResult",
    "ExecutorType",
    "ReuleauxCoderExecutorBackend",
    "TaskSessionRef",
]
