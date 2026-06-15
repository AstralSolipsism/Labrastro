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
    assert profile.worker_kind == models.WorkerKind.SERVER_WORKER
    assert profile.model_request_origin == models.ModelRequestOrigin.SERVER_WORKER_CLI
    assert profile.model == "gpt-5.2-codex"
    assert profile.runtime_home_policy == "per_task"
    assert profile.config_isolation == "per_agent"
    assert profile.credential_refs["model"] == "cred_codex_team"
    assert profile.credential_refs["git"] == "cred_github_repo_writer"
    assert profile.mcp["servers"] == ["github"]


def test_runtime_profile_config_infers_local_cli_origin_for_local_cli_executor() -> None:
    models = _models()

    profile = models.RuntimeProfileConfig.from_dict(
        "codex_local",
        {
            "executor": "codex",
            "execution_location": "local_workspace",
        },
    )

    assert profile.worker_kind == models.WorkerKind.LOCAL_PEER
    assert profile.model_request_origin == models.ModelRequestOrigin.LOCAL_CLI
    assert profile.to_dict()["worker_kind"] == "local_peer"
    assert profile.to_dict()["model_request_origin"] == "local_cli"


def test_agent_config_binds_runtime_profile_prompt_dispatch_and_packages() -> None:
    models = _models()

    agent = models.AgentConfig.from_dict(
        "code_reviewer",
        {
            "name": "Code Reviewer",
            "description": "审查代码风险",
            "runtime_profile": "codex_remote",
            "visibility": "user",
            "chat_entrypoint": False,
            "delegable": True,
            "taskflow_eligible": True,
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
    assert agent.visibility == "user"
    assert agent.chat_entrypoint is False
    assert agent.can_delegate is True
    assert agent.can_run_taskflow is True
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

    components = {
        "mcp:github": models.CapabilityComponentConfig.from_dict(
            "mcp:github",
            {
                "kind": "mcp",
                "name": "github",
                "config": {"command": "github-mcp-server", "args": ["stdio"]},
            },
        ),
        "skill:code-review": models.CapabilityComponentConfig.from_dict(
            "skill:code-review",
            {
                "kind": "skill",
                "name": "code-review",
                "config": {"path_hint": "/skills/code-review"},
            },
        ),
        "envreq:executable:gitnexus": models.CapabilityComponentConfig.from_dict(
            "envreq:executable:gitnexus",
            {
                "kind": "environment_requirement",
                "name": "gitnexus",
                "access": "both",
                "risk_level": "medium",
                "execution_policy": "escalate",
                "config": {
                    "kind": "executable",
                    "command": "gitnexus",
                    "check": "gitnexus --version",
                    "env": {"GITNEXUS_HOME": ".gitnexus"},
                },
            },
        ),
        "builtin_tool:fetch_capabilities": models.CapabilityComponentConfig.from_dict(
            "builtin_tool:fetch_capabilities",
            {
                "kind": "builtin_tool",
                "name": "fetch_capabilities",
                "access": "read",
                "execution_policy": "allow",
                "registry_path": "builtin:fetch_capabilities",
            },
        ),
        "mcp:docs:search": models.CapabilityComponentConfig.from_dict(
            "mcp:docs:search",
            {
                "kind": "mcp_tool",
                "name": "search",
                "description": "Search documentation.",
                "registry_path": "mcp:docs:search",
                "execution_policy": "allow",
                "config": {
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    }
                },
            },
        ),
    }
    packages = {
        "repo": models.CapabilityPackageConfig.from_dict(
            "repo",
            {
                "components": ["mcp:github", "skill:code-review"],
                "permissions": ["repo.read"],
            },
        ),
        "runtime": models.CapabilityPackageConfig.from_dict(
            "runtime",
            {
                "components": ["envreq:executable:gitnexus"],
                "permissions": ["repo.read", "runtime.dispatch"],
            },
        ),
        "docs": models.CapabilityPackageConfig.from_dict(
            "docs",
            {
                "components": ["builtin_tool:fetch_capabilities", "mcp:docs:search"],
                "effective_capabilities": [
                    "Fetch documentation evidence for capability package drafts."
                ],
                "credentials": ["DOCS_TOKEN"],
                "risk_level": "low",
            },
        ),
    }

    resolved = models.resolve_capability_refs(["repo", "runtime", "docs"], packages, components)

    assert [package["id"] for package in resolved["packages"]] == ["repo", "runtime", "docs"]
    assert resolved["mcp_servers"] == ["github"]
    assert resolved["skills"] == ["code-review"]
    assert resolved["environment_requirements"][0]["id"] == "envreq:executable:gitnexus"
    assert resolved["environment_requirements"][0]["kind"] == "executable"
    assert "tools" not in resolved
    assert [item["tool_id"] for item in resolved["tool_specs"]] == [
        "capability:docs:mcp:docs:search",
    ]
    assert resolved["builtin_tool_grants"] == ["fetch_capabilities"]
    assert resolved["mcp_tools"] == ["mcp:docs:search"]
    assert resolved["effective_capabilities"]["tool_specs"] == resolved["tool_specs"]
    assert resolved["capability_overlay"]["tool_specs"] == resolved["tool_specs"]
    assert all(item.get("source_type") != "mcp_server" for item in resolved["tool_specs"])
    assert all(item.get("target_tool_ref") != "mcp:github" for item in resolved["tool_specs"])
    docs_search_spec = resolved["tool_specs"][0]
    assert docs_search_spec["name"] == "search"
    assert docs_search_spec["namespace"] == "capability"
    assert docs_search_spec["source_type"] == "mcp_tool"
    assert docs_search_spec["target_tool_ref"] == "mcp:docs:search"
    assert docs_search_spec["permission"]["policy"] == "allow"
    assert docs_search_spec["metadata"]["component_id"] == "mcp:docs:search"
    assert resolved["credentials"] == ["DOCS_TOKEN"]
    assert [component["id"] for component in resolved["components"]] == [
        "mcp:github",
        "skill:code-review",
        "envreq:executable:gitnexus",
        "builtin_tool:fetch_capabilities",
        "mcp:docs:search",
    ]
    assert resolved["capability_overlay"]["component_ids"] == [
        "mcp:github",
        "skill:code-review",
        "envreq:executable:gitnexus",
        "builtin_tool:fetch_capabilities",
        "mcp:docs:search",
    ]
    assert resolved["capability_overlay"]["skill_roots"] == ["/skills/code-review"]
    assert resolved["capability_overlay"]["env"] == {"GITNEXUS_HOME": ".gitnexus"}
    assert resolved["capability_overlay"]["environment_requirements"][0]["id"] == "envreq:executable:gitnexus"
    assert resolved["capability_overlay"]["mcp"]["servers"]["github"]["command"] == "github-mcp-server"
    assert "tools" not in resolved["effective_capabilities"]
    assert resolved["effective_capabilities"]["credentials"] == ["DOCS_TOKEN"]
    assert resolved["effective_capabilities"]["summaries"] == [
        "Fetch documentation evidence for capability package drafts."
    ]
    assert resolved["packages"][2]["effective_capabilities"] == [
        "Fetch documentation evidence for capability package drafts."
    ]
    assert {
        "target": "envreq:executable:gitnexus",
        "target_type": "capability_component",
        "kind": "environment_requirement",
        "policy": "escalate",
        "risk_level": "medium",
        "access": "both",
    } in resolved["effective_capabilities"]["execution_policies"]
    assert "permissions" not in resolved
    assert "permissions" not in resolved["packages"][0]
    assert "permissions" not in resolved["packages"][1]


def test_resolve_capability_refs_uses_activation_state_not_enabled_only() -> None:
    models = _models()
    components = {
        "skill:review": models.CapabilityComponentConfig.from_dict(
            "skill:review",
            {
                "kind": "skill",
                "name": "review",
                "enabled": True,
                "config": {"path_hint": "/skills/review"},
            },
        )
    }
    packages = {
        "review": models.CapabilityPackageConfig.from_dict(
            "review",
            {
                "enabled": True,
                "status": "installed",
                "state": {"activation_state": "inactive"},
                "components": ["skill:review"],
            },
        )
    }

    resolved = models.resolve_capability_refs(["review"], packages, components)

    assert resolved["packages"] == []
    assert resolved["components"] == []
    assert resolved["skills"] == []
    assert resolved["capability_overlay"]["component_ids"] == []


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
