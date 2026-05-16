"""Inject fresh LSP diagnostics after local file edits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.extensions.lsp.diagnostics import render_blocks


@register_hook(HookPoint.AFTER_TOOL_EXECUTE, priority=-10)
@dataclass(slots=True)
class LspEditObserverHook(TransformHook[AfterToolExecuteContext]):
    """Append diagnostics to successful local edit/write tool results."""

    lsp_manager: "LspManager | None" = None

    def __init__(self, priority: int = -10):
        TransformHook.__init__(
            self,
            name="lsp_edit_observer",
            priority=priority,
            extension_name="core",
        )
        self.lsp_manager = None

    @classmethod
    def create_from_config(cls, config: "Config") -> "LspEditObserverHook":
        return cls(priority=-10)

    def set_lsp_manager(self, manager: "LspManager | None") -> None:
        self.lsp_manager = manager

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        if self.lsp_manager is None or context.tool_call is None:
            return context
        if context.metadata.get("execution_target") == "remote_peer":
            return context
        if context.tool_call.name not in {"write_file", "edit_file"}:
            return context
        if context.result.startswith("Error:"):
            return context

        file_path = context.tool_call.arguments.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return context
        try:
            block = self.lsp_manager.notify_file_changed(file_path)
        except Exception:
            return context
        if block is None:
            return context
        rendered = render_blocks(
            [block],
            max_diagnostics=self.lsp_manager.config.max_diagnostics,
            include_warnings=self.lsp_manager.config.include_warnings,
        )
        if rendered:
            context.result = f"{context.result}\n\n{rendered}"
        return context
