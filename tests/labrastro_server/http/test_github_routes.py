from __future__ import annotations

import hashlib
import hmac
import json
import socket
from typing import Any
from urllib import request
from urllib.error import HTTPError

from reuleauxcoder.domain.config.models import GitHubConfig
from labrastro_server.interfaces.http.remote.service import (
    RemoteRelayHTTPService as _RemoteRelayHTTPService,
)
from labrastro_server.relay.server import RelayServer
from labrastro_server.services.auth.models import AuthPrincipal
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from labrastro_server.services.github.in_memory_store import InMemoryGitHubStore
from labrastro_server.services.github.service import PullRequestService


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

TEST_ADMIN_TOKEN = "test-admin-token"
TEST_ADMIN_HEADERS = {"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}


class _TestAuthService:
    def state(self) -> dict:
        return {"ok": True, "auth_enabled": True, "login_required": True}

    def authenticate_access_token(self, token: str):
        if token == TEST_ADMIN_TOKEN:
            return AuthPrincipal("usr_test", "admin", "superadmin", "dev_test")
        return None

    def me(self, principal):
        return {"ok": True, "user": principal.public_user(), "device": None}

    def bootstrap_token(self, principal, ttl_sec: int):
        return {"ok": True, "bootstrap_token": self._issue_bootstrap_token(ttl_sec)}


def RemoteRelayHTTPService(*args, **kwargs):  # noqa: N802
    service = _TestAuthService()
    auth_service = kwargs.pop("auth_service", service)
    instance = _RemoteRelayHTTPService(*args, auth_service=auth_service, **kwargs)
    service._issue_bootstrap_token = instance.issue_bootstrap_token
    return instance


class FakeGitHubClient:
    def status(self) -> dict[str, Any]:
        return {"repositories": []}

    def find_pull_request(self, *args: Any, **kwargs: Any) -> None:
        return None

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
        return {
            "number": 9,
            "node_id": "PR_node",
            "url": f"https://api.github.com/repos/{owner}/{repo}/pulls/9",
            "html_url": f"https://github.com/{owner}/{repo}/pull/9",
            "state": "open",
            "draft": False,
            "merged": False,
            "head": {"ref": head, "sha": "abc123"},
            "base": {"ref": base},
        }


def test_github_webhook_rejects_bad_signature() -> None:
    relay = RelayServer()
    relay.start()
    port = _free_port()
    config = GitHubConfig(
        enabled=True,
        app_id="1",
        installation_id="2",
        private_key_path="app.pem",
        webhook_secret="secret",
    )
    runtime = AgentRunControlPlane()
    pr_service = PullRequestService(
        config=config,
        store=InMemoryGitHubStore(),
        client=FakeGitHubClient(),  # type: ignore[arg-type]
        runtime_control_plane=runtime,
    )
    service = RemoteRelayHTTPService(
        relay_server=relay,
        bind=f"127.0.0.1:{port}",
        runtime_control_plane=runtime,
        github_pr_service=pr_service,
    )
    service.start()
    try:
        body = {"action": "opened"}
        data = json.dumps(body).encode("utf-8")
        req = request.Request(
            f"{service.base_url}/remote/github/webhook",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=bad",
                "X-GitHub-Delivery": "delivery-1",
                "X-GitHub-Event": "pull_request",
            },
            method="POST",
        )
        try:
            _URLOPEN(req, timeout=5)
            raise AssertionError("webhook should reject bad signature")
        except HTTPError as exc:
            assert exc.code == 403

        status, payload = _json_request(
            "GET",
            f"{service.base_url}/remote/admin/github/status",
            headers=TEST_ADMIN_HEADERS,
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["github"]["enabled"] is True
    finally:
        service.stop()
        relay.stop()


def test_github_webhook_accepts_signed_delivery_once() -> None:
    relay = RelayServer()
    relay.start()
    port = _free_port()
    config = GitHubConfig(
        enabled=True,
        app_id="1",
        installation_id="2",
        private_key_path="app.pem",
        webhook_secret="secret",
    )
    pr_service = PullRequestService(
        config=config,
        store=InMemoryGitHubStore(),
        client=FakeGitHubClient(),  # type: ignore[arg-type]
        runtime_control_plane=AgentRunControlPlane(),
    )
    service = RemoteRelayHTTPService(
        relay_server=relay,
        bind=f"127.0.0.1:{port}",
        github_pr_service=pr_service,
    )
    service.start()
    try:
        body = json.dumps({"action": "opened"}).encode("utf-8")
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + signature,
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
        }

        first = _json_request_raw("POST", f"{service.base_url}/remote/github/webhook", body, headers)
        second = _json_request_raw("POST", f"{service.base_url}/remote/github/webhook", body, headers)

        assert first[1]["duplicate"] is False
        assert second[1]["duplicate"] is True
    finally:
        service.stop()
        relay.stop()


def test_agent_run_complete_creates_github_pr_artifact() -> None:
    relay = RelayServer()
    relay.start()
    port = _free_port()
    runtime = AgentRunControlPlane()
    runtime.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor="fake",
            execution_location="daemon_worktree",
        ),
        task_id="task-pr",
    )
    pr_service = PullRequestService(
        config=GitHubConfig(enabled=True, webhook_secret="secret"),
        store=InMemoryGitHubStore(),
        client=FakeGitHubClient(),  # type: ignore[arg-type]
        runtime_control_plane=runtime,
    )
    service = RemoteRelayHTTPService(
        relay_server=relay,
        bind=f"127.0.0.1:{port}",
        runtime_control_plane=runtime,
        github_pr_service=pr_service,
    )
    service.start()
    try:
        _, register_body = _json_request(
            "POST",
            f"{service.base_url}/remote/register",
            {
                "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                "cwd": "/tmp",
                "features": ["agent_runs"],
            },
        )
        peer_token = register_body["payload"]["peer_token"]
        _, claim_body = _json_request(
            "POST",
            f"{service.base_url}/remote/agent-runs/claim",
            {
                "peer_token": peer_token,
                "worker_id": "worker-1",
                "executors": ["fake"],
            },
        )
        claim = claim_body["claim"]

        _, complete = _json_request(
            "POST",
            f"{service.base_url}/remote/agent-runs/complete",
            {
                "peer_token": peer_token,
                "request_id": claim["request_id"],
                "agent_run_id": "task-pr",
                "worker_id": "worker-1",
                "status": "completed",
                "output": "done",
                "artifacts": [
                    {
                        "type": "branch",
                        "status": "pushed",
                        "branch_name": "agent/coder/task-pr",
                        "metadata": {
                            "repo_url": "https://github.com/org/repo.git",
                            "branch": "agent/coder/task-pr",
                            "base_ref": "main",
                            "pr_enabled": True,
                        },
                    }
                ],
            },
        )

        assert complete["ok"] is True
        assert complete["github"]["ok"] is True
        assert complete["github"]["pull_request"]["url"] == "https://github.com/org/repo/pull/9"
        artifacts = runtime.artifacts_to_dict("task-pr")
        assert [artifact["type"] for artifact in artifacts] == ["branch", "pull_request"]
    finally:
        service.stop()
        relay.stop()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _json_request_raw(
    method: str,
    url: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    req = request.Request(url, data=body, headers=headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        text = resp.read().decode("utf-8")
        return resp.status, json.loads(text) if text else {}
