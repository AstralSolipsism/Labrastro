from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

import labrastro_server.services.capability_packages as capability_packages_module
from labrastro_server.interfaces.http.remote.service import (
    RemoteRelayHTTPService,
    _RemoteSessionRun,
)
from labrastro_server.services.agent_runtime.control_plane import AgentRunControlPlane
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.session_projection import (
    agent_run_events_to_session_events,
    agent_run_event_to_session_events,
)
from labrastro_server.services.capability_packages import (
    CapabilityDraftValidator,
    CapabilityPackagerRunner,
    CapabilityPackageIngestError,
    CapabilityPackageIngestService,
    CapabilityPackageInstaller,
    CapabilityPackageSessionRunService,
    CapabilitySourceCollector,
    EvidenceBundle,
)
from reuleauxcoder.domain.agent_runtime.models import CapabilityComponentConfig
from reuleauxcoder.domain.agent_runtime.models import AgentRunRecord
from reuleauxcoder.domain.session.document import apply_session_event


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
                    "worker_kind": "sandbox_worker",
                    "sandbox": {},
                }
            },
        }
    )


def _wait_for(predicate, *, timeout_sec: float = 3.0):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


def _review_draft(*, command: str = "gh") -> dict[str, object]:
    return {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "environment_requirements": [
                {
                    "id": f"envreq:executable:{command}",
                    "kind": "executable",
                    "name": command,
                    "command": command,
                    "check": f"{command} --version",
                }
            ]
        },
        "install_plan": [f"Install {command}."],
        "usage": [f"Use {command} pr view."],
        "evidence": [{"title": "Project notes", "excerpt": f"Install {command} and run {command} --version"}],
        "credentials": ["GITHUB_TOKEN"],
        "risk_level": "low",
    }


def test_agent_run_log_event_projects_as_process_context() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 1,
            "type": "log",
            "payload": {
                "type": "log",
                "text": "loading source bundle",
                "data": {"level": "info"},
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["context_event"]
    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_log"
    assert payload["log"] == "loading source bundle"
    assert payload["level"] == "info"


def test_agent_run_tool_events_project_with_stable_tool_identity() -> None:
    start_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 2,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "fetch_capabilities",
                    "tool_call_id": "call-1",
                    "input": {"url": "https://example.test/repo"},
                },
            },
        }
    )
    end_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "text": "ok",
                "data": {
                    "tool_name": "fetch_capabilities",
                    "tool_call_id": "call-1",
                    "output": "ok",
                },
            },
        }
    )

    assert start_events[0][0] == "tool_call_start"
    assert end_events[0][0] == "tool_call_end"
    assert start_events[0][1]["tool_name"] == "fetch_capabilities"
    assert end_events[0][1]["tool_name"] == "fetch_capabilities"
    assert start_events[0][1]["tool_call_id"] == "call-1"
    assert end_events[0][1]["tool_call_id"] == "call-1"


def test_agent_run_tool_result_projects_large_output_as_raw_audit_summary() -> None:
    large_output = "A" * 5000 + "TAIL"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "read_file",
                    "tool_call_id": "call-1",
                    "output": large_output,
                    "path": "SKILL.md",
                },
            },
        }
    )

    assert session_events[0][0] == "tool_call_end"
    payload = session_events[0][1]
    assert len(payload["tool_result"]) < len(large_output)
    assert "open raw events for the complete content" in payload["tool_result"]
    assert payload["tool_result"].endswith("TAIL")
    assert payload["meta"]["path"] == "SKILL.md"
    assert payload["meta"]["output_truncated"] is True
    assert payload["meta"]["output_chars"] == len(large_output)
    assert payload["meta"]["output_source"] == "raw_event"
    assert "output" not in payload["meta"]
    assert payload["raw_event_refs"] == [{"agent_run_id": "run-1", "seq": 3, "type": "tool_result"}]


def test_agent_run_tool_use_projects_large_arguments_as_raw_audit_summary() -> None:
    large_content = "C" * 5000 + "END"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 2,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "write_file",
                    "tool_call_id": "call-1",
                    "input": {"path": "SKILL.md", "content": large_content},
                },
            },
        }
    )

    payload = session_events[0][1]
    assert payload["tool_args"]["path"] == "SKILL.md"
    assert len(payload["tool_args"]["content"]) < len(large_content)
    assert payload["tool_args"]["content"].endswith("END")
    assert payload["tool_args"]["truncated_fields"] == ["content"]
    assert payload["tool_args"]["full_payload_source"] == "raw_event"
    assert payload["raw_event_refs"] == [{"agent_run_id": "run-1", "seq": 2, "type": "tool_use"}]


def test_agent_run_result_event_projects_as_structured_process_context() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "result",
            "payload": {
                "type": "result",
                "text": "draft complete",
                "data": {"status": "completed", "output": "draft complete"},
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["context_event"]
    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_result"
    assert payload["agent_run_status"] == "completed"
    assert payload["output"] == "draft complete"


def test_agent_run_result_event_omits_large_output_from_structured_context() -> None:
    large_output = "B" * 5000 + "DONE"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "result",
            "payload": {
                "type": "result",
                "data": {"status": "completed", "output": large_output},
            },
        }
    )

    payload = session_events[0][1]
    assert len(payload["output"]) < len(large_output)
    assert payload["output"].endswith("DONE")
    assert payload["result"]["output_truncated"] is True
    assert payload["result"]["output_chars"] == len(large_output)
    assert "output" not in payload["result"]


