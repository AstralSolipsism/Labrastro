"""Capability package ingestion through a dedicated AgentRun."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from labrastro_server.services.agent_runtime.session_projection import (
    AgentRunSessionProjectionLabels,
    agent_run_events_to_session_events,
    agent_run_event_to_session_events,
)
from reuleauxcoder.domain.agent_runtime.models import (
    AgentRunRecord,
    CAPABILITY_COMPONENT_KINDS,
    CapabilityComponentConfig,
    CapabilityPackageConfig,
    CapabilityPackageDraft,
    PublishPolicy,
    TriggerMode,
    WorktreeRole,
)
from reuleauxcoder.domain.config.models import (
    DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
    EnvironmentRequirementConfig,
    MCPServerConfig,
    SkillRegistrationConfig,
    ensure_default_capability_packages,
)
from reuleauxcoder.domain.environment_requirements import (
    ENVIRONMENT_COMMAND_FIELDS,
    ENVIRONMENT_REQUIREMENT_KINDS,
    normalize_environment_requirement_id,
    resolve_environment_requirement_kind,
)
from reuleauxcoder.domain.runtime_footprint import (
    aggregate_runtime_footprint,
    normalize_runtime_footprint,
    runtime_footprint_for_skill,
)
from reuleauxcoder.extensions.skills.parser import parse_skill_content
from reuleauxcoder.domain.session.locale import (
    normalize_session_locale,
    session_locale_prompt_append,
)
from reuleauxcoder.extensions.tools.builtin.fetch_capabilities import FetchCapabilitiesTool


LOGGER = logging.getLogger(__name__)
CAPABILITY_INGEST_WORKFLOW = "capability_package_ingest"
_CAPABILITY_AGENT_TOOL_EVENTS = {
    "tool_call_start",
    "tool_call_end",
    "tool_call_delta",
    "tool_call_stream",
    "tool_call_protocol_error",
}
_CAPABILITY_READ_SOURCE_TOOL_NAMES = {
    "cat",
    "fetch_capabilities",
    "find",
    "glob",
    "grep",
    "ls",
    "list",
    "list_dir",
    "list_directory",
    "list_file",
    "list_files",
    "read",
    "read_file",
    "read_files",
    "search",
    "search_file",
    "search_files",
}
_CAPABILITY_TEXT: dict[str, dict[str, str]] = {
    "zh-CN": {
        "queued_title": "能力包生成任务已排队",
        "claimed_title": "能力包生成任务已被 sandbox worker 接收",
        "session_ready_title": "能力包执行环境已就绪",
        "session_ready_with_workdir_title": "能力包执行环境已就绪：{workdir}",
        "completed_title": "能力包草案生成完成",
        "failed_title": "能力包草案生成失败",
        "cancelled_title": "能力包草案生成已取消",
        "blocked_title": "能力包草案生成被阻断",
        "start_draft": "开始生成能力包草案",
        "ingest_bound": "能力包生成任务已进入 capability_packager",
        "revision_bound": "能力包修改任务已进入 capability_packager",
        "draft_ready": "能力包草案 {package_id} 已生成",
        "revision_approval_reason": "收到修改意见，重新生成草案。",
        "install_title": "安装能力包 {package_id}",
        "revision_ack": "已收到修改意见，重新生成能力包草案。",
        "revision_requested": "收到修改意见，重新生成能力包草案",
        "revision_requested_with_text": "收到修改意见：{instruction}",
        "install_cancelled": "已取消安装能力包 {package_id}。",
        "install_completed": "能力包 {package_id} 已安装完成。",
        "source_empty": "未能从仓库或文档中抓取到可用于能力包生成的资料。",
        "source_partial": "部分在线资料读取失败，已继续使用可读取内容生成草案。",
        "approval_package_title": "能力包",
        "approval_name": "名称",
        "approval_risk": "风险",
        "approval_components_title": "组件摘要",
        "approval_capabilities": "能力",
        "approval_dependencies": "依赖",
        "approval_runtime_title": "运行责任",
        "approval_runtime_summary": "运行责任",
        "approval_install_required_on": "安装位置",
        "approval_config_required_on": "配置位置",
        "approval_none": "无",
        "approval_install_plan": "安装计划",
        "approval_intent": "确认安装能力包 {package_id}",
        "approval_content": "准备安装能力包 {package_id}。",
        "approval_allow": "安装",
        "approval_deny": "取消",
        "tool_read_source": "读取能力包来源",
        "tool_extract_evidence": "提取能力包证据",
        "materialize_draft": "组装并校验能力包草案",
        "skill_content_unresolved": "无法定位能力包 Skill 内容",
        "command_evidence_missing": "能力包依赖命令缺少来源证据",
        "source_discovery_incomplete": "能力包来源探索不完整",
        "draft_not_produced": "未生成可安装的能力包草案",
        "draft_invalid": "能力包草案未通过校验",
        "output_truncated_marker": "\n... 内容已从主时间线省略，请打开原始事件查看完整内容 ...\n",
    },
    "en": {
        "queued_title": "Capability package generation task queued",
        "claimed_title": "Capability package generation task accepted by sandbox worker",
        "session_ready_title": "Capability package execution environment ready",
        "session_ready_with_workdir_title": "Capability package execution environment ready: {workdir}",
        "completed_title": "Capability package draft generation completed",
        "failed_title": "Capability package draft generation failed",
        "cancelled_title": "Capability package draft generation cancelled",
        "blocked_title": "Capability package draft generation blocked",
        "start_draft": "Starting capability package draft generation",
        "ingest_bound": "Capability package generation task entered capability_packager",
        "revision_bound": "Capability package revision task entered capability_packager",
        "draft_ready": "Capability package draft {package_id} is ready",
        "revision_approval_reason": "Received revision feedback; regenerating draft.",
        "install_title": "Install capability package {package_id}",
        "revision_ack": "Received revision feedback; regenerating the capability package draft.",
        "revision_requested": "Revision feedback received; regenerating capability package draft",
        "revision_requested_with_text": "Revision feedback received: {instruction}",
        "install_cancelled": "Cancelled installing capability package {package_id}.",
        "install_completed": "Capability package {package_id} installed.",
        "source_empty": "No usable material could be fetched from the repository or documentation for capability package generation.",
        "source_partial": "Some online material could not be read; continuing with the readable content to generate the draft.",
        "approval_package_title": "Capability package",
        "approval_name": "Name",
        "approval_risk": "Risk",
        "approval_components_title": "Component summary",
        "approval_capabilities": "Capabilities",
        "approval_dependencies": "Dependencies",
        "approval_runtime_title": "Runtime footprint",
        "approval_runtime_summary": "Runtime responsibility",
        "approval_install_required_on": "Install required on",
        "approval_config_required_on": "Config required on",
        "approval_none": "None",
        "approval_install_plan": "Install plan",
        "approval_intent": "Confirm installing capability package {package_id}",
        "approval_content": "Preparing to install capability package {package_id}.",
        "approval_allow": "Install",
        "approval_deny": "Cancel",
        "tool_read_source": "Reading capability package source",
        "tool_extract_evidence": "Extracting capability package evidence",
        "materialize_draft": "Assembling and validating capability package draft",
        "skill_content_unresolved": "Could not resolve capability package Skill content",
        "command_evidence_missing": "Capability package dependency commands lack source evidence",
        "source_discovery_incomplete": "Capability package source discovery is incomplete",
        "draft_not_produced": "No installable capability package draft was produced",
        "draft_invalid": "Capability package draft did not pass validation",
        "output_truncated_marker": "\n... output omitted from the main timeline; open raw events for the complete content ...\n",
    },
}
MAX_SNIPPET_CHARS = 36_000
DEFAULT_CAPABILITY_FOCUS = "install setup configure authentication requirements runtime sdk executable mcp skill"
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_CAPABILITY_COMPONENT_CONFIG_FIELDS = {
    "command",
    "args",
    "env",
    "cwd",
    "placement",
    "runtime_footprint",
    "distribution",
    "environment_requirement_refs",
    "scope",
    "check",
    "install",
    "version",
    "source",
    "description",
    "path_hint",
    "repo_url",
    "docs",
    "evidence",
    "credentials",
    "risk_level",
    "access",
    "execution_policy",
    "registry_path",
    "source_path",
    "install_prompt",
    "verify_prompt",
    "notes",
    "configure",
    "runtime",
    "language",
    "path",
    "value",
}


def _capability_locale(value: object) -> str:
    text = str(value or "").strip()
    return normalize_session_locale(text) if text else "zh-CN"


def _session_locale(session: Any) -> str:
    return _capability_locale(getattr(session, "locale", "") or "")


def _capability_text(locale: object, key: str, **values: object) -> str:
    normalized = _capability_locale(locale)
    labels = _CAPABILITY_TEXT.get(normalized) or _CAPABILITY_TEXT["en"]
    template = labels.get(key) or _CAPABILITY_TEXT["en"].get(key) or key
    return template.format(**{name: str(value) for name, value in values.items()})


def _capability_agent_run_projection_labels(locale: object) -> AgentRunSessionProjectionLabels:
    return AgentRunSessionProjectionLabels(
        agent_id=DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
        workflow=CAPABILITY_INGEST_WORKFLOW,
        queued_title=_capability_text(locale, "queued_title"),
        claimed_title=_capability_text(locale, "claimed_title"),
        session_ready_title=_capability_text(locale, "session_ready_title"),
        session_ready_with_workdir_title=_capability_text(
            locale,
            "session_ready_with_workdir_title",
            workdir="{workdir}",
        ),
        log_fallback_title="capability_packager log",
        error_fallback_message="capability_packager error",
        output_truncation_marker=_capability_text(locale, "output_truncated_marker"),
        terminal_titles={
            "completed": _capability_text(locale, "completed_title"),
            "failed": _capability_text(locale, "failed_title"),
            "cancelled": _capability_text(locale, "cancelled_title"),
            "blocked": _capability_text(locale, "blocked_title"),
        },
    )


class CapabilityPackageIngestError(Exception):
    """HTTP-safe capability package ingestion error."""

    def __init__(
        self,
        error: str,
        message: str,
        *,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status


@dataclass(frozen=True)
class EvidenceBundle:
    """Source material and immutable evidence used to draft a capability package."""

    source: dict[str, Any]
    documents: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    links: list[dict[str, Any]] | None = None
    errors: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": dict(self.source),
            "documents": [dict(item) for item in self.documents],
            "evidence": [dict(item) for item in self.evidence],
            "links": [dict(item) for item in self.links or []],
            "errors": [dict(item) for item in self.errors or []],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "EvidenceBundle":
        data: dict[str, Any] = value if isinstance(value, dict) else {}
        source = data.get("source")
        return cls(
            source=dict(source) if isinstance(source, dict) else {},
            documents=[
                dict(item)
                for item in data.get("documents", [])
                if isinstance(item, dict)
            ],
            evidence=[
                dict(item)
                for item in data.get("evidence", [])
                if isinstance(item, dict)
            ],
            links=[
                dict(item)
                for item in data.get("links", [])
                if isinstance(item, dict)
            ],
            errors=[
                dict(item)
                for item in data.get("errors", [])
                if isinstance(item, dict)
            ],
        )


@dataclass(frozen=True)
class CapabilityPackageIngestResult:
    agent_run: AgentRunRecord
    source: dict[str, Any]
    source_bundle: dict[str, Any]


@dataclass(frozen=True)
class CapabilityDraftValidationResult:
    ok: bool
    messages: list[str]
    draft: CapabilityPackageDraft | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "messages": list(self.messages),
        }
        if self.draft is not None:
            result["draft"] = self.draft.to_dict()
        return result


@dataclass(frozen=True)
class CapabilitySourceInventory:
    """AgentRun-discovered source files and evidence used for materialization."""

    files: list[dict[str, Any]] = field(default_factory=list)
    skill_files: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_event_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [dict(item) for item in self.files],
            "skill_files": [dict(item) for item in self.skill_files],
            "documents": [
                _public_inventory_document(item) for item in self.documents
            ],
            "evidence": [dict(item) for item in self.evidence],
            "links": [dict(item) for item in self.links],
            "tool_calls": [dict(item) for item in self.tool_calls],
            "raw_event_refs": [dict(item) for item in self.raw_event_refs],
        }


@dataclass(frozen=True)
class CapabilityPackageSkillFileOperation:
    action: str
    path: Path
    content: str = ""


@dataclass(frozen=True)
class CapabilityPackageInstallResult:
    package_id: str
    package: CapabilityPackageConfig
    component_ids: list[str]
    skill_file_operations: list[CapabilityPackageSkillFileOperation] = field(
        default_factory=list
    )


class CapabilitySourceCollector:
    """Collect source seeds before AgentRun performs adaptive discovery."""

    def __init__(self, fetch_tool: Any | None = None) -> None:
        self.fetch_tool = fetch_tool or FetchCapabilitiesTool()

    def collect(self, payload: dict[str, Any]) -> EvidenceBundle:
        source = _normalize_source(payload)
        documents: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        notes = str(source.get("notes") or "").strip()
        if notes:
            note_excerpt = _truncate(notes, 360)
            documents.append(
                {
                    "title": "Project notes",
                    "url": "",
                    "content": notes[:MAX_SNIPPET_CHARS],
                    "source_kind": "project_notes",
                }
            )
            evidence.append(
                {
                    "title": "Project notes",
                    "source_url": "",
                    "excerpt": note_excerpt,
                }
            )

        source_type = str(source.get("type") or "")
        url = str(source.get("url") or "")
        if source_type == "github_repo" and url:
            documents.append(
                {
                    "title": "GitHub repository",
                    "url": url,
                    "content": f"Repository URL: {url}",
                    "source_kind": "source_seed",
                }
            )
            evidence.append(
                {
                    "title": "GitHub repository",
                    "source_url": url,
                    "excerpt": f"Repository URL: {url}",
                }
            )
        elif source_type == "docs_url" and url:
            payload = self._fetch_source(url=url, source_type=source_type)
            document = _document_from_fetch_payload(payload)
            if document is not None:
                documents.append(document)
            evidence.extend(_dict_list(payload.get("evidence")))
            links.extend(_dict_list(payload.get("links")))
            errors.extend(_dict_list(payload.get("errors")))

        return EvidenceBundle(
            source=source,
            documents=documents[:8],
            evidence=_dedupe_evidence(evidence),
            links=_dedupe_links(links),
            errors=errors,
        )

    def _fetch_source(self, *, url: str, source_type: str) -> dict[str, Any]:
        try:
            raw = self.fetch_tool.execute(
                url=url,
                focus=DEFAULT_CAPABILITY_FOCUS,
                source_hint=source_type,
                max_chars=MAX_SNIPPET_CHARS,
            )
            parsed = json.loads(raw)
        except Exception as exc:
            return {
                "ok": False,
                "url": url,
                "title": "",
                "sections": [],
                "links": [],
                "evidence": [],
                "errors": [{"code": "fetch_capabilities_failed", "message": str(exc)}],
            }
        return parsed if isinstance(parsed, dict) else {}


class CapabilityPackagerRunner:
    """Submit and inspect package-drafting AgentRuns."""

    def __init__(
        self,
        runtime_control_plane: AgentRunControlPlane,
        *,
        agent_id: str = DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
    ) -> None:
        self.runtime_control_plane = runtime_control_plane
        self.agent_id = agent_id

    def start(
        self,
        *,
        evidence_bundle: EvidenceBundle,
        workspace_root: str = "",
        agent_run_metadata: dict[str, Any] | None = None,
        revision_draft: dict[str, Any] | None = None,
        revision_instruction: str = "",
    ) -> AgentRunRecord:
        bundle = evidence_bundle.to_dict()
        locale = _metadata_locale(agent_run_metadata)
        prompt = _render_packager_prompt(
            bundle=bundle,
            locale=locale,
            revision_draft=revision_draft,
            revision_instruction=revision_instruction,
        )
        metadata = {
            "workflow": CAPABILITY_INGEST_WORKFLOW,
            "agent_run_source": "capability_ingest",
            "capability_source": evidence_bundle.source,
            "source_bundle": bundle,
        }
        for key in (
            "session_id",
            "session_run_id",
            "client_request_id",
            "workflow_mode",
            "revision_of_agent_run_id",
            "revision_followup_id",
            "locale",
        ):
            value = (agent_run_metadata or {}).get(key)
            if value is not None and str(value).strip():
                metadata[key] = (
                    normalize_session_locale(value) if key == "locale" else str(value)
                )
        revision_text = str(
            (agent_run_metadata or {}).get("revision_instruction")
            or revision_instruction
            or ""
        ).strip()
        if revision_text:
            metadata["revision_instruction"] = revision_text
        source_url = str(evidence_bundle.source.get("url") or "").strip()
        if evidence_bundle.source.get("type") == "github_repo" and source_url:
            metadata["repo_url"] = source_url
        if workspace_root:
            metadata["workspace_root"] = workspace_root
        return self.runtime_control_plane.submit_agent_run(
            AgentRunRequest(
                issue_id="capability-package-ingest",
                agent_id=self.agent_id,
                prompt=prompt,
                source="capability_ingest",
                trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
                worktree_role=WorktreeRole.SOURCE,
                publish_policy=PublishPolicy.NEVER,
                workdir=workspace_root or None,
                parent_task_id=str(metadata.get("revision_of_agent_run_id") or "") or None,
                parent_run_id=str(metadata.get("revision_of_agent_run_id") or "") or None,
                metadata=metadata,
            )
        )

    def status(self, agent_run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        agent_run = self.runtime_control_plane.agent_run_to_dict(agent_run_id)
        events = [
            event.to_dict()
            for event in self.runtime_control_plane.list_events(
                agent_run_id,
                after_seq=0,
                limit=1000,
            )
        ]
        return agent_run, events


class CapabilityDraftValidator:
    """Run deterministic checks against package drafts before installation."""

    def validate(
        self,
        raw_draft: dict[str, Any] | None,
        evidence_bundle: EvidenceBundle | None = None,
    ) -> CapabilityDraftValidationResult:
        data = raw_draft if isinstance(raw_draft, dict) else {}
        package_id = str(data.get("id") or "").strip()
        messages: list[str] = []
        if not package_id:
            messages.append("draft.id is required")
            package_id = "capability-package"
        try:
            draft = CapabilityPackageDraft.from_dict(package_id, data)
        except Exception as exc:
            return CapabilityDraftValidationResult(False, [str(exc)], None)

        if not draft.components:
            messages.append("draft.components must contain at least one component")
        for component in draft.components:
            messages.extend(_component_validation_messages(component))
            messages.extend(
                _component_command_evidence_messages(
                    component,
                    draft=draft,
                    evidence_bundle=evidence_bundle,
                )
            )
        if not draft.evidence:
            messages.append("draft.evidence is required")
        if not draft.risk_level:
            messages.append("risk_level is required")
        if "credentials" in data and not isinstance(data.get("credentials"), list):
            messages.append("credentials must be a list")
        if "install_plan" in data and not isinstance(data.get("install_plan"), list):
            messages.append("install_plan must be a list")
        if "usage" in data and not isinstance(data.get("usage"), list):
            messages.append("usage must be a list")
        return CapabilityDraftValidationResult(not messages, _unique_strings(messages), draft)


class CapabilityPackageInstaller:
    """Install confirmed drafts into capability package and component config."""

    def __init__(self, *, skill_install_root: str | Path | None = None) -> None:
        self.skill_install_root = Path(
            skill_install_root
            if skill_install_root is not None
            else Path.home() / ".rcoder" / "skills" / "packages"
        ).expanduser()
        self.skill_file_operations: list[CapabilityPackageSkillFileOperation] = []

    def install_draft(
        self,
        data: dict[str, Any],
        raw_draft: dict[str, Any],
        *,
        package_id: str = "",
    ) -> CapabilityPackageInstallResult:
        self.skill_file_operations = []
        resolved_package_id = str(
            package_id or raw_draft.get("id") or raw_draft.get("package_id") or ""
        ).strip()
        if not resolved_package_id:
            raise CapabilityPackageIngestError(
                "capability_package_id_required",
                "capability package id is required",
            )
        draft = CapabilityPackageDraft.from_dict(resolved_package_id, raw_draft)
        component_specs = [
            self.component_from_draft(resolved_package_id, item, draft.source.to_dict())
            for item in draft.components
        ]
        _apply_skill_related_requirement_footprints(component_specs)
        if not component_specs:
            raise CapabilityPackageIngestError(
                "capability_package_components_required",
                "capability package draft must contain at least one component",
            )

        components = data.setdefault("capability_components", {})
        if not isinstance(components, dict):
            components = {}
            data["capability_components"] = components
        packages = ensure_default_capability_packages(
            data.get("capability_packages", {})
            if isinstance(data.get("capability_packages"), dict)
            else {}
        )
        component_ids: list[str] = []
        installed_component_footprints: list[dict[str, Any]] = []
        for component in component_specs:
            existing_raw = components.get(component.id)
            if isinstance(existing_raw, dict):
                existing = CapabilityComponentConfig.from_dict(
                    component.id,
                    existing_raw,
                )
                if (
                    existing.kind != component.kind
                    or existing.name != component.name
                    or _stable_json(existing.config) != _stable_json(component.config)
                ):
                    raise CapabilityPackageIngestError(
                        "capability_component_conflict",
                        "shared component id already exists with different definition",
                        status=HTTPStatus.CONFLICT,
                    )
                existing.package_ids = _unique_strings(
                    [*existing.package_ids, resolved_package_id]
                )
                component = existing
            else:
                component.package_ids = _unique_strings(
                    [*component.package_ids, resolved_package_id]
                )
            components[component.id] = component.to_dict()
            component_ids.append(component.id)
            installed_component_footprints.append(component.runtime_footprint)
            self.materialize_component(data, component)

        package = CapabilityPackageConfig(
            id=resolved_package_id,
            name=draft.name or resolved_package_id,
            description=draft.description,
            source=draft.source,
            components=_unique_strings(component_ids),
            enabled=True,
            status="installed",
            install_plan=draft.install_plan,
            usage=draft.usage,
            effective_capabilities=draft.effective_capabilities,
            evidence=draft.evidence,
            credentials=draft.credentials,
            risk_level=draft.risk_level,
            notes=draft.notes,
            runtime_footprint=aggregate_runtime_footprint(installed_component_footprints),
        )
        packages[resolved_package_id] = package.to_dict()
        data["capability_packages"] = packages
        return CapabilityPackageInstallResult(
            package_id=resolved_package_id,
            package=package,
            component_ids=_unique_strings(component_ids),
            skill_file_operations=list(self.skill_file_operations),
        )

    def component_from_draft(
        self,
        package_id: str,
        item: dict[str, Any],
        package_source: dict[str, Any],
    ) -> CapabilityComponentConfig:
        kind = str(item.get("kind", item.get("type", "")) or "").strip().lower()
        if kind in ENVIRONMENT_REQUIREMENT_KINDS:
            item = dict(item)
            raw_config = item.get("config")
            config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, dict) else {}
            config.setdefault("kind", kind)
            item["kind"] = "environment_requirement"
            item["config"] = config
            kind = "environment_requirement"
        if kind not in CAPABILITY_COMPONENT_KINDS:
            raise ValueError(
                "component.kind must be one of "
                + ", ".join(sorted(CAPABILITY_COMPONENT_KINDS))
            )
        name = str(item.get("name", "") or "").strip()
        if not name:
            raise ValueError("component.name is required")
        raw_config = item.get("config")
        config = dict(raw_config) if isinstance(raw_config, dict) else {}
        skill_metadata = (
            _skill_metadata_from_content(str(config.get("skill_content") or item.get("skill_content") or item.get("content") or ""))
            if kind == "skill"
            else {}
        )
        if (
            kind == "environment_requirement"
            and "requirements" in item
            and "requirements" not in config
        ):
            config["requirements"] = item["requirements"]
        if kind == "skill":
            for field_name in ("skill_content", "content"):
                if field_name in item and field_name not in config:
                    config[field_name] = item[field_name]
        component_id = str(item.get("id") or "").strip()
        if not component_id:
            if kind == "environment_requirement":
                requirement_kind = resolve_environment_requirement_kind(
                    candidates=(
                        item.get("resource_kind"),
                        item.get("requirement_kind"),
                        config.get("resource_kind"),
                        config.get("requirement_kind"),
                        config.get("kind"),
                    ),
                    command=item.get("command") or config.get("command"),
                )
                component_id = normalize_environment_requirement_id(
                    kind=requirement_kind,
                    name=name,
                )
            else:
                component_id = f"{kind}:{name}"
        for field in _CAPABILITY_COMPONENT_CONFIG_FIELDS:
            if field in item and field not in config:
                config[field] = item[field]
        if kind == "environment_requirement":
            requirement_kind = resolve_environment_requirement_kind(
                component_id,
                candidates=(
                    item.get("resource_kind"),
                    item.get("requirement_kind"),
                    config.get("resource_kind"),
                    config.get("requirement_kind"),
                    config.get("kind"),
                ),
                command=config.get("command"),
            )
            config.setdefault("kind", requirement_kind)
        access = str(item.get("access") or "").strip().lower()
        if access not in {"read", "write", "both"}:
            access = ""
        execution_policy = str(item.get("execution_policy") or "inherit").strip().lower()
        if execution_policy not in {"allow", "deny", "require_user", "escalate", "inherit"}:
            execution_policy = "inherit"
        raw_runtime_footprint = item.get("runtime_footprint")
        if not isinstance(raw_runtime_footprint, dict):
            raw_runtime_footprint = config.get("runtime_footprint")
        return CapabilityComponentConfig(
            id=component_id,
            kind=kind,
            name=name,
            display_name=str(
                item.get("display_name")
                or config.get("display_name")
                or skill_metadata.get("name")
                or ""
            ),
            summary=str(
                item.get("summary")
                or config.get("summary")
                or skill_metadata.get("description")
                or ""
            ),
            enabled=_bool_field(item, "enabled", True),
            package_ids=[package_id],
            source=CapabilityPackageConfig.from_dict(
                package_id,
                {"source": item.get("source", package_source)},
            ).source,
            config=config,
            managed_by="capability_package",
            status=str(item.get("status", "installed") or "installed"),
            access=access,
            risk_level=str(item.get("risk") or item.get("risk_level") or "").strip().lower(),
            execution_policy=execution_policy,
            registry_path=str(item.get("registry_path") or "").strip(),
            source_path=str(item.get("source_path") or "").strip(),
            runtime_footprint=(
                normalize_runtime_footprint(raw_runtime_footprint)
                if isinstance(raw_runtime_footprint, dict) and raw_runtime_footprint
                else {}
            ),
        )

    def materialize_component(
        self,
        data: dict[str, Any],
        component: CapabilityComponentConfig,
    ) -> None:
        payload = dict(component.config)
        payload["enabled"] = component.enabled
        payload["component_id"] = component.id
        payload["package_ids"] = list(component.package_ids)
        payload["managed_by"] = "capability_package"
        if component.display_name:
            payload["display_name"] = component.display_name
        if component.summary:
            payload["summary"] = component.summary
        payload["runtime_footprint"] = dict(component.runtime_footprint)
        payload.setdefault("source", component.source.url or component.source.type)
        if component.source.url:
            payload.setdefault("repo_url", component.source.url)
        payload.setdefault("last_action", "capability_package_accept")
        if component.kind == "environment_requirement":
            items = _environment_requirement_items(data)
            payload["id"] = component.id
            _assert_materialized_resource_slot(items, component.id, component)
            requirement = EnvironmentRequirementConfig.from_dict(component.id, payload)
            items[requirement.id] = requirement.to_dict()
            return
        if component.kind in {"mcp", "mcp_server"}:
            items = _mcp_server_items(data)
            _assert_materialized_resource_slot(items, component.name, component)
            server = MCPServerConfig.from_dict(component.name, payload)
            items[server.name] = server.to_dict()
            return
        if component.kind == "skill":
            items = _skill_items(data)
            _assert_materialized_resource_slot(items, component.name, component)
            payload = self._materialized_skill_payload(component, payload)
            skill = SkillRegistrationConfig.from_dict(component.name, payload)
            items[skill.name] = skill.to_dict()

    def remove_materialized_component(
        self,
        data: dict[str, Any],
        component: CapabilityComponentConfig,
    ) -> None:
        if component.kind == "environment_requirement":
            items = _environment_requirement_items(data)
            item_id = component.id
        elif component.kind in {"mcp", "mcp_server"}:
            items = _mcp_server_items(data)
            item_id = component.name
        elif component.kind == "skill":
            items = _skill_items(data)
            item_id = component.name
        else:
            return
        current = items.get(item_id)
        if not isinstance(current, dict):
            return
        if str(current.get("component_id") or "") != component.id:
            return
        if str(current.get("managed_by") or "") != "capability_package":
            return
        if component.kind == "skill":
            self._queue_remove_canonical_skill_path(str(current.get("path_hint") or ""))
        del items[item_id]

    def _materialized_skill_payload(
        self,
        component: CapabilityComponentConfig,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        content = _skill_content_from_payload(payload)
        if not content:
            raise CapabilityPackageIngestError(
                "capability_package_skill_content_required",
                f"skill component '{component.name}' must include skill_content for canonical installation",
            )
        installed_path = self._canonical_skill_path(component.id)
        self.skill_file_operations.append(
            CapabilityPackageSkillFileOperation(
                action="write",
                path=installed_path,
                content=content,
            )
        )
        source_path = str(
            payload.get("source_path")
            or component.source_path
            or payload.get("path_hint")
            or ""
        ).strip()
        payload = dict(payload)
        payload["path_hint"] = str(installed_path)
        if source_path:
            payload["source_path"] = source_path
        payload.setdefault("description", component.description)
        payload.pop("skill_content", None)
        payload.pop("content", None)
        return payload

    def _canonical_skill_path(self, component_id: str) -> Path:
        return (
            self.skill_install_root
            / "components"
            / _slug_path_segment(component_id)
            / "SKILL.md"
        )

    def apply_skill_file_operations(
        self,
        operations: list[CapabilityPackageSkillFileOperation] | None = None,
    ) -> None:
        pending = operations if operations is not None else self.skill_file_operations
        for operation in pending:
            if operation.action == "write":
                operation.path.parent.mkdir(parents=True, exist_ok=True)
                operation.path.write_text(operation.content, encoding="utf-8")
                continue
            if operation.action == "delete":
                self._remove_canonical_skill_path(str(operation.path))

    def _queue_remove_canonical_skill_path(self, path_hint: str) -> None:
        path = self._canonical_removable_skill_path(path_hint)
        if path is None:
            return
        self.skill_file_operations.append(
            CapabilityPackageSkillFileOperation(action="delete", path=path)
        )

    def _remove_canonical_skill_path(self, path_hint: str) -> None:
        path = self._canonical_removable_skill_path(path_hint)
        if path is None:
            return
        shutil.rmtree(path.parent, ignore_errors=True)
        for parent in (path.parent.parent,):
            try:
                parent.rmdir()
            except OSError:
                pass

    def _canonical_removable_skill_path(self, path_hint: str) -> Path | None:
        if not path_hint:
            return None
        root = self.skill_install_root.resolve()
        path = Path(path_hint).expanduser().resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        if path.name != "SKILL.md":
            return None
        return path


class CapabilityPackageIngestService:
    """Orchestrate source collection and package-drafting AgentRuns."""

    def __init__(
        self,
        runtime_control_plane: AgentRunControlPlane | None = None,
        *,
        collector: CapabilitySourceCollector | None = None,
        packager_runner: CapabilityPackagerRunner | None = None,
        draft_validator: CapabilityDraftValidator | None = None,
    ) -> None:
        if packager_runner is None and runtime_control_plane is None:
            raise TypeError("runtime_control_plane or packager_runner is required")
        self.collector = collector or CapabilitySourceCollector()
        self.packager_runner = packager_runner or CapabilityPackagerRunner(
            runtime_control_plane  # type: ignore[arg-type]
        )
        self.draft_validator = draft_validator or CapabilityDraftValidator()

    def start(
        self,
        payload: dict[str, Any],
        *,
        agent_run_metadata: dict[str, Any] | None = None,
        revision_draft: dict[str, Any] | None = None,
        revision_instruction: str = "",
    ) -> CapabilityPackageIngestResult:
        raw_source = payload.get("source")
        source_payload: dict[str, Any] = raw_source if isinstance(raw_source, dict) else payload
        evidence_bundle = self.collector.collect(source_payload)
        workspace_root = str(payload.get("workspace_root") or "").strip()
        agent_run = self.packager_runner.start(
            evidence_bundle=evidence_bundle,
            workspace_root=workspace_root,
            agent_run_metadata=agent_run_metadata,
            revision_draft=revision_draft,
            revision_instruction=revision_instruction,
        )
        return CapabilityPackageIngestResult(
            agent_run=agent_run,
            source=evidence_bundle.source,
            source_bundle=evidence_bundle.to_dict(),
        )

    def status(self, agent_run_id: str) -> dict[str, Any]:
        task_id = str(agent_run_id or "").strip()
        if not task_id:
            raise CapabilityPackageIngestError(
                "agent_run_id_required",
                "agent_run_id is required",
            )
        try:
            agent_run, events = self.packager_runner.status(task_id)
        except KeyError as exc:
            raise CapabilityPackageIngestError(
                "agent_run_not_found",
                f"AgentRun not found: {task_id}",
                status=HTTPStatus.NOT_FOUND,
            ) from exc
        draft = _extract_draft(agent_run.get("output"))
        if draft is None:
            for event in reversed(events):
                payload = event.get("payload")
                if isinstance(payload, dict):
                    draft = _extract_draft(payload.get("text") or payload.get("output"))
                    if draft is not None:
                        break
        metadata = agent_run.get("metadata") if isinstance(agent_run, dict) else {}
        source_bundle = (
            metadata.get("source_bundle")
            if isinstance(metadata, dict) and isinstance(metadata.get("source_bundle"), dict)
            else {}
        )
        materialization_bundle = source_bundle
        if draft is not None:
            workspace_root = _agent_run_materialization_workdir(agent_run, metadata)
            materialization_bundle = _source_bundle_with_agent_run_documents(
                source_bundle,
                events,
            )
            draft = _canonical_capability_draft_from_decision(
                draft,
                materialization_bundle,
                workspace_root=workspace_root,
                sandbox_container_id=str(metadata.get("sandbox_container_id") or ""),
            )
        validation: dict[str, Any] | None = None
        if draft is not None:
            validation = self.draft_validator.validate(
                draft,
                EvidenceBundle.from_dict(materialization_bundle),
            ).to_dict()
        failure = _capability_draft_failure(
            draft,
            validation,
            materialization_bundle,
        )
        return {
            "ok": True,
            "agent_run": agent_run,
            "events": events,
            "draft": draft,
            "source_bundle": materialization_bundle,
            "validation": validation,
            "failure": failure,
        }


class CapabilityPackageSessionRunService:
    """Run capability package ingestion as a user-visible SessionRun."""

    TERMINAL_AGENT_STATUSES = {"completed", "failed", "cancelled", "blocked"}
    INSTALL_TOOL_NAME = "install_capability_package"

    def __init__(
        self,
        runtime_control_plane: AgentRunControlPlane,
        admin_manager: Any,
        *,
        poll_timeout_sec: float = 0.5,
    ) -> None:
        self.runtime_control_plane = runtime_control_plane
        self.admin_manager = admin_manager
        self.poll_timeout_sec = max(0.05, float(poll_timeout_sec or 0.5))

    def initial_runtime_state(self) -> dict[str, Any]:
        return _capability_session_runtime_state(self.runtime_control_plane)

    def start(self, session: Any, payload: dict[str, Any]) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(session, dict(payload)),
            daemon=True,
        )
        thread.start()

    def _run(self, session: Any, payload: dict[str, Any]) -> None:
        follow_up_lock = threading.Lock()
        follow_up_queue: list[dict[str, Any]] = []
        active_approval_id: dict[str, str] = {"value": ""}

        def on_follow_up(ticket: dict[str, Any]) -> None:
            approval_id = ""
            revision_reason = _capability_text(
                _session_locale(session),
                "revision_approval_reason",
            )
            followup_id = str(ticket.get("followup_id") or "").strip()
            with follow_up_lock:
                if followup_id and any(
                    str(item.get("followup_id") or "") == followup_id
                    for item in follow_up_queue
                ):
                    return
                follow_up_queue.append(dict(ticket))
                approval_id = active_approval_id.get("value", "")
            if approval_id:
                session.resolve_approval(
                    approval_id,
                    "deny_once",
                    revision_reason,
                )

        session.set_follow_up_callback(on_follow_up)
        try:
            if getattr(session, "cancel_requested", False):
                return
            session.mark_running()
            session.append_event(
                "workflow_step",
                _capability_workflow_step_event(
                    _capability_text(_session_locale(session), "start_draft"),
                    "prepare",
                    {"agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID},
                    status="running",
                ),
            )
            result = CapabilityPackageIngestService(self.runtime_control_plane).start(
                payload,
                agent_run_metadata=self._agent_run_metadata(session),
            )
            _append_source_bundle_notices(session, result.source_bundle)
            while True:
                if getattr(session, "cancel_requested", False):
                    return
                agent_run_id = result.agent_run.id
                self._bind_agent_run(session, result.agent_run)
                self._project_agent_run_events(session, agent_run_id)
                if getattr(session, "cancel_requested", False):
                    return
                completed = self._completed_draft_status(session, agent_run_id)
                if completed is None:
                    return
                draft, source_bundle = completed
                revision = self._request_install_approval(
                    session,
                    draft,
                    source_bundle,
                    agent_run_id,
                    follow_up_queue=follow_up_queue,
                    follow_up_lock=follow_up_lock,
                    active_approval_id=active_approval_id,
                )
                if revision is None:
                    return
                result = self._start_revision_ingest(
                    session,
                    payload,
                    source_bundle,
                    draft,
                    str(revision.get("text") or ""),
                    agent_run_id,
                    str(revision.get("followup_id") or ""),
                )
        except CapabilityPackageIngestError as exc:
            session.append_event("error", {"message": exc.message, "code": exc.error})
            session.append_event(
                "session_run_failed",
                {"message": exc.message, "code": exc.error, "recoverable": False},
            )
        except Exception as exc:
            LOGGER.exception(
                "Capability package SessionRun failed session_run_id=%s",
                getattr(session, "session_run_id", ""),
            )
            session.append_event(
                "error",
                {
                    "code": "capability_package_session_failed",
                    "message_key": "capability_package.session_failed",
                    "diagnostic_error_type": type(exc).__name__,
                    "diagnostic_message": str(exc),
                },
            )
            session.append_event(
                "session_run_failed",
                {
                    "code": "capability_package_session_failed",
                    "message_key": "capability_package.session_failed",
                    "recoverable": False,
                },
            )
        finally:
            session.set_follow_up_callback(None)
            session.mark_done()

    def _agent_run_metadata(
        self,
        session: Any,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "session_id": getattr(session, "session_id", None),
            "session_run_id": getattr(session, "session_run_id", None),
            "client_request_id": getattr(session, "client_request_id", None),
            "workflow_mode": CAPABILITY_INGEST_WORKFLOW,
        }
        locale = str(getattr(session, "locale", "") or "").strip()
        if locale:
            metadata["locale"] = normalize_session_locale(locale)
        if extra:
            metadata.update(extra)
        if metadata.get("locale"):
            metadata["locale"] = normalize_session_locale(metadata["locale"])
        return metadata

    def _bind_agent_run(self, session: Any, agent_run: AgentRunRecord) -> None:
        agent_run_id = agent_run.id
        session.set_cancel_callback(
            lambda reason, run_id=agent_run_id: self.runtime_control_plane.cancel_agent_run(
                run_id,
                reason=reason,
            )
        )
        _set_session_runtime_state(
            session,
            _capability_session_runtime_state(
                self.runtime_control_plane,
                agent_run=agent_run,
            ),
        )
        metadata = dict(agent_run.metadata or {})
        is_revision = bool(metadata.get("revision_of_agent_run_id"))
        locale = _session_locale(session)
        session.append_event(
            "workflow_step",
            _capability_workflow_step_event(
                _capability_text(locale, "revision_bound" if is_revision else "ingest_bound"),
                "read_source" if not is_revision else "extract_evidence",
                status="running",
                summary="capability_package_revision" if is_revision else "capability_package_ingest",
                extra={
                    "phase": "capability_package_revision" if is_revision else "capability_package_ingest",
                    "agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
                    "agent_run_id": agent_run_id,
                    **(
                        {
                            "revision_of_agent_run_id": str(metadata.get("revision_of_agent_run_id")),
                            "revision_followup_id": str(metadata.get("revision_followup_id")),
                        }
                        if is_revision
                        else {}
                    ),
                },
            ),
        )

    def _completed_draft_status(
        self,
        session: Any,
        agent_run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        status = CapabilityPackageIngestService(self.runtime_control_plane).status(agent_run_id)
        task = status.get("agent_run") if isinstance(status.get("agent_run"), dict) else {}
        task_status = str(task.get("status") or "").strip()
        if task_status in {"failed", "blocked", "cancelled"}:
            events = status.get("events") if isinstance(status.get("events"), list) else []
            message = _agent_run_terminal_message(task, events, task_status)
            session.append_event("error", {"message": message, "code": task_status})
            session.append_event(
                "session_run_failed",
                {"message": message, "code": task_status, "recoverable": False},
            )
            return None
        locale = _session_locale(session)
        session.append_event(
            "workflow_step",
            _capability_workflow_step_event(
                _capability_text(locale, "materialize_draft"),
                "materialize_draft",
                {
                    "phase": "capability_package_materialization",
                    "agent_run_id": agent_run_id,
                    "agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
                },
                status="running",
            ),
        )
        draft = status.get("draft")
        validation = status.get("validation") if isinstance(status.get("validation"), dict) else {}
        messages = [
            str(item)
            for item in validation.get("messages", [])
            if str(item).strip()
        ] if isinstance(validation.get("messages"), list) else []
        if not isinstance(draft, dict) or messages:
            failure = (
                dict(status.get("failure"))
                if isinstance(status.get("failure"), dict)
                else _capability_draft_failure(
                    draft if isinstance(draft, dict) else None,
                    validation,
                    status.get("source_bundle") if isinstance(status.get("source_bundle"), dict) else {},
                )
            )
            result_type = str(failure.get("result_type") or "invalid_capability_package_draft")
            title = str(
                failure.get("title")
                or _capability_text(
                    locale,
                    result_type if result_type in _CAPABILITY_TEXT["en"] else "draft_invalid",
                )
            )
            session.append_event(
                "workflow_step",
                _capability_workflow_step_event(
                    title,
                    "materialize_draft",
                    {
                        "phase": result_type,
                        "agent_run_id": agent_run_id,
                        "messages": messages,
                    },
                    status="error",
                ),
            )
            session.append_event(
                "workflow_result",
                _capability_workflow_result_event(
                    title,
                    "error",
                    result_type,
                    failure,
                ),
            )
            session.append_event(
                "error",
                {
                    "message": title,
                    "code": result_type,
                    "details": failure,
                },
            )
            session.append_event(
                "session_run_failed",
                {
                    "message": title,
                    "code": result_type,
                    "recoverable": False,
                },
            )
            return None
        source_bundle = status.get("source_bundle") if isinstance(status.get("source_bundle"), dict) else {}
        session.append_event(
            "workflow_step",
            _capability_workflow_step_event(
                _capability_text(locale, "materialize_draft"),
                "materialize_draft",
                {
                    "phase": "capability_package_materialization",
                    "agent_run_id": agent_run_id,
                    "validation": validation,
                },
                status="done",
            ),
        )
        session.append_event(
            "workflow_artifact",
            _capability_package_artifact_event_payload(
                draft,
                source_bundle,
                agent_run_id,
                validation,
                locale=locale,
            ),
        )
        return draft, source_bundle

    def _start_revision_ingest(
        self,
        session: Any,
        payload: dict[str, Any],
        source_bundle: dict[str, Any],
        draft: dict[str, Any],
        instruction: str,
        previous_agent_run_id: str,
        followup_id: str,
    ) -> CapabilityPackageIngestResult:
        evidence_bundle = EvidenceBundle.from_dict(source_bundle)
        workspace_root = str(payload.get("workspace_root") or "").strip()
        agent_run = CapabilityPackagerRunner(self.runtime_control_plane).start(
            evidence_bundle=evidence_bundle,
            workspace_root=workspace_root,
            revision_draft=draft,
            revision_instruction=instruction,
            agent_run_metadata=self._agent_run_metadata(
                session,
                {
                    "revision_of_agent_run_id": previous_agent_run_id,
                    "revision_followup_id": followup_id,
                    "revision_instruction": instruction,
                },
            ),
        )
        return CapabilityPackageIngestResult(
            agent_run=agent_run,
            source=evidence_bundle.source,
            source_bundle=evidence_bundle.to_dict(),
        )

    def _project_agent_run_events(self, session: Any, agent_run_id: str) -> None:
        cursor = 0
        while True:
            events = self.runtime_control_plane.wait_events(
                agent_run_id,
                after_seq=cursor,
                timeout_sec=self.poll_timeout_sec,
                limit=100,
            )
            event_dicts: list[dict[str, Any]] = []
            for event in events:
                cursor = max(cursor, int(event.seq))
                event_dicts.append(event.to_dict())
            if event_dicts and len(event_dicts) < 100:
                time.sleep(min(0.05, self.poll_timeout_sec))
                extra_events = self.runtime_control_plane.list_events(
                    agent_run_id,
                    after_seq=cursor,
                    limit=100 - len(event_dicts),
                )
                for event in extra_events:
                    cursor = max(cursor, int(event.seq))
                    event_dicts.append(event.to_dict())
            for session_event_type, payload in agent_run_events_to_session_events(
                event_dicts,
                labels=_capability_agent_run_projection_labels(_session_locale(session)),
                terminal_message=_agent_run_terminal_message,
            ):
                projected = _capability_projected_session_event(
                    session_event_type,
                    payload,
                    locale=_session_locale(session),
                )
                if projected is not None:
                    session.append_event(projected[0], projected[1])
            try:
                task = self.runtime_control_plane.agent_run_to_dict(agent_run_id)
            except KeyError:
                return
            status = str(task.get("status") or "").strip()
            if status in self.TERMINAL_AGENT_STATUSES:
                if not any(str(event.get("type") or "") in self.TERMINAL_AGENT_STATUSES for event in event_dicts):
                    session.append_event(
                        "workflow_step",
                        _capability_workflow_step_event(
                            _agent_run_terminal_message(task, event_dicts, status),
                            "compose_draft",
                            status=_workflow_status_from_agent_run_status(status),
                            summary=f"agent_run_{status}",
                            extra={
                                "phase": f"agent_run_{status}",
                                "agent_run_id": agent_run_id,
                                "agent_run_status": status,
                                "agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
                                "terminal_reason": _agent_run_terminal_reason(task, status),
                            },
                        ),
                    )
                return
            if getattr(session, "cancel_requested", False):
                return

    def _request_install_approval(
        self,
        session: Any,
        draft: dict[str, Any],
        source_bundle: Any,
        agent_run_id: str,
        *,
        follow_up_queue: list[dict[str, Any]],
        follow_up_lock: threading.Lock,
        active_approval_id: dict[str, str],
    ) -> dict[str, Any] | None:
        package_id = str(draft.get("id") or "capability-package").strip()
        approval_id = f"capability-package-install:{session.session_run_id}:{agent_run_id}:{package_id}"
        tool_call_id = f"capability-package-install:{agent_run_id or session.session_run_id}"
        locale = _session_locale(session)
        approval_payload = _capability_install_decision_payload(
            approval_id,
            tool_call_id,
            draft,
            agent_run_id,
            locale=locale,
        )
        session.register_approval(approval_id, approval_payload)
        session.append_event("workflow_decision", approval_payload)
        with follow_up_lock:
            active_approval_id["value"] = approval_id
            has_pending_revision = bool(follow_up_queue)
        if has_pending_revision:
            session.resolve_approval(
                approval_id,
                "deny_once",
                _capability_text(locale, "revision_approval_reason"),
            )
        try:
            decision, reason = session.wait_approval(approval_id)
        finally:
            with follow_up_lock:
                if active_approval_id.get("value") == approval_id:
                    active_approval_id["value"] = ""
        if getattr(session, "cancel_requested", False) or getattr(session, "done", False):
            return None
        revision_ticket = self._pop_revision_ticket(session, follow_up_queue, follow_up_lock)
        if revision_ticket is not None:
            session.append_event(
                "approval_resolved",
                {
                    "approval_id": approval_id,
                    "tool_call_id": tool_call_id,
                    "decision": "deny_once",
                    "reason": _capability_text(locale, "revision_approval_reason"),
                },
            )
            response = _capability_text(locale, "revision_ack")
            session.append_event(
                "workflow_result",
                _capability_workflow_result_event(
                    response,
                    "cancelled",
                    "capability_package_install",
                    {"package_id": package_id, "agent_run_id": agent_run_id},
                ),
            )
            followup_id = str(revision_ticket.get("followup_id") or "")
            if followup_id:
                session.mark_follow_up_consumed(followup_id)
            instruction = str(revision_ticket.get("text") or "").strip()
            instruction_title = (
                _capability_text(
                    locale,
                    "revision_requested_with_text",
                    instruction=_truncate_single_line(instruction, 80),
                )
                if instruction
                else _capability_text(locale, "revision_requested")
            )
            session.append_event(
                "workflow_step",
                _capability_workflow_step_event(
                    instruction_title,
                    "compose_draft",
                    status="running",
                    summary="capability_package_revision_requested",
                    extra={
                        "phase": "capability_package_revision_requested",
                        "agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
                        "agent_run_id": agent_run_id,
                        "followup_id": followup_id,
                        "instruction": instruction,
                    },
                ),
            )
            return dict(revision_ticket)
        session.append_event(
            "approval_resolved",
            {
                "approval_id": approval_id,
                "tool_call_id": tool_call_id,
                "decision": decision,
                **({"reason": reason} if reason else {}),
            },
        )
        if decision != "allow_once":
            response = _capability_text(locale, "install_cancelled", package_id=package_id)
            session.append_event(
                "workflow_result",
                _capability_workflow_result_event(
                    response,
                    "cancelled",
                    "capability_package_install",
                    {"package_id": package_id, "agent_run_id": agent_run_id},
                ),
            )
            session.append_event("session_run_end", {"response": response, "response_rendered": True})
            return None
        session.append_event(
            "workflow_step",
            _capability_workflow_step_event(
                _capability_text(locale, "install_title", package_id=package_id),
                "install",
                status="running",
                extra={"package_id": package_id, "agent_run_id": agent_run_id},
            ),
        )
        install_payload = {
            "draft": draft,
            "source_bundle": source_bundle if isinstance(source_bundle, dict) else {},
        }
        result = self.admin_manager.accept_capability_package_draft(install_payload)
        if not getattr(result, "ok", False):
            payload = getattr(result, "payload", {})
            message = (
                str(payload.get("message") or payload.get("error") or "")
                if isinstance(payload, dict)
                else ""
            ).strip() or "capability package install failed"
            session.append_event("error", {"message": message, "code": "capability_package_install_failed"})
            session.append_event(
                "workflow_result",
                _capability_workflow_result_event(
                    message,
                    "error",
                    "capability_package_install",
                    {"package_id": package_id, "agent_run_id": agent_run_id, "result": payload if isinstance(payload, dict) else {}},
                ),
            )
            session.append_event(
                "session_run_failed",
                {
                    "message": message,
                    "code": "capability_package_install_failed",
                    "recoverable": False,
                },
            )
            return None
        response = _capability_text(locale, "install_completed", package_id=package_id)
        session.append_event(
            "workflow_result",
            _capability_workflow_result_event(
                response,
                "done",
                "capability_package_install",
                {
                    "package_id": package_id,
                    "agent_run_id": agent_run_id,
                    "result": getattr(result, "payload", {}),
                },
            ),
        )
        session.append_event("session_run_end", {"response": response, "response_rendered": True})
        return None

    def _pop_revision_ticket(
        self,
        session: Any,
        follow_up_queue: list[dict[str, Any]],
        follow_up_lock: threading.Lock,
    ) -> dict[str, Any] | None:
        while True:
            with follow_up_lock:
                if not follow_up_queue:
                    return None
                ticket = follow_up_queue.pop(0)
            followup_id = str(ticket.get("followup_id") or "").strip()
            if not followup_id:
                return dict(ticket)
            cond = getattr(session, "cond", None)
            if cond is None:
                return dict(ticket)
            with cond:
                live_ticket = getattr(session, "follow_up_tickets", {}).get(followup_id)
                if not isinstance(live_ticket, dict):
                    return dict(ticket)
                if live_ticket.get("state") == "pending":
                    return dict(live_ticket)


def _normalize_source(payload: dict[str, Any]) -> dict[str, Any]:
    source_type = str(payload.get("type") or "").strip().lower()
    url = str(
        payload.get("url")
        or payload.get("repoUrl")
        or payload.get("repo_url")
        or payload.get("docsUrl")
        or payload.get("docs_url")
        or ""
    ).strip()
    repo_url = str(payload.get("repoUrl") or payload.get("repo_url") or "").strip()
    docs_url = str(payload.get("docsUrl") or payload.get("docs_url") or "").strip()
    notes = str(
        payload.get("notes")
        or payload.get("project_notes")
        or payload.get("docsText")
        or ""
    ).strip()
    if not source_type:
        if repo_url:
            source_type = "github_repo"
            url = repo_url
        elif docs_url:
            source_type = "docs_url"
            url = docs_url
        elif notes:
            source_type = "project_notes"
        else:
            raise CapabilityPackageIngestError(
                "capability_source_required",
                "GitHub repository, docs URL, or project notes are required",
            )
    if source_type not in {"github_repo", "docs_url", "project_notes"}:
        raise CapabilityPackageIngestError(
            "invalid_capability_source_type",
            "source.type must be github_repo, docs_url, or project_notes",
        )
    if source_type in {"github_repo", "docs_url"} and not url:
        raise CapabilityPackageIngestError(
            "capability_source_url_required",
            "source.url is required",
        )
    source = {
        "type": source_type,
        "url": url,
        "ref": str(payload.get("ref") or "").strip(),
        "paths": [str(item) for item in payload.get("paths", []) if str(item).strip()]
        if isinstance(payload.get("paths"), list)
        else [],
        "notes": notes,
        "package_id_hint": str(
            payload.get("packageIdHint") or payload.get("package_id_hint") or ""
        ).strip(),
    }
    return {key: value for key, value in source.items() if value not in ("", [])}


def _capability_session_runtime_state(
    runtime_control_plane: AgentRunControlPlane,
    *,
    agent_run: AgentRunRecord | None = None,
) -> dict[str, Any]:
    model_binding: dict[str, Any] = {}
    if agent_run is not None:
        raw_binding = agent_run.metadata.get("model_binding")
        if isinstance(raw_binding, dict):
            model_binding = dict(raw_binding)
    if not model_binding:
        snapshot = getattr(runtime_control_plane, "runtime_snapshot", {})
        agents = snapshot.get("agents") if isinstance(snapshot, dict) else {}
        raw_agent = (
            agents.get(DEFAULT_CAPABILITY_PACKAGER_AGENT_ID)
            if isinstance(agents, dict)
            else {}
        )
        raw_model = raw_agent.get("model") if isinstance(raw_agent, dict) else {}
        if isinstance(raw_model, dict):
            provider = str(raw_model.get("provider") or raw_model.get("provider_id") or "").strip()
            model = str(raw_model.get("model") or raw_model.get("model_id") or "").strip()
            if provider and model:
                model_binding = {
                    "provider": provider,
                    "model": model,
                    **(
                        {"display_name": str(raw_model.get("display_name"))}
                        if raw_model.get("display_name")
                        else {}
                    ),
                    **(
                        {"parameters": dict(raw_model.get("parameters"))}
                        if isinstance(raw_model.get("parameters"), dict)
                        else {}
                    ),
                }
    state: dict[str, Any] = {
        "workflow": CAPABILITY_INGEST_WORKFLOW,
        "workflow_mode": CAPABILITY_INGEST_WORKFLOW,
        "mode": "capability_package",
        "agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
        "active_agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
        "main_agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
    }
    provider = str(model_binding.get("provider") or "").strip()
    model = str(model_binding.get("model") or "").strip()
    if provider and model:
        state.update(
            {
                "active_model_provider": provider,
                "active_model": model,
                "active_model_display_name": str(
                    model_binding.get("display_name") or model
                ),
            }
        )
        parameters = model_binding.get("parameters")
        if isinstance(parameters, dict) and parameters:
            state["active_model_parameters"] = dict(parameters)
    if agent_run is not None:
        state.update(
            {
                "agent_run_id": agent_run.id,
                "runtime_profile_id": agent_run.runtime_profile_id,
                "executor": agent_run.executor.value if agent_run.executor else None,
                "execution_location": (
                    agent_run.execution_location.value
                    if agent_run.execution_location
                    else None
                ),
            }
        )
    return state


def _set_session_runtime_state(session: Any, runtime_state: dict[str, Any]) -> None:
    cond = getattr(session, "cond", None)
    if cond is None:
        session.runtime_state = dict(runtime_state)
        return
    with cond:
        session.runtime_state = dict(runtime_state)


def _append_source_bundle_notices(session: Any, source_bundle: dict[str, Any]) -> None:
    if not isinstance(source_bundle, dict):
        return
    source = source_bundle.get("source") if isinstance(source_bundle.get("source"), dict) else {}
    errors = _dict_list(source_bundle.get("errors"))
    documents = _dict_list(source_bundle.get("documents"))
    evidence = _dict_list(source_bundle.get("evidence"))
    has_document_content = any(str(item.get("content") or "").strip() for item in documents)
    locale = _session_locale(session)
    if (
        not errors
        and not evidence
        and not has_document_content
        and str(source.get("type") or "") in {"github_repo", "docs_url"}
    ):
        errors = [
            {
                "code": "source_evidence_empty",
                "message": _capability_text(locale, "source_empty"),
            }
        ]
    if not errors:
        return
    first_error = dict(errors[0])
    code = str(first_error.get("code") or "source_fetch_warning").strip()
    has_usable_source = bool(evidence or has_document_content)
    detail = (
        _capability_text(locale, "source_partial")
        if has_usable_source
        else _capability_text(locale, "source_empty")
    )
    session.append_event(
        "output",
        {
            "content": detail,
            "format": "plain",
            "level": "warning",
            "code": code,
            "source": dict(source),
            "source_error": first_error,
            "source_errors": [dict(error) for error in errors],
            "workflow": CAPABILITY_INGEST_WORKFLOW,
        },
    )


def _agent_run_terminal_message(
    task: dict[str, Any],
    events: list[Any],
    status: str,
) -> str:
    normalized_status = " ".join(str(status or "").split()).lower()
    generic_values = {
        value
        for value in {
            normalized_status,
            "failed",
            "blocked",
            "cancelled",
            "agent_error",
        }
        if value
    }

    def normalize(value: object) -> str:
        return " ".join(str(value or "").split())

    def first_meaningful(candidates: list[object]) -> str:
        fallback = ""
        for candidate in candidates:
            text = normalize(candidate)
            if not text:
                continue
            if not fallback:
                fallback = text
            if text.lower() not in generic_values:
                return text
        return fallback

    task_candidates = [
        task.get("failure_reason"),
        task.get("cancel_reason"),
        task.get("output"),
    ]
    message = first_meaningful(task_candidates)
    if message and message.lower() not in generic_values:
        return message

    event_candidates: list[object] = []
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        data = payload if isinstance(payload, dict) else {}
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        event_task = (
            data.get("agent_run")
            if isinstance(data.get("agent_run"), dict)
            else {}
        )
        event_candidates.extend(
            [
                result.get("error"),
                data.get("error"),
                data.get("message"),
                event_task.get("failure_reason"),
                event_task.get("cancel_reason"),
                result.get("output"),
                event_task.get("output"),
            ]
        )
    event_message = first_meaningful(event_candidates)
    if event_message and event_message.lower() not in generic_values:
        return event_message
    if message:
        return message
    if event_message:
        return event_message
    status_message = normalize(status)
    return status_message or "capability package ingest failed"


def _agent_run_terminal_reason(task: dict[str, Any], status: str) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    for value in (
        task.get("failure_reason"),
        task.get("cancel_reason"),
        metadata.get("terminal_reason"),
        metadata.get("failure_kind"),
        status,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return status


def _capability_workflow_step_event(
    title: str,
    stage: str,
    extra: dict[str, Any] | None = None,
    *,
    status: str = "running",
    summary: str = "",
) -> dict[str, Any]:
    details = dict(extra or {})
    details.setdefault("phase", details.get("phase") or stage)
    return {
        "lane": "process",
        "workflow": CAPABILITY_INGEST_WORKFLOW,
        "stage": stage,
        "status": _workflow_status(status),
        "title": title,
        "message": title,
        **({"summary": summary} if summary else {}),
        "details": details,
        **details,
    }


def _capability_projected_session_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    locale: str,
) -> tuple[str, dict[str, Any]] | None:
    if event_type == "assistant_delta":
        return None
    if event_type == "context_event":
        return "workflow_step", _capability_workflow_step_from_context(payload)
    if event_type in _CAPABILITY_AGENT_TOOL_EVENTS:
        return "workflow_step", _capability_workflow_step_from_tool_event(
            event_type,
            payload,
            locale=locale,
        )
    return event_type, payload


def _capability_workflow_step_from_tool_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    locale: str,
) -> dict[str, Any]:
    tool_name = str(payload.get("tool_name") or "").strip()
    tool_call_id = str(payload.get("tool_call_id") or "").strip()
    stage = _workflow_stage_from_tool_name(tool_name)
    title_key = "tool_read_source" if stage == "read_source" else "tool_extract_evidence"
    title = _capability_text(locale, title_key)
    if tool_name:
        title = f"{title}: {tool_name}"
    details: dict[str, Any] = {
        "phase": f"agent_run_{event_type}",
        "event_type": event_type,
    }
    if tool_name:
        details["tool_name"] = tool_name
    if tool_call_id:
        details["tool_call_id"] = tool_call_id
    tool_args = payload.get("tool_args")
    if isinstance(tool_args, dict) and tool_args:
        details["tool_args"] = tool_args
    meta = payload.get("meta")
    if isinstance(meta, dict) and meta:
        details["meta"] = meta
    raw_event_refs = payload.get("raw_event_refs")
    if isinstance(raw_event_refs, list) and raw_event_refs:
        details["raw_event_refs"] = raw_event_refs
    return _capability_workflow_step_event(
        title,
        stage,
        details,
        status=_workflow_status_from_tool_event(event_type, payload),
    )


def _capability_workflow_step_from_context(payload: dict[str, Any]) -> dict[str, Any]:
    phase = str(payload.get("phase") or "").strip()
    status = str(payload.get("agent_run_status") or "").strip()
    stage = _workflow_stage_from_phase(phase)
    title = str(payload.get("title") or payload.get("message") or stage).strip()
    details = {
        key: value
        for key, value in payload.items()
        if key not in {"title", "message", "workflow", "stage", "status", "lane"}
    }
    return _capability_workflow_step_event(
        title,
        stage,
        details,
        status=_workflow_status_from_agent_run_status(status),
        summary=phase,
    )


def _workflow_stage_from_tool_name(tool_name: str) -> str:
    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    if normalized in _CAPABILITY_READ_SOURCE_TOOL_NAMES:
        return "read_source"
    return "extract_evidence"


def _workflow_stage_from_phase(phase: str) -> str:
    if phase in {"agent_run_queued", "agent_run_claimed"}:
        return "prepare"
    if phase == "agent_run_session_ready":
        return "read_source"
    if phase in {"agent_run_log", "agent_run_usage"}:
        return "extract_evidence"
    if phase.startswith("agent_run_") or phase.startswith("capability_package_revision"):
        return "compose_draft"
    return "prepare"


def _workflow_status_from_agent_run_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"failed", "blocked"}:
        return "error"
    if normalized == "cancelled":
        return "cancelled"
    if normalized in {"completed", "done", "success"}:
        return "done"
    return "running"


def _workflow_status_from_tool_event(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "tool_call_protocol_error":
        return "error"
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    status_values = [
        payload.get("status"),
        payload.get("tool_status"),
        payload.get("result_status"),
        meta.get("status"),
        meta.get("error"),
        meta.get("is_error"),
    ]
    for value in status_values:
        if value is True:
            return "error"
        normalized = str(value or "").strip().lower()
        if normalized in {"error", "failed", "failure", "blocked", "denied", "protocol_error"}:
            return "error"
        if normalized in {"done", "completed", "success", "succeeded"}:
            return "done"
    if event_type == "tool_call_end":
        return "done"
    return "running"


def _workflow_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"running", "done", "warning", "error", "cancelled"}:
        return normalized
    if normalized in {"completed", "success", "approved"}:
        return "done"
    if normalized in {"failed", "blocked", "denied"}:
        return "error"
    return "running"


def _capability_workflow_result_event(
    title: str,
    status: str,
    result_type: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "lane": "primary",
        "workflow": CAPABILITY_INGEST_WORKFLOW,
        "result_type": result_type,
        "status": _workflow_status(status),
        "title": title,
        "message": title,
        "summary": title,
        "result": dict(result or {}),
    }


def _truncate_single_line(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars - 1]}…"


def _capability_package_artifact_event_payload(
    draft: dict[str, Any],
    source_bundle: dict[str, Any],
    agent_run_id: str,
    validation: dict[str, Any] | None,
    *,
    locale: str,
) -> dict[str, Any]:
    artifact = _capability_package_review(
        draft,
        source_bundle,
        agent_run_id,
        validation if isinstance(validation, dict) else {},
    )
    package_id = str(artifact.get("package_id") or "capability-package").strip()
    title = _capability_text(locale, "draft_ready", package_id=package_id)
    return {
        "lane": "primary",
        "workflow": CAPABILITY_INGEST_WORKFLOW,
        "artifact_type": "capability_package_draft",
        "title": title,
        "message": title,
        "summary": str(artifact.get("description") or ""),
        "artifact": artifact,
        "raw_event_refs": [{"agent_run_id": agent_run_id, "type": "result"}],
    }


def _capability_package_review(
    draft: dict[str, Any],
    source_bundle: dict[str, Any],
    agent_run_id: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    public_draft = _public_capability_package_draft(draft)
    package_id = str(public_draft.get("id") or "capability-package").strip()
    components = _capability_review_components(public_draft)
    capabilities = [
        _capability_component_review_item(item)
        for item in components
        if str(item.get("kind") or item.get("type") or "").strip() in CAPABILITY_COMPONENT_KINDS
    ]
    dependencies = [
        _capability_component_review_item(item)
        for item in components
        if str(item.get("kind") or item.get("type") or "").strip() not in CAPABILITY_COMPONENT_KINDS
    ]
    source = source_bundle.get("source") if isinstance(source_bundle.get("source"), dict) else {}
    source_summary = _source_bundle_summary(source_bundle)
    install_plan = _string_values(public_draft.get("install_plan"))
    usage = _string_values(public_draft.get("usage"))
    evidence = [
        {
            key: value
            for key, value in item.items()
            if key in {"title", "source", "url", "path", "excerpt", "summary"}
        }
        for item in _dict_list(public_draft.get("evidence"))
    ]
    credentials = _string_values(public_draft.get("credentials"))
    risks = _capability_review_risks(public_draft, validation)
    return {
        "package_id": package_id,
        "id": package_id,
        "name": str(public_draft.get("name") or package_id),
        "description": str(public_draft.get("description") or ""),
        "source": source,
        "source_summary": source_summary,
        "components": components,
        "capabilities": capabilities,
        "dependencies": dependencies,
        "runtime_footprint": aggregate_runtime_footprint(
            _runtime_footprints_from_draft(package_id, public_draft)
        ),
        "install_plan": install_plan,
        "usage": usage,
        "evidence": evidence,
        "credentials": credentials,
        "risks": risks,
        "validation": validation,
        "diagnostic_ref": f"agent-run:{agent_run_id}",
    }


def _capability_component_review_item(component: dict[str, Any]) -> dict[str, Any]:
    config = component.get("config") if isinstance(component.get("config"), dict) else {}
    component_id = str(component.get("id") or component.get("name") or "").strip()
    name = str(component.get("name") or config.get("name") or component_id).strip()
    return {
        "id": component_id,
        "name": name,
        "display_name": str(component.get("display_name") or config.get("display_name") or name).strip(),
        "kind": str(component.get("kind") or component.get("type") or "").strip(),
        "summary": str(
            component.get("summary")
            or config.get("summary")
            or component.get("description")
            or config.get("description")
            or ""
        ).strip(),
    }


def _capability_review_components(public_draft: dict[str, Any]) -> list[dict[str, Any]]:
    direct = [
        dict(item)
        for item in public_draft.get("components", [])
        if isinstance(item, dict)
    ]
    contributions = public_draft.get("contributions")
    if not isinstance(contributions, dict):
        return direct
    sections = [
        "skills",
        "mcp_servers",
        "builtin_tools",
        "prompt_fragments",
        "credential_refs",
        "environment_requirements",
    ]
    contributed: list[dict[str, Any]] = []
    for section in sections:
        value = contributions.get(section)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                contributed.append(dict(item))
    return [*direct, *contributed]


def _capability_review_risks(
    public_draft: dict[str, Any],
    validation: dict[str, Any],
) -> list[str]:
    risks: list[str] = []
    risk_level = str(public_draft.get("risk_level") or "").strip()
    if risk_level:
        risks.append(f"risk_level: {risk_level}")
    messages = validation.get("messages")
    if isinstance(messages, list):
        risks.extend(str(item).strip() for item in messages if str(item).strip())
    warnings = validation.get("warnings")
    if isinstance(warnings, list):
        risks.extend(str(item).strip() for item in warnings if str(item).strip())
    return _unique_strings(risks)


def _public_capability_package_draft(draft: dict[str, Any]) -> dict[str, Any]:
    public = deepcopy(draft)

    def scrub(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        result = dict(item)
        content = _component_skill_content_value(result)
        for field_name in ("skill_content", "content"):
            result.pop(field_name, None)
        config = dict(result.get("config")) if isinstance(result.get("config"), dict) else {}
        for field_name in ("skill_content", "content"):
            config.pop(field_name, None)
        if content:
            result["has_skill_content"] = True
            result["skill_content_chars"] = len(content)
        elif str(result.get("kind") or result.get("type") or "").strip().lower() == "skill":
            result["has_skill_content"] = False
        if config:
            result["config"] = config
        elif "config" in result:
            result.pop("config", None)
        return result

    components = public.get("components")
    if isinstance(components, list):
        public["components"] = [scrub(item) for item in components]
    contributions = public.get("contributions")
    if isinstance(contributions, dict):
        next_contributions = dict(contributions)
        skills = next_contributions.get("skills")
        if isinstance(skills, list):
            next_contributions["skills"] = [scrub(item) for item in skills]
        public["contributions"] = next_contributions
    return public


def _apply_skill_related_requirement_footprints(
    components: list[CapabilityComponentConfig],
) -> None:
    requirement_by_id = {
        component.id: component
        for component in components
        if component.kind == "environment_requirement"
    }
    for component in components:
        if component.kind != "skill":
            continue
        requirement_refs = _string_values(
            component.config.get("environment_requirement_refs")
        )
        related_requirements = [
            requirement_by_id[requirement_id]
            for requirement_id in requirement_refs
            if requirement_id in requirement_by_id
        ]
        if related_requirements:
            component.runtime_footprint = runtime_footprint_for_skill(
                component,
                related_requirements,
            )


def _runtime_footprints_from_draft(
    package_id: str,
    draft: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        parsed = CapabilityPackageDraft.from_dict(package_id, draft)
        installer = CapabilityPackageInstaller()
        components = [
            installer.component_from_draft(package_id, item, parsed.source.to_dict())
            for item in parsed.components
        ]
        _apply_skill_related_requirement_footprints(components)
        return [component.runtime_footprint for component in components]
    except (CapabilityPackageIngestError, ValueError, TypeError):
        return [
            normalize_runtime_footprint(
                item.get("runtime_footprint")
                if isinstance(item, dict)
                else {},
            )
            for item in draft.get("components", [])
            if isinstance(item, dict)
        ]


def _runtime_targets_text(locale: str, targets: Any) -> str:
    values = _string_values(targets)
    if not values:
        return _capability_text(locale, "approval_none")
    labels = {
        "server": "Server" if _capability_locale(locale) == "en" else "服务端",
        "local_peer": "Local client" if _capability_locale(locale) == "en" else "本地端",
        "peer": "Local client" if _capability_locale(locale) == "en" else "本地端",
    }
    return ", ".join(
        _unique_strings([labels.get(value, value) for value in values])
    )


def _string_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value).strip()]


def _capability_install_decision_payload(
    approval_id: str,
    tool_call_id: str,
    draft: dict[str, Any],
    agent_run_id: str,
    *,
    locale: str,
) -> dict[str, Any]:
    package_id = str(draft.get("id") or "capability-package").strip()
    components = [
        dict(item)
        for item in draft.get("components", [])
        if isinstance(item, dict)
    ]
    capabilities = [
        str(item.get("name") or item.get("id") or "")
        for item in components
        if str(item.get("kind") or "") in CAPABILITY_COMPONENT_KINDS
    ]
    dependencies = [
        str(item.get("name") or item.get("id") or "")
        for item in components
        if str(item.get("kind") or "") not in CAPABILITY_COMPONENT_KINDS
    ]
    runtime_footprint = aggregate_runtime_footprint(
        _runtime_footprints_from_draft(package_id, draft)
    )
    sections = [
        {
            "title": _capability_text(locale, "approval_package_title"),
            "items": [
                {"label": "ID", "value": package_id},
                {
                    "label": _capability_text(locale, "approval_name"),
                    "value": str(draft.get("name") or package_id),
                },
                {
                    "label": _capability_text(locale, "approval_risk"),
                    "value": str(draft.get("risk_level") or "unrated"),
                },
            ],
        },
        {
            "title": _capability_text(locale, "approval_components_title"),
            "items": [
                {
                    "label": _capability_text(locale, "approval_capabilities"),
                    "value": ", ".join(_unique_strings(capabilities))
                    or _capability_text(locale, "approval_none"),
                },
                {
                    "label": _capability_text(locale, "approval_dependencies"),
                    "value": ", ".join(_unique_strings(dependencies))
                    or _capability_text(locale, "approval_none"),
                },
            ],
        },
        {
            "title": _capability_text(locale, "approval_runtime_title"),
            "items": [
                {
                    "label": _capability_text(locale, "approval_runtime_summary"),
                    "value": str(
                        runtime_footprint.get("user_message")
                        or _capability_text(locale, "approval_none")
                    ),
                },
                {
                    "label": _capability_text(locale, "approval_install_required_on"),
                    "value": _runtime_targets_text(
                        locale,
                        runtime_footprint.get("install_required_on"),
                    ),
                },
                {
                    "label": _capability_text(locale, "approval_config_required_on"),
                    "value": _runtime_targets_text(
                        locale,
                        runtime_footprint.get("config_required_on"),
                    ),
                },
            ],
        },
    ]
    install_plan = [
        str(item)
        for item in draft.get("install_plan", [])
        if str(item).strip()
    ] if isinstance(draft.get("install_plan"), list) else []
    if install_plan:
        sections.append(
            {
                "title": _capability_text(locale, "approval_install_plan"),
                "items": [
                    {"label": str(index + 1), "value": item}
                    for index, item in enumerate(install_plan)
                ],
            }
        )
    review = _capability_package_review(
        draft,
        {"source": draft.get("source") if isinstance(draft.get("source"), dict) else {}},
        agent_run_id,
        {},
    )
    intent = _capability_text(locale, "approval_intent", package_id=package_id)
    content = str(
        draft.get("description")
        or _capability_text(locale, "approval_content", package_id=package_id)
    )
    return {
        "lane": "primary",
        "workflow": CAPABILITY_INGEST_WORKFLOW,
        "decision_type": "capability_package_install",
        "status": "pending",
        "title": intent,
        "summary": content,
        "review": review,
        "actions": [
            {
                "id": "allow_once",
                "label": _capability_text(locale, "approval_allow"),
                "tone": "primary",
            },
            {
                "id": "deny_once",
                "label": _capability_text(locale, "approval_deny"),
                "tone": "secondary",
            },
        ],
        "approval_id": approval_id,
        "tool_call_id": tool_call_id,
        "tool_name": CapabilityPackageSessionRunService.INSTALL_TOOL_NAME,
        "tool_args": {"package_id": package_id, "agent_run_id": agent_run_id},
        "intent": intent,
        "content": content,
        "sections": sections,
    }


def _render_packager_prompt(
    *,
    bundle: dict[str, Any],
    locale: str = "",
    revision_draft: dict[str, Any] | None = None,
    revision_instruction: str = "",
) -> str:
    bundle_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    language_instruction = session_locale_prompt_append(locale)
    language_block = f"{language_instruction}\n" if language_instruction else ""
    use_zh = bool(locale) and normalize_session_locale(locale) == "zh-CN"
    revision_text = str(revision_instruction or "").strip()
    revision_block = ""
    if revision_text or isinstance(revision_draft, dict):
        current_draft_json = json.dumps(
            _public_capability_package_draft(revision_draft or {}),
            ensure_ascii=False,
            indent=2,
        )
        if use_zh:
            revision_block = (
                "\n修改请求：\n"
                "用户已经审阅上一版草案。请输出一份完整的新结构决策，不要输出补丁。"
                "保留仍然有效的字段，并在证据包支持时应用用户的修改意见。\n"
                f"用户意见：\n{revision_text or '（没有额外文字）'}\n"
                "上一版草案：\n"
                f"```json\n{current_draft_json}\n```\n"
            )
        else:
            revision_block = (
                "\nRevision request:\n"
                "The user has reviewed the previous draft. Produce a complete revised structure decision, "
                "not a patch. Keep every field that remains valid, and apply the user's "
                "requested changes when they are supported by the evidence bundle.\n"
                f"User instruction:\n{revision_text or '(no extra text)'}\n"
                "Previous draft:\n"
                f"```json\n{current_draft_json}\n```\n"
            )
    if use_zh:
        return (
            "你是 capability_packager。请主动探索给定仓库/文档，生成一个可安装的能力包结构决策。\n"
            f"{language_block}"
            "证据包只是启动线索，不代表完整仓库事实。发现阶段只允许读取信息：不要运行安装命令，不要修改文件。\n"
            "你必须按顺序完成：source_discovery、evidence_extraction、materialization_plan、draft_decision。\n"
            "对于 GitHub 仓库，优先使用 list/glob/grep/read_file/fetch_capabilities 探索真实结构，"
            "重点检查 skills/**/SKILL.md、SKILL.md、README、docs、llms.txt 和 manifest 文件。\n"
            "如果仓库已经包含 Skill 文件，能力包 Skill 必须来自这些文件的精确 source_path/content_ref。\n"
            "只能提取来源支持的说明；environment_requirements 只记录安装后的能力实际运行/检查需要的依赖，"
            "且 check/install/command 必须有来源中的精确命令证据。不要把外部安装方式（例如 npx skills add）转换成 Labrastro 运行依赖。\n"
            "最终只输出一个紧凑 JSON 对象，不要使用 markdown fence，不要输出完整文件正文。\n"
            "JSON 结构如下：\n"
            "{\n"
            '  "id": "package-id", "name": "Package Name", "description": "...",\n'
            '  "source": {"type": "github_repo|docs_url|project_notes", "url": "..."},\n'
            '  "source_inventory": {"files": [], "skill_files": [], "docs": []},\n'
            '  "materialization_plan": [\n'
            '    {"component_id": "skill:code-review", "source_path": "skills/code-review/SKILL.md", '
            '"content_ref": "read-file-call-id"}\n'
            "  ],\n"
            '  "contributions": {\n'
            '    "skills": [\n'
            '      {"id": "skill:code-review", "kind": "skill", "name": "code-review", '
            '"display_name": "Code review", '
            '"source_path": "skills/code-review/SKILL.md", '
            '"summary": "what this skill does"}\n'
            '    ], "mcp_servers": [], "builtin_tools": [],\n'
            '    "prompt_fragments": [], "credential_refs": [],\n'
            '    "environment_requirements": [\n'
            '      {"id": "envreq:executable:gh", "kind": "executable", '
            '"name": "gh", "command": "gh", "check": "gh --version", '
            '"install": "winget install GitHub.cli", "placement": "peer"}\n'
            "    ]\n"
            "  },\n"
            '  "effective_capabilities": ["Plain language capability added to an Agent"],\n'
            '  "install_plan": [], "usage": [], "evidence": [], "credentials": [], '
            '"risk_level": "low|medium|high", "execution_policy": "inherit", "notes": []\n'
            "}\n\n"
            "由能力包管理的 Skills 必须可以安装到服务端标准 Skill 目录；"
            "每个 Skill 组件必须给出可由证据包或 worktree 定位的 source_path/content_ref，"
            "完整 skill_content 由后端读取并组装，不要在模型输出中搬运大文件。\n"
            "证据包：\n"
            f"```json\n{bundle_json}\n```\n"
            f"{revision_block}"
        )
    return (
        "You are capability_packager. Actively explore the provided repository/docs "
        "and produce one installable capability package structure decision.\n"
        f"{language_block}"
        "The supplied evidence bundle is only a seed, not the complete repository truth. "
        "Discovery is read-only: do not run install commands and do not mutate files.\n"
        "Complete these stages in order: source_discovery, evidence_extraction, "
        "materialization_plan, draft_decision.\n"
        "For GitHub repositories, use list/glob/grep/read_file/fetch_capabilities to inspect "
        "the real structure. Prioritize skills/**/SKILL.md, SKILL.md, README, docs, llms.txt, "
        "and manifest files.\n"
        "When the repository already contains Skill files, package-managed Skills must map "
        "to the exact source_path/content_ref for those files.\n"
        "Extract only instructions supported by source evidence. environment_requirements are "
        "only for dependencies actually needed to run/check the installed capability, and "
        "check/install/command values must have exact command evidence. Do not turn external "
        "installation methods such as npx skills add into Labrastro runtime dependencies.\n"
        "Return final output as one compact JSON object. Do not wrap it in a markdown fence, "
        "and do not output complete file bodies.\n"
        "Use this shape:\n"
        "{\n"
        '  "id": "package-id", "name": "Package Name", "description": "...",\n'
        '  "source": {"type": "github_repo|docs_url|project_notes", "url": "..."},\n'
        '  "source_inventory": {"files": [], "skill_files": [], "docs": []},\n'
        '  "materialization_plan": [\n'
        '    {"component_id": "skill:code-review", "source_path": "skills/code-review/SKILL.md", '
        '"content_ref": "read-file-call-id"}\n'
        "  ],\n"
        '  "contributions": {\n'
        '    "skills": [\n'
        '      {"id": "skill:code-review", "kind": "skill", "name": "code-review", '
        '"display_name": "Code review", '
        '"source_path": "skills/code-review/SKILL.md", '
        '"summary": "what this skill does"}\n'
        '    ], "mcp_servers": [], "builtin_tools": [],\n'
        '    "prompt_fragments": [], "credential_refs": [],\n'
        '    "environment_requirements": [\n'
        '      {"id": "envreq:executable:gh", "kind": "executable", '
        '"name": "gh", "command": "gh", "check": "gh --version", '
        '"install": "winget install GitHub.cli", "placement": "peer"}\n'
        "    ]\n"
        "  },\n"
        '  "effective_capabilities": ["Plain language capability added to an Agent"],\n'
        '  "install_plan": [], "usage": [], "evidence": [], "credentials": [], '
        '"risk_level": "low|medium|high", "execution_policy": "inherit", "notes": []\n'
        "}\n\n"
        "package-managed Skills must be installable into the server canonical Skill directory; "
        "include source_path/content_ref for every Skill component so the backend can read "
        "and assemble canonical skill_content. Do not copy large Skill files into the model output.\n"
        "Evidence bundle:\n"
        f"```json\n{bundle_json}\n```\n"
        f"{revision_block}"
    )


def _metadata_locale(metadata: dict[str, Any] | None) -> str:
    locale = str((metadata or {}).get("locale") or "").strip()
    return normalize_session_locale(locale) if locale else ""


def _extract_draft(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(text))
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and (
            isinstance(parsed.get("contributions"), dict)
            or isinstance(parsed.get("components"), list)
        ):
            return parsed
    return None


def _canonical_capability_draft_from_decision(
    raw_draft: dict[str, Any],
    source_bundle: dict[str, Any] | None,
    *,
    workspace_root: str = "",
    sandbox_container_id: str = "",
) -> dict[str, Any]:
    draft = deepcopy(raw_draft)
    bundle = source_bundle if isinstance(source_bundle, dict) else {}
    materialization_plan = _materialization_plan_index(draft)

    def enrich(item: dict[str, Any]) -> dict[str, Any]:
        item = _component_with_materialization_plan(item, materialization_plan)
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if kind != "skill" or _component_skill_content_value(item):
            return item
        content, source_ref = _resolve_skill_content_from_source_bundle(
            item,
            bundle,
            workspace_root=workspace_root,
            sandbox_container_id=sandbox_container_id,
        )
        if not content:
            reason = _skill_content_resolution_error(
                item,
                bundle,
                workspace_root=workspace_root,
                sandbox_container_id=sandbox_container_id,
            )
            if reason:
                item = dict(item)
                config = dict(item.get("config")) if isinstance(item.get("config"), dict) else {}
                item["skill_content_resolution_error"] = reason
                config["skill_content_resolution_error"] = reason
                item["config"] = config
            return item
        item = dict(item)
        item["skill_content"] = content
        config = dict(item.get("config")) if isinstance(item.get("config"), dict) else {}
        config.setdefault("skill_content", content)
        if source_ref:
            item["source_path"] = source_ref
            config["source_path"] = source_ref
            config["content_source"] = source_ref
        item["config"] = config
        return item

    components = draft.get("components")
    if isinstance(components, list):
        draft["components"] = [
            enrich(dict(item)) if isinstance(item, dict) else item
            for item in components
        ]

    contributions = draft.get("contributions")
    if isinstance(contributions, dict):
        next_contributions = dict(contributions)
        skills = contributions.get("skills")
        if isinstance(skills, list):
            next_contributions["skills"] = [
                enrich(dict(item)) if isinstance(item, dict) else item
                for item in skills
            ]
        draft["contributions"] = next_contributions
    return draft


def _materialization_plan_index(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_plan = (
        draft.get("materialization_plan")
        or draft.get("materialization")
        or draft.get("source_inventory")
    )
    entries: list[dict[str, Any]] = []
    if isinstance(raw_plan, list):
        entries.extend(dict(item) for item in raw_plan if isinstance(item, dict))
    elif isinstance(raw_plan, dict):
        raw_components = raw_plan.get("components")
        raw_items = raw_plan.get("items")
        if isinstance(raw_components, list):
            entries.extend(dict(item) for item in raw_components if isinstance(item, dict))
        if isinstance(raw_items, list):
            entries.extend(dict(item) for item in raw_items if isinstance(item, dict))
        for key, value in raw_plan.items():
            if key in {"components", "items"}:
                continue
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("component_id", key)
                entries.append(entry)
    index: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for key in _component_identity_values(entry):
            index.setdefault(key, entry)
    return index


def _component_with_materialization_plan(
    component: dict[str, Any],
    materialization_plan: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    plan: dict[str, Any] | None = None
    for key in _component_identity_values(component):
        plan = materialization_plan.get(key)
        if plan is not None:
            break
    if not plan:
        return component
    item = dict(component)
    config = dict(item.get("config")) if isinstance(item.get("config"), dict) else {}
    for field_name in (
        "source_path",
        "content_ref",
        "content_path",
        "path",
        "tool_call_id",
        "raw_event_refs",
    ):
        value = plan.get(field_name)
        if value in (None, "", []):
            continue
        if field_name not in item:
            item[field_name] = value
        if field_name not in config:
            config[field_name] = value
    if config:
        item["config"] = config
    return item


def _component_identity_values(component: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    raw_config = component.get("config")
    config = dict(raw_config) if isinstance(raw_config, dict) else {}
    for container in (component, config):
        for field_name in ("id", "component_id", "name"):
            value = str(container.get(field_name) or "").strip()
            if value:
                values.add(value)
                values.add(value.lower())
    kind = str(component.get("kind") or component.get("type") or "").strip().lower()
    name = str(component.get("name") or config.get("name") or "").strip()
    if kind and name:
        values.add(f"{kind}:{name}")
        values.add(f"{kind}:{name}".lower())
        if kind == "environment_requirement":
            values.add(f"envreq:{name}")
            values.add(f"envreq:{name}".lower())
    return values


def _agent_run_materialization_workdir(
    agent_run: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    for value in (
        agent_run.get("workdir"),
        metadata.get("workdir"),
        metadata.get("pinned_session_workdir"),
        metadata.get("session_workdir"),
        metadata.get("workspace_mount"),
        metadata.get("workspace_root"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _source_bundle_with_agent_run_documents(
    source_bundle: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    inventory = _source_inventory_from_agent_run_events(events)
    if not (
        inventory.documents
        or inventory.files
        or inventory.evidence
        or inventory.links
        or inventory.tool_calls
    ):
        return source_bundle
    bundle = deepcopy(source_bundle) if isinstance(source_bundle, dict) else {}
    existing = _dict_list(bundle.get("documents"))
    seen = {
        str(item.get("source_path") or item.get("path") or item.get("title") or "").strip()
        for item in existing
        if isinstance(item, dict)
    }
    for document in inventory.documents:
        key = str(document.get("source_path") or document.get("title") or "").strip()
        if key and key in seen:
            continue
        existing.append(document)
        if key:
            seen.add(key)
    bundle["documents"] = existing
    if inventory.evidence:
        bundle["evidence"] = _dedupe_evidence(
            [*_dict_list(bundle.get("evidence")), *inventory.evidence]
        )
    if inventory.links:
        bundle["links"] = _dedupe_links(
            [*_dict_list(bundle.get("links")), *inventory.links]
        )
    bundle["source_inventory"] = inventory.to_dict()
    return bundle


def _documents_from_agent_run_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _source_inventory_from_agent_run_events(events).documents


def _source_inventory_from_agent_run_events(
    events: list[dict[str, Any]],
) -> CapabilitySourceInventory:
    tool_inputs: dict[str, dict[str, Any]] = {}
    documents: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    raw_event_refs: list[dict[str, Any]] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        data = _agent_run_event_data(event)
        tool_data = _event_tool_data(data)
        tool_call_id = _event_tool_call_id(tool_data, data)
        tool_name = str(tool_data.get("tool_name") or tool_data.get("name") or "").strip()
        if event_type == "tool_use" and tool_call_id:
            tool_inputs[tool_call_id] = dict(tool_data)
        if event_type not in {"tool_use", "tool_result"}:
            continue
        if not _is_source_inventory_tool(tool_name):
            continue
        ref = _agent_run_event_ref(event)
        raw_event_refs.append(ref)
        tool_calls.append(
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "event_type": event_type,
                "raw_event_refs": [ref],
            }
        )
        if event_type != "tool_result":
            continue
        input_data = tool_inputs.get(tool_call_id, {})
        output = _tool_result_output_text(tool_data, data)
        path = _tool_result_source_path(tool_data, input_data)
        for file_ref in _source_file_refs_from_tool_result(
            tool_name,
            output,
            tool_data,
            input_data,
            raw_event_ref=ref,
        ):
            files.append(file_ref)
        if path and output and _tool_result_is_document_read(tool_name):
            document = {
                "title": path,
                "source_path": path,
                "path": path,
                "content": output.replace("\r\n", "\n").strip(),
                "source_kind": "agent_run_tool_result",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "content_ref": tool_call_id,
                "raw_event_refs": [ref],
            }
            documents.append(document)
            files.append(_source_file_ref(path, tool_name, ref))
            if _is_skill_file_path(path):
                evidence.append(
                    {
                        "title": path,
                        "source_url": path,
                        "excerpt": _truncate(output, 360),
                        "raw_event_refs": [ref],
                    }
                )
            continue
        if tool_name.lower().replace("-", "_") == "fetch_capabilities" and output:
            parsed = _json_object_from_text(output)
            if parsed:
                document = _document_from_fetch_payload(parsed)
                if document is not None:
                    document = dict(document)
                    document["source_kind"] = document.get("source_kind") or "fetch_capabilities"
                    document["tool_name"] = tool_name
                    document["tool_call_id"] = tool_call_id
                    document["content_ref"] = tool_call_id
                    document["raw_event_refs"] = [ref]
                    documents.append(document)
                evidence.extend(_dict_list(parsed.get("evidence")))
                links.extend(_dict_list(parsed.get("links")))

    files = _dedupe_source_files(files)
    skill_files = [item for item in files if _is_skill_file_path(str(item.get("path") or ""))]
    return CapabilitySourceInventory(
        files=files,
        skill_files=_dedupe_source_files(skill_files),
        documents=_dedupe_documents(documents),
        evidence=_dedupe_evidence(evidence),
        links=_dedupe_links(links),
        tool_calls=_dedupe_tool_calls(tool_calls),
        raw_event_refs=_dedupe_raw_event_refs(raw_event_refs),
    )


def _agent_run_event_data(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_tool_data(data: dict[str, Any]) -> dict[str, Any]:
    tool_data = data.get("data")
    return tool_data if isinstance(tool_data, dict) else data


def _event_tool_call_id(tool_data: dict[str, Any], data: dict[str, Any]) -> str:
    return str(
        tool_data.get("tool_call_id")
        or tool_data.get("id")
        or data.get("tool_call_id")
        or ""
    ).strip()


def _agent_run_event_ref(event: dict[str, Any]) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "agent_run_id": str(event.get("agent_run_id") or ""),
        "type": str(event.get("type") or ""),
    }
    seq = event.get("seq")
    if isinstance(seq, int):
        ref["seq"] = seq
    elif str(seq or "").strip():
        try:
            ref["seq"] = int(str(seq))
        except ValueError:
            ref["seq"] = str(seq)
    return ref


def _is_source_inventory_tool(tool_name: str) -> bool:
    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    return normalized in _CAPABILITY_READ_SOURCE_TOOL_NAMES


def _tool_result_is_document_read(tool_name: str) -> bool:
    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    return normalized in {"cat", "read", "read_file", "read_files", "list_file"}


def _tool_result_output_text(
    tool_data: dict[str, Any],
    data: dict[str, Any],
) -> str:
    output = tool_data.get("output")
    if isinstance(output, str):
        return output
    if output is not None:
        try:
            return json.dumps(output, ensure_ascii=False)
        except TypeError:
            return str(output)
    return str(data.get("text") or "")


def _source_file_refs_from_tool_result(
    tool_name: str,
    output: str,
    tool_data: dict[str, Any],
    input_data: dict[str, Any],
    *,
    raw_event_ref: dict[str, Any],
) -> list[dict[str, Any]]:
    paths: list[str] = []
    explicit_path = _tool_result_source_path(tool_data, input_data)
    if explicit_path:
        paths.append(explicit_path)
    paths.extend(_paths_from_tool_output(output))
    return [
        _source_file_ref(path, tool_name, raw_event_ref)
        for path in _unique_strings(paths)
        if _looks_like_source_path(path)
    ]


def _source_file_ref(
    path: str,
    tool_name: str,
    raw_event_ref: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_source_path(path)
    return {
        "path": normalized,
        "source_path": normalized,
        "source_kind": "agent_run_tool_result",
        "tool_name": tool_name,
        "kind": "skill" if _is_skill_file_path(normalized) else "file",
        "raw_event_refs": [dict(raw_event_ref)],
    }


def _paths_from_tool_output(output: str) -> list[str]:
    text = str(output or "").strip()
    if not text:
        return []
    parsed = _json_value_from_text(text)
    if parsed is not None:
        return _paths_from_json_value(parsed)
    paths: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*+]\s+", "", line).strip().strip("'\"")
        grep_match = re.match(r"^(.+?)(?::\d+){1,2}:", line)
        if grep_match:
            line = grep_match.group(1).strip()
        elif " " in line and not _is_skill_file_path(line):
            continue
        paths.append(_normalize_source_path(line))
    return [path for path in paths if path]


def _paths_from_json_value(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, str):
        return [_normalize_source_path(value)] if _looks_like_source_path(value) else []
    if isinstance(value, list):
        for item in value:
            paths.extend(_paths_from_json_value(item))
        return paths
    if isinstance(value, dict):
        for field_name in ("path", "source_path", "file", "file_path", "relative_path", "name"):
            raw = value.get(field_name)
            if isinstance(raw, str) and _looks_like_source_path(raw):
                paths.append(_normalize_source_path(raw))
        for field_name in ("files", "paths", "matches", "results", "items"):
            raw_value = value.get(field_name)
            if isinstance(raw_value, (list, dict)):
                paths.extend(_paths_from_json_value(raw_value))
        return paths
    return []


def _json_object_from_text(value: str) -> dict[str, Any]:
    parsed = _json_value_from_text(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_value_from_text(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(text))
    first_object = min(
        [index for index in (text.find("{"), text.find("[")) if index >= 0],
        default=-1,
    )
    last_object = max(text.rfind("}"), text.rfind("]"))
    if first_object >= 0 and last_object > first_object:
        candidates.append(text[first_object : last_object + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _normalize_source_path(value: str) -> str:
    text = str(value or "").strip().strip("'\"").replace("\\", "/")
    text = re.sub(r"^\./+", "", text)
    text = text.strip("/")
    return text


def _looks_like_source_path(value: str) -> bool:
    path = _normalize_source_path(value)
    if not path or "://" in path:
        return False
    lowered = path.lower()
    if _is_skill_file_path(path):
        return True
    source_suffixes = (
        ".md",
        ".mdx",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".rst",
    )
    return "/" in path or lowered.endswith(source_suffixes)


def _is_skill_file_path(value: str) -> bool:
    normalized = _normalize_source_path(value).lower()
    return normalized == "skill.md" or normalized.endswith("/skill.md")


def _dedupe_source_files(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        path = _normalize_source_path(str(value.get("path") or value.get("source_path") or ""))
        if not path or path in seen:
            continue
        seen.add(path)
        item = dict(value)
        item["path"] = path
        item["source_path"] = path
        result.append(item)
    return result


def _dedupe_documents(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = str(
            value.get("source_path")
            or value.get("path")
            or value.get("url")
            or value.get("title")
            or value.get("tool_call_id")
            or ""
        ).strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(dict(value))
    return result


def _dedupe_tool_calls(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = (
            str(value.get("tool_call_id") or ""),
            str(value.get("tool_name") or ""),
            str(value.get("event_type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result


def _dedupe_raw_event_refs(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = (
            str(value.get("agent_run_id") or ""),
            str(value.get("seq") or ""),
            str(value.get("type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result


def _public_inventory_document(document: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in document.items()
        if key not in {"content", "sections"}
    }
    content = str(document.get("content") or "")
    if content:
        result["content_chars"] = len(content)
    sections = document.get("sections")
    if isinstance(sections, list) and sections:
        result["sections"] = len(sections)
    return result


def _tool_result_source_path(
    tool_data: dict[str, Any],
    input_data: dict[str, Any],
) -> str:
    input_payload = input_data.get("input") if isinstance(input_data.get("input"), dict) else {}
    for container in (tool_data, input_data, input_payload):
        for field_name in ("source_path", "path", "file", "file_path", "relative_path"):
            value = str(container.get(field_name) or "").strip()
            if value:
                return _normalize_source_path(value)
    return ""


def _resolve_skill_content_from_source_bundle(
    component: dict[str, Any],
    source_bundle: dict[str, Any],
    *,
    workspace_root: str = "",
    sandbox_container_id: str = "",
) -> tuple[str, str]:
    candidates = _skill_content_source_candidates(component)
    documents = _dict_list(source_bundle.get("documents"))
    if documents:
        for document in documents:
            content = str(document.get("content") or "").replace("\r\n", "\n").strip()
            if not content:
                continue
            identity = _source_document_identity(document)
            if candidates and candidates.intersection(identity):
                return content, _best_source_ref(document)
        component_name = str(component.get("name") or component.get("id") or "").strip().lower()
        skill_documents = [
            document
            for document in documents
            if any(_is_skill_file_path(value) for value in _source_document_identity(document))
        ]
        if component_name and len(skill_documents) <= 1:
            for document in documents:
                content = str(document.get("content") or "").replace("\r\n", "\n").strip()
                if not content:
                    continue
                identity_text = " ".join(sorted(_source_document_identity(document))).lower()
                if component_name in identity_text and "skill.md" in identity_text:
                    return content, _best_source_ref(document)
    if workspace_root:
        content, source_ref = _resolve_skill_content_from_worktree(
            candidates,
            workspace_root,
            sandbox_container_id=sandbox_container_id,
        )
        if content:
            return content, source_ref
    return "", ""


def _skill_content_resolution_error(
    component: dict[str, Any],
    source_bundle: dict[str, Any],
    *,
    workspace_root: str = "",
    sandbox_container_id: str = "",
) -> str:
    candidates = _skill_content_source_candidates(component)
    inventory_matches = _source_bundle_skill_file_refs(source_bundle)
    if len(inventory_matches) > 1:
        relative_matches = {
            path.replace("\\", "/").strip().strip("/").lower()
            for path in inventory_matches
        }
        if candidates and any(
            candidate.replace("\\", "/").strip().strip("/").lower() in relative_matches
            for candidate in candidates
        ):
            return ""
        return (
            "multiple SKILL.md files found in AgentRun source inventory; "
            "draft must provide an exact source_path or content_ref"
        )
    if not workspace_root:
        return ""
    matches = _worktree_skill_file_refs(
        workspace_root,
        sandbox_container_id=sandbox_container_id,
    )
    if len(matches) <= 1:
        return ""
    relative_matches = {path.replace("\\", "/").strip().strip("/").lower() for path in matches}
    if candidates and any(
        candidate.replace("\\", "/").strip().strip("/").lower() in relative_matches
        for candidate in candidates
    ):
        return ""
    return (
        "multiple SKILL.md files found in AgentRun workdir; "
        "draft must provide an exact source_path or content_ref"
    )


def _resolve_skill_content_from_worktree(
    candidates: set[str],
    workspace_root: str,
    *,
    sandbox_container_id: str = "",
) -> tuple[str, str]:
    root = Path(workspace_root).expanduser()
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = None
    if root_resolved is not None and root_resolved.is_dir():
        content, source_ref = _resolve_skill_content_from_local_worktree(candidates, root_resolved)
        if content:
            return content, source_ref
    if sandbox_container_id:
        content, source_ref = _resolve_skill_content_from_container_worktree(
            candidates,
            workspace_root,
            sandbox_container_id,
        )
        if content:
            return content, source_ref
    return "", ""


def _resolve_skill_content_from_local_worktree(
    candidates: set[str],
    root_resolved: Path,
) -> tuple[str, str]:
    for candidate in sorted(candidates):
        if not candidate or candidate.lower() == "readme.md":
            continue
        path = Path(candidate)
        target = path if path.is_absolute() else root_resolved / path
        try:
            target_resolved = target.resolve()
        except OSError:
            continue
        try:
            target_resolved.relative_to(root_resolved)
        except ValueError:
            continue
        if not target_resolved.is_file():
            continue
        try:
            content = target_resolved.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
        except OSError:
            continue
        if content:
            return content, str(target_resolved.relative_to(root_resolved)).replace("\\", "/")
    matches = _unique_skill_files(root_resolved)
    if len(matches) != 1:
        return "", ""
    target_resolved = matches[0]
    try:
        content = target_resolved.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    except OSError:
        return "", ""
    if content:
        return content, str(target_resolved.relative_to(root_resolved)).replace("\\", "/")
    return "", ""


def _unique_skill_files(root_resolved: Path) -> list[Path]:
    if not root_resolved.is_dir():
        return []
    matches: list[Path] = []
    for path in root_resolved.rglob("SKILL.md"):
        try:
            relative = path.relative_to(root_resolved)
        except ValueError:
            continue
        parts = {part.lower() for part in relative.parts[:-1]}
        if ".git" in parts or ".rcoder" in parts:
            continue
        if path.is_file():
            matches.append(path)
    return sorted(matches, key=lambda item: str(item.relative_to(root_resolved)).replace("\\", "/"))


def _worktree_skill_file_refs(
    workspace_root: str,
    *,
    sandbox_container_id: str = "",
) -> list[str]:
    root = Path(workspace_root).expanduser()
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = None
    if root_resolved is not None and root_resolved.is_dir():
        return [
            str(path.relative_to(root_resolved)).replace("\\", "/")
            for path in _unique_skill_files(root_resolved)
        ]
    if sandbox_container_id:
        return _container_skill_file_refs(sandbox_container_id, workspace_root)
    return []


def _source_bundle_skill_file_refs(source_bundle: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for document in _dict_list(source_bundle.get("documents")):
        for field_name in ("source_path", "path", "title"):
            value = str(document.get(field_name) or "").strip()
            if _is_skill_file_path(value):
                refs.append(_normalize_source_path(value))
    inventory = source_bundle.get("source_inventory")
    if isinstance(inventory, dict):
        for item in _dict_list(inventory.get("skill_files")):
            value = str(item.get("source_path") or item.get("path") or "").strip()
            if _is_skill_file_path(value):
                refs.append(_normalize_source_path(value))
    return _unique_strings(refs)


def _resolve_skill_content_from_container_worktree(
    candidates: set[str],
    workspace_root: str,
    sandbox_container_id: str,
) -> tuple[str, str]:
    for candidate in sorted(candidates):
        relative = _safe_container_relative_path(candidate)
        if not relative or relative.lower() == "readme.md":
            continue
        content = _read_container_text(
            sandbox_container_id,
            workspace_root,
            relative,
        )
        if content:
            return content, relative
    matches = _container_skill_file_refs(sandbox_container_id, workspace_root)
    if len(matches) != 1:
        return "", ""
    content = _read_container_text(sandbox_container_id, workspace_root, matches[0])
    if content:
        return content, matches[0]
    return "", ""


def _container_skill_file_refs(container_id: str, workspace_root: str) -> list[str]:
    script = r'''
root="$1"
[ -d "$root" ] || exit 0
cd "$root" || exit 0
find . -type f -name SKILL.md ! -path '*/.git/*' ! -path '*/.rcoder/*' | sed 's#^\./##' | sort
'''
    completed = _run_docker_exec(container_id, script, workspace_root)
    if completed.returncode != 0:
        return []
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if _safe_container_relative_path(line.strip())
    ]


def _read_container_text(
    container_id: str,
    workspace_root: str,
    relative_path: str,
) -> str:
    relative = _safe_container_relative_path(relative_path)
    if not relative:
        return ""
    script = r'''
root="$1"
rel="$2"
target="$root/$rel"
[ -f "$target" ] || exit 1
cat "$target"
'''
    completed = _run_docker_exec(container_id, script, workspace_root, relative)
    if completed.returncode != 0:
        return ""
    return completed.stdout.replace("\r\n", "\n").strip()


def _run_docker_exec(
    container_id: str,
    script: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", "exec", container_id, "sh", "-lc", script, "sh", *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["docker", "exec", container_id],
            returncode=1,
            stdout="",
            stderr=str(exc),
        )


def _safe_container_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip().strip("/")
    if not normalized or normalized == ".":
        return ""
    if normalized.startswith("../") or "/../" in normalized or normalized.endswith("/.."):
        return ""
    if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized):
        return ""
    return normalized


def _skill_content_source_candidates(component: dict[str, Any]) -> set[str]:
    raw_config = component.get("config")
    config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, dict) else {}
    fields = (
        "source_path",
        "content_ref",
        "content_path",
        "path",
        "path_hint",
        "registry_path",
        "tool_call_id",
    )
    values: set[str] = set()
    for container in (component, config):
        for field_name in fields:
            value = str(container.get(field_name) or "").strip()
            if value:
                values.update(_path_identity_values(value))
        raw_refs = container.get("raw_event_refs")
        if isinstance(raw_refs, list):
            for ref in raw_refs:
                if not isinstance(ref, dict):
                    continue
                for field_name in ("seq", "type", "agent_run_id"):
                    value = str(ref.get(field_name) or "").strip()
                    if value:
                        values.add(value)
    return values


def _source_document_identity(document: dict[str, Any]) -> set[str]:
    fields = (
        "source_path",
        "path",
        "url",
        "final_url",
        "title",
        "content_hash",
        "content_ref",
        "tool_call_id",
    )
    values: set[str] = set()
    for field_name in fields:
        value = str(document.get(field_name) or "").strip()
        if value:
            values.update(_path_identity_values(value))
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        for field_name in fields:
            value = str(metadata.get(field_name) or "").strip()
            if value:
                values.update(_path_identity_values(value))
    raw_event_refs = document.get("raw_event_refs")
    if isinstance(raw_event_refs, list):
        for ref in raw_event_refs:
            if not isinstance(ref, dict):
                continue
            for field_name in ("seq", "type", "agent_run_id"):
                value = str(ref.get(field_name) or "").strip()
                if value:
                    values.add(value)
    return values


def _path_identity_values(value: str) -> set[str]:
    normalized = value.replace("\\", "/").strip().strip("/")
    if not normalized:
        return set()
    lowered = normalized.lower()
    result = {normalized, lowered}
    if "/" in lowered:
        lowered_name = lowered.rsplit("/", 1)[-1]
        if lowered_name not in {"skill.md", "readme.md"}:
            result.add(lowered_name)
    if "/" in normalized:
        name = normalized.rsplit("/", 1)[-1]
        if name.lower() not in {"skill.md", "readme.md"}:
            result.add(name)
    return result


def _best_source_ref(document: dict[str, Any]) -> str:
    for field_name in ("source_path", "path", "url", "final_url", "title"):
        value = str(document.get(field_name) or "").strip()
        if value:
            return value
    return ""


def _document_from_fetch_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    sections = _dict_list(payload.get("sections"))
    content_parts: list[str] = []
    for section in sections:
        heading = str(section.get("heading") or "").strip()
        text = str(section.get("text") or "").strip()
        if heading:
            content_parts.append(f"## {heading}")
        if text:
            content_parts.append(text)
        code_blocks = (
            section.get("code_blocks", [])
            if isinstance(section.get("code_blocks"), list)
            else []
        )
        for code in code_blocks:
            code_text = str(code).strip()
            if code_text:
                content_parts.append(f"```text\n{code_text}\n```")
    content = "\n\n".join(content_parts).strip()
    if not content and not payload.get("ok"):
        return None
    return {
        "title": str(payload.get("title") or payload.get("url") or "Documentation"),
        "url": str(payload.get("final_url") or payload.get("url") or ""),
        "source_path": str(payload.get("source_path") or payload.get("path") or ""),
        "content": content[:MAX_SNIPPET_CHARS],
        "source_kind": str(payload.get("source_kind") or ""),
        "content_hash": str(payload.get("content_hash") or ""),
        "fetched_at": str(payload.get("fetched_at") or ""),
        "sections": sections,
        "errors": _dict_list(payload.get("errors")),
    }


def _component_validation_messages(component: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    kind = str(component.get("kind") or component.get("type") or "").strip().lower()
    validation_kind = (
        "environment_requirement" if kind in ENVIRONMENT_REQUIREMENT_KINDS else kind
    )
    if validation_kind not in CAPABILITY_COMPONENT_KINDS:
        messages.append(
            "component.kind must be one of "
                + ", ".join(sorted(CAPABILITY_COMPONENT_KINDS))
        )
    name = str(component.get("name") or "").strip()
    if not name:
        messages.append("component.name is required")
    if validation_kind == "skill" and not _component_skill_content_value(component):
        component_name = name or str(component.get("id") or "skill")
        config = dict(component.get("config")) if isinstance(component.get("config"), dict) else {}
        resolution_error = str(
            component.get("skill_content_resolution_error")
            or config.get("skill_content_resolution_error")
            or ""
        ).strip()
        if resolution_error:
            messages.append(f"skill component '{component_name}' requires skill_content: {resolution_error}")
        else:
            messages.append(f"skill component '{component_name}' requires skill_content")
    access = str(component.get("access") or "").strip().lower()
    if access and access not in {"read", "write", "both"}:
        messages.append("component.access must be read, write, or both")
    execution_policy = str(component.get("execution_policy") or "").strip().lower()
    if execution_policy and execution_policy not in {
        "allow",
        "deny",
        "require_user",
        "escalate",
        "inherit",
    }:
        messages.append(
            "component.execution_policy must be allow, deny, require_user, escalate, or inherit"
        )
    component_id = str(component.get("id") or "").strip()
    if component_id:
        parsed_kind, sep, parsed_name = component_id.partition(":")
        if parsed_kind == "envreq":
            parsed_kind = "environment_requirement"
            _, _, parsed_name = parsed_name.partition(":")
        if sep and parsed_kind in CAPABILITY_COMPONENT_KINDS:
            if validation_kind in CAPABILITY_COMPONENT_KINDS and parsed_kind != validation_kind:
                messages.append("component.id kind must match component.kind")
            if name and parsed_name and parsed_name != name:
                messages.append("component.id name must match component.name")
    return messages


def _capability_draft_failure(
    draft: dict[str, Any] | None,
    validation: dict[str, Any] | None,
    source_bundle: dict[str, Any] | None,
) -> dict[str, Any] | None:
    validation_data = validation if isinstance(validation, dict) else {}
    messages = [
        str(item).strip()
        for item in validation_data.get("messages", [])
        if str(item).strip()
    ] if isinstance(validation_data.get("messages"), list) else []
    if isinstance(draft, dict) and not messages:
        return None
    bundle = source_bundle if isinstance(source_bundle, dict) else {}
    result_type = "draft_invalid"
    if _source_discovery_is_incomplete(bundle):
        result_type = "source_discovery_incomplete"
    elif not isinstance(draft, dict):
        result_type = "draft_not_produced"
    elif any("requires skill_content" in message for message in messages):
        result_type = "skill_content_unresolved"
    elif any("command lacks evidence" in message for message in messages):
        result_type = "command_evidence_missing"
    return {
        "result_type": result_type,
        "code": result_type,
        "messages": messages,
        "validation": validation_data,
        "source_summary": _source_bundle_summary(bundle),
        "source_inventory": bundle.get("source_inventory")
        if isinstance(bundle.get("source_inventory"), dict)
        else {},
    }


def _source_discovery_is_incomplete(source_bundle: dict[str, Any]) -> bool:
    source = source_bundle.get("source") if isinstance(source_bundle.get("source"), dict) else {}
    if str(source.get("type") or "") not in {"github_repo", "docs_url"}:
        return False
    documents = [
        item
        for item in _dict_list(source_bundle.get("documents"))
        if str(item.get("source_kind") or "") != "source_seed"
    ]
    evidence = [
        item
        for item in _dict_list(source_bundle.get("evidence"))
        if str(item.get("title") or "") != "GitHub repository"
    ]
    inventory = source_bundle.get("source_inventory")
    skill_files = (
        _dict_list(inventory.get("skill_files"))
        if isinstance(inventory, dict)
        else []
    )
    files = _dict_list(inventory.get("files")) if isinstance(inventory, dict) else []
    tool_calls = _dict_list(inventory.get("tool_calls")) if isinstance(inventory, dict) else []
    return not documents and not evidence and not skill_files and not files and not tool_calls


def _source_bundle_summary(source_bundle: dict[str, Any]) -> dict[str, Any]:
    inventory = source_bundle.get("source_inventory")
    return {
        "documents": len(_dict_list(source_bundle.get("documents"))),
        "evidence": len(_dict_list(source_bundle.get("evidence"))),
        "errors": len(_dict_list(source_bundle.get("errors"))),
        "files": len(_dict_list(inventory.get("files"))) if isinstance(inventory, dict) else 0,
        "skill_files": len(_dict_list(inventory.get("skill_files"))) if isinstance(inventory, dict) else 0,
    }


def _component_command_evidence_messages(
    component: dict[str, Any],
    *,
    draft: CapabilityPackageDraft,
    evidence_bundle: EvidenceBundle | None,
) -> list[str]:
    searchable = _evidence_search_text(draft=draft, evidence_bundle=evidence_bundle)
    messages: list[str] = []
    for command in _component_command_values(component):
        if command and command not in searchable:
            component_id = str(component.get("id") or component.get("name") or "component")
            messages.append(f"{component_id} command lacks evidence: {command}")
    return messages


def _component_command_values(component: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw_config = component.get("config")
    config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, dict) else {}
    for field in ENVIRONMENT_COMMAND_FIELDS:
        for container in (component, config):
            value = container.get(field)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, list):
                values.extend(str(item).strip() for item in value if str(item).strip())
    return _unique_strings(values)


def _component_skill_content_value(component: dict[str, Any]) -> str:
    raw_config = component.get("config")
    config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, dict) else {}
    for container in (component, config):
        for field_name in ("skill_content", "content"):
            value = container.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _evidence_search_text(
    *,
    draft: CapabilityPackageDraft,
    evidence_bundle: EvidenceBundle | None,
) -> str:
    parts: list[str] = []
    for item in draft.evidence:
        parts.extend(str(value) for value in item.values())
    if evidence_bundle is not None:
        for item in evidence_bundle.evidence:
            parts.extend(str(value) for value in item.values())
        for document in evidence_bundle.documents:
            parts.append(str(document.get("content") or ""))
    return "\n".join(parts)


def _environment_requirement_items(data: dict[str, Any]) -> dict[str, Any]:
    environment = data.setdefault("environment", {})
    if not isinstance(environment, dict):
        environment = {}
        data["environment"] = environment
    items = environment.setdefault("requirements", {})
    if not isinstance(items, dict):
        items = {}
        environment["requirements"] = items
    return items


def _mcp_server_items(data: dict[str, Any]) -> dict[str, Any]:
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    items = mcp.setdefault("servers", {})
    if not isinstance(items, dict):
        items = {}
        mcp["servers"] = items
    return items


def _skill_items(data: dict[str, Any]) -> dict[str, Any]:
    skills = data.setdefault("skills", {})
    if not isinstance(skills, dict):
        skills = {}
        data["skills"] = skills
    items = skills.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        skills["items"] = items
    return items


def _assert_materialized_resource_slot(
    items: dict[str, Any],
    item_id: str,
    component: CapabilityComponentConfig,
) -> None:
    current = items.get(item_id)
    if not isinstance(current, dict):
        return
    if str(current.get("managed_by") or "") == "capability_package":
        existing_component_id = str(current.get("component_id") or "").strip()
        if existing_component_id == component.id:
            return
        raw_package_ids = current.get("package_ids", [])
        package_ids = (
            _unique_strings([str(item) for item in raw_package_ids])
            if isinstance(raw_package_ids, list)
            else []
        )
        details = [
            f"existing component_id={existing_component_id or '<missing>'}",
            f"incoming component_id={component.id}",
        ]
        if package_ids:
            details.append("package_ids=" + ",".join(package_ids))
        raise CapabilityPackageIngestError(
            "capability_resource_conflict",
            f"{component.kind} resource '{item_id}' is already managed by another capability component; "
            + "; ".join(details),
            status=HTTPStatus.CONFLICT,
        )
    raise CapabilityPackageIngestError(
        "capability_resource_conflict",
        f"{component.kind} resource '{item_id}' is already user-managed",
        status=HTTPStatus.CONFLICT,
    )


def _bool_field(payload: dict[str, Any], field_name: str, default: Any) -> bool:
    if field_name not in payload:
        return bool(default)
    value = payload.get(field_name)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _skill_content_from_payload(payload: dict[str, Any]) -> str:
    for field_name in ("skill_content", "content"):
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.replace("\r\n", "\n")
    return ""


def _skill_metadata_from_content(content: str) -> dict[str, str]:
    if not content.strip():
        return {}
    skill, _diagnostics = parse_skill_content(
        content.replace("\r\n", "\n"),
        skill_md_path=Path("SKILL.md"),
        scope="package",
    )
    if skill is None:
        return {}
    return {
        "name": skill.name,
        "description": skill.description,
    }


def _slug_path_segment(value: str) -> str:
    text = str(value or "").strip().lower()
    chars = [
        char
        if char.isalnum() or char in {"-", "_", "."}
        else "-"
        for char in text
    ]
    slug = "".join(chars).strip(".-")
    return slug or "item"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _dedupe_links(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = (str(value.get("title") or ""), str(value.get("url") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _dedupe_evidence(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = (
            str(value.get("source_url") or ""),
            str(value.get("content_hash") or ""),
            str(value.get("excerpt") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)].rstrip() + "... (truncated)"


__all__ = [
    "CAPABILITY_INGEST_WORKFLOW",
    "CapabilityDraftValidationResult",
    "CapabilityDraftValidator",
    "CapabilityPackagerRunner",
    "CapabilityPackageIngestError",
    "CapabilityPackageIngestResult",
    "CapabilityPackageIngestService",
    "CapabilityPackageSessionRunService",
    "CapabilityPackageInstallResult",
    "CapabilityPackageSkillFileOperation",
    "CapabilityPackageInstaller",
    "CapabilitySourceCollector",
    "EvidenceBundle",
]
