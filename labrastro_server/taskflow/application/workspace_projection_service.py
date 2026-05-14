"""Taskflow Workspace V1 projection."""

from __future__ import annotations

from typing import Any

from labrastro_server.taskflow.application.project_memory_service import (
    ProjectMemoryService,
)
from labrastro_server.taskflow.application.projector_preview_service import (
    ProjectorPreviewService,
)
from labrastro_server.taskflow.domain.project_state import ProjectState
from labrastro_server.taskflow.domain.taskflow_state import TaskflowState
from labrastro_server.taskflow.interaction.review_cards_v1 import ReviewCardV1


class WorkspaceProjectionService:
    """Aggregate the V1 control-plane workspace DTO."""

    def __init__(
        self,
        *,
        project_memory_service: ProjectMemoryService | None = None,
        projector_preview_service: ProjectorPreviewService | None = None,
    ) -> None:
        self.project_memory_service = project_memory_service or ProjectMemoryService()
        self.projector_preview_service = (
            projector_preview_service or ProjectorPreviewService()
        )

    def project(
        self,
        *,
        taskflow: TaskflowState,
        project: ProjectState,
        review_cards: list[ReviewCardV1],
        runtime_projection: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "taskflow.workspace.v1",
            "taskflow_id": taskflow.meta.taskflow_id,
            "project_id": project.project_id,
            "goal_id": taskflow.meta.goal_id,
            "taskflow": taskflow.to_dict(),
            "review_cards": [card.to_dict() for card in review_cards],
            "project_memory": self.project_memory_service.view(project),
            "compiler_review": {
                "stale": bool(taskflow.compiler.traceability_index.get("compiler_review_stale")),
                "decisions": [
                    dict(item)
                    for item in getattr(taskflow.outputs, "compiler_decisions", [])
                    if isinstance(item, dict)
                ],
            },
            "dispatch_runtime": runtime_projection,
            "trace": self._trace(taskflow, project),
            "projector_previews": [
                self.projector_preview_service.preview(
                    taskflow=taskflow,
                    project=project,
                    target=target,
                )
                for target in ("openspec", "speckit")
            ],
        }

    def _trace(
        self, taskflow: TaskflowState, project: ProjectState
    ) -> dict[str, Any]:
        links = [
            *[item.to_dict() for item in taskflow.relations.goal_work_links],
            *[item.to_dict() for item in taskflow.relations.decision_work_links],
            *[item.to_dict() for item in taskflow.relations.acceptance_trace_links],
            *[item.to_dict() for item in project.traceability.decision_links],
            *[item.to_dict() for item in project.traceability.artifact_links],
            *[item.to_dict() for item in project.traceability.task_run_links],
        ]
        return {
            "source_refs": {
                "brief_version": taskflow.outputs.confirmed_brief_version,
                "review_card_answers": [
                    item.to_dict() for item in taskflow.outputs.review_card_answers
                ],
                "task_run_refs": list(taskflow.outputs.task_run_refs),
            },
            "links": links,
        }


__all__ = ["WorkspaceProjectionService"]
