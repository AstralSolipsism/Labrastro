"""ReuleauxCoder executor adapter for Taskflow TaskRuns."""

from __future__ import annotations

from typing import Any

from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from labrastro_server.services.agent_runtime.scheduler import BasicAgentScheduler
from labrastro_server.taskflow.domain.project_state import TaskRun
from labrastro_server.taskflow.ports.dispatch import TaskflowDispatchResult
from reuleauxcoder.domain.agent_runtime.models import AgentConfig, TaskStatus


class ReuleauxCoderTaskflowDispatcher:
    """Dispatch neutral TaskRun records through built-in AgentRuns."""

    def __init__(self, runtime_control_plane: Any) -> None:
        self.runtime_control_plane = runtime_control_plane

    def dispatch_task_run(
        self,
        task_run: TaskRun,
        *,
        executor_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskflowDispatchResult:
        """Select a ReuleauxCoder agent and submit an AgentRun."""

        selected_executor_id = self._select_executor(
            executor_hint=executor_hint,
        )
        if not selected_executor_id:
            return TaskflowDispatchResult(
                selected_executor_id=None,
                reason="agent_selection_required",
            )

        agent_run = self.runtime_control_plane.submit_agent_run(
            self._runtime_request(
                task_run,
                selected_executor_id,
                metadata=dict(metadata or {}),
            )
        )
        return TaskflowDispatchResult(
            selected_executor_id=selected_executor_id,
            candidates=[{"executor_id": selected_executor_id}],
            reason="agent_run_submitted",
            agent_run_ref=self.runtime_control_plane.agent_run_to_dict(agent_run.id),
        )

    def load_agent_run(self, agent_run_id: str) -> dict[str, Any] | None:
        """Return an AgentRun projection for Taskflow detail views."""

        try:
            return self.runtime_control_plane.agent_run_to_dict(agent_run_id)
        except Exception:
            return None

    def _select_executor(
        self,
        *,
        executor_hint: str | None,
    ) -> str | None:
        snapshot = getattr(self.runtime_control_plane, "runtime_snapshot", {}) or {}
        agents = dict(snapshot.get("agents") or {})
        if executor_hint:
            agent_data = agents.get(executor_hint)
            if not isinstance(agent_data, dict):
                return None
            agent = AgentConfig.from_dict(executor_hint, agent_data)
            return executor_hint if agent.can_run_taskflow else None
        if not agents:
            return None
        parsed_agents = {
            str(agent_id): AgentConfig.from_dict(str(agent_id), dict(agent_data or {}))
            for agent_id, agent_data in agents.items()
            if isinstance(agent_data, dict)
        }
        running_tasks = []
        list_agent_runs = getattr(self.runtime_control_plane, "list_agent_runs", None)
        if callable(list_agent_runs):
            for row in list_agent_runs(limit=500):
                try:
                    running_tasks.append(
                        type(
                            "_Task",
                            (),
                            {
                                "agent_id": str(row.get("agent_id") or ""),
                                "status": TaskStatus(str(row.get("status") or "queued")),
                            },
                        )()
                    )
                except Exception:
                    continue
        return BasicAgentScheduler(
            parsed_agents,
            running_tasks=running_tasks,
        ).choose_agent().agent_id

    def _runtime_request(
        self,
        task_run: TaskRun,
        selected_executor_id: str,
        *,
        metadata: dict[str, Any],
    ) -> AgentRunRequest:
        request_metadata = dict(task_run.metadata)
        request_metadata.update(metadata)
        request_metadata.setdefault("dispatch_source", "taskflow")
        request_metadata.setdefault("agent_run_source", "taskflow")
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
        return AgentRunRequest(
            issue_id=task_run.work_item_id,
            agent_id=selected_executor_id,
            prompt=prompt,
            source="taskflow",
            runtime_profile_id=self._runtime_profile_id(selected_executor_id),
            metadata=request_metadata,
        )

    def _runtime_profile_id(self, executor_id: str) -> str | None:
        snapshot = getattr(self.runtime_control_plane, "runtime_snapshot", {}) or {}
        agent = dict((snapshot.get("agents") or {}).get(executor_id) or {})
        runtime_profile = str(agent.get("runtime_profile") or "")
        return runtime_profile or None

__all__ = ["ReuleauxCoderTaskflowDispatcher"]
