from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from reuleauxcoder.domain.agent_runtime.models import ArtifactStatus, ArtifactType
from reuleauxcoder.domain.config.models import GitHubConfig
from labrastro_server.adapters.reuleauxcoder.taskflow_dispatcher import (
    ReuleauxCoderTaskflowDispatcher,
)
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRuntimeControlPlane,
    RuntimeTaskRequest,
)
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.github.auth import GitHubInstallationTokenProvider
from labrastro_server.services.github.client import GitHubAPIError
from labrastro_server.services.github.in_memory_store import InMemoryGitHubStore
from labrastro_server.services.github.models import GitHubPullRequestRecord
from labrastro_server.services.github.service import PullRequestService, WebhookService
from labrastro_server.services.taskflow.service import TaskflowService


class FakeGitHubClient:
    def __init__(self) -> None:
        self.find_response: dict[str, Any] | None = None
        self.create_response: dict[str, Any] | Exception = _pull_payload()
        self.status_response: dict[str, Any] = {"repositories": [{"full_name": "org/repo"}]}
        self.created: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return self.status_response

    def find_pull_request(
        self, owner: str, repo: str, *, head: str, state: str = "open"
    ) -> dict[str, Any] | None:
        return self.find_response

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        self.created.append(
            {
                "owner": owner,
                "repo": repo,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            }
        )
        if isinstance(self.create_response, Exception):
            raise self.create_response
        return self.create_response


class FakeAppAuth:
    def app_jwt(self) -> str:
        return "app.jwt"

    def installation_token_path(self) -> str:
        return "/app/installations/2/access_tokens"


def test_installation_token_provider_caches_token() -> None:
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> dict[str, Any]:
        calls.append((method, url, headers, body))
        return {"token": "installation-token", "expires_at": 9999999999}

    provider = GitHubInstallationTokenProvider(
        _config(),
        auth=FakeAppAuth(),  # type: ignore[arg-type]
        transport=transport,
        now=lambda: 100,
    )

    assert provider() == "installation-token"
    assert provider() == "installation-token"
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert calls[0][2]["Authorization"] == "Bearer app.jwt"


def test_ensure_pr_for_task_creates_pr_from_branch_artifact() -> None:
    control = AgentRuntimeControlPlane()
    control.submit_task(RuntimeTaskRequest(issue_id="issue-1", agent_id="coder", prompt="run"), task_id="task-1")
    control.attach_artifact(
        "task-1",
        type=ArtifactType.BRANCH.value,
        status=ArtifactStatus.PUSHED.value,
        branch_name="agent/coder/task-1",
        metadata={
            "repo_url": "https://github.com/org/repo.git",
            "branch": "agent/coder/task-1",
            "head_sha": "abc123",
            "base_ref": "main",
            "pr_title": "Agent task title",
            "pr_body": "body",
            "pr_enabled": True,
        },
    )
    store = InMemoryGitHubStore()
    client = FakeGitHubClient()
    service = PullRequestService(
        config=_config(),
        store=store,
        client=client,  # type: ignore[arg-type]
        runtime_control_plane=control,
    )

    result = service.ensure_pr_for_task("task-1")

    assert result.ok is True
    assert result.record is not None
    assert result.record.repository == "org/repo"
    assert client.created[0]["head"] == "agent/coder/task-1"
    artifacts = control.artifacts_to_dict("task-1")
    assert artifacts[-1]["type"] == "pull_request"
    assert artifacts[-1]["status"] == "pr_created"
    assert control.task_to_dict("task-1")["pr_url"] == "https://github.com/org/repo/pull/7"
    assert [event.type for event in control.list_events("task-1")][-1] == "status"
    assert store.get_pull_request("org/repo", 7) is not None


def test_ensure_pr_for_task_records_failure_without_failing_task() -> None:
    control = AgentRuntimeControlPlane()
    control.submit_task(RuntimeTaskRequest(issue_id="issue-1", agent_id="coder", prompt="run"), task_id="task-1")
    control.attach_artifact(
        "task-1",
        type="branch",
        status="pushed",
        branch_name="agent/coder/task-1",
        metadata={"repo_url": "https://github.com/org/repo", "branch": "agent/coder/task-1"},
    )
    client = FakeGitHubClient()
    client.create_response = GitHubAPIError(500, "boom")
    service = PullRequestService(
        config=_config(),
        store=InMemoryGitHubStore(),
        client=client,  # type: ignore[arg-type]
        runtime_control_plane=control,
    )

    result = service.ensure_pr_for_task("task-1")

    assert result.ok is False
    assert "boom" in result.error
    task = control.task_to_dict("task-1")
    assert task["status"] == "queued"
    failed = control.artifacts_to_dict("task-1")[-1]
    assert failed["type"] == "pull_request"
    assert failed["status"] == "failed"
    assert failed["metadata"]["stage"] == "pr_create"


def test_webhook_review_comment_is_idempotent_and_creates_followup_assignment() -> None:
    control = AgentRuntimeControlPlane()
    control.submit_task(RuntimeTaskRequest(issue_id="issue-1", agent_id="coder", prompt="run"), task_id="task-1")
    taskflow = TaskflowService(dispatcher=ReuleauxCoderTaskflowDispatcher(control))
    collaboration = IssueAssignmentService(taskflow_service=taskflow)
    store = InMemoryGitHubStore()
    record = store.upsert_pull_request(
        GitHubPullRequestRecord(
            id="gh-pr-1",
            task_id="task-1",
            artifact_id=None,
            repository="org/repo",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
    )
    pr_service = PullRequestService(
        config=_config(),
        store=store,
        client=FakeGitHubClient(),  # type: ignore[arg-type]
        runtime_control_plane=control,
        issue_assignment_service=collaboration,
    )
    webhook = WebhookService(config=_config(), pr_service=pr_service)
    payload = {
        "action": "created",
        "repository": {"full_name": "org/repo"},
        "pull_request": {"number": 7},
        "comment": {
            "id": 123,
            "body": "Please adjust this line.",
            "path": "app.py",
            "line": 12,
            "html_url": "https://github.com/org/repo/pull/7#discussion_r123",
            "user": {"login": "reviewer"},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = _webhook_headers(body, delivery="delivery-1")

    first = webhook.handle(body=body, headers=headers)
    second = webhook.handle(body=body, headers=headers)

    assert first["ok"] is True
    assert second["duplicate"] is True
    comments = store.list_review_comments("task-1")
    assert len(comments) == 1
    assert comments[0].assignment_id
    assert comments[0].work_item_id
    assignment = collaboration.load_assignment_detail(comments[0].assignment_id)
    assert assignment["work_item_id"] == comments[0].work_item_id
    assert control.list_tasks(limit=20) == [control.task_to_dict("task-1")]
    assert store.get_pull_request(record.repository, record.number) is not None


def _config() -> GitHubConfig:
    return GitHubConfig(
        enabled=True,
        app_id="1",
        installation_id="2",
        private_key_path="app.pem",
        webhook_secret="secret",
    )


def _pull_payload() -> dict[str, Any]:
    return {
        "number": 7,
        "node_id": "PR_node",
        "url": "https://api.github.com/repos/org/repo/pulls/7",
        "html_url": "https://github.com/org/repo/pull/7",
        "state": "open",
        "draft": False,
        "merged": False,
        "head": {"ref": "agent/coder/task-1", "sha": "abc123"},
        "base": {"ref": "main"},
    }


def _webhook_headers(body: bytes, *, delivery: str) -> dict[str, str]:
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": "sha256=" + digest,
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": "pull_request_review_comment",
    }
