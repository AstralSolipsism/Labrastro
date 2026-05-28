from __future__ import annotations

import json
from pathlib import Path

import pytest

from labrastro_server.services.agent_runtime.control_plane import AgentRunControlPlane
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from labrastro_server.services.capability_packages import (
    CapabilityDraftValidator,
    CapabilityPackagerRunner,
    CapabilityPackageIngestError,
    CapabilityPackageIngestService,
    CapabilityPackageInstaller,
    CapabilitySourceCollector,
    EvidenceBundle,
)
from reuleauxcoder.domain.agent_runtime.models import CapabilityComponentConfig
from reuleauxcoder.domain.agent_runtime.models import AgentRunRecord


def _control_plane() -> AgentRunControlPlane:
    return AgentRunControlPlane(
        runtime_snapshot={
            "agents": {
                "capability_packager": {
                    "runtime_profile": "capability_packager_remote",
                }
            },
            "runtime_profiles": {
                "capability_packager_remote": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
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
    assert '"skill_content"' in result.agent_run.prompt
    assert "package-managed Skills must be installable into the server canonical Skill directory" in result.agent_run.prompt


def test_source_collector_uses_fetch_capabilities_for_url_sources() -> None:
    class FakeFetchCapabilitiesTool:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def execute(self, **kwargs: object) -> str:
            self.calls.append(kwargs)
            return json.dumps(
                {
                    "ok": True,
                    "url": kwargs["url"],
                    "final_url": kwargs["url"],
                    "source_kind": "docs_site",
                    "title": "Example Tool",
                    "sections": [
                        {
                            "heading": "Install",
                            "source_url": f"{kwargs['url']}#install",
                            "text": "Install with npm.",
                            "code_blocks": ["npm install -g example-tool"],
                        }
                    ],
                    "links": [
                        {
                            "title": "Repository",
                            "url": "https://github.com/acme/example-tool",
                            "kind": "github_repo",
                        }
                    ],
                    "evidence": [
                        {
                            "title": "Install",
                            "source_url": f"{kwargs['url']}#install",
                            "excerpt": "Install with npm.",
                            "content_hash": "abc123",
                            "fetched_at": "2026-05-22T00:00:00Z",
                        }
                    ],
                    "content_hash": "abc123",
                    "fetched_at": "2026-05-22T00:00:00Z",
                    "errors": [],
                }
            )

    fetch_tool = FakeFetchCapabilitiesTool()
    collector = CapabilitySourceCollector(fetch_tool=fetch_tool)

    bundle = collector.collect(
        {
            "type": "docs_url",
            "url": "https://docs.example.com/example-tool",
            "notes": "Prefer global CLI install.",
        }
    )

    assert fetch_tool.calls == [
        {
            "url": "https://docs.example.com/example-tool",
                "focus": "install setup configure authentication requirements runtime sdk executable mcp skill",
            "source_hint": "docs_url",
            "max_chars": 36000,
        }
    ]
    assert bundle.source["type"] == "docs_url"
    assert bundle.documents[0]["title"] == "Project notes"
    assert bundle.documents[1]["title"] == "Example Tool"
    assert "npm install -g example-tool" in bundle.documents[1]["content"]
    assert any(item.get("content_hash") == "abc123" for item in bundle.evidence)
    assert bundle.links[0]["kind"] == "github_repo"


def test_ingest_service_only_orchestrates_collector_and_runner() -> None:
    class FakeCollector:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def collect(self, payload: dict[str, object]) -> EvidenceBundle:
            self.payloads.append(payload)
            return EvidenceBundle(
                source={"type": "project_notes", "notes": "Use gh."},
                documents=[
                    {
                        "title": "Project notes",
                        "url": "",
                        "content": "Use gh.",
                    }
                ],
                evidence=[
                    {
                        "title": "Project notes",
                        "source_url": "",
                        "excerpt": "Use gh.",
                    }
                ],
            )

    class FakeRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start(
            self,
            *,
            evidence_bundle: EvidenceBundle,
            workspace_root: str = "",
        ) -> AgentRunRecord:
            self.calls.append(
                {
                    "evidence_bundle": evidence_bundle,
                    "workspace_root": workspace_root,
                }
            )
            return AgentRunRecord(
                id="run-1",
                issue_id="capability-package-ingest",
                agent_id="custom-packager",
                source="capability_ingest",
                metadata={"source_bundle": evidence_bundle.to_dict()},
            )

    collector = FakeCollector()
    runner = FakeRunner()
    service = CapabilityPackageIngestService(collector=collector, packager_runner=runner)

    result = service.start(
        {
            "source": {"type": "project_notes", "notes": "Use gh."},
            "workspace_root": "D:/repo",
        }
    )

    assert collector.payloads == [{"type": "project_notes", "notes": "Use gh."}]
    assert runner.calls[0]["workspace_root"] == "D:/repo"
    assert result.agent_run.agent_id == "custom-packager"
    assert result.source_bundle["evidence"][0]["excerpt"] == "Use gh."


def test_draft_validator_requires_valid_components_and_evidence() -> None:
    validator = CapabilityDraftValidator()
    bundle = EvidenceBundle(
        source={"type": "project_notes"},
        documents=[{"title": "Project notes", "url": "", "content": "Install gh."}],
        evidence=[{"title": "Project notes", "source_url": "", "excerpt": "Install gh."}],
    )

    result = validator.validate(
        {
            "id": "review",
            "components": [{"id": "shell:gh", "kind": "shell", "name": "gh"}],
            "install_plan": ["Install gh."],
            "usage": ["Use gh."],
        },
        bundle,
    )

    assert result.ok is False
    assert any(message.startswith("component.kind must be one of ") for message in result.messages)
    assert "draft.evidence is required" in result.messages
    assert "risk_level is required" in result.messages


def test_draft_validator_requires_configure_command_evidence() -> None:
    validator = CapabilityDraftValidator()
    bundle = EvidenceBundle(
        source={"type": "project_notes"},
        documents=[{"title": "Project notes", "url": "", "content": "Install gh."}],
        evidence=[{"title": "Project notes", "source_url": "", "excerpt": "Install gh."}],
    )

    result = validator.validate(
        {
            "id": "review",
            "components": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "environment_requirement",
                    "name": "gh",
                    "config": {
                        "kind": "executable",
                        "configure": "gh auth login",
                    },
                }
            ],
            "install_plan": ["Install gh."],
            "usage": ["Use gh."],
            "evidence": [{"title": "Project notes", "excerpt": "Install gh."}],
            "risk_level": "low",
        },
        bundle,
    )

    assert result.ok is False
    assert "envreq:executable:gh command lacks evidence: gh auth login" in result.messages


def test_package_installer_preserves_environment_requirement_requirements() -> None:
    data: dict[str, object] = {}
    result = CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "dotnet-sdk",
            "components": [
                {
                    "kind": "environment_requirement",
                    "name": "dotnet",
                    "resource_kind": "sdk",
                    "requirements": {"version": ">=8"},
                }
            ],
        },
    )

    assert result.component_ids == ["envreq:sdk:dotnet"]
    requirement = data["environment"]["requirements"]["envreq:sdk:dotnet"]
    assert requirement["kind"] == "sdk"
    assert requirement["requirements"] == {"version": ">=8"}


