from __future__ import annotations

import importlib

import pytest


def _models():
    return importlib.import_module("reuleauxcoder.domain.agent_runtime.models")


def test_runtime_profile_config_preserves_cli_isolation_and_credentials() -> None:
    models = _models()

    profile = models.RuntimeProfileConfig.from_dict(
        "codex_remote",
        {
            "executor": "codex",
            "execution_location": "remote_server",
            "model": "gpt-5.2-codex",
            "command": "codex",
            "args": ["--json"],
            "env": {"CODEX_HOME_MODE": "isolated"},
            "runtime_home_policy": "per_task",
            "approval_mode": "autonomous",
            "config_isolation": "per_agent",
            "credential_refs": {
                "model": "cred_codex_team",
                "git": "cred_github_repo_writer",
            },
            "mcp": {"servers": ["github"]},
        },
    )

    assert profile.id == "codex_remote"
    assert profile.executor == models.ExecutorType.CODEX
    assert profile.executor.value == "codex"
    assert profile.execution_location == models.ExecutionLocation.REMOTE_SERVER
    assert profile.execution_location.value == "remote_server"
    assert profile.model == "gpt-5.2-codex"
    assert profile.runtime_home_policy == "per_task"
    assert profile.config_isolation == "per_agent"
    assert profile.credential_refs["model"] == "cred_codex_team"
    assert profile.credential_refs["git"] == "cred_github_repo_writer"
    assert profile.mcp["servers"] == ["github"]


def test_agent_config_binds_runtime_profile_prompt_dispatch_and_packages() -> None:
    models = _models()

    agent = models.AgentConfig.from_dict(
        "code_reviewer",
        {
            "name": "Code Reviewer",
            "description": "审查代码风险",
            "runtime_profile": "codex_remote",
            "dispatch": {
                "profile": "适合审查代码风险、阅读仓库并指出缺失测试。",
                "examples": ["审查后端运行时变更", "检查 PR 的测试覆盖"],
                "avoid": ["线上发布操作"],
            },
            "capability_refs": ["github-review"],
            "prompt": {
                "agent_md": ".agents/code_reviewer/AGENT.md",
                "system_append": "你专注于发现风险、回归和缺失测试。",
            },
            "max_concurrent_tasks": 2,
        },
    )

    assert agent.id == "code_reviewer"
    assert agent.runtime_profile == "codex_remote"
    assert agent.dispatch.profile.startswith("适合审查代码风险")
    assert agent.dispatch.examples == ["审查后端运行时变更", "检查 PR 的测试覆盖"]
    assert agent.dispatch.avoid == ["线上发布操作"]
    assert agent.capability_refs == ["github-review"]
    assert agent.prompt.agent_md == ".agents/code_reviewer/AGENT.md"
    assert "风险" in agent.prompt.system_append
    assert agent.max_concurrent_tasks == 2


def test_agent_config_rejects_plaintext_secrets() -> None:
    models = _models()

    with pytest.raises(ValueError, match="credential_refs"):
        models.AgentConfig.from_dict(
            "unsafe_agent",
            {
                "runtime_profile": "codex_remote",
                "dispatch": {"profile": "Reads repository context."},
                "secrets": {"OPENAI_API_KEY": "sk-should-not-be-stored"},
            },
        )


def test_agent_config_rejects_removed_mixed_capability_fields() -> None:
    models = _models()

    with pytest.raises(ValueError, match="capabilities"):
        models.AgentConfig.from_dict(
            "legacy_agent",
            {
                "runtime_profile": "codex_remote",
                "capabilities": ["read_repo"],
            },
        )

    with pytest.raises(ValueError, match="dispatch_tags"):
        models.AgentConfig.from_dict(
            "legacy_dispatch_agent",
            {
                "runtime_profile": "codex_remote",
                "dispatch_tags": ["read_repo"],
            },
        )


def test_resolve_capability_refs_merges_all_packages() -> None:
    models = _models()

    packages = {
        "repo": models.CapabilityPackageConfig.from_dict(
            "repo",
            {
                "mcp_servers": ["github"],
                "skills": ["code-review"],
                "permissions": ["repo.read"],
            },
        ),
        "runtime": models.CapabilityPackageConfig.from_dict(
            "runtime",
            {
                "cli_tools": ["gitnexus"],
                "permissions": ["repo.read", "runtime.dispatch"],
            },
        ),
    }

    resolved = models.resolve_capability_refs(["repo", "runtime"], packages)

    assert [package["id"] for package in resolved["packages"]] == ["repo", "runtime"]
    assert resolved["mcp_servers"] == ["github"]
    assert resolved["skills"] == ["code-review"]
    assert resolved["cli_tools"] == ["gitnexus"]
    assert "permissions" not in resolved
    assert "permissions" not in resolved["packages"][0]
    assert "permissions" not in resolved["packages"][1]


def test_task_and_artifact_status_are_independent() -> None:
    models = _models()

    task = models.AgentRunRecord(
        id="task-1",
        issue_id="issue-1",
        agent_id="code_reviewer",
        trigger_mode=models.TriggerMode.ISSUE_TASK,
        status=models.TaskStatus.COMPLETED,
    )
    artifact = models.TaskArtifact(
        id="artifact-1",
        task_id="task-1",
        type=models.ArtifactType.PULL_REQUEST,
        status=models.ArtifactStatus.PR_REVIEWING,
        branch_name="agent/code-reviewer/task-1",
        pr_url="https://example.test/pr/1",
    )

    assert task.is_terminal is True
    assert task.status == models.TaskStatus.COMPLETED
    assert artifact.status == models.ArtifactStatus.PR_REVIEWING
    assert artifact.status != models.ArtifactStatus.MERGED
    assert artifact.requires_user_merge is True


def test_non_code_task_allows_report_artifact_without_branch_or_pr() -> None:
    models = _models()

    task = models.AgentRunRecord(
        id="task-2",
        issue_id="issue-2",
        agent_id="researcher",
        trigger_mode=models.TriggerMode.ISSUE_TASK,
        status=models.TaskStatus.COMPLETED,
    )
    artifact = models.TaskArtifact(
        id="artifact-2",
        task_id="task-2",
        type=models.ArtifactType.REPORT,
        status=models.ArtifactStatus.GENERATED,
        content="调研结论",
    )

    assert task.status == models.TaskStatus.COMPLETED
    assert artifact.type == models.ArtifactType.REPORT
    assert artifact.branch_name is None
    assert artifact.pr_url is None
    assert artifact.requires_user_merge is False
