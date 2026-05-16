from pathlib import Path

from reuleauxcoder.extensions.tools.builtin.lsp import LspTool, set_lsp_manager


class FakeManager:
    def __init__(self, workspace_cwd: Path):
        self.workspace_cwd = workspace_cwd

    def request(self, method, file_path, params):
        return [{"uri": params["textDocument"]["uri"], "range": {"start": {"line": 0, "character": 0}}}]


def test_lsp_tool_uses_injected_manager(tmp_path: Path) -> None:
    set_lsp_manager(FakeManager(tmp_path))
    try:
        result = LspTool().execute(
            operation="goToDefinition",
            filePath="main.py",
            line=1,
            character=1,
        )
    finally:
        set_lsp_manager(None)

    assert result == "main.py:1:1"


def test_lsp_tool_reports_missing_manager() -> None:
    set_lsp_manager(None)

    result = LspTool().execute(operation="documentSymbol", filePath="main.py")

    assert "manager is not available" in result
