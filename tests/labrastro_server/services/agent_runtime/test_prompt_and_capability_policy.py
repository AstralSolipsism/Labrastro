from __future__ import annotations

import importlib


def _prompt_renderer():
    return importlib.import_module("labrastro_server.services.agent_runtime.prompt_renderer")


def _policy():
    return importlib.import_module("labrastro_server.services.agent_runtime.policy")


def test_prompt_renderer_targets_executor_native_instruction_files() -> None:
    renderer_module = _prompt_renderer()

    context = renderer_module.CanonicalAgentContext(
        agent_id="code_reviewer",
        agent_name="Code Reviewer",
        agent_md=".agents/code_reviewer/AGENT.md",
        system_append="你专注于发现风险、回归和缺失测试。",
        dispatch={
            "profile": "适合审查代码风险、阅读仓库并指出缺失测试。",
            "examples": ["审查后端运行时变更"],
            "avoid": ["线上发布操作"],
        },
        capability_refs=["github-review"],
        resolved_capabilities={
            "packages": [{"id": "github-review", "name": "GitHub Review"}],
            "mcp_servers": ["github"],
            "skills": ["code-review"],
            "environment_requirements": [
                {
                    "id": "envreq:executable:gitnexus",
                    "kind": "executable",
                    "name": "gitnexus",
                }
            ],
        },
    )

    codex = renderer_module.ExecutorPromptRenderer().render("codex", context)
    claude = renderer_module.ExecutorPromptRenderer().render("claude", context)
    gemini = renderer_module.ExecutorPromptRenderer().render("gemini", context)

    assert "AGENTS.md" in codex.files
    assert "CLAUDE.md" not in codex.files
    assert "CLAUDE.md" in claude.files
    assert "GEMINI.md" in gemini.files
    assert "Code Reviewer" in codex.files["AGENTS.md"]
    assert "风险" in claude.files["CLAUDE.md"]
    assert "Dispatch Profile" in gemini.files["GEMINI.md"]
    assert "审查后端运行时变更" in gemini.files["GEMINI.md"]
    assert "GitHub Review" in gemini.files["GEMINI.md"]
    assert "gitnexus" in gemini.files["GEMINI.md"]
    assert "Permissions" not in gemini.files["GEMINI.md"]


def test_prompt_renderer_does_not_render_raw_secret_values() -> None:
    renderer_module = _prompt_renderer()

    context = renderer_module.CanonicalAgentContext(
        agent_id="unsafe",
        agent_name="Unsafe",
        system_append="use token sk-should-not-render",
        credential_refs={"model": "cred_codex_team"},
    )

    rendered = renderer_module.ExecutorPromptRenderer().render("codex", context)

    assert "cred_codex_team" in rendered.metadata["credential_refs"].values()
    assert "sk-should-not-render" not in str(rendered.files)


def test_prompt_renderer_does_not_render_full_capability_tool_specs() -> None:
    renderer_module = _prompt_renderer()

    context = renderer_module.CanonicalAgentContext(
        agent_id="reviewer",
        agent_name="Reviewer",
        capability_refs=["review"],
        resolved_capabilities={
            "packages": [{"id": "review", "name": "Review"}],
            "tool_specs": [
                {
                    "tool_id": "capability:review:lookup",
                    "name": "lookup",
                    "namespace": "capability",
                    "description": "Lookup review context.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query.",
                            }
                        },
                    },
                }
            ],
        },
    )

    rendered = renderer_module.ExecutorPromptRenderer().render("codex", context)
    text = rendered.files["AGENTS.md"]

    assert "Review" in text
    assert "capability:review:lookup" not in text
    assert "input_schema" not in text
    assert "query" not in text


def test_platform_mcp_policy_allows_only_agent_declared_servers() -> None:
    policy_module = _policy()

    effective = policy_module.PlatformMCPPolicy(
        platform_servers={
            "github": {"command": "github-mcp", "tools": ["create_pr", "comment"]},
            "filesystem": {"command": "filesystem-mcp", "tools": ["apply_patch"]},
        },
        allowed_servers=["github"],
    ).render_for_agent({"servers": ["github", "filesystem"]})

    assert list(effective["servers"].keys()) == ["github"]
    assert effective["servers"]["github"]["tools"] == ["create_pr", "comment"]
    assert "filesystem" not in effective["servers"]


def test_capability_package_policy_blocks_ungranted_package() -> None:
    policy_module = _policy()

    policy = policy_module.CapabilityPackagePolicy(
        available_packages=["repo-read", "issue-comment", "create-pr"],
        granted_packages=["repo-read", "issue-comment"],
    )

    assert policy.allows("repo-read") is True
    assert policy.allows("issue-comment") is True
    assert policy.allows("create-pr") is False
    assert policy.explain_denial("create-pr") == "capability package not granted to agent"
