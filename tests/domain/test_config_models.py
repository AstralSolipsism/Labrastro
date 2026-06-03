from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    AgentRegistryConfig,
    AuthConfig,
    AuthSuperadminConfig,
    Config,
    DIAGNOSTICS_CONFIG_FIELDS,
    DiagnosticsConfig,
    EnvironmentRequirementConfig,
    LLM_TRACE_DIAGNOSTICS_CONFIG_FIELDS,
    LLMTraceDiagnosticsConfig,
    MCPArtifactConfig,
    MCPLaunchConfig,
    MCPServerConfig,
    MODEL_PROFILE_CONFIG_FIELDS,
    ModeConfig,
    ModelProfileConfig,
    PROVIDER_CONFIG_FIELDS,
    ProviderConfig,
    ProvidersConfig,
    RemoteExecConfig,
    SkillRegistrationConfig,
    TOOL_DIAGNOSTICS_CONFIG_FIELDS,
    ToolDiagnosticsConfig,
    infer_provider_compat,
)
from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AgentModelConfig,
    CapabilityComponentConfig,
    CapabilityPackageConfig,
)
from reuleauxcoder.domain.runtime_footprint import (
    aggregate_runtime_footprint,
    runtime_footprint_for_environment_requirement,
    runtime_footprint_for_mcp,
    runtime_footprint_for_skill,
)


def test_mcp_server_config_roundtrip() -> None:
    config = MCPServerConfig(
        name="demo",
        display_name="Demo MCP",
        summary="Search demo resources.",
        command="npx",
        args=["-y", "server"],
        env={"FOO": "bar"},
        cwd="/tmp",
        enabled=False,
    )
    restored = MCPServerConfig.from_dict("demo", config.to_dict())
    assert restored == config
    assert restored.runtime_footprint["runs_on"] == "server"
    assert restored.runtime_footprint["user_message"] == "服务端运行，无需本机安装"


def test_mcp_server_config_roundtrip_preserves_lifecycle_hooks() -> None:
    config = MCPServerConfig(
        name="audit",
        command="npx",
        args=["audit-mcp"],
        hooks=[
            {
                "event": "PostToolUse",
                "handler_type": "mcp_tool",
                "handler_ref": "audit.record",
                "display_name": "Audit tool result",
                "summary": "Record MCP tool results for review.",
                "permissions": ["audit.write"],
            }
        ],
    )

    restored = MCPServerConfig.from_dict("audit", config.to_dict())

    assert restored.hooks == config.hooks


def test_runtime_footprint_for_mcp_uses_local_peer_runtime() -> None:
    config = MCPServerConfig(
        name="edgeone-pages-mcp-server",
        command="npx",
        args=["edgeone-pages-mcp"],
        runtime_footprint={
            "runs_on": "local_peer",
            "install_required_on": ["local_peer"],
            "config_required_on": ["local_peer"],
        },
    )

    footprint = runtime_footprint_for_mcp(config)

    assert footprint == {
        "runs_on": "local_peer",
        "install_required_on": ["local_peer"],
        "config_required_on": ["local_peer"],
        "user_message": "需要在本机安装/配置",
    }


def test_skill_registration_config_roundtrip_preserves_display_fields() -> None:
    config = SkillRegistrationConfig(
        name="code-review",
        display_name="Code review",
        summary="Review repository changes before merging.",
        path_hint="/srv/skills/code-review/SKILL.md",
        description="Review code changes.",
        environment_requirement_refs=["envreq:executable:gh"],
    )

    restored = SkillRegistrationConfig.from_dict("code-review", config.to_dict())

    assert restored == config
    assert restored.environment_requirement_refs == ["envreq:executable:gh"]
    assert restored.runtime_footprint == {
        "runs_on": "agent_only",
        "install_required_on": [],
        "config_required_on": [],
        "user_message": "仅 Agent 指令能力，无需外部进程",
    }


def test_skill_registration_config_roundtrip_preserves_lifecycle_hooks() -> None:
    config = SkillRegistrationConfig(
        name="code-review",
        hooks=[
            {
                "event": "UserPromptSubmit",
                "handler_type": "prompt",
                "handler_ref": "skills/code-review/SKILL.md",
                "display_name": "Code review prompt context",
                "summary": "Adds code-review skill context to matching prompts.",
                "permissions": [],
            }
        ],
    )

    restored = SkillRegistrationConfig.from_dict("code-review", config.to_dict())

    assert restored.hooks == config.hooks