def test_agent_run_terminal_event_omits_large_output_from_process_context() -> None:
    large_output = "D" * 5000 + "DONE"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 5,
            "type": "completed",
            "payload": {
                "result": {"status": "completed", "output": large_output},
                "agent_run": {
                    "id": "run-1",
                    "status": "completed",
                    "output": large_output,
                },
            },
        }
    )

    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_completed"
    assert len(payload["output"]) < len(large_output)
    assert payload["output"].endswith("DONE")
    assert len(payload["message"]) < len(large_output)
    assert payload["message"].endswith("DONE")
    assert payload["terminal"]["output_truncated"] is True
    assert payload["terminal"]["message_truncated"] is True
    assert payload["raw_event_refs"] == [{"agent_run_id": "run-1", "seq": 5, "type": "completed"}]


def test_agent_run_projection_batches_consecutive_text_and_thinking() -> None:
    session_events = agent_run_events_to_session_events(
        [
            {
                "agent_run_id": "run-1",
                "seq": 1,
                "type": "thinking",
                "payload": {"type": "thinking", "text": "a"},
            },
            {
                "agent_run_id": "run-1",
                "seq": 2,
                "type": "thinking",
                "payload": {"type": "thinking", "text": "b"},
            },
            {
                "agent_run_id": "run-1",
                "seq": 3,
                "type": "text",
                "payload": {"type": "text", "text": "c"},
            },
            {
                "agent_run_id": "run-1",
                "seq": 4,
                "type": "text",
                "payload": {"type": "text", "text": "d"},
            },
        ]
    )

    assert [event_type for event_type, _ in session_events] == [
        "reasoning_delta",
        "assistant_delta",
    ]
    assert session_events[0][1]["content"] == "ab"
    assert session_events[1][1]["content"] == "cd"
    assert [item["seq"] for item in session_events[0][1]["raw_event_refs"]] == [1, 2]


def test_agent_run_projection_summarizes_large_batched_text() -> None:
    events = [
        {
            "agent_run_id": "run-1",
            "seq": seq,
            "type": "text",
            "payload": {"type": "text", "text": "E" * 1000},
        }
        for seq in range(1, 12)
    ]
    session_events = agent_run_events_to_session_events(events)

    assert [event_type for event_type, _ in session_events] == ["assistant_delta"]
    payload = session_events[0][1]
    assert len(payload["content"]) < 11_000
    assert "open raw events for the complete content" in payload["content"]
    assert payload["content_projection"]["content_truncated"] is True
    assert [item["seq"] for item in payload["raw_event_refs"]] == list(range(1, 12))


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
    assert result.agent_run.metadata["worker_kind"] == "sandbox_worker"
    assert result.agent_run.metadata["workflow"] == "capability_package_ingest"
    assert result.source["type"] == "project_notes"
    assert result.source["package_id_hint"] == "review"
    assert result.source_bundle["documents"][0]["title"] == "Project notes"
    assert "capability_packages" not in control.runtime_snapshot
    assert "capability_components" not in control.runtime_snapshot
    assert '"skill_content"' not in result.agent_run.prompt
    assert "source_path" in result.agent_run.prompt
    assert "Do not copy large Skill files into the model output." in result.agent_run.prompt


def test_revision_prompt_uses_public_draft_without_skill_content() -> None:
    control = _control_plane()
    runner = CapabilityPackagerRunner(control)
    task = runner.start(
        evidence_bundle=EvidenceBundle(
            source={"type": "project_notes"},
            documents=[{"title": "Project notes", "content": "Review code changes."}],
            evidence=[{"title": "Project notes", "excerpt": "Review code changes."}],
        ),
        revision_instruction="rename the package",
        revision_draft={
            "id": "review",
            "name": "Review",
            "contributions": {
                "skills": [
                    {
                        "id": "skill:code-review",
                        "kind": "skill",
                        "name": "code-review",
                        "skill_content": "---\nname: code-review\n---\nlarge body",
                    }
                ]
            },
        },
    )
    assert '"skill_content":' not in task.prompt
    assert "skill_content_chars" in task.prompt


def test_github_repo_ingest_sets_repo_url_for_sandbox_worktree() -> None:
    class FakeFetchCapabilitiesTool:
        def execute(self, **kwargs: object) -> str:
            return json.dumps(
                {
                    "ok": True,
                    "url": kwargs["url"],
                    "title": "Example Tool",
                    "sections": [],
                    "links": [],
                    "evidence": [],
                    "errors": [],
                }
            )

    control = _control_plane()
    service = CapabilityPackageIngestService(
        control,
        collector=CapabilitySourceCollector(fetch_tool=FakeFetchCapabilitiesTool()),
    )

    result = service.start({"repoUrl": "https://github.com/acme/example-tool"})

    assert result.agent_run.metadata["worker_kind"] == "sandbox_worker"
    assert result.agent_run.metadata["repo_url"] == "https://github.com/acme/example-tool"
    assert result.agent_run.metadata["capability_source"]["type"] == "github_repo"
    assert result.agent_run.metadata["capability_source"]["url"] == "https://github.com/acme/example-tool"


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
            agent_run_metadata: dict[str, object] | None = None,
            revision_draft: dict[str, object] | None = None,
            revision_instruction: str = "",
        ) -> AgentRunRecord:
            self.calls.append(
                {
                    "evidence_bundle": evidence_bundle,
                    "workspace_root": workspace_root,
                    "agent_run_metadata": agent_run_metadata or {},
                    "revision_draft": revision_draft,
                    "revision_instruction": revision_instruction,
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
    assert requirement["runtime_footprint"]["runs_on"] == "local_peer"


def test_package_installer_writes_component_and_package_runtime_footprint() -> None:
    data: dict[str, object] = {}
    result = CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "mcp_server",
                    "name": "github",
                    "command": "github-mcp-server",
                    "runtime_footprint": {
                        "runs_on": "server",
                        "install_required_on": ["server"],
                        "config_required_on": ["server"],
                    },
                },
                {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "command": "gh",
                    "placement": "peer",
                },
            ],
        },
    )

    assert result.component_ids == ["mcp_server:github", "envreq:executable:gh"]
    mcp_component = data["capability_components"]["mcp_server:github"]
    env_component = data["capability_components"]["envreq:executable:gh"]
    package = data["capability_packages"]["review"]
    assert mcp_component["runtime_footprint"]["runs_on"] == "server"
    assert env_component["runtime_footprint"]["runs_on"] == "local_peer"
    assert package["runtime_footprint"] == {
        "runs_on": "both",
        "install_required_on": ["server", "local_peer"],
        "config_required_on": ["server", "local_peer"],
        "user_message": "服务端和本地端都需要配置",
    }


