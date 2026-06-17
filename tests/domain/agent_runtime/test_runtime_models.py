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
            "callable_scopes": ["ephemeral", "persistent"],
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
    assert agent.can_call_ephemeral is True
    assert agent.can_call_persistent is True
    assert agent.can_run_taskflow is True
    assert agent.runtime_profile == "codex_remote"
    assert agent.dispatch.profile.startswith("适合审查代码风险")
    assert agent.dispatch.examples == ["审查后端运行时变更", "检查 PR 的测试覆盖"]
    assert agent.dispatch.avoid == ["线上发布操作"]
    assert agent.capability_refs == ["github-review"]
    assert agent.prompt.agent_md == ".agents/code_reviewer/AGENT.md"
    assert "风险" in agent.prompt.system_append
    assert agent.max_concurrent_tasks == 2


def test_agent_config_rejects_removed_delegable_field() -> None:
    models = _models()

    with pytest.raises(ValueError, match="callable_scopes"):
        models.AgentConfig.from_dict(
            "legacy_agent",
            {
                "runtime_profile": "codex_remote",
                "delegable": True,
            },
        )


def test_agent_config_rejects_removed_callable_modes_field() -> None:
    models = _models()

    with pytest.raises(ValueError, match="callable_scopes"):
        models.AgentConfig.from_dict(
            "legacy_agent",
            {
                "runtime_profile": "codex_remote",
                "callable_modes": ["delegate"],
            },
        )


def test_agent_run_activation_feedback_models_keep_lifecycle_boundaries() -> None:
    models = _models()

    run = models.AgentRun(
        id="run-1",
        agent_id="reviewer",
        owner_session_run_id="session-1",
        status="waiting",
        waiting_reason="server_processing",
        resume_policy="automatic",
        current_activation_id="act-1",
    )
    activation = models.AgentRunActivation(
        id="act-1",
        agent_run_id="run-1",
        seq=1,
        input_kind="user_request",
        prompt="inspect the repository",
        worker_id="worker-1",
        request_id="request-1",
    )
    feedback = models.AgentRunFeedback(
        id="feedback-1",
        agent_run_id="run-1",
        source="server",
        kind="candidate_validation_failed",
        payload={"field": "components"},
        requires_activation=True,
    )
    relation = models.AgentRunRelation(
        id="relation-1",
        owner_agent_run_id="run-1",
        related_agent_run_id="run-2",
        relation_type="agent_call_ephemeral",
        relation_scope="session",
        created_by_activation_id="act-1",
        payload={"conversation_scope": "ephemeral", "wait": True},
    )
    binding = models.AgentThreadBinding(
        id="binding-1",
        owner_session_run_id="session-1",
        main_agent_run_id="run-1",
        agent_id="project_researcher",
        target_agent_run_id="run-2",
        thread_key="backend",
        thread_summary="后端调查线程",
        binding_lifetime="session",
    )

    assert run.status == models.AgentRunStatus.WAITING
    assert run.waiting_reason == models.AgentRunWaitingReason.SERVER_PROCESSING
    assert not hasattr(run, "prompt")
    assert not hasattr(run, "output")
    assert not hasattr(run, "worker_id")
    assert not hasattr(run, "issue_id")
    assert activation.input_kind == models.AgentRunActivationInputKind.USER_REQUEST
    assert activation.prompt == "inspect the repository"
    assert activation.worker_id == "worker-1"
    assert feedback.kind == models.AgentRunFeedbackKind.CANDIDATE_VALIDATION_FAILED
    assert feedback.requires_activation is True
    assert feedback.payload == {"field": "components"}
    assert relation.relation_type == models.AgentRunRelationType.AGENT_CALL_EPHEMERAL
    assert relation.relation_scope == "session"
    assert binding.target_agent_run_id == "run-2"
    assert binding.thread_key == "backend"
    assert binding.binding_lifetime == models.AgentThreadBindingLifetime.SESSION