def test_runtime_footprint_aggregates_components() -> None:
    server_mcp = CapabilityComponentConfig(
        id="mcp:github",
        kind="mcp_server",
        name="github",
        config={"command": "github-mcp-server"},
    )
    local_dependency = CapabilityComponentConfig(
        id="envreq:executable:gh",
        kind="environment_requirement",
        name="gh",
        config={"kind": "executable", "command": "gh", "placement": "peer"},
    )
    skill = CapabilityComponentConfig(
        id="skill:code-review",
        kind="skill",
        name="code-review",
    )

    aggregate = aggregate_runtime_footprint([
        server_mcp.runtime_footprint,
        local_dependency.runtime_footprint,
        skill.runtime_footprint,
    ])

    assert runtime_footprint_for_mcp(server_mcp)["runs_on"] == "server"
    assert runtime_footprint_for_environment_requirement(local_dependency)["runs_on"] == "local_peer"
    assert runtime_footprint_for_skill(skill)["runs_on"] == "agent_only"
    assert aggregate == {
        "runs_on": "both",
        "install_required_on": ["server", "local_peer"],
        "config_required_on": ["server", "local_peer"],
        "user_message": "服务端和本地端都需要配置",
    }


def test_runtime_footprint_for_skill_aggregates_related_requirements() -> None:
    skill = SkillRegistrationConfig(
        name="review-with-gh",
        environment_requirement_refs=["envreq:executable:gh"],
    )
    local_dependency = EnvironmentRequirementConfig(
        id="envreq:executable:gh",
        kind="executable",
        name="gh",
        command="gh",
        placement="peer",
    )

    footprint = runtime_footprint_for_skill(skill, [local_dependency])

    assert footprint == {
        "runs_on": "local_peer",
        "install_required_on": ["local_peer"],
        "config_required_on": ["local_peer"],
        "user_message": "需要在本机安装/配置",
    }


def test_capability_package_config_roundtrip_preserves_runtime_footprint() -> None:
    package = CapabilityPackageConfig(
        id="review",
        components=["mcp:github", "envreq:executable:gh"],
        runtime_footprint={
            "runs_on": "both",
            "install_required_on": ["server", "local_peer"],
            "config_required_on": ["server", "local_peer"],
            "user_message": "服务端和本地端都需要配置",
        },
    )

    restored = CapabilityPackageConfig.from_dict("review", package.to_dict())

    assert restored == package


def test_capability_component_and_package_config_roundtrip_preserve_lifecycle_hooks() -> None:
    component = CapabilityComponentConfig(
        id="skill:review",
        kind="skill",
        name="review",
        hooks=[
            {
                "event": "PreToolUse",
                "handler_type": "agent",
                "handler_ref": "agent:review-policy",
                "display_name": "Review tool policy",
                "summary": "Checks tool use against review policy.",
                "permissions": [],
            }
        ],
    )
    package = CapabilityPackageConfig(
        id="review-pack",
        components=["skill:review"],
        hooks=[
            {
                "event": "SessionStart",
                "handler_type": "prompt",
                "handler_ref": "package:review-pack/session-start",
                "display_name": "Review package startup",
                "summary": "Adds review package startup context.",
                "permissions": [],
            }
        ],
    )

    restored_component = CapabilityComponentConfig.from_dict(
        "skill:review",
        component.to_dict(),
    )
    restored_package = CapabilityPackageConfig.from_dict(
        "review-pack",
        package.to_dict(),
    )

    assert restored_component.hooks == component.hooks
    assert restored_package.hooks == package.hooks


def test_environment_requirement_config_roundtrip() -> None:
    config = EnvironmentRequirementConfig(
        id="envreq:executable:gitnexus",
        kind="executable",
        name="gitnexus",
        command="gitnexus",
        tags=["repo_index", "git_graph"],
        check="gitnexus --version",
        install="npm install -g gitnexus",
        version="latest",
        source="npm",
        description="Repository indexing CLI",
    )

    restored = EnvironmentRequirementConfig.from_dict(
        "envreq:executable:gitnexus",
        config.to_dict(),
    )

    assert restored == config
    assert restored.runtime_footprint == {
        "runs_on": "local_peer",
        "install_required_on": ["local_peer"],
        "config_required_on": ["local_peer"],
        "user_message": "需要在本机安装/配置",
    }


def test_environment_requirement_config_defaults_missing_kind_to_runtime() -> None:
    config = EnvironmentRequirementConfig.from_dict(
        "node",
        {"name": "node"},
    )

    assert config.id == "envreq:runtime:node"
    assert config.kind == "runtime"