def test_package_installer_aggregates_skill_runtime_from_environment_refs() -> None:
    data: dict[str, object] = {}

    CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "skill_content": "---\nname: code-review\ndescription: Review code.\n---\nReview code.\n",
                    "environment_requirement_refs": ["envreq:executable:gh"],
                },
                {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "command": "gh",
                    "placement": "peer",
                },
            ],
        },
    )

    component = data["capability_components"]["skill:code-review"]
    skill = data["skills"]["items"]["code-review"]
    package = data["capability_packages"]["review"]
    assert component["runtime_footprint"]["runs_on"] == "local_peer"
    assert component["config"]["environment_requirement_refs"] == ["envreq:executable:gh"]
    assert skill["environment_requirement_refs"] == ["envreq:executable:gh"]
    assert skill["runtime_footprint"]["runs_on"] == "local_peer"
    assert package["runtime_footprint"]["runs_on"] == "local_peer"


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
                    "display_name": "Code review",
                    "summary": "Review repository changes before merging.",
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
    component = data["capability_components"]["skill:code-review"]
    assert component["display_name"] == "Code review"
    assert component["summary"] == "Review repository changes before merging."
    assert skill["display_name"] == "Code review"
    assert skill["summary"] == "Review repository changes before merging."
    assert skill["path_hint"] == str(installed_path)
    assert skill["source_path"] == "skills/code-review/SKILL.md"
    assert skill["managed_by"] == "capability_package"
    assert "skill_content" not in skill


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


def test_ingest_status_builds_skill_content_from_source_bundle() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill.",
            }
        }
    )
    skill_content = "---\nname: code-review\ndescription: Review code changes.\n---\n\nReview code changes.\n"
    source_bundle = {
        "source": {"type": "github_repo", "url": "https://github.com/acme/review"},
        "documents": [
            {
                "title": "skills/code-review/SKILL.md",
                "source_path": "skills/code-review/SKILL.md",
                "content": skill_content,
            }
        ],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "errors": [],
    }
    control.get_agent_run(result.agent_run.id).metadata["source_bundle"] = source_bundle
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "github_repo", "url": "https://github.com/acme/review"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                    "source_path": "skills/code-review/SKILL.md",
                    "summary": "Review code changes.",
                }
            ]
        },
        "install_plan": ["Install the packaged skill."],
        "usage": ["Use the review skill."],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    assert skill["config"]["skill_content"] == skill_content.strip()
    assert status["validation"]["ok"] is True


def test_ingest_status_builds_skill_content_from_workspace_root(tmp_path: Path) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    skill_path = tmp_path / "skills" / "code-review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: code-review\ndescription: Review code changes.\n---\n\nReview code changes.\n",
        encoding="utf-8",
    )
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill.",
            },
            "workspace_root": str(tmp_path),
        }
    )
    control.get_agent_run(result.agent_run.id).metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "errors": [],
    }
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                    "source_path": "skills/code-review/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["draft"]["contributions"]["skills"][0]["skill_content"].startswith("---\nname: code-review")
    assert status["validation"]["ok"] is True