def test_agent_run_relation_contract_uses_typed_payloads_for_decision_fields() -> None:
    models = _models()

    relation = models.AgentRunRelation(
        id="relation-branch",
        owner_agent_run_id="run-base",
        related_agent_run_id="run-branch",
        relation_type="branch",
        payload={
            "source_agent_run_id": "run-base",
            "target_agent_run_id": "run-branch",
            "base_session_item_id": "session-item-12",
            "base_git_ref": "refs/heads/main",
            "base_tree_ref": "tree:base",
            "branch_name": "agent-run/run-branch",
            "branch_git_ref": "refs/heads/agent-run/run-branch",
            "branch_worktree_ref": "worktree:run-branch",
            "permission_recompute_policy": "recompute_or_reject",
            "reuse_live_executor_session": False,
            "cleanup_policy": "delete_with_owner_session",
            "runtime_root": "runtime",
            "source_workspace_root": "workspace:source",
        },
    )

    assert relation.relation_type == models.AgentRunRelationType.BRANCH
    assert relation.payload["base_session_item_id"] == "session-item-12"
    assert relation.payload["reuse_live_executor_session"] is False
    assert relation.metadata == {}


@pytest.mark.parametrize(
    "relation_type,payload",
    [
        (
            "agent_call_ephemeral",
            {
                "conversation_scope": "ephemeral",
                "wait": True,
                "parent_session_id": "session-1",
            },
        ),
        (
            "agent_call_persistent",
            {
                "conversation_scope": "persistent",
                "wait": False,
                "thread_key": "backend",
                "thread_summary": "Backend thread",
                "parent_session_id": "session-1",
                "workspace_root": "G:/AboutDEV/EZCode/Labrastro",
            },
        ),
        (
            "branch",
            {
                "source_agent_run_id": "run-base",
                "target_agent_run_id": "run-branch",
                "base_session_item_id": "session-item-12",
                "base_git_ref": "refs/heads/main",
                "base_tree_ref": "tree:base",
                "branch_name": "agent-run/run-branch",
                "branch_git_ref": "refs/heads/agent-run/run-branch",
                "branch_worktree_ref": "worktree:run-branch",
                "permission_recompute_policy": "recompute_or_reject",
                "reuse_live_executor_session": False,
                "cleanup_policy": "delete_with_owner_session",
                "runtime_root": "runtime",
                "source_workspace_root": "workspace:source",
            },
        ),
        (
            "fork",
                {
                    "source_agent_run_id": "run-base",
                    "target_agent_run_id": "run-fork",
                    "base_session_item_id": "session-item-12",
                    "fork_workspace_ref": "workspace:fork",
                    "target_owner_session_run_id": "session-target",
                    "permission_recompute_policy": "recompute_or_reject",
                    "reuse_live_executor_session": False,
                    "cleanup_policy": "delete_with_owner_session",
                "provenance_status": "visible",
            },
        ),
        (
            "review",
            {
                "review_kind": "lifecycle_hook",
                "lifecycle_hook_id": "hook:lifecycle-agent-review",
                "parent_session_id": "session-1",
                "parent_turn_id": "turn-1",
            },
        ),
        (
            "diagnostic_probe",
            {
                "probe_kind": "environment",
                "parent_session_id": "session-1",
            },
        ),
    ],
)
def test_agent_run_relation_accepts_required_typed_payloads(
    relation_type: str,
    payload: dict[str, object],
) -> None:
    models = _models()

    relation = models.AgentRunRelation(
        id=f"relation-{relation_type}",
        owner_agent_run_id="run-1",
        related_agent_run_id="run-2",
        relation_type=relation_type,
        payload=payload,
    )

    assert relation.payload == payload


