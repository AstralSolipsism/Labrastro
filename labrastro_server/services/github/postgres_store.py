"""Postgres-backed GitHub PR lifecycle store."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import json

from reuleauxcoder.domain.taskflow.models import utc_now
from labrastro_server.services.github.models import (
    GitHubPullRequestRecord,
    GitHubReviewCommentRecord,
)

try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


def _require_sqlalchemy() -> None:
    if text is None:
        raise RuntimeError("Postgres GitHub store requires sqlalchemy and psycopg.")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


class PostgresGitHubStore:
    def __init__(self, engine: Any) -> None:
        _require_sqlalchemy()
        self.engine = engine

    def upsert_pull_request(
        self, record: GitHubPullRequestRecord
    ) -> GitHubPullRequestRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_github_pull_requests (
                        id, task_id, artifact_id, repository, owner, repo, number,
                        node_id, url, api_url, base_ref, head_ref, head_sha,
                        status, review_state, merge_status, draft, metadata,
                        last_synced_at
                    ) VALUES (
                        :id, :task_id, :artifact_id, :repository, :owner, :repo, :number,
                        :node_id, :url, :api_url, :base_ref, :head_ref, :head_sha,
                        :status, :review_state, :merge_status, :draft,
                        CAST(:metadata AS JSONB), now()
                    )
                    ON CONFLICT (repository, number) DO UPDATE SET
                        task_id=EXCLUDED.task_id,
                        artifact_id=COALESCE(EXCLUDED.artifact_id, labrastro_github_pull_requests.artifact_id),
                        node_id=EXCLUDED.node_id,
                        url=EXCLUDED.url,
                        api_url=EXCLUDED.api_url,
                        base_ref=EXCLUDED.base_ref,
                        head_ref=EXCLUDED.head_ref,
                        head_sha=EXCLUDED.head_sha,
                        status=EXCLUDED.status,
                        review_state=EXCLUDED.review_state,
                        merge_status=EXCLUDED.merge_status,
                        draft=EXCLUDED.draft,
                        metadata=EXCLUDED.metadata,
                        last_synced_at=now(),
                        updated_at=now()
                    """
                ),
                self._pr_params(record),
            )
            self._sync_runtime_artifact(conn, record)
        return self.get_pull_request(record.repository, record.number) or record

    def get_pull_request(
        self, repository: str, number: int
    ) -> GitHubPullRequestRecord | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_github_pull_requests
                    WHERE repository=:repository AND number=:number
                    """
                ),
                {"repository": repository, "number": int(number)},
            ).mappings().first()
        return GitHubPullRequestRecord.from_dict(_row_dict(row)) if row else None

    def get_pull_request_for_task(
        self, task_id: str
    ) -> GitHubPullRequestRecord | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_github_pull_requests
                    WHERE task_id=:task_id
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
        return GitHubPullRequestRecord.from_dict(_row_dict(row)) if row else None

    def list_open_pull_requests(self) -> list[GitHubPullRequestRecord]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_github_pull_requests
                    WHERE status='open'
                    ORDER BY updated_at DESC
                    """
                )
            ).mappings()
            return [GitHubPullRequestRecord.from_dict(_row_dict(row)) for row in rows]

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
        current = self.get_pull_request(repository, number)
        if current is None:
            return None
        if status is not None:
            current.status = status
        if review_state is not None:
            current.review_state = review_state
        if merge_status is not None:
            current.merge_status = merge_status
        if draft is not None:
            current.draft = draft
        if head_sha is not None:
            current.head_sha = head_sha
        if metadata:
            current.metadata.update(metadata)
        current.last_synced_at = utc_now()
        current.updated_at = utc_now()
        return self.upsert_pull_request(current)

    def record_webhook_delivery(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str,
        payload: dict[str, Any],
    ) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO labrastro_github_webhook_deliveries (
                        delivery_id, event, action, payload, status
                    ) VALUES (
                        :delivery_id, :event, :action, CAST(:payload AS JSONB), 'processing'
                    )
                    ON CONFLICT (delivery_id) DO NOTHING
                    """
                ),
                {
                    "delivery_id": delivery_id,
                    "event": event,
                    "action": action,
                    "payload": _json(payload),
                },
            )
            return bool(result.rowcount)

    def mark_webhook_delivery(
        self, delivery_id: str, *, status: str, error: str = ""
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE labrastro_github_webhook_deliveries
                    SET status=:status, error=:error, processed_at=now()
                    WHERE delivery_id=:delivery_id
                    """
                ),
                {"delivery_id": delivery_id, "status": status, "error": error},
            )

    def upsert_review_comment(
        self, record: GitHubReviewCommentRecord
    ) -> GitHubReviewCommentRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_github_review_comments (
                        id, github_id, pr_record_id, task_id, repository, pr_number,
                        author, body, path, line, side, url, state,
                        task_draft_id, assignment_id, metadata
                    ) VALUES (
                        :id, :github_id, :pr_record_id, :task_id, :repository, :pr_number,
                        :author, :body, :path, :line, :side, :url, :state,
                        :task_draft_id, :assignment_id, CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (github_id) DO UPDATE SET
                        author=EXCLUDED.author,
                        body=EXCLUDED.body,
                        path=EXCLUDED.path,
                        line=EXCLUDED.line,
                        side=EXCLUDED.side,
                        url=EXCLUDED.url,
                        state=EXCLUDED.state,
                        task_draft_id=COALESCE(EXCLUDED.task_draft_id, labrastro_github_review_comments.task_draft_id),
                        assignment_id=COALESCE(EXCLUDED.assignment_id, labrastro_github_review_comments.assignment_id),
                        metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """
                ),
                self._comment_params(record),
            )
            row = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_github_review_comments
                    WHERE github_id=:github_id
                    """
                ),
                {"github_id": record.github_id},
            ).mappings().first()
        return GitHubReviewCommentRecord.from_dict(_row_dict(row)) if row else record

    def list_review_comments(self, task_id: str) -> list[GitHubReviewCommentRecord]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_github_review_comments
                    WHERE task_id=:task_id
                    ORDER BY created_at ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings()
            return [GitHubReviewCommentRecord.from_dict(_row_dict(row)) for row in rows]

    def set_review_comment_followup(
        self,
        comment_id: str,
        *,
        task_draft_id: str | None,
        assignment_id: str | None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE labrastro_github_review_comments
                    SET task_draft_id=:task_draft_id,
                        assignment_id=:assignment_id,
                        updated_at=now()
                    WHERE github_id=:comment_id
                    """
                ),
                {
                    "comment_id": comment_id,
                    "task_draft_id": task_draft_id,
                    "assignment_id": assignment_id,
                },
            )

    def _sync_runtime_artifact(self, conn: Any, record: GitHubPullRequestRecord) -> None:
        if not record.artifact_id:
            return
        artifact_status = {
            "merged": "merged",
            "closed": "closed",
        }.get(record.status)
        if artifact_status is None:
            artifact_status = {
                "approved": "pr_approved",
                "changes_requested": "pr_changes_requested",
                "commented": "pr_reviewing",
            }.get(record.review_state, "pr_created")
        conn.execute(
            text(
                """
                UPDATE labrastro_runtime_artifacts
                SET status=:status,
                    merge_status=:merge_status,
                    pr_url=:pr_url,
                    metadata=metadata || CAST(:metadata AS JSONB)
                WHERE id=:artifact_id
                """
            ),
            {
                "artifact_id": record.artifact_id,
                "status": artifact_status,
                "merge_status": record.merge_status,
                "pr_url": record.url,
                "metadata": _json({"github": record.to_dict()}),
            },
        )
        if record.status == "merged":
            conn.execute(
                text(
                    """
                    UPDATE labrastro_runtime_tasks
                    SET issue_status='done', updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {"task_id": record.task_id},
            )

    def _pr_params(self, record: GitHubPullRequestRecord) -> dict[str, Any]:
        return {
            **record.to_dict(),
            "metadata": _json(record.metadata),
        }

    def _comment_params(self, record: GitHubReviewCommentRecord) -> dict[str, Any]:
        return {
            **record.to_dict(),
            "metadata": _json(record.metadata),
        }