def test_environment_requirement_config_rejects_invalid_explicit_kind() -> None:
    try:
        EnvironmentRequirementConfig.from_dict(
            "node",
            {"name": "node", "kind": "runttime"},
        )
    except ValueError as exc:
        assert "environment requirement kind" in str(exc)
    else:
        raise AssertionError("invalid explicit environment requirement kind was accepted")


def test_environment_requirement_config_rejects_invalid_id_kind() -> None:
    try:
        EnvironmentRequirementConfig.from_dict(
            "envreq:runttime:node",
            {"name": "node"},
        )
    except ValueError as exc:
        assert "environment requirement kind" in str(exc)
    else:
        raise AssertionError("invalid environment requirement id kind was accepted")


def test_environment_requirement_config_rejects_invalid_id_kind_with_explicit_kind() -> None:
    try:
        EnvironmentRequirementConfig.from_dict(
            "envreq:runttime:node",
            {"id": "envreq:runttime:node", "name": "node", "kind": "runtime"},
        )
    except ValueError as exc:
        assert "environment requirement kind" in str(exc)
    else:
        raise AssertionError("invalid environment requirement id kind was accepted")


def test_peer_mcp_server_config_roundtrip() -> None:
    config = MCPServerConfig(
        name="filesystem",
        command="",
        placement="peer",
        distribution="artifact",
        version="1.0.0",
        launch=MCPLaunchConfig(
            command="{{bundle}}/filesystem-mcp",
            args=["--root", "{{workspace}}"],
            env={"MODE": "local"},
        ),
        artifacts={
            "linux-amd64": MCPArtifactConfig(
                path="filesystem/1.0.0/linux-amd64.tar.gz",
                sha256="abc",
                launch=MCPLaunchConfig(command="{{bundle}}/run.sh"),
            )
        },
        permissions={"tools": {"write_file": "require_approval"}},
        environment_requirement_refs=["envreq:runtime:node", "envreq:executable:npm"],
        build={"type": "node", "package": "@demo/filesystem"},
    )

    restored = MCPServerConfig.from_dict("filesystem", config.to_dict())

    assert restored == config
    assert restored.placement == "peer"
    assert restored.runtime_footprint == {
        "runs_on": "local_peer",
        "install_required_on": ["local_peer"],
        "config_required_on": ["local_peer"],
        "user_message": "需要在本机安装/配置",
    }


def test_legacy_peer_mcp_with_artifacts_defaults_to_artifact_distribution() -> None:
    config = MCPServerConfig.from_dict(
        "filesystem",
        {
            "command": "",
            "placement": "peer",
            "version": "1.0.0",
            "artifacts": {
                "linux-amd64": {
                    "path": "filesystem/1.0.0/linux-amd64.tar.gz",
                    "sha256": "abc",
                }
            },
        },
    )

    assert config.distribution == "artifact"


def test_mcp_server_config_reads_manifest_fields() -> None:
    config = MCPServerConfig.from_dict(
        "gitnexus",
        {
            "command": "gitnexus",
            "args": ["mcp"],
            "placement": "peer",
            "distribution": "command",
            "check": "gitnexus --version",
            "install": "npm install -g gitnexus@1.6.3",
            "source": "npm:gitnexus",
            "description": "Repository indexing MCP server",
            "environment_requirement_refs": [
                "envreq:runtime:node",
                "envreq:executable:npm",
            ],
        },
    )

    assert config.distribution == "command"
    assert config.check == "gitnexus --version"
    assert config.install == "npm install -g gitnexus@1.6.3"
    assert config.source == "npm:gitnexus"
    assert config.description == "Repository indexing MCP server"
    assert config.environment_requirement_refs == [
        "envreq:runtime:node",
        "envreq:executable:npm",
    ]


def test_mcp_server_config_accepts_both_placement() -> None:
    config = MCPServerConfig.from_dict(
        "browser",
        {
            "command": "npx",
            "args": ["-y", "@demo/browser@1.2.3"],
            "placement": "both",
            "version": "1.2.3",
        },
    )

    assert config.placement == "both"
    assert config.runtime_footprint == {
        "runs_on": "both",
        "install_required_on": ["server", "local_peer"],
        "config_required_on": ["server", "local_peer"],
        "user_message": "服务端和本地端都需要配置",
    }