@pytest.mark.parametrize(
    "metadata_key",
    [
        "conversation_scope",
        "wait",
        "thread_key",
        "thread_summary",
        "parent_session_id",
        "parent_turn_id",
        "workspace_root",
        "lifecycle_hook_id",
        "base_session_item_id",
        "base_git_ref",
        "branch_name",
        "branch_git_ref",
        "branch_worktree_ref",
        "fork_workspace_ref",
        "reuse_live_executor_session",
        "permission_recompute_policy",
        "cleanup_policy",
        "runtime_root",
        "source_workspace_root",
    ],
)
def test_agent_run_relation_rejects_decision_fields_in_metadata(
    metadata_key: str,
) -> None:
    models = _models()

    with pytest.raises(ValueError, match="AgentRunRelation.metadata"):
        models.AgentRunRelation(
            id="relation-1",
            owner_agent_run_id="run-1",
            related_agent_run_id="run-2",
            relation_type="agent_call_persistent",
            metadata={metadata_key: "not-allowed"},
        )


def test_activation_steer_contract_supports_delivering_and_idempotency_metadata() -> None:
    models = _models()

    steer = models.ActivationSteer(
        id="steer-1",
        activation_id="activation-1",
        source="user",
        status="delivering",
        payload={
            "items": [
                {"type": "text", "text": "先查影响范围"},
                {"type": "artifact_ref", "artifact_id": "artifact-1"},
            ]
        },
        metadata={
            "client_steer_id": "client-1",
            "idempotency_key": "client-1",
            "sender": "user-1",
        },
    )

    assert steer.status == models.ActivationSteerStatus.DELIVERING
    assert steer.payload["items"][0]["type"] == "text"
    assert steer.metadata["idempotency_key"] == "client-1"


def test_agent_run_rejects_taskflow_business_identity_in_metadata() -> None:
    models = _models()

    with pytest.raises(ValueError, match="AgentRun.metadata"):
        models.AgentRun(
            id="run-1",
            agent_id="reviewer",
            metadata={
                "issue_id": "issue-1",
                "trigger_comment_id": "comment-1",
                "assignment_id": "assignment-1",
            },
        )


def test_agent_run_has_no_record_alias_for_business_identity_metadata() -> None:
    models = _models()

    with pytest.raises(ValueError, match="AgentRun.metadata"):
        models.AgentRun(
            id="run-1",
            agent_id="reviewer",
            metadata={"issue_id": "issue-1"},
        )


def test_agent_callable_projection_and_grant_use_agent_id_and_scopes() -> None:
    models = _models()

    projection = models.AgentCallableProjectionEntry(
        agent_id="project_researcher",
        display_name="Project Researcher",
        exposure="deferred",
        callable_scopes=["persistent", "ephemeral"],
        authorization_status="allowed",
        existing_threads=[
            {
                "thread_key": "backend",
                "summary": "后端调查线程",
                "status": "active",
                "binding_id": "binding-1",
                "pending_results_count": 0,
            }
        ],
        capability_scope={"packages": ["repo-read"]},
    )
    grant = models.AgentCallGrant(
        user_id="user-1",
        grant_scope="workspace:demo",
        main_agent_id="main_chat",
        target_agent_id="project_researcher",
        conversation_scope="persistent",
        capability_scope={"packages": ["repo-read"]},
        target_config_version="v1",
    )

    assert projection.agent_id == "project_researcher"
    assert projection.callable_scopes == ["persistent", "ephemeral"]
    assert projection.authorization_status == (
        models.AgentCallableAuthorizationStatus.ALLOWED
    )
    assert projection.existing_threads[0]["thread_key"] == "backend"
    assert grant.conversation_scope == "persistent"
    assert grant.grant_scope == "workspace:demo"
    assert grant.target_agent_id == "project_researcher"


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
        "capability:docs:builtin_tool:fetch_capabilities",
        "capability:docs:mcp:docs:search",
    ]
    assert resolved["builtin_tool_grants"] == ["fetch_capabilities"]
    assert resolved["mcp_tools"] == ["mcp:docs:search"]
    assert resolved["effective_capabilities"]["tool_specs"] == resolved["tool_specs"]
    assert resolved["capability_overlay"]["tool_specs"] == resolved["tool_specs"]
    assert all(item.get("source_type") != "mcp_server" for item in resolved["tool_specs"])
    assert all(item.get("target_tool_ref") != "mcp:github" for item in resolved["tool_specs"])
    fetch_tool_spec = resolved["tool_specs"][0]
    assert fetch_tool_spec["name"] == "fetch_capabilities"
    assert fetch_tool_spec["namespace"] == "capability"
    assert fetch_tool_spec["source_type"] == "builtin_tool"
    assert fetch_tool_spec["target_tool_ref"] == "builtin:fetch_capabilities"
    assert fetch_tool_spec["permission"]["policy"] == "allow"
    assert fetch_tool_spec["metadata"]["component_id"] == "builtin_tool:fetch_capabilities"
    docs_search_spec = resolved["tool_specs"][1]
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


