from __future__ import annotations

import pytest

from labrastro_server.interfaces.http.remote.protocol import (
    EnvironmentManifestResponse,
    EnvironmentRequirementManifest,
)
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
)
from labrastro_server.services.environment_run import (
    EnvironmentRunError,
    EnvironmentRunService,
)


def _control(*, agents: dict | None = None) -> AgentRunControlPlane:
    return AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "environment_local": {
                    "executor": "reuleauxcoder",
                    "execution_location": "local_workspace",
                }
            },
            "agents": agents
            or {
                "environment_configurator": {
                    "runtime_profile": "environment_local",
                    "capability_refs": ["environment"],
                    "resolved_capabilities": {},
                }
            },
        }
    )


def _manifest() -> EnvironmentManifestResponse:
    return EnvironmentManifestResponse(
        environment_requirements=[
            EnvironmentRequirementManifest(
                id="envreq:executable:gitnexus",
                kind="executable",
                name="gitnexus",
                command="gitnexus",
                check="gitnexus --version",
                install="npm install -g gitnexus",
            ),
            EnvironmentRequirementManifest(
                id="envreq:credential:github-token",
                kind="credential",
                name="github-token",
                description="GitHub token from peer environment.",
            )
        ]
    )


def test_environment_run_uses_default_agent_and_sets_check_metadata() -> None:
    control = _control()

    result = EnvironmentRunService(control).submit(
        mode="check",
        manifest=_manifest(),
        workspace_root="/repo",
    )

    task = control.get_agent_run(result.agent_run.id)
    assert result.agent_id == "environment_configurator"
    assert task.trigger_mode.value == "environment_config"
    assert task.metadata["workflow"] == "environment_config"
    assert task.metadata["environment_mode"] == "check"
    assert task.metadata["entry_ids"] == [
        "envreq:executable:gitnexus",
        "envreq:credential:github-token",
    ]
    assert task.metadata["allowed_commands"] == [
        {
            "entry_id": "envreq:executable:gitnexus",
            "kind": "environment_requirement",
            "name": "gitnexus",
            "phase": "check",
            "command": "gitnexus --version",
        }
    ]
    activations = control.load_agent_run_detail(task.id)["activations"]
    assert "Check mode" in activations[0]["prompt"]


def test_environment_run_configure_includes_install_command() -> None:
    control = _control()

    result = EnvironmentRunService(control).submit(
        mode="configure",
        manifest=_manifest(),
        workspace_root="/repo",
        agent_id="environment_configurator",
    )

    task = control.get_agent_run(result.agent_run.id)
    assert task.metadata["environment_mode"] == "configure"
    assert {
        "entry_id": "envreq:executable:gitnexus",
        "kind": "environment_requirement",
        "name": "gitnexus",
        "phase": "install",
        "command": "npm install -g gitnexus",
    } in task.metadata["allowed_commands"]


def test_environment_run_rejects_missing_agent_candidate() -> None:
    control = _control(agents={"coder": {"capability_refs": ["repo-dev"]}})

    with pytest.raises(EnvironmentRunError) as raised:
        EnvironmentRunService(control).submit(
            mode="check",
            manifest=_manifest(),
            workspace_root="/repo",
        )

    assert raised.value.error == "environment_agent_not_found"


def test_environment_run_rejects_non_builtin_selected_agent() -> None:
    control = _control(
        agents={
            "coder": {
                "resolved_capabilities": {},
            }
        }
    )

    with pytest.raises(EnvironmentRunError) as raised:
        EnvironmentRunService(control).submit(
            mode="configure",
            manifest=_manifest(),
            workspace_root="/repo",
            agent_id="coder",
        )

    assert raised.value.error == "environment_agent_not_allowed"
