"""CLI approval interaction provider.

.. deprecated::
    Replaced by :func:`reuleauxcoder.interfaces.cli.approval_handler.make_cli_handler`
    + :class:`reuleauxcoder.domain.approval.SharedApprovalProvider`.
    This module will be removed once the new path is verified.
"""

from __future__ import annotations

from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)
from reuleauxcoder.interfaces.interactions import ReviewRequest, UIInteractor
from reuleauxcoder.interfaces.shared.approval_preview import build_preview_diff


class CLIApprovalProvider(ApprovalProvider):
    """Approval provider backed by the shared UIInteractor.

    .. deprecated::
        Use :class:`~reuleauxcoder.domain.approval.SharedApprovalProvider`
        with :func:`~reuleauxcoder.interfaces.cli.approval_handler.make_cli_handler`.
    """

    def __init__(self, ui_interactor: UIInteractor):
        self.ui_interactor = ui_interactor

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        sections: list[dict] = []
        diff_text = self._build_preview_diff(request)
        if diff_text is not None:
            sections.append(
                {
                    "id": "diff",
                    "title": "Proposed patch diff",
                    "kind": "diff",
                    "content": diff_text,
                }
            )
        elif request.tool_args:
            sections.append(
                {
                    "id": "args",
                    "title": "Arguments",
                    "kind": "json",
                    "content": request.tool_args,
                }
            )

        response = self.ui_interactor.review(
            ReviewRequest(
                title=f"Approval required: {request.tool_name}",
                summary=(
                    f"Tool '{request.tool_name}' from source '{request.tool_source}' requires approval."
                ),
                sections=sections,
                metadata={
                    "tool_name": request.tool_name,
                    "tool_source": request.tool_source,
                    "reason": request.reason,
                    **request.metadata,
                },
            )
        )
        if response.approved:
            return ApprovalDecision.allow_once(
                response.reason or "approved via UI interactor"
            )
        return ApprovalDecision.deny_once(response.reason or "denied via UI interactor")

    def _build_preview_diff(self, request: ApprovalRequest) -> str | None:
        return build_preview_diff(request)
