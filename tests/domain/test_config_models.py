from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    AgentRegistryConfig,
    AuthConfig,
    AuthSuperadminConfig,
    Config,
    DIAGNOSTICS_CONFIG_FIELDS,
    DiagnosticsConfig,
    EnvironmentCLIToolConfig,
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
    TOOL_DIAGNOSTICS_CONFIG_FIELDS,
    ToolDiagnosticsConfig,
    infer_provider_compat,
)
from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AgentModelConfig,
)


def test_mcp_server_config_roundtrip() -> None:
    config = MCPServerConfig(
        name="demo",
        command="npx",
        args=["-y", "server"],
        env={"FOO": "bar"},
        cwd="/tmp",
        enabled=False,
    )
    restored = MCPServerConfig.from_dict("demo", config.to_dict())
    assert restored == config


def test_environment_cli_tool_config_roundtrip() -> None:
    config = EnvironmentCLIToolConfig(
        name="gitnexus",
        command="gitnexus",
        tags=["repo_index", "git_graph"],
        check="gitnexus --version",
        install="npm install -g gitnexus",
        version="latest",
        source="npm",
        description="Repository indexing CLI",
    )

    restored = EnvironmentCLIToolConfig.from_dict("gitnexus", config.to_dict())

    assert restored == config


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
        requirements={"node": "required", "npm": "required"},
        build={"type": "node", "package": "@demo/filesystem"},
    )

    restored = MCPServerConfig.from_dict("filesystem", config.to_dict())

    assert restored == config


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
            "requirements": {"node": ">=20", "npm": "required"},
        },
    )

    assert config.distribution == "command"
    assert config.check == "gitnexus --version"
    assert config.install == "npm install -g gitnexus@1.6.3"
    assert config.source == "npm:gitnexus"
    assert config.description == "Repository indexing MCP server"
    assert config.requirements["node"] == ">=20"


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
