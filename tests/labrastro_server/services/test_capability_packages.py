from __future__ import annotations

import json

from labrastro_server.services.agent_runtime.control_plane import AgentRunControlPlane
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from labrastro_server.services.capability_packages import CapabilityPackageIngestService


def _control_plane() -> AgentRunControlPlane:
    return AgentRunControlPlane(
        runtime_snapshot={
            "agents": {
                "capability_packager": {
                    "runtime_profile": "capability_packager_local",
                }
            },
            "runtime_profiles": {
                "capability_packager_local": {
                    "executor": "fake",
                    "execution_location": "local_workspace",
                }
            },
        }
    )


def test_project_notes_input_creates_read_only_ingest_run() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)

    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
                "package_id_hint": "review",
            }
        }
    )

    assert result.agent_run.agent_id == "capability_packager"
    assert result.agent_run.source.value == "capability_ingest"
    assert result.agent_run.metadata["workflow"] == "capability_package_ingest"
    assert result.source["type"] == "project_notes"
    assert result.source["package_id_hint"] == "review"
    assert result.source_bundle["documents"][0]["title"] == "Project notes"
    assert "capability_packages" not in control.runtime_snapshot
    assert "capability_components" not in control.runtime_snapshot


def test_ingest_status_extracts_completed_draft_json() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        }
    )
    draft = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "components": [
            {
                "id": "cli:gh",
                "kind": "cli",
                "name": "gh",
                "config": {"command": "gh"},
            }
        ],
        "install_plan": ["Install GitHub CLI."],
        "usage": ["Use gh pr view."],
        "evidence": [{"title": "Project notes", "excerpt": "Install gh"}],
        "credentials": ["GITHUB_TOKEN"],
        "risk_level": "low",
    }

    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["agent_run"]["status"] == "completed"
    assert status["draft"]["id"] == "review"
    assert status["draft"]["components"][0]["id"] == "cli:gh"
