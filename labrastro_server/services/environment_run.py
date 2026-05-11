"""Environment configuration runs submitted through AgentRun."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from labrastro_server.interfaces.http.remote.protocol import (
    EnvironmentManifestResponse,
)
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from reuleauxcoder.domain.agent_runtime.models import AgentRunRecord, TriggerMode
from reuleauxcoder.domain.config.models import DEFAULT_ENVIRONMENT_AGENT_ID


ENVIRONMENT_WORKFLOW = "environment_config"
ENVIRONMENT_MODE_PERMISSIONS = {
    "check": {"environment.check", "environment.manifest.read"},
    "configure": {"environment.configure", "environment.manifest.read"},
}


class EnvironmentRunError(Exception):
    """HTTP-safe environment run submission error."""

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
class EnvironmentRunResult:
    agent_run: AgentRunRecord
    agent_id: str
    entry_ids: list[str]
    manifest_hash: str


class EnvironmentRunService:
    """Submit environment check/configure work as a normal AgentRun."""

    def __init__(self, runtime_control_plane: AgentRunControlPlane) -> None:
        self.runtime_control_plane = runtime_control_plane

    def submit(
        self,
        *,
        mode: str,
        manifest: EnvironmentManifestResponse,
        workspace_root: str,
        entry_ids: list[str] | None = None,
        agent_id: str | None = None,
    ) -> EnvironmentRunResult:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in ENVIRONMENT_MODE_PERMISSIONS:
            raise EnvironmentRunError(
                "invalid_environment_mode",
                "mode must be check or configure",
            )
        selected_entries = _select_entries(manifest, entry_ids or [])
        selected_ids = [entry["id"] for entry in selected_entries]
        selected_manifest = {
            "cli_tools": [
                entry["manifest"]
                for entry in selected_entries
                if entry["kind"] == "cli"
            ],
            "mcp_servers": [
                entry["manifest"]
                for entry in selected_entries
                if entry["kind"] == "mcp"
            ],
            "skills": [
                entry["manifest"]
                for entry in selected_entries
                if entry["kind"] == "skill"
            ],
        }
        manifest_hash = _manifest_hash(selected_manifest)
        selected_agent_id = self._select_agent(
            mode=normalized_mode,
            preferred_agent_id=agent_id,
        )
        allowed_commands = _allowed_commands(selected_entries, normalized_mode)
        prompt = _render_environment_prompt(
            mode=normalized_mode,
            workspace_root=workspace_root,
            selected_manifest=selected_manifest,
            allowed_commands=allowed_commands,
        )
        metadata = {
            "workflow": ENVIRONMENT_WORKFLOW,
            "agent_run_source": "environment",
            "environment_mode": normalized_mode,
            "entry_ids": list(selected_ids),
            "manifest_hash": manifest_hash,
            "allowed_commands": allowed_commands,
        }
        if workspace_root:
            metadata["workspace_root"] = workspace_root
        agent_run = self.runtime_control_plane.submit_agent_run(
            AgentRunRequest(
                issue_id=f"environment-{normalized_mode}",
                agent_id=selected_agent_id,
                prompt=prompt,
                source="environment",
                trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
                workdir=workspace_root or None,
                metadata=metadata,
            )
        )
        return EnvironmentRunResult(
            agent_run=agent_run,
            agent_id=selected_agent_id,
            entry_ids=selected_ids,
            manifest_hash=manifest_hash,
        )

    def _select_agent(self, *, mode: str, preferred_agent_id: str | None) -> str:
        required_permissions = ENVIRONMENT_MODE_PERMISSIONS[mode]
        snapshot = self.runtime_control_plane.runtime_snapshot
        agents = snapshot.get("agents", {})
        if not isinstance(agents, dict):
            agents = {}
        selected_id = str(preferred_agent_id or DEFAULT_ENVIRONMENT_AGENT_ID).strip()
        agent = agents.get(selected_id)
        if not isinstance(agent, dict):
            raise EnvironmentRunError(
                "environment_agent_not_found",
                f"environment agent not found: {selected_id}",
                status=HTTPStatus.NOT_FOUND,
            )
        permissions = _resolved_permissions(agent)
        if not required_permissions <= permissions:
            missing = sorted(required_permissions - permissions)
            raise EnvironmentRunError(
                "environment_agent_permission_mismatch",
                f"agent {selected_id} lacks environment permissions: {', '.join(missing)}",
            )
        return selected_id


def _resolved_permissions(agent: dict[str, Any]) -> set[str]:
    resolved = agent.get("resolved_capabilities", {})
    if not isinstance(resolved, dict):
        return set()
    permissions = resolved.get("permissions", [])
    if not isinstance(permissions, list):
        return set()
    return {str(item) for item in permissions if str(item).strip()}


def _manifest_entries(manifest: EnvironmentManifestResponse) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for tool in manifest.cli_tools:
        data = tool.to_dict()
        entries.append(
            {
                "id": f"cli:{tool.name}",
                "kind": "cli",
                "name": tool.name,
                "manifest": data,
                "check": str(data.get("check", "") or ""),
                "install": str(data.get("install", "") or ""),
            }
        )
    for server in manifest.mcp_servers:
        data = server.to_dict()
        entries.append(
            {
                "id": f"mcp:{server.name}",
                "kind": "mcp",
                "name": server.name,
                "manifest": data,
                "check": str(data.get("check", "") or ""),
                "install": str(data.get("install", "") or ""),
            }
        )
    for skill in manifest.skills:
        data = skill.to_dict()
        entries.append(
            {
                "id": f"skill:{skill.name}",
                "kind": "skill",
                "name": skill.name,
                "manifest": data,
                "check": str(data.get("check", "") or ""),
                "install": str(data.get("install", "") or ""),
            }
        )
    return entries


def _select_entries(
    manifest: EnvironmentManifestResponse, requested_ids: list[str]
) -> list[dict[str, Any]]:
    entries = _manifest_entries(manifest)
    if not requested_ids:
        return entries
    by_id = {entry["id"]: entry for entry in entries}
    missing = [entry_id for entry_id in requested_ids if entry_id not in by_id]
    if missing:
        raise EnvironmentRunError(
            "environment_entry_not_found",
            f"environment manifest entry not found: {missing[0]}",
            status=HTTPStatus.NOT_FOUND,
        )
    return [by_id[entry_id] for entry_id in requested_ids]


def _allowed_commands(entries: list[dict[str, Any]], mode: str) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    for entry in entries:
        check = str(entry.get("check", "") or "").strip()
        if check:
            commands.append(
                {
                    "entry_id": str(entry["id"]),
                    "kind": str(entry["kind"]),
                    "name": str(entry["name"]),
                    "phase": "check",
                    "command": check,
                }
            )
        install = str(entry.get("install", "") or "").strip()
        if mode == "configure" and install:
            commands.append(
                {
                    "entry_id": str(entry["id"]),
                    "kind": str(entry["kind"]),
                    "name": str(entry["name"]),
                    "phase": "install",
                    "command": install,
                }
            )
    return commands


def _manifest_hash(selected_manifest: dict[str, Any]) -> str:
    raw = json.dumps(selected_manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _render_environment_prompt(
    *,
    mode: str,
    workspace_root: str,
    selected_manifest: dict[str, Any],
    allowed_commands: list[dict[str, str]],
) -> str:
    workspace = workspace_root or "(worker workspace)"
    manifest_json = json.dumps(selected_manifest, ensure_ascii=False, indent=2)
    commands_json = json.dumps(allowed_commands, ensure_ascii=False, indent=2)
    mode_instruction = (
        "Check mode: run only commands whose phase is `check`. Do not run install commands."
        if mode == "check"
        else "Configure mode: run check commands first; run an install command only when the corresponding check fails and normal approval is granted."
    )
    return (
        "You are an environment configuration Agent running through the normal Agent runtime.\n"
        "The server manifest is authoritative. Do not discover unrelated tools, scan PATH broadly, or invent commands.\n\n"
        f"Mode: {mode}\n"
        f"Workspace: {workspace}\n\n"
        f"{mode_instruction}\n"
        "Use only the commands listed in `allowed_commands`. After any install command, rerun that entry's check command.\n"
        "If a required base runtime or credential is missing, report it as a blocker instead of installing a substitute.\n"
        "Finish with a compact summary covering each selected entry.\n\n"
        "Selected environment manifest:\n"
        f"```json\n{manifest_json}\n```\n\n"
        "allowed_commands:\n"
        f"```json\n{commands_json}\n```\n"
    )


__all__ = [
    "ENVIRONMENT_AGENT_CAPABILITIES",
    "ENVIRONMENT_WORKFLOW",
    "EnvironmentRunError",
    "EnvironmentRunResult",
    "EnvironmentRunService",
]
