"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecToolRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30
    preview_identity: dict[str, Any] = field(default_factory=dict)
    approved_save_candidate: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    permission_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
            "preview_identity": dict(self.preview_identity or {}),
            "approved_save_candidate": dict(self.approved_save_candidate or {}),
            "tool_call_id": self.tool_call_id,
            "permission_context": dict(self.permission_context or {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolRequest":
        return cls(
            tool_name=d["tool_name"],
            args=d.get("args", {}),
            cwd=d.get("cwd"),
            timeout_sec=d.get("timeout_sec", 30),
            tool_call_id=d.get("tool_call_id"),
            preview_identity=d.get("preview_identity", {})
            if isinstance(d.get("preview_identity", {}), dict)
            else {},
            approved_save_candidate=d.get("approved_save_candidate", {})
            if isinstance(d.get("approved_save_candidate", {}), dict)
            else {},
            permission_context=d.get("permission_context", {})
            if isinstance(d.get("permission_context", {}), dict)
            else {},
        )


@dataclass
class ExecToolResult:
    ok: bool
    result: str = ""
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolResult":
        return cls(
            ok=d["ok"],
            result=d.get("result", ""),
            error_code=d.get("error_code"),
            error_message=d.get("error_message"),
            meta=d.get("meta", {}),
        )

@dataclass
class ToolPreviewRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolPreviewRequest":
        return cls(
            tool_name=d["tool_name"],
            args=d.get("args", {}),
            cwd=d.get("cwd"),
            timeout_sec=d.get("timeout_sec", 30),
        )

@dataclass
class ToolPreviewResult:
    ok: bool
    sections: list[dict[str, Any]] = field(default_factory=list)
    resolved_path: str | None = None
    diff: str = ""
    original_text: str | None = None
    modified_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sections": self.sections,
            "resolved_path": self.resolved_path,
            "diff": self.diff,
            "original_text": self.original_text,
            "modified_text": self.modified_text,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolPreviewResult":
        return cls(
            ok=bool(d.get("ok", False)),
            sections=[
                dict(item)
                for item in d.get("sections", [])
                if isinstance(item, dict)
            ],
            resolved_path=d.get("resolved_path"),
            diff=str(d.get("diff", "")),
            original_text=d.get("original_text"),
            modified_text=d.get("modified_text"),
            error_code=d.get("error_code"),
            error_message=d.get("error_message"),
            meta=d.get("meta", {}) if isinstance(d.get("meta", {}), dict) else {},
        )


# ---------------------------------------------------------------------------
# Stream chunk transport
# ---------------------------------------------------------------------------

@dataclass
class ToolStreamChunk:
    chunk_type: str  # "stdout" | "stderr" | "exit"
    data: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_type": self.chunk_type,
            "data": self.data,
            "meta": self.meta,
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolStreamChunk":
        return cls(
            chunk_type=d["chunk_type"],
            data=d.get("data", ""),
            meta=d.get("meta", {}) if isinstance(d.get("meta", {}), dict) else {},
            tool_call_id=d.get("tool_call_id"),
        )


# ---------------------------------------------------------------------------
# Disconnect / Cleanup
# ---------------------------------------------------------------------------

@dataclass
class CleanupRequest:
    pass

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupRequest":
        return cls()

@dataclass
class CleanupResult:
    ok: bool
    removed_items: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "removed_items": self.removed_items,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupResult":
        return cls(
            ok=d["ok"],
            removed_items=d.get("removed_items", []),
            error_message=d.get("error_message"),
        )


# ---------------------------------------------------------------------------
# Generic error
# ---------------------------------------------------------------------------

__all__ = [
    "ExecToolRequest",
    "ExecToolResult",
    "ToolPreviewRequest",
    "ToolPreviewResult",
    "ToolStreamChunk",
    "CleanupRequest",
    "CleanupResult",
]