def test_ingest_status_uses_agent_run_workdir_and_unique_skill_fallback(tmp_path: Path) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    skill_path = tmp_path / "SKILL.md"
    skill_content = (
        "---\n"
        "name: stop-slop\n"
        "description: Detect vague AI writing.\n"
        "---\n\n"
        "Detect vague AI writing.\n"
    )
    skill_path.write_text(skill_content, encoding="utf-8")
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install the stop-slop skill from the checked out worktree.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = str(tmp_path)
    task.metadata["source_bundle"] = {
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "errors": [],
    }
    draft_decision = {
        "id": "stop-slop",
        "name": "Stop Slop",
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:stop-slop",
                    "kind": "skill",
                    "name": "stop-slop",
                    "source_path": "skills/stop-slop/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    assert skill["source_path"] == "SKILL.md"
    assert status["validation"]["ok"] is True


def test_ingest_status_reads_skill_content_from_sandbox_container_workdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    skill_content = (
        "---\n"
        "name: stop-slop\n"
        "description: Detect vague AI writing.\n"
        "---\n\n"
        "Detect vague AI writing.\n"
    )

    def fake_docker_exec(
        container_id: str,
        script: str,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        assert container_id == "sandbox-container-1"
        if "find ." in script:
            return subprocess.CompletedProcess(
                args=["docker", "exec"],
                returncode=0,
                stdout="SKILL.md\n",
                stderr="",
            )
        if "cat" in script:
            assert args[0] == "/workspace/.rcoder/agent-runs/workspace/task/workdir/stop-slop"
            if args[1] != "SKILL.md":
                return subprocess.CompletedProcess(
                    args=["docker", "exec"],
                    returncode=1,
                    stdout="",
                    stderr="missing",
                )
            return subprocess.CompletedProcess(
                args=["docker", "exec"],
                returncode=0,
                stdout=skill_content,
                stderr="",
            )
        raise AssertionError(f"unexpected docker script: {script}")

    monkeypatch.setattr(
        capability_packages_module,
        "_run_docker_exec",
        fake_docker_exec,
    )
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install the stop-slop skill from the checked out sandbox worktree.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = "/workspace/.rcoder/agent-runs/workspace/task/workdir/stop-slop"
    task.metadata["sandbox_container_id"] = "sandbox-container-1"
    task.metadata["source_bundle"] = {
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "errors": [],
    }
    draft_decision = {
        "id": "stop-slop",
        "name": "Stop Slop",
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:stop-slop",
                    "kind": "skill",
                    "name": "stop-slop",
                    "source_path": "skills/stop-slop/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    assert skill["source_path"] == "SKILL.md"
    assert status["validation"]["ok"] is True


def test_ingest_status_reports_ambiguous_workdir_skill_content(tmp_path: Path) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    for name in ("one", "two"):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\n\n{name}\n", encoding="utf-8")
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install one generated skill.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = str(tmp_path)
    task.metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Generated skill."}],
        "errors": [],
    }
    draft_decision = {
        "id": "ambiguous-skill",
        "name": "Ambiguous Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:ambiguous",
                    "kind": "skill",
                    "name": "ambiguous",
                    "source_path": "SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Generated skill."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["validation"]["ok"] is False
    assert any(
        "multiple SKILL.md files found" in message
        and "exact source_path or content_ref" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_builds_skill_content_from_agent_run_read_file_event() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill.",
            }
        }
    )
    skill_content = "---\nname: code-review\ndescription: Review code changes.\n---\n\nReview code changes.\n"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "input": {"path": "skills/code-review/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "output": skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                    "source_path": "skills/code-review/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["draft"]["contributions"]["skills"][0]["skill_content"] == skill_content.strip()
    assert status["validation"]["ok"] is True


def test_ingest_status_materializes_gsap_style_skill_repo_from_agent_run_reads() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    skill_contents = {
        "skills/gsap-core/SKILL.md": (
            "---\n"
            "name: gsap-core\n"
            "description: Use GSAP core animation APIs.\n"
            "---\n\n"
            "Use GSAP core animation APIs with source-backed guidance.\n"
        ),
        "skills/gsap-timeline/SKILL.md": (
            "---\n"
            "name: gsap-timeline\n"
            "description: Build GSAP timelines.\n"
            "---\n\n"
            "Build coordinated GSAP timelines.\n"
        ),
    }
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "glob",
                "tool_call_id": "glob-skills",
                "input": {"pattern": "skills/**/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "glob",
                "tool_call_id": "glob-skills",
                "output": "\n".join(skill_contents),
            },
        ),
    )
    for index, (path, content) in enumerate(skill_contents.items(), start=1):
        call_id = f"read-skill-{index}"
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="tool_use",
                data={
                    "tool_name": "read_file",
                    "tool_call_id": call_id,
                    "input": {"path": path},
                },
            ),
        )
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="tool_result",
                data={
                    "tool_name": "read_file",
                    "tool_call_id": call_id,
                    "output": content,
                },
            ),
        )
    draft_decision = {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"},
        "source_inventory": {
            "skill_files": list(skill_contents),
        },
        "materialization_plan": [
            {
                "component_id": "skill:gsap-core",
                "source_path": "skills/gsap-core/SKILL.md",
                "content_ref": "read-skill-1",
            },
            {
                "component_id": "skill:gsap-timeline",
                "source_path": "skills/gsap-timeline/SKILL.md",
                "content_ref": "read-skill-2",
            },
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "summary": "Use GSAP core animation APIs.",
                },
                {
                    "id": "skill:gsap-timeline",
                    "kind": "skill",
                    "name": "gsap-timeline",
                    "summary": "Build GSAP timelines.",
                },
            ]
        },
        "evidence": [{"title": "GSAP skills", "excerpt": "skills/gsap-core/SKILL.md"}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skills = {
        item["name"]: item
        for item in status["draft"]["contributions"]["skills"]
    }
    assert skills["gsap-core"]["skill_content"] == skill_contents["skills/gsap-core/SKILL.md"].strip()
    assert skills["gsap-timeline"]["skill_content"] == skill_contents["skills/gsap-timeline/SKILL.md"].strip()
    assert status["validation"]["ok"] is True
    inventory = status["source_bundle"]["source_inventory"]
    assert {item["path"] for item in inventory["skill_files"]} == set(skill_contents)
    assert all("content" not in item for item in inventory["documents"])
    assert inventory["raw_event_refs"]


def test_ingest_status_uses_exact_source_path_when_workdir_has_multiple_skills(
    tmp_path: Path,
) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    for name in ("one", "two"):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\n\n{name} skill\n", encoding="utf-8")
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install one exact skill from a multi-skill worktree.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = str(tmp_path)
    task.metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "two skill"}],
        "errors": [],
    }
    draft_decision = {
        "id": "two-skill",
        "name": "Two Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:two",
                    "kind": "skill",
                    "name": "two",
                    "source_path": "skills/two/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "two skill"}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == "---\nname: two\n---\n\ntwo skill"
    assert skill["source_path"] == "skills/two/SKILL.md"
    assert status["validation"]["ok"] is True


def test_ingest_status_reports_unsupported_external_install_envreq() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    skill_content = "---\nname: gsap-core\n---\n\nUse GSAP core.\n"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-core",
                "input": {"path": "skills/gsap-core/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-core",
                "output": skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"},
        "materialization_plan": [
            {
                "component_id": "skill:gsap-core",
                "source_path": "skills/gsap-core/SKILL.md",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                }
            ],
            "environment_requirements": [
                {
                    "id": "envreq:executable:npx",
                    "kind": "executable",
                    "name": "npx",
                    "command": "npx",
                    "check": "npx --version",
                    "install": "Install Node.js (https://nodejs.org) which includes npx",
                }
            ],
        },
        "evidence": [{"title": "GSAP", "excerpt": "Use GSAP core."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["validation"]["ok"] is False
    assert status["draft"]["contributions"]["environment_requirements"][0]["id"] == "envreq:executable:npx"
    assert status["validation"]["draft"]["contributions"]["environment_requirements"][0]["id"] == "envreq:executable:npx"
    assert status["failure"]["result_type"] == "command_evidence_missing"
    assert any(
        "envreq:executable:npx command lacks evidence: npx --version" in message
        for message in status["validation"]["messages"]
    )


def test_capability_package_session_reports_structured_skill_content_failure(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("invalid draft must not install")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-skill-content-failure",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Generate one missing skill.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    draft_decision = {
        "id": "missing-skill",
        "name": "Missing Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:missing",
                    "kind": "skill",
                    "name": "missing",
                }
            ]
        },
        "evidence": [{"title": "Project notes", "excerpt": "Generate one missing skill."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    _wait_for(lambda: session.done)

    result = next(
        event["payload"]
        for event in session.events
        if event["type"] == "workflow_result"
        and event["payload"].get("result_type") == "skill_content_unresolved"
    )
    assert result["status"] == "error"
    assert result["result"]["code"] == "skill_content_unresolved"
    assert any(
        "requires skill_content" in message
        for message in result["result"]["messages"]
    )
    error = next(event["payload"] for event in session.events if event["type"] == "error")
    assert error["code"] == "skill_content_unresolved"


def test_capability_package_session_run_requests_install_approval_and_installs(tmp_path: Path) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)

            class Result:
                ok = True
                status = 200
                payload = {"ok": True, "package_id": "review"}

            return Result()

    control = _control_plane()
    admin = FakeAdminManager()
    document: dict[str, object] | None = None

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        nonlocal document
        document = apply_session_event(
            document,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            session_event_seq=(int(document.get("last_event_seq") or 0) + 1)
            if isinstance(document, dict)
            else 1,
            session_run_id=session_run_id,
            session_run_seq=session_run_seq,
        )
        return int(document.get("last_event_seq") or 0)

    session = _RemoteSessionRun(
        session_run_id="session-run-1",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
        trace_event_sink=trace_sink,
    )
    session.enable_trace_persistence("session-1")
    session.append_event(
        "session_run_start",
        {
            "prompt": "Create capability package",
            "mode": "capability_package",
            "workflow_mode": "capability_package_ingest",
            "locale": "en",
        },
    )
    session.mark_running()
    service = CapabilityPackageSessionRunService(
        control,
        admin,
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "input": {"path": "skills/review/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "output": "Review package skill content.",
                "path": "skills/review/SKILL.md",
            },
        ),
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
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )
    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    draft_event = next(event for event in session.events if event["type"] == "workflow_artifact")
    assert draft_event["payload"]["artifact_type"] == "capability_package_draft"
    assert draft_event["payload"]["artifact"]["package_id"] == "review"
    assert approval["tool_name"] == "install_capability_package"
    assert approval["tool_call_id"]
    assert approval["intent"] == "Confirm installing capability package review"
    assert approval["sections"][0]["title"] == "Capability package"
    assert approval["sections"][1]["title"] == "Component summary"
    assert approval["sections"][2]["title"] == "Runtime footprint"
    assert approval["sections"][2]["items"][0]["value"] == "需要在本机安装/配置"
    assert approval["sections"][2]["items"][1]["value"] == "Local client"
    assert approval["decision_type"] == "capability_package_install"
    assert approval["review"]["package_id"] == "review"
    session.append_event("reasoning_delta", {"content": "Installing package."})
    session.resolve_approval(str(approval["approval_id"]), "allow_once", None)
    _wait_for(lambda: session.done)

    assert admin.payloads
    assert admin.payloads[0]["draft"]["id"] == "review"  # type: ignore[index]
    assert not any(event["type"] in {"tool_call_start", "tool_call_end"} for event in session.events)
    tool_steps = [
        event["payload"]
        for event in session.events
        if event["type"] == "workflow_step"
        and event["payload"].get("details", {}).get("tool_call_id") == "read-skill"
    ]
    assert {step["status"] for step in tool_steps} == {"running", "done"}
    assert all(step["stage"] == "read_source" for step in tool_steps)
    done_tool_step = next(step for step in tool_steps if step["status"] == "done")
    assert done_tool_step["details"]["tool_name"] == "read_file"
    assert done_tool_step["details"]["tool_call_id"] == "read-skill"
    assert done_tool_step["details"]["raw_event_refs"][0]["type"] == "tool_result"
    assert "tool_result" not in done_tool_step
    assert "tool_result" not in done_tool_step["details"]
    assert any(event["type"] == "workflow_result" for event in session.events)
    assert session.events[-1]["type"] in {"session_run_end", "approval_resolved", "workflow_result"}
    assert any(
        event["type"] == "session_run_end"
        and event["payload"].get("response") == "Capability package review installed."
        and event["payload"].get("response_rendered") is True
        for event in session.events
    )
    assert isinstance(document, dict)
    assert document["stats"]["runStatus"] == "done"  # type: ignore[index]
    parts = document["turns"][0]["assistantMessages"][0]["parts"]  # type: ignore[index]
    assert not any(
        part.get("type") == "thinking" and part.get("active") is True
        for part in parts
    )


def test_capability_package_session_process_text_follows_english_locale(tmp_path: Path) -> None:
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-1",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
    )
    service = CapabilityPackageSessionRunService(
        control,
        object(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_id="peer-1",
    )
    assert claim is not None
    control.complete_claimed_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="failed",
            output="",
            error="No model provider/profile is configured.",
        ),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    _wait_for(lambda: session.done)

    messages = [
        str(event["payload"].get("message") or "")
        for event in session.events
        if event["type"] == "workflow_step"
    ]
    assert "Starting capability package draft generation" in messages
    assert "Capability package generation task entered capability_packager" in messages
    assert "Capability package generation task queued" in messages
    assert "Capability package generation task accepted by sandbox worker" in messages
    assert not any("能力包" in message for message in messages)


