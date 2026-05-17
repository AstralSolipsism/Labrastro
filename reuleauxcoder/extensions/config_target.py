"""Resolve the authoritative config target for admin CLI commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def resolve_cli_config_path(
    args: Any,
    *,
    require: bool,
    purpose: str,
) -> Path | None:
    """Prefer --config, then RCODER_CONFIG_PATH, and optionally require one."""
    raw = getattr(args, "config", None) or os.environ.get("RCODER_CONFIG_PATH")
    if raw:
        return Path(str(raw)).expanduser()
    if require:
        raise ValueError(
            f"{purpose} requires --config or RCODER_CONFIG_PATH; "
            "refusing to write ~/.rcoder/config.yaml implicitly"
        )
    return None

