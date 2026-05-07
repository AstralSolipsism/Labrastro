"""GitHub App pull request lifecycle orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import hashlib
import json
import uuid
from typing import Any

from reuleauxcoder.domain.agent_runtime.models import ArtifactStatus, ArtifactType
from reuleauxcoder.domain.config.models import GitHubConfig
from reuleauxcoder.domain.taskflow.models import utc_now
from labrastro_server.services.agent_runtime.control_plane import AgentRuntimeControlPlane
from labrastro_server.services.agent_runtime.executor_backend import ExecutorEvent
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.github.client import GitHubAPIError, GitHubClient
from labrastro_server.services.github.models import (
    GitHubPullRequestRecord,
    GitHubReviewCommentRecord,
)
from labrastro_server.services.github.store import GitHubStore


@dataclass
class PullRequestEnsureResult:
    ok: bool
    record: GitHubPullRequestRecord | None = None
    artifact: dict[str, Any] | None = None
    error: str = ""
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pull_request": self.record.to_dict() if self.record is not None else None,
            "artifact": self.artifact,
            "error": self.error,
            "reused": self.reused,
        }


class PullRequestService:
    def __init__(
        self,
        *,
        config: GitHubConfig,
        store: GitHubStore,
        client: GitHubClient,
        runtime_control_plane: AgentRuntimeControlPlane,
        issue_assignment_service: IssueAssignmentService | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.runtime_control_plane = runtime_control_plane
        self.issue_assignment_service = issue_assignment_service

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def status(self) -> dict[str, Any]:
        config = self.config.to_dict(mask_secret=True)
        if not self.enabled:
            return {"enabled": False, "config": config, "api": {"ok": False}}
        try:
            data = self.client.status()
            return {
                "enabled": True,
                "config": config,
                "api": {
                    "ok": True,
                    "repositories_count": len(data.get("repositories", []))
                    if isinstance(data, dict)
                    else None,
                },
            }
        except Exception as exc:
            return {
                "enabled": True,
                "config": config,
                "api": {"ok": False, "error": str(exc)},
            }

    def ensure_pr_for_task(self, task_id: str) -> PullRequestEnsureResult:
        if not self.enabled:
            return PullRequestEnsureResult(ok=False, error="github_disabled")
        existing = self.store.get_pull_request_for_task(task_id)
        if existing is not None:
            return PullRequestEnsureResult(ok=True, record=existing, reused=True)
        branch_artifact = self._latest_branch_artifact(task_id)
        if branch_artifact is None:
            return PullRequestEnsureResult(ok=False, error="branch_artifact_missing")
        metadata = _dict(branch_artifact.get("metadata"))
        if not _bool(metadata.get("pr_enabled"), True):
            return PullRequestEnsureResult(ok=False, error="pr_disabled")
        try:
            repo_ref = _repo_ref(metadata, branch_artifact)
            base_ref = _first_str(
                metadata.get("pr_base"),
                metadata.get("base_ref"),
                metadata.get("base"),
                "main",
            )
            head_ref = _first_str(
                metadata.get("branch"),
                branch_artifact.get("branch_name"),
            )
            if not head_ref:
                raise ValueError("branch artifact missing branch name")
            title = _first_str(metadata.get("pr_title"), f"Agent task {task_id}")
            body = _first_str(
                metadata.get("pr_body"),
                self._default_pr_body(task_id, branch_artifact, base_ref),
            )
            created_or_existing, reused = self._create_or_reuse_pr(
                repo_ref, head=head_ref, base=base_ref, title=title, body=body
            )
            artifact = self.runtime_control_plane.attach_artifact(
                task_id,
                type=ArtifactType.PULL_REQUEST.value,
                status=ArtifactStatus.PR_CREATED.value,
                branch_name=head_ref,
                pr_url=str(created_or_existing.get("html_url") or ""),
                content=body,
                metadata={
                    "github": _pull_payload_summary(created_or_existing),
                    "repository": repo_ref.repository,
                    "base_ref": base_ref,
                    "head_ref": head_ref,
                    "head_sha": _head_sha(created_or_existing)
                    or str(metadata.get("head_sha") or ""),
                    "reused": reused,
                },
            )
            artifact_dict = self._artifact_dict(task_id, artifact.id)
            record = self._record_from_pull(
                task_id,
                artifact_id=artifact.id,
                repo_ref=repo_ref,
                pull=created_or_existing,
                base_ref=base_ref,
                head_ref=head_ref,
                metadata={
                    "branch_artifact_id": branch_artifact.get("id"),
                    "branch_metadata": metadata,
                    "repo_url": metadata.get("repo_url"),
                    "reused": reused,
                },
            )
            saved = self.store.upsert_pull_request(record)
            self._append_status_event(
                task_id,
                "pr_created",
                {
                    "pr_url": saved.url,
                    "repository": saved.repository,
                    "number": saved.number,
                    "reused": reused,
                },
            )
            return PullRequestEnsureResult(
                ok=True,
                record=saved,
                artifact=artifact_dict,
                reused=reused,
            )
        except Exception as exc:
            message = str(exc)
            try:
                failed = self.runtime_control_plane.attach_artifact(
                    task_id,
                    type=ArtifactType.PULL_REQUEST.value,
                    status=ArtifactStatus.FAILED.value,
                    branch_name=str(branch_artifact.get("branch_name") or ""),
                    content=message,
                    metadata={
                        "stage": "pr_create",
                        "branch_artifact_id": branch_artifact.get("id"),
                    },
                )
                artifact_dict = next(
                    (
                        artifact
                        for artifact in self.runtime_control_plane.artifacts_to_dict(
                            task_id
                        )
                        if artifact.get("id") == failed.id
                    ),
                    None,
                )
            except Exception:
                artifact_dict = None
            self._append_status_event(
                task_id,
                "pr_create_failed",
                {"error": message, "branch_artifact": branch_artifact},
            )
            return PullRequestEnsureResult(ok=False, artifact=artifact_dict, error=message)

    def retry_pr_for_task(
        self, *, task_id: str | None = None, artifact_id: str | None = None
    ) -> PullRequestEnsureResult:
        if task_id is None and artifact_id:
            task_id = self._task_id_for_artifact(artifact_id)
        if not task_id:
            raise ValueError("task_id_or_artifact_id_required")
        return self.ensure_pr_for_task(task_id)

    def reconcile_pr(self, record: GitHubPullRequestRecord) -> GitHubPullRequestRecord:
        pull = self.client.get_pull_request(record.owner, record.repo, record.number)
        reviews = self.client.list_reviews(record.owner, record.repo, record.number)
        comments = self.client.list_review_comments(record.owner, record.repo, record.number)
        review_state = _review_state_from_reviews(reviews) or record.review_state
        status, merge_status = _pull_status(pull)
        updated = self.store.update_pull_request_state(
            record.repository,
            record.number,
            status=status,
            review_state=review_state,
            merge_status=merge_status,
            draft=bool(pull.get("draft", False)),
            head_sha=_head_sha(pull),
            metadata={"last_pull": _pull_payload_summary(pull), "reconciled_at": utc_now()},
        )
        for comment in comments:
            self.record_review_comment(record, comment, source="reconcile")
        return updated or record

    def reconcile_open(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for record in self.store.list_open_pull_requests():
            try:
                updated = self.reconcile_pr(record)
                results.append({"ok": True, "pull_request": updated.to_dict()})
            except Exception as exc:
                results.append(
                    {
                        "ok": False,
                        "pull_request": record.to_dict(),
                        "error": str(exc),
                    }
                )
        return results

    def sync_pull_request_payload(self, payload: dict[str, Any]) -> GitHubPullRequestRecord | None:
        repo_ref = _repo_ref_from_payload(payload)
        pull = _dict(payload.get("pull_request"))
        if not repo_ref or not pull:
            return None
        number = int(pull.get("number") or 0)
        record = self.store.get_pull_request(repo_ref.repository, number)
        if record is None:
            return None
        status, merge_status = _pull_status(pull)
        return self.store.update_pull_request_state(
            repo_ref.repository,
            number,
            status=status,
            merge_status=merge_status,
            draft=bool(pull.get("draft", False)),
            head_sha=_head_sha(pull),
            metadata={"last_webhook_action": str(payload.get("action") or "")},
        )

    def sync_review_payload(self, payload: dict[str, Any]) -> GitHubPullRequestRecord | None:
        repo_ref = _repo_ref_from_payload(payload)
        pull = _dict(payload.get("pull_request"))
        review = _dict(payload.get("review"))
        if not repo_ref or not pull or not review:
            return None
        state = str(review.get("state") or "").lower() or "commented"
        return self.store.update_pull_request_state(
            repo_ref.repository,
            int(pull.get("number") or 0),
            review_state=_normalize_review_state(state),
            metadata={"last_review": review},
        )

    def record_review_comment(
        self,
        record: GitHubPullRequestRecord,
        comment: dict[str, Any],
        *,
        source: str,
    ) -> GitHubReviewCommentRecord:
        github_id = str(comment.get("id") or comment.get("node_id") or "")
        if not github_id:
            github_id = f"{record.repository}#{record.number}:{uuid.uuid4().hex}"
        user = _dict(comment.get("user"))
        line = comment.get("line") or comment.get("original_line")
        saved = self.store.upsert_review_comment(
            GitHubReviewCommentRecord(
                id=f"gh-comment-{uuid.uuid4().hex}",
                github_id=github_id,
                pr_record_id=record.id,
                task_id=record.task_id,
                repository=record.repository,
                pr_number=record.number,
                author=str(user.get("login") or comment.get("author") or ""),
                body=str(comment.get("body") or ""),
                path=str(comment["path"]) if comment.get("path") is not None else None,
                line=int(line) if line is not None else None,
                side=str(comment["side"]) if comment.get("side") is not None else None,
                url=str(comment.get("html_url") or comment.get("url") or ""),
                metadata={
                    "source": source,
                    "pull_request_url": record.url,
                    "raw": comment,
                },
            )
        )
        if saved.assignment_id or self.issue_assignment_service is None:
            return saved
        try:
            assignment = self._create_followup_assignment(record, saved)
            self.store.set_review_comment_followup(
                saved.github_id,
                task_draft_id=assignment.task_draft_id,
                assignment_id=assignment.id,
            )
            saved.task_draft_id = assignment.task_draft_id
            saved.assignment_id = assignment.id
        except Exception as exc:
            self.store.set_review_comment_followup(
                saved.github_id,
                task_draft_id=None,
                assignment_id=None,
            )
            saved.metadata["followup_error"] = str(exc)
            self.store.upsert_review_comment(saved)
        return saved

    def record_review_comment_payload(
        self, payload: dict[str, Any], *, source: str
    ) -> GitHubReviewCommentRecord | None:
        repo_ref = _repo_ref_from_payload(payload)
        issue = _dict(payload.get("issue"))
        if issue and not issue.get("pull_request") and not payload.get("pull_request"):
            return None
        pull = _dict(payload.get("pull_request") or payload.get("issue"))
        comment = _dict(payload.get("comment"))
        if not repo_ref or not pull or not comment:
            return None
        number = int(pull.get("number") or 0)
        record = self.store.get_pull_request(repo_ref.repository, number)
        if record is None:
            return None
        return self.record_review_comment(record, comment, source=source)

    def list_review_comments(self, task_id: str) -> list[dict[str, Any]]:
        return [comment.to_dict() for comment in self.store.list_review_comments(task_id)]

    def _create_or_reuse_pr(
        self,
        repo_ref: "_RepoRef",
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> tuple[dict[str, Any], bool]:
        existing = self.client.find_pull_request(repo_ref.owner, repo_ref.repo, head=head)
        if existing is not None:
            return existing, True
        try:
            return (
                self.client.create_pull_request(
                    repo_ref.owner,
                    repo_ref.repo,
                    head=head,
                    base=base,
                    title=title,
                    body=body,
                ),
                False,
            )
        except GitHubAPIError as exc:
            if exc.status == 422:
                existing = self.client.find_pull_request(
                    repo_ref.owner, repo_ref.repo, head=head, state="all"
                )
                if existing is not None:
                    return existing, True
            raise

    def _record_from_pull(
        self,
        task_id: str,
        *,
        artifact_id: str,
        repo_ref: "_RepoRef",
        pull: dict[str, Any],
        base_ref: str,
        head_ref: str,
        metadata: dict[str, Any],
    ) -> GitHubPullRequestRecord:
        status, merge_status = _pull_status(pull)
        number = int(pull.get("number") or 0)
        if number <= 0:
            raise RuntimeError("GitHub PR response missing number")
        return GitHubPullRequestRecord(
            id=f"gh-pr-{uuid.uuid4().hex}",
            task_id=task_id,
            artifact_id=artifact_id,
            repository=repo_ref.repository,
            owner=repo_ref.owner,
            repo=repo_ref.repo,
            number=number,
            node_id=str(pull.get("node_id") or ""),
            url=str(pull.get("html_url") or ""),
            api_url=str(pull.get("url") or ""),
            base_ref=str(_dict(pull.get("base")).get("ref") or base_ref),
            head_ref=str(_dict(pull.get("head")).get("ref") or head_ref),
            head_sha=_head_sha(pull),
            status=status,
            merge_status=merge_status,
            draft=bool(pull.get("draft", False)),
            metadata=metadata,
            last_synced_at=utc_now(),
        )

    def _default_pr_body(
        self, task_id: str, branch_artifact: dict[str, Any], base_ref: str
    ) -> str:
        task = self.runtime_control_plane.task_to_dict(task_id)
        lines = [
            "Agent runtime task completed.",
            "",
            f"Task: {task_id}",
            f"Issue: {task.get('issue_id', '')}",
            f"Agent: {task.get('agent_id', '')}",
            f"Branch: {branch_artifact.get('branch_name', '')}",
            f"Base: {base_ref}",
        ]
        return "\n".join(lines)

    def _latest_branch_artifact(self, task_id: str) -> dict[str, Any] | None:
        artifacts = self.runtime_control_plane.artifacts_to_dict(task_id)
        branches = [
            artifact
            for artifact in artifacts
            if artifact.get("type") == ArtifactType.BRANCH.value
            and artifact.get("status") == ArtifactStatus.PUSHED.value
        ]
        return branches[-1] if branches else None

    def _task_id_for_artifact(self, artifact_id: str) -> str | None:
        for task in self.runtime_control_plane.list_tasks(limit=500):
            for artifact in self.runtime_control_plane.artifacts_to_dict(task["id"]):
                if artifact.get("id") == artifact_id:
                    return str(task["id"])
        return None

    def _artifact_dict(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        return next(
            (
                artifact
                for artifact in self.runtime_control_plane.artifacts_to_dict(task_id)
                if artifact.get("id") == artifact_id
            ),
            None,
        )

    def _create_followup_assignment(
        self,
        record: GitHubPullRequestRecord,
        comment: GitHubReviewCommentRecord,
    ) -> Any:
        service = self.issue_assignment_service
        if service is None:
            raise RuntimeError("issue assignment service unavailable")
        task = self.runtime_control_plane.task_to_dict(record.task_id)
        metadata = {
            "source": "github_review_comment",
            "github_review_comment_id": comment.github_id,
            "github_pr_record_id": record.id,
            "github_pr_url": record.url,
            "parent_task_id": record.task_id,
            "trigger_comment_id": comment.github_id,
            "repository": record.repository,
            "path": comment.path,
            "line": comment.line,
        }
        issue = service.create_issue(
            title=f"GitHub review follow-up: {record.repository}#{record.number}",
            description=comment.body,
            source="github_review_comment",
            metadata=metadata,
        )
        prompt = _followup_prompt(record, comment)
        return service.create_assignment(
            issue.id,
            target_agent_id=str(task.get("agent_id") or "") or None,
            title=issue.title,
            prompt=prompt,
            repo_url=str(record.metadata.get("repo_url") or ""),
            execution_location=str(task.get("execution_location") or ""),
            reason="github_review_comment",
            source="github_review_comment",
            metadata=metadata,
        )

    def _append_status_event(self, task_id: str, status: str, data: dict[str, Any]) -> None:
        self.runtime_control_plane.append_executor_event(
            task_id,
            ExecutorEvent(type="status", data={"status": status, **data}),
        )


class WebhookService:
    def __init__(self, *, config: GitHubConfig, pr_service: PullRequestService) -> None:
        self.config = config
        self.pr_service = pr_service

    def handle(
        self,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if not self.pr_service.enabled:
            return {"ok": False, "error": "github_disabled"}
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        self._verify_signature(
            body, normalized_headers.get("x-hub-signature-256", "")
        )
        delivery_id = str(normalized_headers.get("x-github-delivery") or "")
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery header is required")
        event = str(normalized_headers.get("x-github-event") or "")
        payload = json.loads(body.decode("utf-8")) if body else {}
        action = str(payload.get("action") or "")
        first_seen = self.pr_service.store.record_webhook_delivery(
            delivery_id=delivery_id,
            event=event,
            action=action,
            payload=payload,
        )
        if not first_seen:
            return {"ok": True, "duplicate": True, "delivery_id": delivery_id}
        try:
            result = self._dispatch(event, payload)
            self.pr_service.store.mark_webhook_delivery(delivery_id, status="processed")
            return {
                "ok": True,
                "duplicate": False,
                "delivery_id": delivery_id,
                "event": event,
                "result": result,
            }
        except Exception as exc:
            self.pr_service.store.mark_webhook_delivery(
                delivery_id, status="failed", error=str(exc)
            )
            raise

    def _verify_signature(self, body: bytes, signature: str) -> None:
        secret = self.config.webhook_secret
        if not secret:
            raise ValueError("github.webhook_secret is required")
        digest = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        expected = "sha256=" + digest
        if not hmac.compare_digest(expected, signature):
            raise PermissionError("invalid_github_webhook_signature")

    def _dispatch(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        if event == "pull_request":
            record = self.pr_service.sync_pull_request_payload(payload)
            return {"pull_request": record.to_dict() if record is not None else None}
        if event == "pull_request_review":
            record = self.pr_service.sync_review_payload(payload)
            return {"pull_request": record.to_dict() if record is not None else None}
        if event in {"pull_request_review_comment", "issue_comment"}:
            comment = self.pr_service.record_review_comment_payload(
                payload, source=f"webhook:{event}"
            )
            return {
                "review_comment": comment.to_dict() if comment is not None else None
            }
        return {"ignored": True}


class ReconcileService:
    def __init__(self, pr_service: PullRequestService) -> None:
        self.pr_service = pr_service

    def reconcile(
        self,
        *,
        task_id: str | None = None,
        repository: str | None = None,
        number: int | None = None,
    ) -> list[dict[str, Any]]:
        if task_id:
            record = self.pr_service.store.get_pull_request_for_task(task_id)
            if record is None:
                return [{"ok": False, "error": "pull_request_not_found"}]
            return [{"ok": True, "pull_request": self.pr_service.reconcile_pr(record).to_dict()}]
        if repository and number is not None:
            record = self.pr_service.store.get_pull_request(repository, int(number))
            if record is None:
                return [{"ok": False, "error": "pull_request_not_found"}]
            return [{"ok": True, "pull_request": self.pr_service.reconcile_pr(record).to_dict()}]
        return self.pr_service.reconcile_open()


@dataclass(frozen=True)
class _RepoRef:
    owner: str
    repo: str

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _bool(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return fallback
    return text not in {"0", "false", "no", "off"}


def _repo_ref(metadata: dict[str, Any], artifact: dict[str, Any]) -> _RepoRef:
    repository = _first_str(
        metadata.get("repository"),
        metadata.get("repo_full_name"),
        artifact.get("repository"),
    )
    if repository:
        parts = repository.rstrip("/").removesuffix(".git").split("/")
        if len(parts) >= 2:
            return _RepoRef(owner=parts[-2], repo=parts[-1])
    repo_url = _first_str(metadata.get("repo_url"), artifact.get("repo_url"))
    parsed = _repo_ref_from_url(repo_url)
    if parsed is None:
        raise ValueError("branch artifact missing GitHub repository")
    return parsed


def _repo_ref_from_url(repo_url: str) -> _RepoRef | None:
    text = repo_url.strip().removesuffix(".git")
    if not text:
        return None
    if text.startswith("git@"):
        _, _, path = text.partition(":")
        parts = path.strip("/").split("/")
    else:
        path = text
        if "://" in path:
            path = path.split("://", 1)[1]
            path = path.split("/", 1)[1] if "/" in path else ""
        parts = path.strip("/").split("/")
    if len(parts) < 2:
        return None
    return _RepoRef(owner=parts[-2], repo=parts[-1])


def _repo_ref_from_payload(payload: dict[str, Any]) -> _RepoRef | None:
    repository = _dict(payload.get("repository"))
    full_name = str(repository.get("full_name") or "")
    if "/" in full_name:
        owner, repo = full_name.split("/", 1)
        return _RepoRef(owner=owner, repo=repo)
    return None


def _pull_status(pull: dict[str, Any]) -> tuple[str, str]:
    state = str(pull.get("state") or "open").lower()
    if state == "closed" and bool(pull.get("merged", False)):
        return "merged", "merged_by_user"
    if state == "closed":
        return "closed", "closed"
    return "open", "pending_user"


def _head_sha(pull: dict[str, Any]) -> str:
    return str(_dict(pull.get("head")).get("sha") or "")


def _review_state_from_reviews(reviews: list[dict[str, Any]]) -> str:
    for review in reversed(reviews):
        state = _normalize_review_state(str(review.get("state") or ""))
        if state != "none":
            return state
    return "none"


def _normalize_review_state(state: str) -> str:
    normalized = state.strip().lower()
    if normalized in {"approved", "changes_requested", "commented"}:
        return normalized
    return "none"


def _pull_payload_summary(pull: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pull.get("number"),
        "node_id": pull.get("node_id"),
        "url": pull.get("url"),
        "html_url": pull.get("html_url"),
        "state": pull.get("state"),
        "draft": pull.get("draft"),
        "merged": pull.get("merged"),
        "head": _dict(pull.get("head")),
        "base": _dict(pull.get("base")),
    }


def _followup_prompt(
    record: GitHubPullRequestRecord,
    comment: GitHubReviewCommentRecord,
) -> str:
    location = ""
    if comment.path:
        location = comment.path
        if comment.line is not None:
            location += f":{comment.line}"
    lines = [
        f"Address the GitHub review comment on {record.repository}#{record.number}.",
        f"PR: {record.url}",
    ]
    if location:
        lines.append(f"Location: {location}")
    if comment.author:
        lines.append(f"Reviewer: {comment.author}")
    lines.extend(["", comment.body])
    return "\n".join(lines)