def test_package_installer_infers_executable_requirement_from_command() -> None:
    data: dict[str, object] = {}
    result = CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "github-cli",
            "components": [
                {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "command": "gh",
                }
            ],
        },
    )

    assert result.component_ids == ["envreq:executable:gh"]
    requirement = data["environment"]["requirements"]["envreq:executable:gh"]
    assert requirement["kind"] == "executable"
    assert requirement["command"] == "gh"


def test_package_installer_materializes_skill_to_canonical_server_path(tmp_path) -> None:
    install_root = tmp_path / "skills" / "packages"
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes.\n"
        "---\n"
        "Use the repository review checklist.\n"
    )
    data: dict[str, object] = {}

    installer = CapabilityPackageInstaller(skill_install_root=install_root)
    result = installer.install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "description": "Review code changes.",
                    "source_path": "skills/code-review/SKILL.md",
                    "skill_content": skill_content,
                }
            ],
        },
    )

    installed_path = install_root / "components" / "skill-code-review" / "SKILL.md"
    assert result.component_ids == ["skill:code-review"]
    assert not installed_path.exists()
    installer.apply_skill_file_operations(result.skill_file_operations)
    assert installed_path.read_text(encoding="utf-8") == skill_content
    skill = data["skills"]["items"]["code-review"]
    assert skill["path_hint"] == str(installed_path)
    assert skill["source_path"] == "skills/code-review/SKILL.md"
    assert skill["managed_by"] == "capability_package"


