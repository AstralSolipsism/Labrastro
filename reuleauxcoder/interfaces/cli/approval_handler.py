"""CLI approval handler — resolves approvals via terminal UI interactor.

This replaces the old ``CLIApprovalProvider`` class.  The handler is
injected into ``SharedApprovalProvider``, keeping the approval
infrastructure unified across CLI and TUI.
"""

from __future__ import annotations

from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalHandler,
    PendingApproval,
)
from reuleauxcoder.interfaces.shared.approval_preview import build_preview_diff
from reuleauxcoder.interfaces.interactions import ReviewRequest, UIInteractor


def make_cli_handler(ui_interactor: UIInteractor) -> ApprovalHandler:
    """Create a CLI approval handler backed by the terminal UI interactor.

    The returned handler resolves ``PendingApproval`` synchronously in
    the same thread — ``resolve()`` is called before
    ``SharedApprovalProvider`` reaches ``wait()``, so the ``Event`` is
    already set and ``wait()`` returns immediately (zero blocking).
    """

    def handle(pending: PendingApproval) -> None:
        req = pending.request

        # ── Build diff / args sections ──
        sections: list[dict] = []
        diff_text = build_preview_diff(req)
        if diff_text is not None:
            sections.append(
                {
                    "id": "diff",
                    "title": "Proposed patch diff",
                    "kind": "diff",
                    "content": diff_text,
                }
            )
        elif req.tool_args:
            sections.append(
                {
                    "id": "args",
                    "title": "Arguments",
                    "kind": "json",
                    "content": req.tool_args,
                }
            )

        # ── Blocking UI review (same thread — safe) ──
        response = ui_interactor.review(
            ReviewRequest(
                title=f"Approval required: {req.tool_name}",
                summary=(
                    f"Tool '{req.tool_name}' from source '{req.tool_source}'"
                    " requires approval."
                ),
                sections=sections,
                metadata={
                    "tool_name": req.tool_name,
                    "tool_source": req.tool_source,
                    "reason": req.reason,
                    **req.metadata,
                },
            )
        )

        if response.approved:
            pending.resolve(
                ApprovalDecision.allow_once(response.reason or "approved via CLI")
            )
        else:
            pending.resolve(
                ApprovalDecision.deny_once(response.reason or "denied via CLI")
            )

    return handle
