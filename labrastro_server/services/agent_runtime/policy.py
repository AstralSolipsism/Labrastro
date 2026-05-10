"""Agent runtime capability package and MCP policy helpers."""

from __future__ import annotations

from typing import Any


def _server_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        servers = value.get("servers", {})
        if isinstance(servers, dict):
            return [str(name) for name in servers.keys()]
        if isinstance(servers, list):
            return [str(name) for name in servers]
    if isinstance(value, list):
        return [str(name) for name in value]
    return []


class PlatformMCPPolicy:
    """Render MCP config from platform inventory and Agent allowlist."""

    def __init__(
        self,
        *,
        platform_servers: dict[str, dict[str, Any]],
        allowed_servers: list[str],
    ) -> None:
        self.platform_servers = dict(platform_servers)
        self.allowed_servers = [str(server) for server in allowed_servers]

    def render_for_agent(self, agent_mcp: dict[str, Any] | None) -> dict[str, Any]:
        requested = _server_names(agent_mcp or {})
        effective: dict[str, Any] = {}
        for server in requested:
            if server not in self.allowed_servers:
                continue
            if server not in self.platform_servers:
                continue
            effective[server] = dict(self.platform_servers[server])
        return {"servers": effective}


class CapabilityPackagePolicy:
    """Check whether an Agent may use a capability package grant."""

    def __init__(
        self,
        *,
        available_packages: list[str],
        granted_packages: list[str],
    ) -> None:
        self.available_packages = set(available_packages)
        self.granted_packages = set(granted_packages)

    def allows(self, package_id: str) -> bool:
        return package_id in self.available_packages and package_id in self.granted_packages

    def explain_denial(self, package_id: str) -> str:
        if package_id not in self.available_packages:
            return "capability package not available on platform"
        if package_id not in self.granted_packages:
            return "capability package not granted to agent"
        return "capability package allowed"
