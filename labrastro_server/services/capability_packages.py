"""Capability package ingestion through a dedicated AgentRun."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from reuleauxcoder.domain.agent_runtime.models import AgentRunRecord, TriggerMode
from reuleauxcoder.domain.config.models import DEFAULT_CAPABILITY_PACKAGER_AGENT_ID


CAPABILITY_INGEST_WORKFLOW = "capability_package_ingest"
MAX_FETCH_BYTES = 192_000
MAX_SNIPPET_CHARS = 36_000
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


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
class CapabilityPackageIngestResult:
    agent_run: AgentRunRecord
    source: dict[str, Any]
    source_bundle: dict[str, Any]


class CapabilityPackageIngestService:
    """Create read-only source bundles and submit package-drafting AgentRuns."""

    def __init__(self, runtime_control_plane: AgentRunControlPlane) -> None:
        self.runtime_control_plane = runtime_control_plane

    def start(self, payload: dict[str, Any]) -> CapabilityPackageIngestResult:
        source = _normalize_source(payload.get("source") if isinstance(payload.get("source"), dict) else payload)
        bundle = _build_source_bundle(source)
        prompt = _render_packager_prompt(source=source, bundle=bundle)
        metadata = {
            "workflow": CAPABILITY_INGEST_WORKFLOW,
            "agent_run_source": "capability_ingest",
            "capability_source": source,
            "source_bundle": bundle,
        }
        workspace_root = str(payload.get("workspace_root") or "").strip()
        if workspace_root:
            metadata["workspace_root"] = workspace_root
        agent_run = self.runtime_control_plane.submit_agent_run(
            AgentRunRequest(
                issue_id="capability-package-ingest",
                agent_id=DEFAULT_CAPABILITY_PACKAGER_AGENT_ID,
                prompt=prompt,
                source="capability_ingest",
                trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
                workdir=workspace_root or None,
                metadata=metadata,
            )
        )
        return CapabilityPackageIngestResult(agent_run=agent_run, source=source, source_bundle=bundle)

    def status(self, agent_run_id: str) -> dict[str, Any]:
        task_id = str(agent_run_id or "").strip()
        if not task_id:
            raise CapabilityPackageIngestError(
                "agent_run_id_required",
                "agent_run_id is required",
            )
        try:
            agent_run = self.runtime_control_plane.agent_run_to_dict(task_id)
        except KeyError as exc:
            raise CapabilityPackageIngestError(
                "agent_run_not_found",
                f"AgentRun not found: {task_id}",
                status=HTTPStatus.NOT_FOUND,
            ) from exc
        events = [
            event.to_dict()
            for event in self.runtime_control_plane.list_events(task_id, after_seq=0, limit=200)
        ]
        draft = _extract_draft(agent_run.get("output"))
        if draft is None:
            for event in reversed(events):
                payload = event.get("payload")
                if isinstance(payload, dict):
                    draft = _extract_draft(payload.get("text") or payload.get("output"))
                    if draft is not None:
                        break
        return {
            "ok": True,
            "agent_run": agent_run,
            "events": events,
            "draft": draft,
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
    notes = str(payload.get("notes") or payload.get("project_notes") or payload.get("docsText") or "").strip()
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
        "package_id_hint": str(payload.get("packageIdHint") or payload.get("package_id_hint") or "").strip(),
    }
    return {key: value for key, value in source.items() if value not in ("", [])}


def _build_source_bundle(source: dict[str, Any]) -> dict[str, Any]:
    documents: list[dict[str, str]] = []
    notes = str(source.get("notes") or "").strip()
    if notes:
        documents.append(
            {
                "title": "Project notes",
                "url": "",
                "content": notes[:MAX_SNIPPET_CHARS],
            }
        )
    source_type = str(source.get("type") or "")
    url = str(source.get("url") or "")
    if source_type == "docs_url" and url:
        documents.append(_fetch_document(url, title="Documentation"))
    if source_type == "github_repo" and url:
        for doc_url in _github_candidate_doc_urls(url, str(source.get("ref") or "")):
            document = _fetch_document(doc_url, title=doc_url.rsplit("/", 1)[-1])
            if document.get("content"):
                documents.append(document)
    return {"documents": documents[:8]}


def _fetch_document(url: str, *, title: str) -> dict[str, str]:
    try:
        request = Request(url, headers={"User-Agent": "EZCode-Capability-Ingest/1.0"})
        with urlopen(request, timeout=12) as response:
            raw = response.read(MAX_FETCH_BYTES)
        text = raw.decode("utf-8", errors="replace")
        return {"title": title, "url": url, "content": text[:MAX_SNIPPET_CHARS]}
    except (OSError, URLError, ValueError) as exc:
        return {"title": title, "url": url, "content": "", "error": str(exc)}


def _github_candidate_doc_urls(repo_url: str, ref: str) -> list[str]:
    match = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", repo_url.strip())
    if match is None:
        return [repo_url]
    owner, repo = match.group(1), match.group(2).removesuffix(".git")
    branch = ref or "main"
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
    return [
        f"{base}/README.md",
        f"{base}/README.zh-CN.md",
        f"{base}/docs/README.md",
        f"{base}/docs/installation.md",
        f"{base}/docs/install.md",
    ]


def _render_packager_prompt(*, source: dict[str, Any], bundle: dict[str, Any]) -> str:
    source_json = json.dumps(source, ensure_ascii=False, indent=2)
    bundle_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    return (
        "You are capability_packager. Analyze the provided repository/docs bundle and produce one capability package draft.\n"
        "Discovery is read-only: do not run install commands and do not mutate files.\n"
        "Extract only instructions supported by the supplied evidence. Prefer exact install/check/launch commands from docs.\n"
        "Return final output as a single fenced JSON object with this shape:\n"
        "{\n"
        '  "id": "package-id", "name": "Package Name", "description": "...",\n'
        '  "source": {"type": "github_repo|docs_url|project_notes", "url": "..."},\n'
        '  "components": [{"id": "cli:gh", "kind": "cli|mcp|skill", "name": "gh", "config": {}}],\n'
        '  "install_plan": [], "usage": [], "evidence": [], "credentials": [], "risk_level": "low|medium|high", "notes": []\n'
        "}\n\n"
        f"Source:\n```json\n{source_json}\n```\n\n"
        f"Source bundle:\n```json\n{bundle_json}\n```\n"
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


__all__ = [
    "CAPABILITY_INGEST_WORKFLOW",
    "CapabilityPackageIngestError",
    "CapabilityPackageIngestResult",
    "CapabilityPackageIngestService",
]
