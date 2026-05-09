"""Domain records for GitHub pull request lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from labrastro_server.taskflow.domain.time import utc_now


@dataclass
class GitHubPullRequestRecord:
    id: str
    task_id: str
    artifact_id: str | None
    repository: str
    owner: str
    repo: str
    number: int
    node_id: str = ""
    url: str = ""
    api_url: str = ""
    base_ref: str = ""
    head_ref: str = ""
    head_sha: str = ""
    status: str = "open"
    review_state: str = "none"
    merge_status: str = "pending_user"
    draft: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    last_synced_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GitHubPullRequestRecord":
        return cls(
            id=str(data.get("id") or ""),
            task_id=str(data.get("task_id") or ""),
            artifact_id=(
                str(data["artifact_id"]) if data.get("artifact_id") is not None else None
            ),
            repository=str(data.get("repository") or ""),
            owner=str(data.get("owner") or ""),
            repo=str(data.get("repo") or ""),
            number=int(data.get("number") or 0),
            node_id=str(data.get("node_id") or ""),
            url=str(data.get("url") or ""),
            api_url=str(data.get("api_url") or ""),
            base_ref=str(data.get("base_ref") or ""),
            head_ref=str(data.get("head_ref") or ""),
            head_sha=str(data.get("head_sha") or ""),
            status=str(data.get("status") or "open"),
            review_state=str(data.get("review_state") or "none"),
            merge_status=str(data.get("merge_status") or "pending_user"),
            draft=bool(data.get("draft", False)),
            metadata=dict(data.get("metadata") or {}),
            last_synced_at=(
                str(data["last_synced_at"])
                if data.get("last_synced_at") is not None
                else None
            ),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "artifact_id": self.artifact_id,
            "repository": self.repository,
            "owner": self.owner,
            "repo": self.repo,
            "number": self.number,
            "node_id": self.node_id,
            "url": self.url,
            "api_url": self.api_url,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "status": self.status,
            "review_state": self.review_state,
            "merge_status": self.merge_status,
            "draft": self.draft,
            "metadata": dict(self.metadata),
            "last_synced_at": self.last_synced_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class GitHubReviewCommentRecord:
    id: str
    github_id: str
    pr_record_id: str
    task_id: str
    repository: str
    pr_number: int
    author: str = ""
    body: str = ""
    path: str | None = None
    line: int | None = None
    side: str | None = None
    url: str = ""
    state: str = "open"
    work_item_id: str | None = None
    assignment_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GitHubReviewCommentRecord":
        line = data.get("line")
        return cls(
            id=str(data.get("id") or ""),
            github_id=str(data.get("github_id") or ""),
            pr_record_id=str(data.get("pr_record_id") or ""),
            task_id=str(data.get("task_id") or ""),
            repository=str(data.get("repository") or ""),
            pr_number=int(data.get("pr_number") or 0),
            author=str(data.get("author") or ""),
            body=str(data.get("body") or ""),
            path=str(data["path"]) if data.get("path") is not None else None,
            line=int(line) if line is not None else None,
            side=str(data["side"]) if data.get("side") is not None else None,
            url=str(data.get("url") or ""),
            state=str(data.get("state") or "open"),
            work_item_id=(
                str(data["work_item_id"])
                if data.get("work_item_id") is not None
                else None
            ),
            assignment_id=(
                str(data["assignment_id"])
                if data.get("assignment_id") is not None
                else None
            ),
            metadata=dict(data.get("metadata") or {}),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "github_id": self.github_id,
            "pr_record_id": self.pr_record_id,
            "task_id": self.task_id,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "author": self.author,
            "body": self.body,
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "url": self.url,
            "state": self.state,
            "work_item_id": self.work_item_id,
            "assignment_id": self.assignment_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
