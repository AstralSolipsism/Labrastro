"""ReuleauxCoder executor adapter for Taskflow TaskRuns."""

from __future__ import annotations

from typing import Any

from labrastro_server.services.agent_runtime.control_plane import RuntimeTaskRequest
from labrastro_server.taskflow.domain.project_state import TaskRun
from labrastro_server.taskflow.ports.dispatch import TaskflowDispatchResult


class ReuleauxCoderTaskflowDispatcher:
    """Dispatch neutral TaskRun records through the built-in runtime."""

    def __init__(self, runtime_control_plane: Any) -> None:
        self.runtime_control_plane = runtime_control_plane

    def dispatch_task_run(
        self,
        task_run: TaskRun,
        *,
        executor_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskflowDispatchResult:
        """Select a ReuleauxCoder agent and submit a runtime task."""

        selected_executor_id = self._select_executor(
            executor_hint=executor_hint,
            required_capabilities=self._string_list(
                task_run.metadata.get("required_capabilities")
            ),
        )
        if not selected_executor_id:
            return TaskflowDispatchResult(
                selected_executor_id=None,
                reason="no_reuleauxcoder_executor_available",
            )

        runtime_task = self.runtime_control_plane.submit_task(
            self._runtime_request(
                task_run,
                selected_executor_id,
                metadata=dict(metadata or {}),
            )
        )
        return TaskflowDispatchResult(
            selected_executor_id=selected_executor_id,
            candidates=[{"executor_id": selected_executor_id}],
            reason="reuleauxcoder_runtime_submitted",
            runtime_task_id=runtime_task.id,
            runtime_task=self.runtime_control_plane.task_to_dict(runtime_task.id),
        )

    def load_runtime_task(self, runtime_task_id: str) -> dict[str, Any] | None:
        """Return a runtime task projection for Taskflow detail views."""

        try:
            return self.runtime_control_plane.task_to_dict(runtime_task_id)
        except Exception:
            return None

    def _select_executor(
        self,
        *,
        executor_hint: str | None,
        required_capabilities: list[str],
    ) -> str | None:
        snapshot = getattr(self.runtime_control_plane, "runtime_snapshot", {}) or {}
        agents = dict(snapshot.get("agents") or {})
        if executor_hint:
            return executor_hint if executor_hint in agents else None
        for agent_id in sorted(agents):
            agent = agents[agent_id] or {}
            capabilities = set(self._string_list(agent.get("capabilities")))
            if set(required_capabilities) <= capabilities:
                return str(agent_id)
        return None

    def _runtime_request(
        self,
        task_run: TaskRun,
        selected_executor_id: str,
        *,
        metadata: dict[str, Any],
    ) -> RuntimeTaskRequest:
        request_metadata = dict(task_run.metadata)
        request_metadata.update(metadata)
        request_metadata.setdefault("dispatch_source", "taskflow")
        request_metadata.setdefault("taskflow_id", request_metadata.get("taskflow_id"))
        request_metadata.setdefault("task_run_id", task_run.id)
        request_metadata.setdefault("work_item_id", task_run.work_item_id)
        request_metadata.setdefault("goal_id", task_run.goal_id)
        prompt = str(
            request_metadata.get("prompt")
            or request_metadata.get("work_item_description")
            or request_metadata.get("work_item_title")
            or task_run.work_item_id
        )
        return RuntimeTaskRequest(
            issue_id=task_run.work_item_id,
            agent_id=selected_executor_id,
            prompt=prompt,
            runtime_profile_id=self._runtime_profile_id(selected_executor_id),
            metadata=request_metadata,
        )

    def _runtime_profile_id(self, executor_id: str) -> str | None:
        snapshot = getattr(self.runtime_control_plane, "runtime_snapshot", {}) or {}
        agent = dict((snapshot.get("agents") or {}).get(executor_id) or {})
        runtime_profile = str(agent.get("runtime_profile") or "")
        return runtime_profile or None

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if value is None or value == "":
            return []
        return [str(value)]


__all__ = ["ReuleauxCoderTaskflowDispatcher"]
