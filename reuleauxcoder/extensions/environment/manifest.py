"""Lightweight environment requirement manifest recording."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reuleauxcoder.domain.config.models import EnvironmentRequirementConfig
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


@dataclass(slots=True)
class EnvironmentRecordResult:
    requirement_id: str
    path: Path
    created: bool


class EnvironmentManifestManager:
    """Record server-authoritative environment requirements.

    This manager intentionally does not scan, verify, install, or inspect the local
    machine. It only updates the manifest that environment-capable Agent runtime
    tasks consume.
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or ConfigLoader.GLOBAL_CONFIG_PATH

    def record_requirement(
        self, requirement: EnvironmentRequirementConfig
    ) -> EnvironmentRecordResult:
        if not requirement.id.strip():
            raise ValueError("requirement id is required")
        if not requirement.name.strip():
            raise ValueError("requirement name is required")

        data = self._load_data()
        env_data = data.setdefault("environment", {})
        if not isinstance(env_data, dict):
            env_data = {}
            data["environment"] = env_data
        requirements = env_data.setdefault("requirements", {})
        if not isinstance(requirements, dict):
            requirements = {}
            env_data["requirements"] = requirements

        created = requirement.id not in requirements
        requirements[requirement.id] = requirement.to_dict()
        save_yaml_config(self.config_path, data)
        return EnvironmentRecordResult(
            requirement_id=requirement.id,
            path=self.config_path,
            created=created,
        )

    def _load_data(self) -> dict:
        try:
            data = load_yaml_config(self.config_path)
        except FileNotFoundError:
            data = {}
        return data if isinstance(data, dict) else {}


def run_env_record_cli(args) -> int:
    try:
        name = str(args.tool_name)
        requirement = EnvironmentRequirementConfig(
            id=f"envreq:executable:{name}",
            kind="executable",
            name=name,
            command=str(args.tool_command),
            tags=[str(item) for item in args.tag],
            check=str(args.check),
            install=str(args.install or ""),
            version=str(args.version) if args.version else None,
            source=str(args.source or ""),
            description=str(args.description or ""),
        )
        config_path = resolve_cli_config_path(args, require=True, purpose="env record")
        result = EnvironmentManifestManager(config_path).record_requirement(requirement)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    verb = "Created" if result.created else "Updated"
    print(f"{verb} environment requirement '{result.requirement_id}' in {result.path}")
    return 0