def test_external_mcp_tools_contribution_maps_to_internal_tool_spec_projection() -> None:
    models = _models()
    optional_features = [
        {
            "id": "optional:readability",
            "title": "Readability extraction",
            "placement": "server",
            "default_selected": False,
            "selection_scope": "user",
        }
    ]
    draft = models.CapabilityPackageDraft.from_dict(
        "github-tools",
        {
            "id": "github-tools",
            "name": "GitHub Tools",
            "contributions": {
                "mcp_servers": [
                    {
                        "id": "mcp:github",
                        "kind": "mcp_server",
                        "name": "github",
                        "config": {"command": "github-mcp-server"},
                    }
                ],
                "mcp_tools": [
                    {
                        "id": "mcp:github:search",
                        "kind": "mcp_tool",
                        "name": "search",
                        "registry_path": "mcp:github:search",
                    }
                ],
            },
            "optional_features": optional_features,
            "install_plan": [],
            "usage": [],
            "evidence": [],
            "risk_level": "low",
        },
    )

    components = {
        item["id"]: models.CapabilityComponentConfig.from_dict(item["id"], item)
        for item in draft.components
    }
    packages = {
        "github-tools": models.CapabilityPackageConfig.from_dict(
            "github-tools",
            {
                "components": [item["id"] for item in draft.components],
            },
        )
    }

    resolved = models.resolve_capability_refs(["github-tools"], packages, components)

    assert draft.optional_features == optional_features
    assert [item["kind"] for item in draft.components] == ["mcp_server", "mcp_tool"]
    assert draft.components[1]["id"] == "mcp:github:search"
    assert draft.components[1]["registry_path"] == "mcp:github:search"
    assert resolved["mcp_servers"] == ["github"]
    assert resolved["mcp_tools"] == ["mcp:github:search"]
    assert "tools" not in resolved
    assert [item["target_tool_ref"] for item in resolved["tool_specs"]] == [
        "mcp:github:search"
    ]
    assert resolved["tool_specs"][0]["source_type"] == "mcp_tool"
    assert resolved["effective_capabilities"]["tool_specs"] == resolved["tool_specs"]


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

    task = models.AgentRun(
        id="task-1",
        agent_id="code_reviewer",
        trigger_mode=models.TriggerMode.ISSUE_TASK,
        status=models.AgentRunStatus.COMPLETED,
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
    assert task.status == models.AgentRunStatus.COMPLETED
    assert artifact.status == models.ArtifactStatus.PR_REVIEWING
    assert artifact.status != models.ArtifactStatus.MERGED
    assert artifact.requires_user_merge is True


def test_non_code_task_allows_report_artifact_without_branch_or_pr() -> None:
    models = _models()

    task = models.AgentRun(
        id="task-2",
        agent_id="researcher",
        trigger_mode=models.TriggerMode.ISSUE_TASK,
        status=models.AgentRunStatus.COMPLETED,
    )
    artifact = models.TaskArtifact(
        id="artifact-2",
        task_id="task-2",
        type=models.ArtifactType.REPORT,
        status=models.ArtifactStatus.GENERATED,
        content="调研结论",
    )

    assert task.status == models.AgentRunStatus.COMPLETED
    assert artifact.type == models.ArtifactType.REPORT
    assert artifact.branch_name is None
    assert artifact.pr_url is None
    assert artifact.requires_user_merge is False
