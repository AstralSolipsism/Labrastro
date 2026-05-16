"""LSP configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config


@dataclass(slots=True)
class LspServerOverride:
    """Per-language language-server override."""

    language: str
    cmd: str | None = None
    args: list[str] | None = None
    workspace_root: str | None = None
    init_opts: dict[str, Any] | None = None


@dataclass(slots=True)
class LspConfig:
    """Parsed LSP config."""

    enabled: bool = True
    poll_timeout_ms: int = 5000
    max_diagnostics: int = 20
    include_warnings: bool = False
    server_overrides: dict[str, LspServerOverride] = field(default_factory=dict)

    def get_override(self, language_key: str) -> LspServerOverride | None:
        return self.server_overrides.get(language_key)

    @classmethod
    def from_config(cls, config: "Config") -> "LspConfig":
        raw = getattr(config, "lsp", None)
        if not isinstance(raw, dict):
            return cls()

        servers = raw.get("servers", {})
        overrides: dict[str, LspServerOverride] = {}
        if isinstance(servers, dict):
            for language, server in servers.items():
                if not isinstance(server, dict):
                    continue
                args = server.get("args")
                init_opts = server.get("init_opts")
                overrides[str(language)] = LspServerOverride(
                    language=str(language),
                    cmd=str(server["cmd"]) if server.get("cmd") else None,
                    args=[str(arg) for arg in args] if isinstance(args, list) else None,
                    workspace_root=(
                        str(server["workspace_root"])
                        if server.get("workspace_root") is not None
                        else None
                    ),
                    init_opts=init_opts if isinstance(init_opts, dict) else None,
                )

        return cls(
            enabled=bool(raw.get("enabled", True)),
            poll_timeout_ms=int(raw.get("poll_timeout_ms", 5000) or 5000),
            max_diagnostics=int(raw.get("max_diagnostics", 20) or 20),
            include_warnings=bool(raw.get("include_warnings", False)),
            server_overrides=overrides,
        )
