"""Task and artifact lifecycle helpers for AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from reuleauxcoder.domain.agent_runtime.models import (
    ArtifactStatus,
    ArtifactType,
    MergeStatus,
    TaskArtifact,
    AgentRunRecord,
    TaskStatus,
    TriggerMode,
)


class IssueStatus(str, Enum):
    """Issue-level status derived from task and artifact state."""

    OPEN = "open"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class TaskLifecycleState:
    """In-memory lifecycle state for one task and its artifacts."""

    task: AgentRunRecord
    artifacts: dict[str, TaskArtifact] = field(default_factory=dict)
    issue_status: IssueStatus = IssueStatus.OPEN

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        issue_id: str,
        agent_id: str,
    ) -> "TaskLifecycleState":
        return cls(
            task=AgentRunRecord(
                id=task_id,
                issue_id=issue_id,
                agent_id=agent_id,
                trigger_mode=TriggerMode.ISSUE_TASK,
                status=TaskStatus.QUEUED,
            )
        )

    def attach_artifact(
        self,
        *,
        artifact_id: str,
        type: str,
        status: str = "generated",
        branch_name: str | None = None,
        pr_url: str | None = None,
        content: str | None = None,
        path: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> TaskArtifact:
        artifact = TaskArtifact(
            id=artifact_id,
            task_id=self.task.id,
            type=ArtifactType(type),
            status=ArtifactStatus(status),
            branch_name=branch_name,
            pr_url=pr_url,
            content=content,
            path=path,
            metadata=dict(metadata or {}),
        )
        self.artifacts[artifact_id] = artifact
        return artifact

    def complete_agent_run(self, *, output: str) -> None:
        self.task.status = TaskStatus.COMPLETED
        self.task.output = output
        self.issue_status = (
            IssueStatus.IN_REVIEW
            if self._has_unmerged_pull_request()
            else IssueStatus.DONE
        )

    def mark_artifact_merged(self, artifact_id: str, *, actor_user_id: str) -> None:
        artifact = self.artifacts[artifact_id]
        artifact.status = ArtifactStatus.MERGED
        artifact.merge_status = MergeStatus.MERGED_BY_USER
        artifact.merged_by = actor_user_id
        if not self._has_unmerged_pull_request():
            self.issue_status = IssueStatus.DONE

    def create_followup_task_from_comment(
        self, *, comment_id: str, agent_id: str
    ) -> AgentRunRecord:
        artifact = self._primary_pull_request_artifact()
        return AgentRunRecord(
            id=f"{self.task.id}:{comment_id}",
            issue_id=self.task.issue_id,
            agent_id=agent_id,
            trigger_mode=TriggerMode.ISSUE_TASK,
            status=TaskStatus.QUEUED,
            parent_task_id=self.task.id,
            trigger_comment_id=comment_id,
            branch_name=artifact.branch_name if artifact else None,
            pr_url=artifact.pr_url if artifact else None,
        )

    def _has_unmerged_pull_request(self) -> bool:
        for artifact in self.artifacts.values():
            if artifact.type != ArtifactType.PULL_REQUEST:
                continue
            if artifact.status not in {ArtifactStatus.MERGED, ArtifactStatus.CLOSED}:
                return True
        return False

    def _primary_pull_request_artifact(self) -> TaskArtifact | None:
        for artifact in self.artifacts.values():
            if artifact.type == ArtifactType.PULL_REQUEST:
                return artifact
        return None
