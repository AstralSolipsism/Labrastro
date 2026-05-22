"""Capability package ingestion through a dedicated AgentRun."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
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
    EnvironmentCLIToolConfig,
    EnvironmentSkillConfig,
    MCPServerConfig,
    ensure_default_capability_packages,
)
from reuleauxcoder.extensions.tools.builtin.fetch_capabilities import FetchCapabilitiesTool


CAPABILITY_INGEST_WORKFLOW = "capability_package_ingest"
MAX_SNIPPET_CHARS = 36_000
DEFAULT_CAPABILITY_FOCUS = "install setup configure authentication requirements cli mcp skill"
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_CAPABILITY_COMPONENT_CONFIG_FIELDS = {
    "command",
    "args",
    "env",
    "cwd",
    "placement",
    "distribution",
    "requirements",
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
        data = value if isinstance(value, dict) else {}
        return cls(
            source=dict(data.get("source") if isinstance(data.get("source"), dict) else {}),
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
class CapabilityPackageInstallResult:
    package_id: str
    package: CapabilityPackageConfig
    component_ids: list[str]


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
    ) -> AgentRunRecord:
        bundle = evidence_bundle.to_dict()
        prompt = _render_packager_prompt(bundle=bundle)
        metadata = {
            "workflow": CAPABILITY_INGEST_WORKFLOW,
            "agent_run_source": "capability_ingest",
            "capability_source": evidence_bundle.source,
            "source_bundle": bundle,
        }
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

    def install_draft(
        self,
        data: dict[str, Any],
        raw_draft: dict[str, Any],
        *,
        package_id: str = "",
    ) -> CapabilityPackageInstallResult:
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
        )

    def component_from_draft(
        self,
        package_id: str,
        item: dict[str, Any],
        package_source: dict[str, Any],
    ) -> CapabilityComponentConfig:
        kind = str(item.get("kind", item.get("type", "")) or "").strip().lower()
        if kind not in CAPABILITY_COMPONENT_KINDS:
            raise ValueError(
                "component.kind must be one of "
                + ", ".join(sorted(CAPABILITY_COMPONENT_KINDS))
            )
        name = str(item.get("name", "") or "").strip()
        if not name:
            raise ValueError("component.name is required")
        component_id = str(item.get("id") or f"{kind}:{name}").strip()
        raw_config = item.get("config")
        config = dict(raw_config) if isinstance(raw_config, dict) else {}
        for field in _CAPABILITY_COMPONENT_CONFIG_FIELDS:
            if field in item and field not in config:
                config[field] = item[field]
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
        if component.kind not in {"cli", "cli_tool", "mcp", "skill"}:
            return
        items = _toolchain_items(data, component.kind)
        payload = dict(component.config)
        payload["enabled"] = component.enabled
        payload["component_id"] = component.id
        payload["package_ids"] = list(component.package_ids)
        payload["managed_by"] = "capability_package"
        payload.setdefault("source", component.source.url or component.source.type)
        if component.source.url:
            payload.setdefault("repo_url", component.source.url)
        payload.setdefault("last_action", "capability_package_accept")
        items[component.name] = _normalize_toolchain_item(
            component.kind,
            component.name,
            payload,
        )

    def remove_materialized_component(
        self,
        data: dict[str, Any],
        component: CapabilityComponentConfig,
    ) -> None:
        items = _toolchain_items(data, component.kind)
        current = items.get(component.name)
        if not isinstance(current, dict):
            return
        if str(current.get("component_id") or "") != component.id:
            return
        if str(current.get("managed_by") or "") != "capability_package":
            return
        del items[component.name]


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

    def start(self, payload: dict[str, Any]) -> CapabilityPackageIngestResult:
        source_payload = (
            payload.get("source")
            if isinstance(payload.get("source"), dict)
            else payload
        )
        evidence_bundle = self.collector.collect(source_payload)
        workspace_root = str(payload.get("workspace_root") or "").strip()
        agent_run = self.packager_runner.start(
            evidence_bundle=evidence_bundle,
            workspace_root=workspace_root,
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
        validation: dict[str, Any] | None = None
        if draft is not None:
            metadata = agent_run.get("metadata") if isinstance(agent_run, dict) else {}
            source_bundle = (
                metadata.get("source_bundle")
                if isinstance(metadata, dict) and isinstance(metadata.get("source_bundle"), dict)
                else {}
            )
            validation = self.draft_validator.validate(
                draft,
                EvidenceBundle.from_dict(source_bundle),
            ).to_dict()
        return {
            "ok": True,
            "agent_run": agent_run,
            "events": events,
            "draft": draft,
            "validation": validation,
        }


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


def _render_packager_prompt(*, bundle: dict[str, Any]) -> str:
    bundle_json = json.dumps(bundle, ensure_ascii=False, indent=2)
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
        '  "components": [{"id": "cli:gh", '
        '"kind": "builtin_tool|cli_tool|credential|env|mcp_server|mcp_tool|skill", '
        '"name": "gh", "access": "read|write|both", '
        '"execution_policy": "allow|deny|require_user|escalate|inherit", '
        '"config": {}}],\n'
        '  "effective_capabilities": ["Plain language capability added to an Agent"],\n'
        '  "install_plan": [], "usage": [], "evidence": [], "credentials": [], '
        '"risk_level": "low|medium|high", "execution_policy": "inherit", "notes": []\n'
        "}\n\n"
        "Evidence bundle:\n"
        f"```json\n{bundle_json}\n```\n"
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
        if isinstance(parsed, dict) and isinstance(parsed.get("components"), list):
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
    if kind not in CAPABILITY_COMPONENT_KINDS:
        messages.append(
            "component.kind must be one of "
            + ", ".join(sorted(CAPABILITY_COMPONENT_KINDS))
        )
    name = str(component.get("name") or "").strip()
    if not name:
        messages.append("component.name is required")
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
        if sep and parsed_kind in CAPABILITY_COMPONENT_KINDS:
            if kind in CAPABILITY_COMPONENT_KINDS and parsed_kind != kind:
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
    config = component.get("config") if isinstance(component.get("config"), dict) else {}
    for field in ("command", "check", "install"):
        for container in (component, config):
            value = container.get(field)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, list):
                values.extend(str(item).strip() for item in value if str(item).strip())
    return _unique_strings(values)


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


def _toolchain_items(data: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "cli_tool":
        kind = "cli"
    if kind in {"cli", "skill"}:
        environment = data.setdefault("environment", {})
        if not isinstance(environment, dict):
            environment = {}
            data["environment"] = environment
        key = "cli_tools" if kind == "cli" else "skills"
        items = environment.setdefault(key, {})
        if not isinstance(items, dict):
            items = {}
            environment[key] = items
        return items

    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    items = mcp.setdefault("servers", {})
    if not isinstance(items, dict):
        items = {}
        mcp["servers"] = items
    return items


def _normalize_toolchain_item(kind: str, name: str, item: dict[str, Any]) -> dict[str, Any]:
    if kind == "cli_tool":
        kind = "cli"
    if kind == "cli":
        return EnvironmentCLIToolConfig.from_dict(name, item).to_dict()
    if kind == "skill":
        return EnvironmentSkillConfig.from_dict(name, item).to_dict()
    return MCPServerConfig.from_dict(name, item).to_dict()


def _bool_field(payload: dict[str, Any], field_name: str, default: Any) -> bool:
    if field_name not in payload:
        return bool(default)
    value = payload.get(field_name)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


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
    "CapabilityPackageInstallResult",
    "CapabilityPackageInstaller",
    "CapabilitySourceCollector",
    "EvidenceBundle",
]
