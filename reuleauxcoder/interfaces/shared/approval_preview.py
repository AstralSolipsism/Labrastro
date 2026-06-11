"""Shared diff-preview utilities for tool-approval requests."""

from __future__ import annotations

import os
from pathlib import Path

from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.files import LocalWorkspaceMutationBackend


def build_preview_diff(request: ApprovalRequest) -> str | None:
    """Build a unified-diff preview for ``apply_patch`` approval requests."""
    if request.tool_name != "apply_patch":
        return None

    patch = request.tool_args.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        return None

    workspace_root = (
        request.metadata.get("runtime_workspace_root")
        or request.metadata.get("workspace_root")
        or request.metadata.get("cwd")
        or os.getcwd()
    )
    try:
        result = LocalWorkspaceMutationBackend(Path(str(workspace_root))).preview_text_patch(
            patch
        )
    except Exception:
        return None
    if not result.diff:
        return None
    result_text = result.diff
    if len(result_text) > 3000:
        result_text = result_text[:2500] + "\n... (diff truncated)\n"
    return result_text or None
