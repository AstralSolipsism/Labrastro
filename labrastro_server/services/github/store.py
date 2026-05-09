"""Storage protocol for GitHub PR lifecycle records."""

from __future__ import annotations

from typing import Any, Protocol

from labrastro_server.services.github.models import (
    GitHubPullRequestRecord,
    GitHubReviewCommentRecord,
)


class GitHubStore(Protocol):
    def upsert_pull_request(
        self, record: GitHubPullRequestRecord
    ) -> GitHubPullRequestRecord: ...

    def get_pull_request(
        self, repository: str, number: int
    ) -> GitHubPullRequestRecord | None: ...

    def get_pull_request_for_task(
        self, task_id: str
    ) -> GitHubPullRequestRecord | None: ...

    def list_open_pull_requests(self) -> list[GitHubPullRequestRecord]: ...

    def update_pull_request_state(
        self,
        repository: str,
        number: int,
        *,
        status: str | None = None,
        review_state: str | None = None,
        merge_status: str | None = None,
        draft: bool | None = None,
        head_sha: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GitHubPullRequestRecord | None: ...

    def record_webhook_delivery(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str,
        payload: dict[str, Any],
    ) -> bool: ...

    def mark_webhook_delivery(
        self, delivery_id: str, *, status: str, error: str = ""
    ) -> None: ...

    def upsert_review_comment(
        self, record: GitHubReviewCommentRecord
    ) -> GitHubReviewCommentRecord: ...

    def list_review_comments(self, task_id: str) -> list[GitHubReviewCommentRecord]: ...

    def set_review_comment_followup(
        self,
        comment_id: str,
        *,
        work_item_id: str | None,
        assignment_id: str | None,
    ) -> None: ...
