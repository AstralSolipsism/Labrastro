"""Runtime footprint helpers for capability installation and display."""

from __future__ import annotations

from typing import Any

RuntimeLocation = str

RUNS_ON_VALUES = {"server", "local_peer", "both", "agent_only"}
TARGET_VALUES = {"server", "local_peer"}
TARGET_ORDER = ("server", "local_peer")

MESSAGE_AGENT_ONLY = "仅 Agent 指令能力，无需外部进程"
MESSAGE_SERVER = "服务端运行，无需本机安装"
MESSAGE_LOCAL = "需要在本机安装/配置"
MESSAGE_BOTH = "服务端和本地端都需要配置"


def normalize_runtime_footprint(
    value: Any,
    *,
    default_runs_on: RuntimeLocation = "agent_only",
) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    runs_on = _normalize_runs_on(raw.get("runs_on") or default_runs_on)
    default_targets = _targets_for_runs_on(runs_on)
    install_required_on = _normalize_targets(
        raw.get("install_required_on"),
        default=default_targets,
    )
    config_required_on = _normalize_targets(
        raw.get("config_required_on"),
        default=default_targets,
    )
    if runs_on == "agent_only":
        install_required_on = []
        config_required_on = []
    return {
        "runs_on": runs_on,
        "install_required_on": install_required_on,
        "config_required_on": config_required_on,
        "user_message": str(raw.get("user_message") or _user_message(runs_on)),
    }


def runtime_footprint_for_mcp(value: Any) -> dict[str, Any]:
    raw = _runtime_footprint_value(value)
    runs_on = _runs_on_from_placement(_field_value(value, "placement", "server"))
    return normalize_runtime_footprint(raw, default_runs_on=runs_on)


def runtime_footprint_for_environment_requirement(value: Any) -> dict[str, Any]:
    raw = _runtime_footprint_value(value)
    runs_on = _runs_on_from_placement(_field_value(value, "placement", "peer"))
    return normalize_runtime_footprint(raw, default_runs_on=runs_on)


def runtime_footprint_for_skill(
    value: Any,
    related_requirements: list[Any] | None = None,
) -> dict[str, Any]:
    raw = _runtime_footprint_value(value)
    footprint = normalize_runtime_footprint(raw, default_runs_on="agent_only")
    if not related_requirements:
        return footprint
    return aggregate_runtime_footprint(
        [
            footprint,
            *[
                runtime_footprint_for_environment_requirement(requirement)
                for requirement in related_requirements
            ],
        ]
    )


def runtime_footprint_for_component(value: Any) -> dict[str, Any]:
    kind = str(_field_value(value, "kind", "") or "").strip().lower()
    if kind in {"mcp", "mcp_server"}:
        return runtime_footprint_for_mcp(value)
    if kind == "skill":
        return runtime_footprint_for_skill(value)
    if kind == "environment_requirement":
        return runtime_footprint_for_environment_requirement(value)
    return normalize_runtime_footprint(_runtime_footprint_value(value))


def aggregate_runtime_footprint(values: list[Any]) -> dict[str, Any]:
    footprints = [
        normalize_runtime_footprint(value)
        for value in values
        if isinstance(value, dict)
    ]
    targets = _normalize_targets(
        [
            target
            for footprint in footprints
            for target in _targets_for_runs_on(str(footprint.get("runs_on") or ""))
        ],
        default=[],
    )
    install_required_on = _normalize_targets(
        [
            target
            for footprint in footprints
            for target in footprint.get("install_required_on", [])
        ],
        default=targets,
    )
    config_required_on = _normalize_targets(
        [
            target
            for footprint in footprints
            for target in footprint.get("config_required_on", [])
        ],
        default=targets,
    )
    combined_targets = _normalize_targets(
        [*targets, *install_required_on, *config_required_on],
        default=[],
    )
    runs_on = _runs_on_from_targets(combined_targets)
    return {
        "runs_on": runs_on,
        "install_required_on": install_required_on,
        "config_required_on": config_required_on,
        "user_message": _user_message(runs_on),
    }


def runs_on_to_mcp_placement(runs_on: str) -> str:
    if runs_on == "local_peer":
        return "peer"
    if runs_on == "both":
        return "both"
    return "server"


def runs_on_to_environment_placement(runs_on: str) -> str:
    if runs_on == "server":
        return "server"
    if runs_on == "both":
        return "both"
    return "peer"


def _runtime_footprint_value(value: Any) -> dict[str, Any]:
    direct = _field_value(value, "runtime_footprint", {})
    if isinstance(direct, dict) and direct:
        return direct
    config = _field_value(value, "config", {})
    if isinstance(config, dict) and isinstance(config.get("runtime_footprint"), dict):
        return config["runtime_footprint"]
    return {}


def _field_value(value: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        config = value.get("config")
        if field_name in value:
            return value[field_name]
        if isinstance(config, dict) and field_name in config:
            return config[field_name]
        return default
    if hasattr(value, field_name):
        field_value = getattr(value, field_name)
        if field_value not in (None, "", [], {}):
            return field_value
    config = getattr(value, "config", None)
    if isinstance(config, dict) and field_name in config:
        return config[field_name]
    return default


def _normalize_runs_on(value: Any) -> str:
    text = str(value or "").strip()
    if text == "peer":
        return "local_peer"
    if text in RUNS_ON_VALUES:
        return text
    return "agent_only"


def _runs_on_from_placement(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "peer":
        return "local_peer"
    if text == "both":
        return "both"
    return "server"


def _targets_for_runs_on(runs_on: str) -> list[str]:
    if runs_on == "server":
        return ["server"]
    if runs_on == "local_peer":
        return ["local_peer"]
    if runs_on == "both":
        return ["server", "local_peer"]
    return []


def _runs_on_from_targets(targets: list[str]) -> str:
    normalized = set(targets)
    if normalized == {"server", "local_peer"}:
        return "both"
    if normalized == {"server"}:
        return "server"
    if normalized == {"local_peer"}:
        return "local_peer"
    return "agent_only"


def _normalize_targets(value: Any, *, default: list[str]) -> list[str]:
    raw_values = value if isinstance(value, list) else default
    seen: set[str] = set()
    result: list[str] = []
    for item in raw_values:
        text = str(item or "").strip()
        if text == "peer":
            text = "local_peer"
        if text not in TARGET_VALUES or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return [target for target in TARGET_ORDER if target in result]


def _user_message(runs_on: str) -> str:
    if runs_on == "both":
        return MESSAGE_BOTH
    if runs_on == "local_peer":
        return MESSAGE_LOCAL
    if runs_on == "server":
        return MESSAGE_SERVER
    return MESSAGE_AGENT_ONLY