def test_model_profile_config_from_dict_uses_defaults() -> None:
    profile = ModelProfileConfig.from_dict("main", {})
    assert profile.name == "main"
    assert profile.model == ""
    assert profile.provider is None
    assert profile.max_tokens == 0
    assert profile.max_context_tokens == 0
    assert profile.temperature == 0.0
    assert profile.preserve_reasoning_content is True
    assert profile.backfill_reasoning_content_for_tool_calls is False


def test_model_profile_config_reads_provider_reference() -> None:
    profile = ModelProfileConfig.from_dict(
        "main",
        {
            "model": "claude",
            "provider": "anthropic-main",
            "max_tokens": 8192,
            "max_context_tokens": 200000,
        },
    )

    assert profile.provider == "anthropic-main"
    assert profile.max_context_tokens == 200000


def test_model_profile_config_only_materializes_current_fields() -> None:
    profile = ModelProfileConfig.from_dict(
        "main",
        {
            "model": "claude",
            "provider": "anthropic-main",
            "api_key": "sk-old",
            "base_url": "https://api.example.test/v1",
        },
    )

    assert profile.model == "claude"
    assert profile.provider == "anthropic-main"
    assert not hasattr(profile, "api_key")
    assert not hasattr(profile, "base_url")


def test_model_profile_config_fields_match_serialized_shape() -> None:
    assert set(ModelProfileConfig(name="demo").to_dict()) == set(
        MODEL_PROFILE_CONFIG_FIELDS
    )


def test_provider_config_roundtrip() -> None:
    config = ProviderConfig(
        id="anthropic-main",
        type="anthropic_messages",
        api_key="sk-ant",
        base_url="https://api.anthropic.com",
        headers={"X-Demo": "yes"},
        timeout_sec=90,
        max_retries=2,
    )

    restored = ProviderConfig.from_dict("anthropic-main", config.to_dict())

    assert restored == config


def test_provider_config_fields_match_serialized_shape() -> None:
    assert set(ProviderConfig(id="demo").to_dict()) == set(PROVIDER_CONFIG_FIELDS)


def test_diagnostics_config_fields_match_serialized_shape() -> None:
    assert set(DiagnosticsConfig().to_dict()) == set(DIAGNOSTICS_CONFIG_FIELDS)
    assert set(LLMTraceDiagnosticsConfig().to_dict()) == set(
        LLM_TRACE_DIAGNOSTICS_CONFIG_FIELDS
    )
    assert set(ToolDiagnosticsConfig().to_dict()) == set(
        TOOL_DIAGNOSTICS_CONFIG_FIELDS
    )


def test_provider_config_reads_and_infers_compat() -> None:
    explicit = ProviderConfig.from_dict(
        "kimi", {"type": "openai_chat", "compat": "kimi"}
    )
    inferred = ProviderConfig.from_dict(
        "deepseek",
        {"type": "openai_chat", "base_url": "https://api.deepseek.com"},
    )

    assert explicit.compat == "kimi"
    assert inferred.compat == "deepseek"
    assert infer_provider_compat("https://dashscope.aliyuncs.com/compatible-mode/v1") == "qwen"


def test_provider_config_accepts_labrastro_server_provider() -> None:
    provider = ProviderConfig.from_dict(
        "labrastro-server",
        {"type": "labrastro_server", "enabled": True},
    )

    assert provider.type == "labrastro_server"
    assert provider.api_key == ""


def test_mode_config_from_dict_normalizes_invalid_fields() -> None:
    mode = ModeConfig.from_dict(
        "coder",
        {
            "description": None,
            "tools": ["shell", 123],
            "prompt_append": None,
        },
    )
    assert mode.name == "coder"
    assert mode.description == ""
    assert mode.tools == ["shell", "123"]
    assert mode.prompt_append == ""


def test_config_validate_collects_multiple_errors() -> None:
    config = Config(
        tool_output_max_chars=0,
        tool_output_max_lines=0,
        active_main_model_profile="missing-main",
        active_sub_model_profile="missing-sub",
        active_mode="missing-mode",
        model_profiles={
            "bad": ModelProfileConfig(
                name="bad",
                model="gpt",
                max_tokens=0,
                temperature=5.0,
                max_context_tokens=0,
            )
        },
        modes={"coder": ModeConfig(name="coder")},
        approval=ApprovalConfig(
            default_mode="invalid",  # type: ignore[arg-type]
            rules=[ApprovalRuleConfig(action="invalid")],  # type: ignore[arg-type]
        ),
    )

    errors = config.validate()

    assert "tool_output_max_chars must be positive" in errors
    assert "tool_output_max_lines must be positive" in errors
    assert "active_main_model_profile must exist in model_profiles" in errors
    assert "active_sub_model_profile must exist in model_profiles" in errors
    assert "active_mode must exist in modes" in errors
    assert "models.profiles.bad.provider is required" in errors
    assert "models.profiles.bad.max_tokens must be positive" in errors
    assert "models.profiles.bad.max_context_tokens must be positive" in errors
    assert "models.profiles.bad.temperature must be between 0 and 2" in errors
    assert (
        "approval.default_mode must be one of allow, warn, require_approval, deny"
        in errors
    )
    assert (
        "approval.rules[0].action must be one of allow, warn, require_approval, deny"
        in errors
    )


