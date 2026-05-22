from __future__ import annotations

import json

from labrastro_server.services.agent_runtime.control_plane import AgentRunControlPlane
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from labrastro_server.services.capability_packages import (
    CapabilityDraftValidator,
    CapabilityPackagerRunner,
    CapabilityPackageIngestService,
    CapabilitySourceCollector,
    EvidenceBundle,
)
from reuleauxcoder.domain.agent_runtime.models import AgentRunRecord


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
            "focus": "install setup configure authentication requirements cli mcp skill",
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
    assert "component.kind must be cli, mcp, or skill" in result.messages
    assert "draft.evidence is required" in result.messages
    assert "risk_level is required" in result.messages


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
    assert status["validation"]["ok"] is True