def test_capability_package_session_unknown_failure_uses_session_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_start(self, payload, *, agent_run_metadata=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(CapabilityPackageIngestService, "start", fail_start)
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-1",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="zh-CN",
    )
    service = CapabilityPackageSessionRunService(
        control,
        object(),
        poll_timeout_sec=0.05,
    )

    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    _wait_for(lambda: session.done)

    error_event = next(event for event in session.events if event["type"] == "error")
    failed_event = next(event for event in session.events if event["type"] == "session_run_failed")
    assert error_event["payload"]["message"] == "能力包流程执行失败。"
    assert error_event["payload"]["message_key"] == "capability_package.session_failed"
    assert error_event["payload"]["diagnostic_message"] == "boom"
    assert failed_event["payload"]["message"] == "能力包流程执行失败。"


def test_capability_package_session_follow_up_revises_pending_draft(tmp_path: Path) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)
            raise AssertionError("draft revision should not install the previous approval")

    control = _control_plane()
    admin = FakeAdminManager()
    session = _RemoteSessionRun(
        session_run_id="session-run-revise",
        peer_id="peer-1",
        session_hint="session-1",
        locale="zh-CN",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(control, admin, poll_timeout_sec=0.05)
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install hub, then use hub pr show for review.",
            }
        },
    )
    first_agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    first_agent_run = control.agent_run_to_dict(str(first_agent_run_id))
    assert first_agent_run["metadata"]["locale"] == "zh-CN"
    assert "所有用户可见的生成内容都必须使用简体中文" in first_agent_run["prompt"]
    assert "生成草案中的自然语言字段" in first_agent_run["prompt"]
    assert "你是 capability_packager" in first_agent_run["prompt"]
    first_draft = _review_draft(command="hub")
    control.complete_agent_run(
        str(first_agent_run_id),
        ExecutorRunResult(
            task_id=str(first_agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(first_draft)}\n```",
        ),
    )
    first_approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )

    session.submit_follow_up(
        "把依赖改成 gh，不要用 hub",
        followup_id="follow-revise",
        client_request_id="pending-revise",
    )
    second_agent_run_id = _wait_for(
        lambda: next(
            (
                run["id"]
                for run in control.list_agent_runs(agent_id="capability_packager")
                if run["id"] != first_agent_run_id
            ),
            "",
        )
    )
    second_agent_run = control.agent_run_to_dict(str(second_agent_run_id))
    assert second_agent_run["metadata"]["session_run_id"] == "session-run-revise"
    assert second_agent_run["metadata"]["locale"] == "zh-CN"
    assert second_agent_run["metadata"]["revision_of_agent_run_id"] == first_agent_run_id
    assert second_agent_run["metadata"]["revision_followup_id"] == "follow-revise"
    assert second_agent_run["metadata"]["revision_instruction"] == "把依赖改成 gh，不要用 hub"
    assert second_agent_run["parent_task_id"] == first_agent_run_id
    assert "用户意见：" in second_agent_run["prompt"]
    assert "把依赖改成 gh，不要用 hub" in second_agent_run["prompt"]
    assert '"command": "hub"' in second_agent_run["prompt"]
    assert any(
        event["type"] == "approval_resolved"
        and event["payload"].get("approval_id") == first_approval["approval_id"]
        and event["payload"].get("reason") == "收到修改意见，重新生成草案。"
        for event in session.events
    )
    assert any(
        event["type"] == "session_run_follow_up_consumed"
        and event["payload"].get("followup_id") == "follow-revise"
        for event in session.events
    )
    assert any(
        event["type"] == "workflow_step"
        and event["payload"].get("details", {}).get("phase") == "capability_package_revision_requested"
        and "把依赖改成 gh，不要用 hub" in event["payload"].get("message", "")
        and event["payload"].get("details", {}).get("instruction") == "把依赖改成 gh，不要用 hub"
        for event in session.events
    )

    second_draft = _review_draft(command="gh")
    control.complete_agent_run(
        str(second_agent_run_id),
        ExecutorRunResult(
            task_id=str(second_agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(second_draft)}\n```",
        ),
    )
    approvals = _wait_for(
        lambda: [
            event["payload"]
            for event in session.events
            if event["type"] == "workflow_decision"
        ] if len([event for event in session.events if event["type"] == "workflow_decision"]) >= 2 else []
    )
    second_approval = approvals[-1]
    assert second_approval["approval_id"] != first_approval["approval_id"]
    assert second_approval["tool_name"] == "install_capability_package"
    assert second_approval["tool_args"]["agent_run_id"] == second_agent_run_id

    session.resolve_approval(str(second_approval["approval_id"]), "deny_once", "test_cleanup")
    _wait_for(lambda: session.done)
    assert admin.payloads == []


