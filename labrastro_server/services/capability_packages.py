"""Capability package ingestion through a dedicated AgentRun."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from reuleauxcoder.domain.agent_runtime.models import (
    AgentRunRecord,
    CAPABILITY_COMPONENT_KINDS,
    CapabilityComponentConfig,
    CapabilityPackageConfig,
    CapabilityPackageDraft,
    TriggerMode,
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
from reuleauxcoder.extensions.tools.builtin.fetch_capabilities import FetchCapabilitiesTool


CAPABILITY_INGEST_WORKFLOW = "capability_package_ingest"
MAX_SNIPPET_CHARS = 36_000
DEFAULT_CAPABILITY_FOCUS = "install setup configure authentication requirements runtime sdk executable mcp skill"
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_CAPABILITY_COMPONENT_CONFIG_FIELDS = {
    "command",
    "args",
    "env",
    "cwd",
    "placement",
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
    """Collect read-only source evidence through fetch_capabilities."""

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
        if source_type in {"docs_url", "github_repo"} and url:
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
        prompt = _render_packager_prompt(
            bundle=bundle,
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
        ):
            value = (agent_run_metadata or {}).get(key)
            if value is not None and str(value).strip():
                metadata[key] = str(value)
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
                workdir=workspace_root or None,
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
                limit=200,
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
        return CapabilityComponentConfig(
            id=component_id,
            kind=kind,
            name=name,
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
        validation: dict[str, Any] | None = None
        if draft is not None:
            validation = self.draft_validator.validate(
                draft,
                EvidenceBundle.from_dict(source_bundle),
            ).to_dict()
        return {
            "ok": True,
            "agent_run": agent_run,
            "events": events,
            "draft": draft,
            "source_bundle": source_bundle,
            "validation": validation,
        }


class CapabilityPackageSessionRunService:
    """Run capability package ingestion as a user-visible SessionRun."""

    TERMINAL_AGENT_STATUSES = {"completed", "failed", "cancelled", "blocked"}
    INSTALL_TOOL_NAME = "install_capability_package"
    REVISION_APPROVAL_REASON = "收到修改意见，重新生成草案。"

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
                    self.REVISION_APPROVAL_REASON,
                )

        session.set_follow_up_callback(on_follow_up)
        try:
            if getattr(session, "cancel_requested", False):
                return
            session.mark_running()
            session.append_event(
                "context_event",
                _capability_context_event(
                    "开始生成能力包草案",
                    "capability_package_ingest",
                    {"agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID},
                ),
            )
            result = CapabilityPackageIngestService(self.runtime_control_plane).start(
                payload,
                agent_run_metadata=self._agent_run_metadata(session),
            )
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
            session.append_event(
                "error",
                {"message": str(exc), "code": "capability_package_session_failed"},
            )
            session.append_event(
                "session_run_failed",
                {
                    "message": str(exc),
                    "code": "capability_package_session_failed",
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
        if extra:
            metadata.update(extra)
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
        session.append_event(
            "context_event",
            _capability_context_event(
                "能力包修改任务已进入 capability_packager"
                if is_revision
                else "能力包生成任务已进入 capability_packager",
                "capability_package_revision" if is_revision else "capability_package_ingest",
                {
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
            message = str(task.get("output") or task_status or "capability package ingest failed")
            session.append_event("error", {"message": message, "code": task_status})
            session.append_event(
                "session_run_failed",
                {"message": message, "code": task_status, "recoverable": False},
            )
            return None
        draft = status.get("draft")
        validation = status.get("validation") if isinstance(status.get("validation"), dict) else {}
        messages = [
            str(item)
            for item in validation.get("messages", [])
            if str(item).strip()
        ] if isinstance(validation.get("messages"), list) else []
        if not isinstance(draft, dict) or messages:
            message = "; ".join(messages) if messages else "capability package draft was not produced"
            session.append_event("error", {"message": message, "code": "invalid_capability_package_draft"})
            session.append_event(
                "session_run_failed",
                {
                    "message": message,
                    "code": "invalid_capability_package_draft",
                    "recoverable": False,
                },
            )
            return None
        source_bundle = status.get("source_bundle") if isinstance(status.get("source_bundle"), dict) else {}
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
            for event in events:
                cursor = max(cursor, int(event.seq))
                for session_event_type, payload in _agent_event_to_session_events(event.to_dict()):
                    session.append_event(session_event_type, payload)
            try:
                task = self.runtime_control_plane.agent_run_to_dict(agent_run_id)
            except KeyError:
                return
            status = str(task.get("status") or "").strip()
            if status in self.TERMINAL_AGENT_STATUSES and not events:
                return
            if status in self.TERMINAL_AGENT_STATUSES:
                tail = self.runtime_control_plane.list_events(
                    agent_run_id,
                    after_seq=cursor,
                    limit=100,
                )
                if not tail:
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
        approval_payload = _capability_install_approval_payload(
            approval_id,
            tool_call_id,
            draft,
            agent_run_id,
        )
        session.append_event(
            "tool_call_start",
            {
                "tool_call_id": tool_call_id,
                "tool_name": self.INSTALL_TOOL_NAME,
                "tool_args": {
                    "package_id": package_id,
                    "agent_run_id": agent_run_id,
                },
                "title": f"安装能力包 {package_id}",
            },
        )
        session.register_approval(approval_id, approval_payload)
        session.append_event("approval_request", approval_payload)
        with follow_up_lock:
            active_approval_id["value"] = approval_id
            has_pending_revision = bool(follow_up_queue)
        if has_pending_revision:
            session.resolve_approval(
                approval_id,
                "deny_once",
                self.REVISION_APPROVAL_REASON,
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
                    "reason": self.REVISION_APPROVAL_REASON,
                },
            )
            response = "已收到修改意见，重新生成能力包草案。"
            session.append_event(
                "tool_call_end",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": self.INSTALL_TOOL_NAME,
                    "status": "cancelled",
                    "output": response,
                },
            )
            followup_id = str(revision_ticket.get("followup_id") or "")
            if followup_id:
                session.mark_follow_up_consumed(followup_id)
            instruction = str(revision_ticket.get("text") or "").strip()
            instruction_title = (
                f"收到修改意见：{_truncate_single_line(instruction, 80)}"
                if instruction
                else "收到修改意见，重新生成能力包草案"
            )
            session.append_event(
                "context_event",
                _capability_context_event(
                    instruction_title,
                    "capability_package_revision_requested",
                    {
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
            response = f"已取消安装能力包 {package_id}。"
            session.append_event(
                "tool_call_end",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": self.INSTALL_TOOL_NAME,
                    "status": "cancelled",
                    "output": response,
                },
            )
            session.append_event("session_run_end", {"response": response})
            return None
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
                "tool_call_end",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": self.INSTALL_TOOL_NAME,
                    "status": "failed",
                    "output": message,
                },
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
        response = f"能力包 {package_id} 已安装完成。"
        session.append_event(
            "tool_call_end",
            {
                "tool_call_id": tool_call_id,
                "tool_name": self.INSTALL_TOOL_NAME,
                "status": "completed",
                "output": response,
                "meta": getattr(result, "payload", {}),
            },
        )
        session.append_event("session_run_end", {"response": response})
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


def _capability_context_event(
    title: str,
    phase: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "message": title,
        "phase": phase,
        "workflow": CAPABILITY_INGEST_WORKFLOW,
        **(extra or {}),
    }


def _truncate_single_line(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars - 1]}…"


def _agent_event_to_session_events(
    event: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    data = payload if isinstance(payload, dict) else {}
    agent_run_id = str(event.get("agent_run_id") or "")
    base = {
        "agent_run_id": agent_run_id,
        "agent_id": DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
        "workflow": CAPABILITY_INGEST_WORKFLOW,
    }
    if event_type == "queued":
        task = data.get("agent_run") if isinstance(data.get("agent_run"), dict) else {}
        return [
            (
                "context_event",
                _capability_context_event(
                    "能力包生成任务已排队",
                    "agent_run_queued",
                    {
                        **base,
                        "agent_run_status": str(task.get("status") or "queued"),
                    },
                ),
            )
        ]
    if event_type == "status":
        status_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        status = str(status_data.get("status") or "").strip()
        if not status:
            return []
        return [
            (
                "context_event",
                _capability_context_event(
                    f"capability_packager {status}",
                    f"agent_run_{status}",
                    {**base, "agent_run_status": status, **status_data},
                ),
            )
        ]
    if event_type == "text":
        text = str(data.get("text") or "")
        return [("assistant_delta", {**base, "content": text})] if text else []
    if event_type == "thinking":
        text = str(data.get("text") or "")
        return [("reasoning_delta", {**base, "content": text})] if text else []
    if event_type == "log":
        text = str(data.get("text") or data.get("message") or "")
        level_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        level = str(level_data.get("level") or "info")
        return [
            (
                "context_event",
                _capability_context_event(
                    text or "capability_packager log",
                    "agent_run_log",
                    {**base, "level": level, "log": text},
                ),
            )
        ]
    if event_type == "error":
        message = str(data.get("text") or data.get("message") or "capability_packager error")
        return [("error", {**base, "message": message, "code": "agent_run_error"})]
    if event_type == "tool_use":
        tool_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        tool_name = str(
            tool_data.get("tool_name")
            or tool_data.get("name")
            or tool_data.get("tool")
            or "tool"
        )
        tool_call_id = str(
            tool_data.get("tool_call_id")
            or tool_data.get("id")
            or f"{agent_run_id}:tool:{event.get('seq') or 0}"
        )
        return [
            (
                "tool_call_start",
                {
                    **base,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_args": tool_data.get("input")
                    if isinstance(tool_data.get("input"), dict)
                    else tool_data,
                },
            )
        ]
    if event_type == "tool_result":
        tool_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        tool_name = str(
            tool_data.get("tool_name")
            or tool_data.get("name")
            or tool_data.get("tool")
            or "tool"
        )
        tool_call_id = str(
            tool_data.get("tool_call_id")
            or tool_data.get("id")
            or f"{agent_run_id}:tool:{event.get('seq') or 0}"
        )
        output = tool_data.get("output")
        if not isinstance(output, str):
            output = str(data.get("text") or "")
        return [
            (
                "tool_call_end",
                {
                    **base,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_result": output,
                    "meta": tool_data,
                },
            )
        ]
    if event_type in {"completed", "failed", "cancelled", "blocked"}:
        title = {
            "completed": "能力包草案生成完成",
            "failed": "能力包草案生成失败",
            "cancelled": "能力包草案生成已取消",
            "blocked": "能力包草案生成被阻断",
        }.get(event_type, event_type)
        task = data.get("agent_run") if isinstance(data.get("agent_run"), dict) else {}
        return [
            (
                "context_event",
                _capability_context_event(
                    title,
                    f"agent_run_{event_type}",
                    {
                        **base,
                        "agent_run_status": event_type,
                        "output": str(task.get("output") or ""),
                    },
                ),
            )
        ]
    return []


def _capability_install_approval_payload(
    approval_id: str,
    tool_call_id: str,
    draft: dict[str, Any],
    agent_run_id: str,
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
    sections = [
        {
            "title": "能力包",
            "items": [
                {"label": "ID", "value": package_id},
                {"label": "名称", "value": str(draft.get("name") or package_id)},
                {"label": "风险", "value": str(draft.get("risk_level") or "unrated")},
            ],
        },
        {
            "title": "组件摘要",
            "items": [
                {"label": "能力", "value": ", ".join(_unique_strings(capabilities)) or "无"},
                {"label": "依赖", "value": ", ".join(_unique_strings(dependencies)) or "无"},
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
                "title": "安装计划",
                "items": [{"label": str(index + 1), "value": item} for index, item in enumerate(install_plan)],
            }
        )
    return {
        "approval_id": approval_id,
        "tool_call_id": tool_call_id,
        "tool_name": CapabilityPackageSessionRunService.INSTALL_TOOL_NAME,
        "tool_args": {"package_id": package_id, "agent_run_id": agent_run_id},
        "intent": f"确认安装能力包 {package_id}",
        "content": str(draft.get("description") or f"准备安装能力包 {package_id}。"),
        "sections": sections,
    }


def _render_packager_prompt(
    *,
    bundle: dict[str, Any],
    revision_draft: dict[str, Any] | None = None,
    revision_instruction: str = "",
) -> str:
    bundle_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    revision_text = str(revision_instruction or "").strip()
    revision_block = ""
    if revision_text or isinstance(revision_draft, dict):
        current_draft_json = json.dumps(revision_draft or {}, ensure_ascii=False, indent=2)
        revision_block = (
            "\nRevision request:\n"
            "The user has reviewed the previous draft. Produce a complete revised draft, "
            "not a patch. Keep every field that remains valid, and apply the user's "
            "requested changes when they are supported by the evidence bundle.\n"
            f"User instruction:\n{revision_text or '(no extra text)'}\n"
            "Previous draft:\n"
            f"```json\n{current_draft_json}\n```\n"
        )
    return (
        "You are capability_packager. Analyze the provided repository/docs bundle "
        "and produce one capability package draft.\n"
        "Discovery is read-only: do not run install commands and do not mutate files.\n"
        "Extract only instructions supported by the supplied evidence. Prefer exact "
        "install/check/launch commands from docs.\n"
        "Return final output as a single fenced JSON object with this shape:\n"
        "{\n"
        '  "id": "package-id", "name": "Package Name", "description": "...",\n'
        '  "source": {"type": "github_repo|docs_url|project_notes", "url": "..."},\n'
        '  "contributions": {\n'
        '    "skills": [\n'
        '      {"id": "skill:code-review", "kind": "skill", "name": "code-review", '
        '"skill_content": "---\\nname: code-review\\ndescription: ...\\n---\\n..."}\n'
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
        'include "skill_content" for every Skill component or omit the Skill.\n'
        "Evidence bundle:\n"
        f"```json\n{bundle_json}\n```\n"
        f"{revision_block}"
    )


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
