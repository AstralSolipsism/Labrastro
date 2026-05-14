"""Read-only external artifact preview contracts for Taskflow V1."""

from __future__ import annotations

from typing import Any

from labrastro_server.taskflow.domain.project_state import ProjectState
from labrastro_server.taskflow.domain.taskflow_state import TaskflowState


class ProjectorPreviewService:
    """Build metadata-only previews for future external artifact exporters."""

    SUPPORTED_TARGETS = {"openspec", "speckit"}

    def preview(
        self,
        *,
        taskflow: TaskflowState,
        project: ProjectState,
        target: str,
    ) -> dict[str, Any]:
        normalized = str(target or "openspec").lower().strip()
        if normalized not in self.SUPPORTED_TARGETS:
            raise ValueError(f"unsupported projector target: {target}")
        latest_plan = taskflow.outputs.plan_drafts[-1] if taskflow.outputs.plan_drafts else {}
        return {
            "target": normalized,
            "status": "preview_only",
            "read_only": True,
            "truth_source": "taskflow_project_state",
            "taskflow_id": taskflow.meta.taskflow_id,
            "project_id": project.project_id,
            "goal_id": taskflow.meta.goal_id,
            "source_versions": {
                "taskflow_state_version": taskflow.meta.state_version,
                "brief_version": taskflow.outputs.confirmed_brief_version,
                "plan_id": latest_plan.get("id") if isinstance(latest_plan, dict) else None,
            },
            "sections": self._sections(taskflow, project, normalized),
            "limitations": [
                "V1 only returns a read-only preview contract.",
                "Exporter materialization is reserved for post-V1.",
            ],
        }

    def _sections(
        self, taskflow: TaskflowState, project: ProjectState, target: str
    ) -> list[dict[str, Any]]:
        if target == "openspec":
            return [
                {"id": "goal", "title": "Goal", "item_count": 1},
                {"id": "decisions", "title": "Decisions", "item_count": len(taskflow.design.local_decisions)},
                {"id": "acceptance", "title": "Acceptance", "item_count": len(taskflow.clarification.acceptance_examples) + len(taskflow.clarification.examples)},
                {"id": "work_items", "title": "Work Items", "item_count": len(project.list_work_items())},
            ]
        return [
            {"id": "spec", "title": "Spec", "item_count": 1},
            {"id": "plan", "title": "Plan", "item_count": len(taskflow.outputs.plan_drafts)},
            {"id": "tasks", "title": "Tasks", "item_count": len(project.list_work_items())},
            {"id": "trace", "title": "Trace", "item_count": len(project.traceability.decision_links) + len(project.traceability.task_run_links)},
        ]


__all__ = ["ProjectorPreviewService"]