def test_package_installer_rejects_package_skill_without_installable_content(tmp_path) -> None:
    data: dict[str, object] = {}

    with pytest.raises(CapabilityPackageIngestError) as exc_info:
        CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
            data,
            {
                "id": "review",
                "components": [
                    {
                        "kind": "skill",
                        "name": "code-review",
                        "path_hint": "/external/skills/code-review/SKILL.md",
                    }
                ],
            },
        )

    assert exc_info.value.error == "capability_package_skill_content_required"
    assert "code-review" not in data.get("skills", {}).get("items", {})


def test_package_installer_keeps_shared_skill_path_stable_when_owner_changes(tmp_path) -> None:
    install_root = tmp_path / "skills" / "packages"
    installer = CapabilityPackageInstaller(skill_install_root=install_root)
    data: dict[str, object] = {}
    draft = {
        "components": [
            {
                "kind": "skill",
                "name": "code-review",
                "skill_content": "Review code changes.\n",
            }
        ],
    }

    first_result = installer.install_draft(data, {"id": "review-a", **draft})
    installer.apply_skill_file_operations(first_result.skill_file_operations)
    first_path = Path(data["skills"]["items"]["code-review"]["path_hint"])
    second_result = installer.install_draft(data, {"id": "review-b", **draft})
    installer.apply_skill_file_operations(second_result.skill_file_operations)
    second_path = Path(data["skills"]["items"]["code-review"]["path_hint"])

    assert first_path == install_root / "components" / "skill-code-review" / "SKILL.md"
    assert second_path == first_path
    assert first_path.exists()
    assert not (install_root / "review-a").exists()
    assert not (install_root / "review-b").exists()

    component = CapabilityComponentConfig.from_dict(
        "skill:code-review",
        data["capability_components"]["skill:code-review"],
    )
    component.package_ids = [
        package_id for package_id in component.package_ids if package_id != "review-a"
    ]
    data["capability_components"]["skill:code-review"] = component.to_dict()
    installer.materialize_component(data, component)
    installer.apply_skill_file_operations(installer.skill_file_operations)

    assert Path(data["skills"]["items"]["code-review"]["path_hint"]) == first_path
    assert first_path.exists()

    installer.skill_file_operations = []
    component.package_ids = []
    installer.remove_materialized_component(data, component)
    installer.apply_skill_file_operations(installer.skill_file_operations)

    assert "code-review" not in data["skills"]["items"]
    assert not first_path.exists()
    assert not first_path.parent.exists()


def test_package_installer_delete_cleans_canonical_skill_path(tmp_path) -> None:
    install_root = tmp_path / "skills" / "packages"
    data: dict[str, object] = {}
    installer = CapabilityPackageInstaller(skill_install_root=install_root)
    result = installer.install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "skill_content": "Review code changes.\n",
                }
            ],
        },
    )
    installed_path = install_root / "components" / "skill-code-review" / "SKILL.md"
    installer.apply_skill_file_operations(result.skill_file_operations)
    assert installed_path.exists()
    component = CapabilityComponentConfig.from_dict(
        "skill:code-review",
        data["capability_components"]["skill:code-review"],
    )

    installer.skill_file_operations = []
    installer.remove_materialized_component(
        data,
        component,
    )
    installer.apply_skill_file_operations(installer.skill_file_operations)

    assert "code-review" not in data["skills"]["items"]
    assert not installed_path.exists()
    assert not installed_path.parent.exists()


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
        "contributions": {
            "environment_requirements": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "check": "gh --version",
                }
            ]
        },
        "install_plan": ["Install GitHub CLI."],
        "usage": ["Use gh pr view."],
        "evidence": [{"title": "Project notes", "excerpt": "Install gh and run gh --version"}],
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
    assert (
        status["draft"]["contributions"]["environment_requirements"][0]["id"]
        == "envreq:executable:gh"
    )
    assert status["source_bundle"]["evidence"][0]["excerpt"] == (
        "Install gh, then use gh pr view for review."
    )
    assert status["validation"]["ok"] is True