def test_config_validate_accepts_provider_backed_profile_without_profile_api_key() -> None:
    config = Config(
        providers=ProvidersConfig(
            items={
                "anthropic-main": ProviderConfig(
                    id="anthropic-main",
                    type="anthropic_messages",
                    api_key="sk-ant",
                )
            }
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                model="claude",
                provider="anthropic-main",
                max_tokens=8192,
                max_context_tokens=200000,
            )
        },
        active_main_model_profile="main",
    )

    assert config.validate() == []


def test_config_validate_rejects_missing_profile_provider_reference() -> None:
    config = Config(
        providers=ProvidersConfig(),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                model="claude",
                provider="missing",
                max_tokens=8192,
                max_context_tokens=200000,
            )
        },
    )

    errors = config.validate()

    assert "models.profiles.main.provider references unknown provider" in errors


def test_config_validate_accepts_agent_default_model_provider_reference() -> None:
    config = Config(
        providers=ProvidersConfig(
            items={
                "deepseek": ProviderConfig(
                    id="deepseek",
                    type="openai_chat",
                    api_key="sk-ds",
                )
            }
        ),
        agent_registry=AgentRegistryConfig(
            agents={
                "coder": AgentConfig(
                    id="coder",
                    model=AgentModelConfig(provider="deepseek", model="V4PRO"),
                )
            }
        ),
    )

    assert config.validate() == []


def test_config_validate_rejects_missing_agent_default_model_provider() -> None:
    config = Config(
        providers=ProvidersConfig(),
        agent_registry=AgentRegistryConfig(
            agents={
                "coder": AgentConfig(
                    id="coder",
                    model=AgentModelConfig(provider="missing", model="V4PRO"),
                )
            }
        ),
    )

    errors = config.validate()

    assert "agent_registry.agents[coder].model.provider must exist in providers.items" in errors


def test_config_is_valid_for_minimal_valid_configuration() -> None:
    config = Config(
        approval=ApprovalConfig(default_mode="allow"),
    )
    assert config.is_valid() is True


def test_sandbox_provider_config_defaults_and_validation() -> None:
    config = Config()

    assert config.sandbox_provider.type == "none"
    assert config.sandbox_provider.worker_image == "labrastro-host:test"
    assert config.sandbox_provider.workspace_volume_root == "labrastro-workspaces"
    assert config.validate() == []


def test_config_validate_allows_remote_host_without_model_key() -> None:
    config = Config(
        remote_exec=RemoteExecConfig(enabled=True, host_mode=True),
        auth=AuthConfig(
            enabled=True,
            token_secret="test-secret",
            superadmins=[
                AuthSuperadminConfig(
                    username="admin",
                    password="plain-admin-password",
                )
            ],
        ),
    )

    assert "api_key is required" not in config.validate()


def test_config_validate_allows_unconfigured_llm_outside_remote_host() -> None:
    config = Config()

    assert config.validate() == []

    invalid = Config()
    invalid.sandbox_provider.type = "bad"
    invalid.sandbox_provider.idle_ttl_seconds = 0

    errors = invalid.validate()
    assert "sandbox_provider.type must be one of docker, external, k8s, none" in errors
    assert "sandbox_provider.idle_ttl_seconds must be positive" in errors


def test_config_has_no_flat_llm_runtime_fields() -> None:
    config = Config()
    for field in (
        "model",
        "api_key",
        "base_url",
        "max_tokens",
        "max_context_tokens",
        "temperature",
    ):
        assert not hasattr(config, field)


def test_remote_exec_config_defaults() -> None:
    config = Config()
    assert isinstance(config.remote_exec, RemoteExecConfig)
    assert config.remote_exec.enabled is False
    assert config.remote_exec.host_mode is False
    assert config.remote_exec.relay_bind == "127.0.0.1:8765"
    assert config.auth.enabled is False
