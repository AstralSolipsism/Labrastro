"""Server-side executor skeleton for typed capability package actions."""

from __future__ import annotations

import shutil
from pathlib import Path

from labrastro_server.services.capability_package_install_plan import (
    InstallAction,
    InstallActionResult,
)


class CapabilityPackageServerExecutor:
    """Execute or plan only server-owned capability package actions."""

    def __init__(self, *, runtime_root: str | Path) -> None:
        self.runtime_root = Path(runtime_root).expanduser().resolve()

    def execute_action(self, action: InstallAction) -> InstallActionResult:
        server_action = action
        if action.target == "both":
            server_action = InstallAction(
                id=action.id,
                type=action.type,
                target="server",
                params=dict(action.params),
                component_id=action.component_id,
                requirement_id=action.requirement_id,
                depends_on=list(action.depends_on),
            )
        if server_action.target != "server":
            return InstallActionResult(
                action_id=server_action.id,
                action_type=server_action.type,
                target=server_action.target,
                status="blocked",
                details={"reason": "target_owned_by_local_peer"},
                target_facts={},
            )
        if server_action.type == "check_executable":
            return self._check_executable(server_action)
        if server_action.type == "install_python_packages":
            package_id = str(server_action.params.get("package_id") or server_action.component_id or "")
            runtime_path = self.plan_runtime_path(
                package_id=package_id or "package",
                action_type=server_action.type,
            )
            return InstallActionResult(
                action_id=server_action.id,
                action_type=server_action.type,
                target=server_action.target,
                status="planned",
                details={"runtime_path": str(runtime_path)},
                target_facts={"server": {"install_state": "registered"}},
            )
        return InstallActionResult(
            action_id=server_action.id,
            action_type=server_action.type,
            target=server_action.target,
            status="planned",
            details={"reason": "executor_action_not_implemented"},
            target_facts={"server": {"install_state": "registered"}},
        )

    def plan_runtime_path(self, *, package_id: str, action_type: str) -> Path:
        package_slug = _slug_path_segment(package_id)
        action_slug = _slug_path_segment(action_type)
        if action_type == "install_python_packages":
            action_slug = "python"
        path = (self.runtime_root / "capability-packages" / package_slug / action_slug).resolve()
        try:
            path.relative_to(self.runtime_root)
        except ValueError as exc:
            raise ValueError("runtime path escaped runtime_root") from exc
        return path

    def _check_executable(self, action: InstallAction) -> InstallActionResult:
        executable = str(action.params.get("executable") or "").strip()
        if not executable:
            return InstallActionResult(
                action_id=action.id,
                action_type=action.type,
                target=action.target,
                status="failed",
                details={"reason": "executable_required"},
                target_facts={"server": {"check_state": "failed"}},
            )
        found_path = shutil.which(executable)
        status = "passed" if found_path else "missing"
        details = {"executable": executable}
        if found_path:
            details["path"] = found_path
        return InstallActionResult(
            action_id=action.id,
            action_type=action.type,
            target=action.target,
            status=status,
            details=details,
            target_facts={
                "server": {
                    "check_state": status,
                    "executable": executable,
                }
            },
        )


def _slug_path_segment(value: str) -> str:
    text = str(value or "").strip().lower()
    chars = [
        char
        if char.isalnum() or char in {"-", "_", "."}
        else "-"
        for char in text
    ]
    return ("".join(chars).strip(".-") or "item")[:120]


__all__ = ["CapabilityPackageServerExecutor"]
