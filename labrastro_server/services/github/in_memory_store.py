"""In-memory GitHub lifecycle store for tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.services.github.models import (
    GitHubPullRequestRecord,
    GitHubReviewCommentRecord,
)


class InMemoryGitHubStore:
    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], GitHubPullRequestRecord] = {}
        self._deliveries: dict[str, dict[str, Any]] = {}
        self._comments: dict[str, GitHubReviewCommentRecord] = {}

    def upsert_pull_request(
        self, record: GitHubPullRequestRecord
    ) -> GitHubPullRequestRecord:
        record.updated_at = utc_now()
        self._prs[(record.repository, record.number)] = deepcopy(record)
        return deepcopy(record)

    def get_pull_request(
        self, repository: str, number: int
    ) -> GitHubPullRequestRecord | None:
        record = self._prs.get((repository, int(number)))
        return deepcopy(record) if record is not None else None

    def get_pull_request_for_task(
        self, task_id: str
    ) -> GitHubPullRequestRecord | None:
        for record in self._prs.values():
            if record.task_id == task_id:
                return deepcopy(record)
        return None

    def list_open_pull_requests(self) -> list[GitHubPullRequestRecord]:
        return [
            deepcopy(record)
            for record in self._prs.values()
            if record.status == "open"
        ]

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
    ) -> GitHubPullRequestRecord | None:
        record = self._prs.get((repository, int(number)))
        if record is None:
            return None
        if status is not None:
            record.status = status
        if review_state is not None:
            record.review_state = review_state
        if merge_status is not None:
            record.merge_status = merge_status
        if draft is not None:
            record.draft = draft
        if head_sha is not None:
            record.head_sha = head_sha
        if metadata:
            record.metadata.update(metadata)
        record.last_synced_at = utc_now()
        record.updated_at = utc_now()
        return deepcopy(record)

    def record_webhook_delivery(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str,
        payload: dict[str, Any],
    ) -> bool:
        if delivery_id in self._deliveries:
            return False
        self._deliveries[delivery_id] = {
            "event": event,
            "action": action,
            "payload": deepcopy(payload),
            "status": "processing",
            "error": "",
        }
        return True

    def mark_webhook_delivery(
        self, delivery_id: str, *, status: str, error: str = ""
    ) -> None:
        if delivery_id in self._deliveries:
            self._deliveries[delivery_id]["status"] = status
            self._deliveries[delivery_id]["error"] = error

    def upsert_review_comment(
        self, record: GitHubReviewCommentRecord
    ) -> GitHubReviewCommentRecord:
        existing = self._comments.get(record.github_id)
        if existing is not None:
            record.work_item_id = record.work_item_id or existing.work_item_id
            record.assignment_id = record.assignment_id or existing.assignment_id
        record.updated_at = utc_now()
        self._comments[record.github_id] = deepcopy(record)
        return deepcopy(record)

    def list_review_comments(self, task_id: str) -> list[GitHubReviewCommentRecord]:
        return [
            deepcopy(comment)
            for comment in self._comments.values()
            if comment.task_id == task_id
        ]

    def set_review_comment_followup(
        self,
        comment_id: str,
        *,
        work_item_id: str | None,
        assignment_id: str | None,
    ) -> None:
        comment = self._comments.get(comment_id)
        if comment is None:
            return
        comment.work_item_id = work_item_id
        comment.assignment_id = assignment_id
        comment.updated_at = utc_now()
