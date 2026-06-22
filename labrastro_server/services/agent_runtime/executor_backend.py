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
    SESSION_RUN_EVENT = "session_run_event"
    AGENT_RELATION_COMPLETED = "agent_relation_completed"


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
    def session_run_event(
        cls,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> "ExecutorEvent":
        return cls(
            type=ExecutorEventType.SESSION_RUN_EVENT,
            data={
                "event_type": str(event_type or ""),
                "payload": dict(payload or {}),
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
    """Executor adapter for the ReuleauxCoder Agent kernel: LLM, tools, hooks and session."""

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
        capture_state: dict[str, Any] = {
            "assistant_chunks": [],
            "reasoning_chunks": [],
            "provider_output_count": 0,
            "provider_reasoning_count": 0,
            "provider_tool_delta_count": 0,
            "last_body_chunk_at": 0.0,
            "last_reasoning_chunk_at": 0.0,
            "last_tool_delta_at": 0.0,
            "patch_syntax_error_codes": {},
            "patch_semantic_error_codes": {},
            "explicit_rendered_output": False,
        }
        lifecycle_handler = self._attach_lifecycle_event_capture(
            agent,
            events,
            runtime_artifacts,
            capture_state,
        )
        try:
            output = self._agent_chat(agent, request.prompt)
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            message = str(exc)
            events.append(ExecutorEvent.error(message, **self._exception_payload(exc)))
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
        self._append_captured_session_run_summaries(events, capture_state)
        if output and not bool(capture_state.get("explicit_rendered_output")):
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
    def _exception_payload(exc: Exception) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_type": type(exc).__name__}
        diagnostic_path = str(getattr(exc, "llm_diagnostic_path", "") or "").strip()
        if diagnostic_path:
            payload["diagnostic_path"] = diagnostic_path
        provider_phase = str(getattr(exc, "provider_error_phase", "") or "").strip()
        if provider_phase:
            payload["provider_error_phase"] = provider_phase
        provider_id = str(getattr(exc, "provider_id", "") or "").strip()
        if provider_id:
            payload["provider_id"] = provider_id
        provider_type = str(getattr(exc, "provider_type", "") or "").strip()
        if provider_type:
            payload["provider_type"] = provider_type
        code = str(getattr(exc, "code", "") or "").strip()
        if code:
            payload["code"] = code
        failure_kind = str(getattr(exc, "failure_kind", "") or "").strip()
        if failure_kind:
            payload["failure_kind"] = failure_kind
        tool_name = str(getattr(exc, "tool_name", "") or "").strip()
        if tool_name:
            payload["tool_name"] = tool_name
        tool_call_id = str(getattr(exc, "tool_call_id", "") or "").strip()
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
        recoverable = getattr(exc, "recoverable", None)
        if recoverable is not None:
            payload["recoverable"] = bool(recoverable)
        tool_diagnostics = getattr(exc, "tool_diagnostics", None)
        if isinstance(tool_diagnostics, list):
            payload["tool_diagnostics"] = [
                dict(item) for item in tool_diagnostics if isinstance(item, dict)
            ]
        upstream_status = getattr(exc, "status_code", None)
        if upstream_status is None:
            upstream_status = getattr(exc, "status", None)
        if upstream_status is None:
            upstream_status = getattr(getattr(exc, "response", None), "status_code", None)
        try:
            if upstream_status is not None:
                payload["upstream_status"] = int(upstream_status)
        except (TypeError, ValueError):
            pass
        return payload

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
    def _append_captured_session_run_summaries(
        events: list[ExecutorEvent],
        capture_state: dict[str, Any],
    ) -> None:
        reasoning = "".join(
            str(chunk)
            for chunk in capture_state.get("reasoning_chunks", [])
            if chunk is not None
        )
        assistant = "".join(
            str(chunk)
            for chunk in capture_state.get("assistant_chunks", [])
            if chunk is not None
        )
        if reasoning:
            events.append(
                ExecutorEvent.session_run_event(
                    "reasoning_message",
                    {"content": reasoning},
                )
            )
        if assistant:
            events.append(
                ExecutorEvent.session_run_event(
                    "assistant_message",
                    {"content": assistant},
                )
            )
        if (
            int(capture_state.get("provider_output_count") or 0)
            or int(capture_state.get("provider_reasoning_count") or 0)
            or int(capture_state.get("provider_tool_delta_count") or 0)
            or capture_state.get("patch_syntax_error_codes")
            or capture_state.get("patch_semantic_error_codes")
        ):
            events.append(
                ExecutorEvent.session_run_event(
                    "stream_observability",
                    {
                        "schema": "stream_observability.v1",
                        "provider_output_count": int(
                            capture_state.get("provider_output_count") or 0
                        ),
                        "provider_reasoning_count": int(
                            capture_state.get("provider_reasoning_count") or 0
                        ),
                        "provider_tool_delta_count": int(
                            capture_state.get("provider_tool_delta_count") or 0
                        ),
                        "last_body_chunk_at": float(
                            capture_state.get("last_body_chunk_at") or 0.0
                        ),
                        "last_reasoning_chunk_at": float(
                            capture_state.get("last_reasoning_chunk_at") or 0.0
                        ),
                        "last_tool_delta_at": float(
                            capture_state.get("last_tool_delta_at") or 0.0
                        ),
                        "patch_syntax_error_count": sum(
                            int(count)
                            for count in dict(
                                capture_state.get("patch_syntax_error_codes") or {}
                            ).values()
                        ),
                        "patch_syntax_error_codes": dict(
                            capture_state.get("patch_syntax_error_codes") or {}
                        ),
                        "patch_semantic_error_count": sum(
                            int(count)
                            for count in dict(
                                capture_state.get("patch_semantic_error_codes") or {}
                            ).values()
                        ),
                        "patch_semantic_error_codes": dict(
                            capture_state.get("patch_semantic_error_codes") or {}
                        ),
                        "server_enqueue_latency_ms": 0,
                    },
                )
            )

    @staticmethod
    def _attach_lifecycle_event_capture(
        agent: Any,
        events: list[ExecutorEvent],
        runtime_artifacts: list[dict[str, Any]],
        capture_state: dict[str, Any],
    ) -> Callable[[Any], None] | None:
        add_event_handler = getattr(agent, "add_event_handler", None)
        if not callable(add_event_handler):
            return None

        def _on_agent_event(event: Any) -> None:
            event_type = getattr(event, "event_type", None)
            event_type_value = str(getattr(event_type, "value", event_type) or "")
            data = getattr(event, "data", {})
            payload = dict(data) if isinstance(data, dict) else {}
            if event_type_value == AgentEventType.SESSION_RUN_EVENT.value:
                session_event_type = str(payload.get("event_type") or "").strip()
                session_payload = (
                    dict(payload.get("payload"))
                    if isinstance(payload.get("payload"), dict)
                    else {}
                )
                if session_event_type:
                    events.append(
                        ExecutorEvent.session_run_event(
                            session_event_type,
                            session_payload,
                        )
                    )
                    if session_event_type in {
                        "assistant_delta",
                        "assistant_message",
                        "document_draft_started",
                        "document_draft_preview_chunk",
                        "document_draft_progress",
                        "document_draft_snapshot",
                        "document_draft_commit_requested",
                        "document_draft_committed",
                    }:
                        capture_state["explicit_rendered_output"] = True
                return
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
            if event_type_value == AgentEventType.STREAM_TOKEN.value:
                token = str(payload.get("token") or "")
                if token:
                    capture_state["assistant_chunks"].append(token)
                    capture_state["provider_output_count"] = int(
                        capture_state.get("provider_output_count") or 0
                    ) + 1
                    capture_state["last_body_chunk_at"] = float(
                        getattr(event, "timestamp", None) or time.time()
                    )
                    capture_state["explicit_rendered_output"] = True
                    events.append(
                        ExecutorEvent.session_run_event(
                            "assistant_delta",
                            {"content": token},
                        )
                    )
                return
            if event_type_value == AgentEventType.REASONING_TOKEN.value:
                token = str(payload.get("token") or "")
                if token:
                    capture_state["reasoning_chunks"].append(token)
                    capture_state["provider_reasoning_count"] = int(
                        capture_state.get("provider_reasoning_count") or 0
                    ) + 1
                    capture_state["last_reasoning_chunk_at"] = float(
                        getattr(event, "timestamp", None) or time.time()
                    )
                    events.append(
                        ExecutorEvent.session_run_event(
                            "reasoning_delta",
                            {"content": token},
                        )
                    )
                return
            if event_type_value == AgentEventType.TOOL_CALL_DELTA.value:
                capture_state["provider_tool_delta_count"] = int(
                    capture_state.get("provider_tool_delta_count") or 0
                ) + 1
                capture_state["last_tool_delta_at"] = float(
                    getattr(event, "timestamp", None) or time.time()
                )
                events.append(
                    ExecutorEvent.session_run_event("tool_call_delta", payload)
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
                return
            if event_type_value == AgentEventType.AGENT_RELATION_COMPLETED.value:
                events.append(
                    ExecutorEvent(
                        type=ExecutorEventType.AGENT_RELATION_COMPLETED,
                        data=payload,
                    )
                )
                return
            if event_type_value == AgentEventType.ERROR.value:
                message = str(getattr(event, "error_message", None) or payload.get("message") or "")
                events.append(
                    ExecutorEvent.session_run_event(
                        "error",
                        {
                            **payload,
                            "message": message or "agent_error",
                        },
                    )
                )
                return
            if event_type_value in {
                AgentEventType.TOOL_ARGUMENTS_COMPLETE.value,
                AgentEventType.TOOL_ARGUMENTS_VALID.value,
                AgentEventType.TOOL_ARGUMENTS_INVALID.value,
                AgentEventType.MUTATION_PREVIEWING.value,
                AgentEventType.MUTATION_PREVIEW_READY.value,
                AgentEventType.MUTATION_PREVIEW_FAILED.value,
                AgentEventType.TOOL_CALL_PROTOCOL_ERROR.value,
                AgentEventType.FILE_CHANGE_STARTED.value,
                AgentEventType.FILE_CHANGE_PATCH_UPDATED.value,
                AgentEventType.FILE_CHANGE_APPROVAL_REQUESTED.value,
                AgentEventType.FILE_CHANGE_APPROVAL_RESOLVED.value,
                AgentEventType.FILE_CHANGE_COMPLETED.value,
                AgentEventType.TURN_DIFF_UPDATED.value,
                AgentEventType.DOCUMENT_DRAFT_STARTED.value,
                AgentEventType.DOCUMENT_DRAFT_PREVIEW_CHUNK.value,
                AgentEventType.DOCUMENT_DRAFT_PROGRESS.value,
                AgentEventType.DOCUMENT_DRAFT_SNAPSHOT.value,
                AgentEventType.DOCUMENT_DRAFT_COMMIT_REQUESTED.value,
                AgentEventType.DOCUMENT_DRAFT_COMMITTED.value,
                AgentEventType.DOCUMENT_DRAFT_FAILED.value,
                AgentEventType.DOCUMENT_DRAFT_CANCELLED.value,
                AgentEventType.DRAFT_BODY_STALLED.value,
                AgentEventType.DRAFT_INTERRUPTED_RECOVERABLE.value,
                AgentEventType.PROVIDER_STREAM_INTERRUPTED.value,
                AgentEventType.PROVIDER_STREAM_RECOVERING.value,
                AgentEventType.PROVIDER_STREAM_RECOVERED.value,
                AgentEventType.SESSION_RUN_INTERRUPTED.value,
                AgentEventType.RUNTIME_STATUS.value,
            }:
                if event_type_value == AgentEventType.TOOL_ARGUMENTS_INVALID.value:
                    code = str(payload.get("code") or "unknown").strip() or "unknown"
                    codes = dict(capture_state.get("patch_syntax_error_codes") or {})
                    codes[code] = int(codes.get(code) or 0) + 1
                    capture_state["patch_syntax_error_codes"] = codes
                if event_type_value == AgentEventType.MUTATION_PREVIEW_FAILED.value:
                    code = (
                        str(payload.get("failure_code") or payload.get("code") or "unknown").strip()
                        or "unknown"
                    )
                    codes = dict(capture_state.get("patch_semantic_error_codes") or {})
                    codes[code] = int(codes.get(code) or 0) + 1
                    capture_state["patch_semantic_error_codes"] = codes
                if event_type_value.startswith("document_draft_") or event_type_value in {
                    AgentEventType.DRAFT_BODY_STALLED.value,
                    AgentEventType.DRAFT_INTERRUPTED_RECOVERABLE.value,
                }:
                    capture_state["explicit_rendered_output"] = True
                events.append(ExecutorEvent.session_run_event(event_type_value, payload))

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
        branch_binding_id = str(metadata.get("branch_binding_id") or "").strip()
        setattr(agent, "runtime_agent_run_id", request.task_id)
        if branch_binding_id:
            setattr(agent, "runtime_branch_binding_id", branch_binding_id)
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
