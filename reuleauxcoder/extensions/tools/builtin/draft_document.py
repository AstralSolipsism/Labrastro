"""Declare a long document draft target without transferring document content."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
import uuid

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
class DraftDocumentBeginTool(Tool):
    name = "draft_document_begin"
    description = (
        "Declare that the next assistant markdown stream is a long document draft "
        "for a target workspace path. Do not pass document content as tool arguments."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target_path": {
                "type": "string",
                "description": "Workspace-relative markdown target path for the draft.",
            },
            "title": {
                "type": "string",
                "description": "Human-readable draft title.",
            },
            "format": {
                "type": "string",
                "enum": ["markdown"],
                "description": "Draft format. Only markdown is supported.",
            },
        },
        "required": ["target_path", "title", "format"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def preflight_validate(self, **kwargs) -> str | None:
        if "content" in kwargs:
            return "Error: draft_document_begin must not include content"
        target_path = kwargs.get("target_path")
        if not isinstance(target_path, str) or not target_path.strip():
            return "Error: draft_document_begin requires target_path"
        if _is_absolute_or_traversal(target_path):
            return "Error: target_path must be workspace-relative and cannot contain .."
        title = kwargs.get("title")
        if not isinstance(title, str) or not title.strip():
            return "Error: draft_document_begin requires title"
        if kwargs.get("format") != "markdown":
            return "Error: draft_document_begin format must be markdown"
        return None

    def execute(self, target_path: str, title: str, format: str = "markdown") -> str:
        validation_error = self.preflight_validate(
            target_path=target_path,
            title=title,
            format=format,
        )
        if validation_error:
            return validation_error
        return self.run_backend(target_path=target_path, title=title, format=format)

    @backend_handler("remote_relay")
    def _execute_remote(self, target_path: str, title: str, format: str = "markdown") -> str:
        validation_error = self.preflight_validate(
            target_path=target_path,
            title=title,
            format=format,
        )
        if validation_error:
            return validation_error
        return self._execute_local(target_path=target_path, title=title, format=format)

    @backend_handler("local")
    def _execute_local(self, target_path: str, title: str, format: str = "markdown") -> str:
        draft_id = f"draft-{uuid.uuid4().hex}"
        return (
            f"Draft document declared: {title}\n"
            f"draft_id: {draft_id}\n"
            f"target_path: {target_path}\n"
            "Continue the document body in assistant markdown stream."
        )


def _is_absolute_or_traversal(path: str) -> bool:
    value = path.strip()
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return True
    parts = set(PurePosixPath(value).parts) | set(PureWindowsPath(value).parts)
    return ".." in parts