def test_peer_shutdown_keeps_capability_package_session_run_active(tmp_path: Path) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("peer shutdown must not install")

    control = _control_plane()
    http_service = object.__new__(RemoteRelayHTTPService)
    http_service._session_runs_lock = threading.Lock()
    http_service._session_runs = {}
    session = _RemoteSessionRun(
        session_run_id="session-run-peer-shutdown",
        peer_id="peer-1",
        session_hint="session-1",
        mode="capability_package",
        workflow_mode="capability_package_ingest",
        runtime_state={"mode": "capability_package", "workflow_mode": "capability_package_ingest"},
        artifact_root=tmp_path,
    )
    http_service._session_runs[session.session_run_id] = session
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )

    http_service._abort_peer_session_runs("peer-1", "peer_disconnected: peer_shutdown")
    time.sleep(0.1)

    task = control.agent_run_to_dict(str(agent_run_id))
    assert task["status"] in {"queued", "running"}
    assert session.done is False
    assert session.status == "running"
    assert not any(
        event["type"] == "error"
        and event["payload"].get("message") == "peer_disconnected: peer_shutdown"
        for event in session.events
    )

    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_stays_attached_when_agent_run_lease_expires(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("lease recovery test should stop at approval")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-lease-recover",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_id="peer-1",
        lease_sec=1,
    )
    assert claim is not None
    assert claim.task.id == agent_run_id

    recovered = control.recover_stale_agent_runs(now=time.time() + 2)
    assert agent_run_id in recovered
    assert control.agent_run_to_dict(str(agent_run_id))["status"] == "queued"

    draft = _review_draft(command="gh")
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )
    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    assert approval["tool_name"] == "install_capability_package"
    assert approval["tool_args"]["agent_run_id"] == agent_run_id
    assert session.done is False

    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_cancel_cancels_agent_run(tmp_path: Path) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("cancelled session must not install")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-2",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("user_cancelled")
    _wait_for(lambda: session.done)

    task = control.agent_run_to_dict(str(agent_run_id))
    assert task["status"] == "cancelled"


