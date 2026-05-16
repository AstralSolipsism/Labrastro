from pathlib import Path

from reuleauxcoder.extensions.lsp.tool_helpers import (
    build_request,
    execute_lsp_operation,
    format_response,
)


class FakeManager:
    def __init__(self, workspace_cwd: Path):
        self.workspace_cwd = workspace_cwd
        self.calls = []

    def request(self, method, file_path, params):
        self.calls.append((method, file_path, params))
        return [{"uri": params["textDocument"]["uri"], "range": {"start": {"line": 0, "character": 2}}}]


def test_build_request_converts_positions_to_zero_based(tmp_path: Path) -> None:
    params = build_request("goToDefinition", tmp_path, "main.py", 3, 5)

    assert params["position"] == {"line": 2, "character": 4}


def test_execute_lsp_operation_formats_locations(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)

    result = execute_lsp_operation(
        manager,
        operation="goToDefinition",
        file_path="main.py",
        line=1,
        character=1,
    )

    assert result == "main.py:1:3"
    assert manager.calls[0][0] == "textDocument/definition"


def test_format_response_flattens_document_symbols() -> None:
    response = format_response(
        "documentSymbol",
        [
            {
                "name": "Outer",
                "kind": 5,
                "selectionRange": {"start": {"line": 1, "character": 0}},
                "children": [
                    {
                        "name": "Inner",
                        "selectionRange": {"start": {"line": 2, "character": 4}},
                    }
                ],
            }
        ],
        Path.cwd(),
    )

    assert "Outer:2:1 kind=5" in response
    assert "Outer.Inner:3:5" in response
