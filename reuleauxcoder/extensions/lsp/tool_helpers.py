"""Helpers for the active read-only LSP tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from reuleauxcoder.extensions.lsp.manager import LspError, LspManager


_OPERATIONS = {
    "goToDefinition": "textDocument/definition",
    "findReferences": "textDocument/references",
    "documentSymbol": "textDocument/documentSymbol",
}


def execute_lsp_operation(
    manager: LspManager | None,
    *,
    operation: str,
    file_path: str,
    line: int | None = None,
    character: int | None = None,
) -> str:
    """Execute a supported LSP operation and return text for the model."""
    if manager is None:
        return "Error: LSP manager is not available"
    if operation not in _OPERATIONS:
        return (
            "Error: unsupported LSP operation. "
            "Use one of: goToDefinition, findReferences, documentSymbol"
        )
    if not isinstance(file_path, str) or not file_path.strip():
        return "Error: filePath must be a non-empty string"

    try:
        request = build_request(operation, manager.workspace_cwd, file_path, line, character)
        result = manager.request(_OPERATIONS[operation], file_path, request)
        return format_response(operation, result, manager.workspace_cwd)
    except LspError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error executing LSP operation: {exc}"


def build_request(
    operation: str,
    workspace_cwd: Path,
    file_path: str,
    line: int | None,
    character: int | None,
) -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_absolute():
        path = workspace_cwd / path
    document = {"uri": path.resolve().as_uri()}
    if operation == "documentSymbol":
        return {"textDocument": document}
    if line is None or character is None:
        raise LspError(f"{operation} requires line and character")
    if line < 1 or character < 1:
        raise LspError("line and character are 1-based positive integers")
    params: dict[str, Any] = {
        "textDocument": document,
        "position": {"line": line - 1, "character": character - 1},
    }
    if operation == "findReferences":
        params["context"] = {"includeDeclaration": True}
    return params


def format_response(operation: str, result: Any, workspace_cwd: Path) -> str:
    if result is None or result == []:
        return "No LSP results."
    if operation in {"goToDefinition", "findReferences"}:
        locations = result if isinstance(result, list) else [result]
        lines = [_format_location(item, workspace_cwd) for item in locations if isinstance(item, dict)]
        return "\n".join(line for line in lines if line) or "No LSP results."
    if operation == "documentSymbol":
        symbols = _flatten_symbols(result if isinstance(result, list) else [])
        return "\n".join(symbols) if symbols else "No document symbols."
    return str(result)


def _format_location(item: dict[str, Any], workspace_cwd: Path) -> str:
    uri = str(item.get("uri") or item.get("targetUri") or "")
    raw_range = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange") or {}
    start = raw_range.get("start") or {}
    path = _uri_to_display_path(uri, workspace_cwd)
    return f"{path}:{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1}"


def _flatten_symbols(items: list[Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        kind = item.get("kind")
        raw_range = (item.get("selectionRange") or item.get("range") or {}).get("start") or {}
        label = f"{prefix}{name}"
        suffix = f" kind={kind}" if kind is not None else ""
        lines.append(
            f"{label}:{int(raw_range.get('line', 0)) + 1}:{int(raw_range.get('character', 0)) + 1}{suffix}"
        )
        children = item.get("children")
        if isinstance(children, list):
            lines.extend(_flatten_symbols(children, prefix=f"{prefix}{name}."))
    return lines


def _uri_to_display_path(uri: str, workspace_cwd: Path) -> str:
    if not uri:
        return "<unknown>"
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    raw_path = unquote(parsed.path)
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    path = Path(raw_path)
    try:
        return path.resolve().relative_to(workspace_cwd).as_posix()
    except Exception:
        return path.as_posix()