def test_capability_package_session_cancel_during_install_approval_does_not_append_install_terminal_events(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)
            raise AssertionError("cancelled session must not install")

    control = _control_plane()
    admin = FakeAdminManager()
    session = _RemoteSessionRun(
        session_run_id="session-run-approval-cancel",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        admin,
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
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
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )
    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    assert approval["tool_name"] == "install_capability_package"

    first_request, resolved_approvals = session.request_cancel("user_cancelled")
    assert first_request is True
    session.append_event("session_run_cancel_requested", {"reason": "user_cancelled"})
    for event_payload in resolved_approvals:
        session.append_event("approval_resolved", event_payload)
    session.append_event("session_run_cancelled", {"reason": "user_cancelled"})
    session.mark_done("user_cancelled")
    time.sleep(0.2)

    events = session.events
    assert admin.payloads == []
    assert any(event["type"] == "session_run_cancelled" for event in events)
    assert not any(event["type"] == "session_run_end" for event in events)
    assert not any(
        event["type"] == "workflow_result"
        and event["payload"].get("result_type") == "capability_package_install"
        for event in events
    )


def test_capability_package_session_persists_agent_run_progress_and_failure(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("failed draft generation must not install")

    persisted: list[dict[str, object]] = []

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": dict(payload),
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-trace",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        trace_event_sink=trace_sink,
    )
    session.enable_trace_persistence("session-1")
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_id="peer-1",
    )
    assert claim is not None
    assert claim.task.id == agent_run_id
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent.status("preparing_worktree"),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent.status("worktree_ready", workdir="/tmp/work"),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    for idx in range(250):
        control.append_executor_event(
            str(agent_run_id),
            ExecutorEvent.text_event(f"progress line {idx}"),
            request_id=claim.request_id,
            worker_id="worker-1",
            peer_id="peer-1",
        )
    control.complete_claimed_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="failed",
            output="",
            error="No model provider/profile is configured.",
        ),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )

    _wait_for(lambda: session.done)

    phases = [
        event["payload"].get("details", {}).get("phase")
        for event in persisted
        if event["event_type"] == "workflow_step"
    ]
    assert "agent_run_queued" in phases
    assert "agent_run_claimed" in phases
    assert "agent_run_worktree_ready" in phases
    assert "agent_run_failed" in phases
    assistant_deltas = [
        event for event in persisted if event["event_type"] == "assistant_delta"
    ]
    assert len(assistant_deltas) < 250
    assert any(
        event["event_type"] == "error"
        and "No model provider/profile is configured."
        in str(event["payload"].get("message") or "")
        for event in persisted
    )


