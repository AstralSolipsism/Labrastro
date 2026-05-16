"""Read-only LSP navigation tool."""

from __future__ import annotations

from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.tool_helpers import execute_lsp_operation
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool

_LSP_MANAGER: LspManager | None = None


def set_lsp_manager(manager: LspManager | None) -> None:
    global _LSP_MANAGER
    _LSP_MANAGER = manager


@register_tool
class LspTool(Tool):
    name = "lsp"
    description = (
        "Read-only language-server navigation. Use for goToDefinition, "
        "findReferences, and documentSymbol when LSP is available."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["goToDefinition", "findReferences", "documentSymbol"],
            },
            "filePath": {"type": "string"},
            "line": {
                "type": "integer",
                "description": "1-based line number; required except documentSymbol.",
            },
            "character": {
                "type": "integer",
                "description": "1-based character number; required except documentSymbol.",
            },
        },
        "required": ["operation", "filePath"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self,
        operation: str,
        filePath: str,
        line: int | None = None,
        character: int | None = None,
    ) -> str:
        return self.run_backend(
            operation=operation,
            filePath=filePath,
            line=line,
            character=character,
        )

    @backend_handler("remote_relay")
    def _execute_remote(
        self,
        operation: str,
        filePath: str,
        line: int | None = None,
        character: int | None = None,
    ) -> str:
        return self.backend.exec_tool(
            "lsp",
            {
                "operation": operation,
                "filePath": filePath,
                "line": line,
                "character": character,
            },
        )

    @backend_handler("local")
    def _execute_local(
        self,
        operation: str,
        filePath: str,
        line: int | None = None,
        character: int | None = None,
    ) -> str:
        return execute_lsp_operation(
            _LSP_MANAGER,
            operation=operation,
            file_path=filePath,
            line=line,
            character=character,
        )
