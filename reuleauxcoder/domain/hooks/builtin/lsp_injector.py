"""Inject cached LSP diagnostics into the next LLM request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint


@register_hook(HookPoint.BEFORE_LLM_REQUEST, priority=0)
@dataclass(slots=True)
class LspDiagnosticInjectorHook(TransformHook[BeforeLLMRequestContext]):
    """Add cached diagnostics as compact system context."""

    lsp_manager: "LspManager | None" = None

    def __init__(self, priority: int = 0):
        TransformHook.__init__(
            self,
            name="lsp_diagnostic_injector",
            priority=priority,
            extension_name="core",
        )
        self.lsp_manager = None

    @classmethod
    def create_from_config(cls, config: "Config") -> "LspDiagnosticInjectorHook":
        return cls(priority=0)

    def set_lsp_manager(self, manager: "LspManager | None") -> None:
        self.lsp_manager = manager

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        if self.lsp_manager is None:
            return context
        rendered = self.lsp_manager.render_cached_diagnostics()
        if not rendered:
            return context
        context.messages.append(
            {
                "role": "system",
                "content": "Current LSP diagnostics:\n" + rendered,
            }
        )
        return context