def test_remote_session_run_replays_pending_trace_events_when_sink_is_attached(
    tmp_path: Path,
) -> None:
    http_service = object.__new__(RemoteRelayHTTPService)
    http_service._session_runs_lock = threading.Lock()
    http_service._session_runs = {}
    http_service.session_trace_event_sink = None
    session = _RemoteSessionRun(
        session_run_id="session-run-late-sink",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    http_service._session_runs[session.session_run_id] = session
    session.enable_trace_persistence("session-1")
    session.append_event(
        "context_event",
        {"message": "sink not attached yet", "phase": "late_sink"},
    )
    persisted: list[dict[str, object]] = []

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": dict(payload),
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    http_service.set_session_trace_event_sink(trace_sink)

    assert persisted == [
        {
            "session_id": "session-1",
            "event_type": "context_event",
            "payload": {"message": "sink not attached yet", "phase": "late_sink"},
            "session_run_id": "session-run-late-sink",
            "session_run_seq": 1,
            "source": "remote_session_run",
            "replayable": True,
        }
    ]


def test_remote_session_run_does_not_persist_when_sink_is_attached_without_trace_enable(
    tmp_path: Path,
) -> None:
    http_service = object.__new__(RemoteRelayHTTPService)
    http_service._session_runs_lock = threading.Lock()
    http_service._session_runs = {}
    http_service.session_trace_event_sink = None
    session = _RemoteSessionRun(
        session_run_id="session-run-memory-only",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    http_service._session_runs[session.session_run_id] = session
    session.append_event(
        "context_event",
        {"message": "memory only", "phase": "memory_only"},
    )
    persisted: list[dict[str, object]] = []

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": dict(payload),
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    http_service.set_session_trace_event_sink(trace_sink)

    assert persisted == []

    session.enable_trace_persistence("session-1")

    assert persisted == [
        {
            "session_id": "session-1",
            "event_type": "context_event",
            "payload": {"message": "memory only", "phase": "memory_only"},
            "session_run_id": "session-run-memory-only",
            "session_run_seq": 1,
            "source": "remote_session_run",
            "replayable": True,
        }
    ]


def test_capability_package_session_surfaces_source_bundle_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("source warning test should not install")

    def fake_fetch(self, **kwargs: object) -> str:
        return json.dumps(
            {
                "ok": False,
                "url": kwargs["url"],
                "title": "",
                "sections": [],
                "links": [],
                "evidence": [],
                "errors": [
                    {
                        "code": "fetch_failed",
                        "message": "The read operation timed out",
                        "url": kwargs["url"],
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
        fake_fetch,
    )
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-source-warning",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {"docsUrl": "https://docs.example.com/example-tool"},
    )

    warning = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "output"
                and event["payload"].get("level") == "warning"
            ),
            None,
        )
    )
    assert warning["code"] == "fetch_failed"
    assert warning["content"] == "未能从仓库或文档中抓取到可用于能力包生成的资料。"
    assert "The read operation timed out" not in warning["content"]
    assert warning["source_error"]["message"] == "The read operation timed out"
    assert warning["source_errors"][0]["message"] == "The read operation timed out"

    _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_softens_partial_source_fetch_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("partial source warning test should not install")

    def fake_fetch(self, **kwargs: object) -> str:
        return json.dumps(
            {
                "ok": True,
                "url": kwargs["url"],
                "title": "Example Tool",
                "sections": [
                    {
                        "heading": "Install",
                        "source_url": f"{kwargs['url']}#install",
                        "text": "Install with npm.",
                    }
                ],
                "links": [],
                "evidence": [
                    {
                        "title": "Install",
                        "source_url": f"{kwargs['url']}#install",
                        "excerpt": "Install with npm.",
                    }
                ],
                "errors": [
                    {
                        "code": "network_error",
                        "message": "Remote end closed connection without response",
                        "url": kwargs["url"],
                        "attempts": 3,
                        "retryable": True,
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
        fake_fetch,
    )
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-partial-source-warning",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(session, {"docsUrl": "https://docs.example.com/example-tool"})

    warning = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "output"
                and event["payload"].get("level") == "warning"
            ),
            None,
        )
    )
    assert warning["code"] == "network_error"
    assert warning["content"] == "部分在线资料读取失败，已继续使用可读取内容生成草案。"
    assert "Remote end closed" not in warning["content"]
    assert warning["source_error"]["message"] == "Remote end closed connection without response"
    assert warning["source_errors"][0]["attempts"] == 3

    _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_surfaces_empty_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("empty evidence test should not install")

    def fake_fetch(self, **kwargs: object) -> str:
        return json.dumps(
            {
                "ok": True,
                "url": kwargs["url"],
                "title": "Empty docs",
                "sections": [],
                "links": [],
                "evidence": [],
                "errors": [],
            }
        )

    monkeypatch.setattr(
        "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
        fake_fetch,
    )
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-empty-evidence",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(session, {"docsUrl": "https://docs.example.com/empty-tool"})

    warning = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "output"
                and event["payload"].get("level") == "warning"
            ),
            None,
        )
    )
    assert warning["code"] == "source_evidence_empty"
    assert "未能从仓库或文档中抓取到可用于能力包生成的资料" in warning["content"]

    _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)
