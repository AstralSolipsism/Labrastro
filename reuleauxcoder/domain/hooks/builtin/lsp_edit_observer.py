"""Inject fresh LSP diagnostics after local patch mutations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.lifecycle import build_lifecycle_event_context
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.extensions.lsp.diagnostics import render_blocks


@register_hook(HookPoint.AFTER_TOOL_EXECUTE, priority=-10)
@dataclass(slots=True)
class LspEditObserverHook(TransformHook[AfterToolExecuteContext]):
    """Append diagnostics to successful local patch tool results."""

    lsp_manager: "LspManager | None" = None
    lifecycle_dispatcher: Any | None = None

    def __init__(self, priority: int = -10):
        TransformHook.__init__(
            self,
            name="lsp_edit_observer",
            priority=priority,
            extension_name="core",
        )
        self.lsp_manager = None
        self.lifecycle_dispatcher = None

    @classmethod
    def create_from_config(cls, config: "Config") -> "LspEditObserverHook":
        return cls(priority=-10)

    def set_lsp_manager(self, manager: "LspManager | None") -> None:
        self.lsp_manager = manager

    def set_lifecycle_dispatcher(self, dispatcher: Any | None) -> None:
        self.lifecycle_dispatcher = dispatcher

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        if self.lsp_manager is None or context.tool_call is None:
            return context
        if context.metadata.get("execution_target") == "remote_peer":
            return context
        if context.tool_call.name != "apply_patch":
            return context
        if context.result.startswith("Error:"):
            return context

        file_paths = _patch_file_paths(context.tool_call.arguments.get("patch"))
        if not file_paths:
            return context
        blocks = []
        for file_path in file_paths:
            try:
                block = self.lsp_manager.notify_file_changed(file_path)
            except Exception:
                continue
            self._dispatch_file_changed_lifecycle(context, file_path, block)
            if block is not None:
                blocks.append(block)
        if not blocks:
            return context
        rendered = render_blocks(
            blocks,
            max_diagnostics=self.lsp_manager.config.max_diagnostics,
            include_warnings=self.lsp_manager.config.include_warnings,
        )
        if rendered:
            context.result = f"{context.result}\n\n{rendered}"
        return context

    def _dispatch_file_changed_lifecycle(
        self,
        context: AfterToolExecuteContext,
        file_path: str,
        block: Any | None,
    ) -> None:
        dispatcher = self.lifecycle_dispatcher
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return
        metadata = dict(context.metadata or {})
        tool_call = context.tool_call
        diagnostic_count = len(getattr(block, "items", []) or []) if block is not None else 0
        execution_target = str(metadata.get("execution_target") or "local")
        payload = {
            "file_path": file_path,
            "watcher": "lsp",
            "tool_names": [str(getattr(tool_call, "name", "") or "")],
            "tool_call_ids": [str(getattr(tool_call, "id", "") or "")],
            "execution_target": execution_target,
            "runtime_working_directory": str(
                metadata.get("runtime_working_directory") or ""
            ),
            "runtime_workspace_root": str(
                metadata.get("runtime_workspace_root") or ""
            ),
            "path_space": (
                "local_workspace"
                if execution_target == "local"
                else str(metadata.get("path_space") or execution_target)
            ),
            "diagnostic_count": diagnostic_count,
        }
        lifecycle_context = build_lifecycle_event_context(
            "FileChanged",
            placement="server",
            trigger_source=str(metadata.get("trigger_source") or "tool"),
            session_run_id=str(context.session_id or ""),
            agent_run_id=str(metadata.get("agent_run_id") or ""),
            turn_id=str(metadata.get("turn_id") or ""),
            origin="agent",
            metadata={
                "tool_source": str(metadata.get("tool_source") or "builtin"),
                "mcp_server": str(metadata.get("mcp_server") or ""),
            },
            payload=payload,
        )
        try:
            dispatch(lifecycle_context)
        except Exception:
            return


def _patch_file_paths(patch: Any) -> list[str]:
    if not isinstance(patch, str) or not patch.strip():
        return []
    paths: list[str] = []
    pending_update: str | None = None
    for line in patch.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("*** Add File: "):
            pending_update = None
            _append_unique(paths, line[len("*** Add File: ") :].strip())
            continue
        if line.startswith("*** Delete File: "):
            pending_update = None
            _append_unique(paths, line[len("*** Delete File: ") :].strip())
            continue
        if line.startswith("*** Update File: "):
            pending_update = line[len("*** Update File: ") :].strip()
            _append_unique(paths, pending_update)
            continue
        if line.startswith("*** Move to: "):
            _append_unique(paths, line[len("*** Move to: ") :].strip())
            pending_update = None
            continue
        if line.startswith("*** "):
            pending_update = None
    if pending_update:
        _append_unique(paths, pending_update)
    return paths


def _append_unique(paths: list[str], value: str) -> None:
    if value and value not in paths:
        paths.append(value)
