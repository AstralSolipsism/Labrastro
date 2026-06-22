"""UI event bus and notification models for interface-layer output."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType


class UIEventLevel(Enum):
    """Visual severity / style for UI events."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


class UIEventKind(Enum):
    """Logical kind for interface-layer events."""

    SYSTEM = "system"
    COMMAND = "command"
    SESSION = "session"
    MODEL = "model"
    MCP = "mcp"
    APPROVAL = "approval"
    VIEW = "view"
    AGENT = "agent"
    CONTEXT = "context"
    REMOTE = "remote"


@dataclass
class UIEvent:
    """A user-facing event emitted through the UI bus."""

    message: str
    level: UIEventLevel = UIEventLevel.INFO
    kind: UIEventKind = UIEventKind.SYSTEM
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def info(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.INFO, kind=kind, data=data)

    @classmethod
    def success(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.SUCCESS, kind=kind, data=data)

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.WARNING, kind=kind, data=data)

    @classmethod
    def error(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.ERROR, kind=kind, data=data)

    @classmethod
    def debug(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.DEBUG, kind=kind, data=data)


class UIEventBus:
    """Publish/subscribe bus for UI events.

    Two delivery modes:

    * **Synchronous** (default) — ``emit()`` calls every handler immediately
      on the calling thread.  Used by CLI (single-thread).
    * **Queued** — pass a ``queue.Queue`` at construction time.  ``emit()``
      pushes events onto the queue; the UI thread must periodically call
      ``drain()`` to dispatch them.  Used by TUI (cross-thread).

    Handlers are always called on the **draining thread** — never on the
    emitting thread when queued.
    """

    def __init__(
        self,
        *,
        event_queue: queue.Queue | None = None,
        lifecycle_dispatcher: Any | None = None,
    ):
        self._queue = event_queue
        self._handlers: list[Callable[[UIEvent], None]] = []
        self._history: list[UIEvent] = []
        self._lifecycle_dispatcher = lifecycle_dispatcher

    @property
    def is_queued(self) -> bool:
        """True when this bus uses cross-thread queued delivery."""
        return self._queue is not None

    def subscribe(
        self,
        handler: Callable[[UIEvent], None],
        *,
        replay_history: bool = True,
    ) -> None:
        self._handlers.append(handler)
        if replay_history:
            for event in self._history:
                try:
                    handler(event)
                except Exception:
                    pass

    def set_lifecycle_dispatcher(self, dispatcher: Any | None) -> None:
        self._lifecycle_dispatcher = dispatcher

    def emit(self, event: UIEvent) -> None:
        self._publish(event)
        self._dispatch_notification_lifecycle(event)

    def _publish(self, event: UIEvent) -> None:
        self._history.append(event)
        if self._queue is not None:
            self._queue.put(event)
        else:
            self._dispatch(event)

    def _dispatch_notification_lifecycle(self, event: UIEvent) -> None:
        if self._notification_lifecycle_skip(event):
            return
        dispatcher = self._lifecycle_dispatcher
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return
        try:
            from reuleauxcoder.domain.hooks.lifecycle import (
                build_lifecycle_event_context,
            )

            context = build_lifecycle_event_context(
                "Notification",
                placement="server",
                trigger_source=str(event.data.get("trigger_source") or event.kind.value),
                session_run_id=str(event.data.get("session_run_id") or ""),
                agent_run_id=str(event.data.get("agent_run_id") or ""),
                turn_id=str(event.data.get("turn_id") or ""),
                origin="ui",
                metadata={
                    "ui_event_kind": event.kind.value,
                    "ui_event_level": event.level.value,
                },
                payload={
                    "message": event.message,
                    "level": event.level.value,
                    "kind": event.kind.value,
                    **dict(event.data),
                },
            )
            results = dispatch(context)
        except Exception:
            return
        for result in results or []:
            audit = self._notification_lifecycle_audit_event(event, context, result)
            if audit is not None:
                self._publish(audit)

    @staticmethod
    def _notification_lifecycle_skip(event: UIEvent) -> bool:
        data = event.data if isinstance(event.data, dict) else {}
        if data.get("event_type") == "lifecycle_hook":
            return True
        if data.get("agent_event") is not None:
            return True
        if data.get("event_type"):
            return True
        if event.kind is UIEventKind.VIEW:
            return True
        return False

    @staticmethod
    def _notification_lifecycle_audit_event(
        event: UIEvent,
        context: Any,
        result: Any,
    ) -> UIEvent | None:
        declaration = getattr(result, "declaration", None)
        output = getattr(result, "output", None)
        if output is not None:
            try:
                from reuleauxcoder.domain.hooks.lifecycle import (
                    annotate_lifecycle_output_diagnostics,
                )

                annotate_lifecycle_output_diagnostics("Notification", output)
            except Exception:
                pass
        output_dict = output.to_dict() if hasattr(output, "to_dict") else {}
        if not isinstance(output_dict, dict):
            output_dict = {}
        diagnostics = output_dict.get("diagnostics")
        if not isinstance(diagnostics, list):
            diagnostics = []
        decision = str(output_dict.get("decision") or "none")
        continue_flow = output_dict.get("continue_flow", True)
        if not isinstance(continue_flow, bool):
            continue_flow = True
        message = (
            str(output_dict.get("user_message") or "").strip()
            or str(output_dict.get("reason") or "").strip()
            or f"Notification observed: {event.message}"
        )
        level = event.level
        if diagnostics and level is UIEventLevel.INFO:
            level = UIEventLevel.WARNING
        payload = {
            "event_type": "lifecycle_hook",
            "phase": "result",
            "event_name": "Notification",
            "placement": str(getattr(context, "placement", "") or "server"),
            "session_run_id": str(getattr(context, "session_run_id", "") or ""),
            "agent_run_id": str(getattr(context, "agent_run_id", "") or ""),
            "turn_id": str(getattr(context, "turn_id", "") or ""),
            "trigger_source": str(getattr(context, "source", "") or ""),
            "hook_id": str(getattr(declaration, "id", "") or ""),
            "display_name": str(getattr(declaration, "display_name", "") or ""),
            "source": str(getattr(declaration, "source", "") or ""),
            "decision": decision,
            "continue_flow": continue_flow,
            "diagnostics": diagnostics,
            "level": level.value,
            "title": str(getattr(declaration, "display_name", "") or "Notification"),
            "message": message,
            "payload": dict(getattr(context, "payload", {}) or {}),
            "output": output_dict,
        }
        return UIEvent(
            message=message,
            level=level,
            kind=UIEventKind.AGENT,
            data=payload,
        )

    def emit_lifecycle_hook_audit_events(self, result: Any) -> None:
        """Publish structured lifecycle audit for internally bridged hook points."""

        context = getattr(result, "lifecycle_context", None)
        if context is None:
            return
        for dispatch_result in getattr(result, "dispatch_results", []) or []:
            audit = self._lifecycle_hook_audit_event(context, dispatch_result)
            if audit is not None:
                self.emit(audit)

    @staticmethod
    def _lifecycle_hook_audit_event(context: Any, result: Any) -> UIEvent | None:
        declaration = getattr(result, "declaration", None)
        output = getattr(result, "output", None)
        output_dict = output.to_dict() if hasattr(output, "to_dict") else {}
        if not isinstance(output_dict, dict):
            output_dict = {}
        diagnostics = output_dict.get("diagnostics")
        if not isinstance(diagnostics, list):
            diagnostics = []
        decision = str(output_dict.get("decision") or "none")
        continue_flow = output_dict.get("continue_flow", True)
        if not isinstance(continue_flow, bool):
            continue_flow = True
        event_name = str(getattr(context, "event_name", "") or "")
        display_name = str(getattr(declaration, "display_name", "") or event_name)
        message = (
            str(output_dict.get("user_message") or "").strip()
            or str(output_dict.get("reason") or "").strip()
            or f"{display_name} observed {event_name}"
        )
        level = UIEventLevel.WARNING if diagnostics else UIEventLevel.INFO
        payload = {
            "event_type": "lifecycle_hook",
            "phase": "result",
            "event_name": event_name,
            "placement": str(getattr(context, "placement", "") or "server"),
            "session_run_id": str(getattr(context, "session_run_id", "") or ""),
            "agent_run_id": str(getattr(context, "agent_run_id", "") or ""),
            "turn_id": str(getattr(context, "turn_id", "") or ""),
            "trigger_source": str(getattr(context, "source", "") or ""),
            "origin": str(getattr(context, "origin", "") or ""),
            "hook_id": str(getattr(declaration, "id", "") or ""),
            "display_name": display_name,
            "source": str(getattr(declaration, "source", "") or ""),
            "handler_type": str(getattr(declaration, "handler_type", "") or ""),
            "decision": decision,
            "continue_flow": continue_flow,
            "diagnostics": diagnostics,
            "level": level.value,
            "title": display_name,
            "message": message,
            "payload": _safe_lifecycle_context_payload(context),
            "output": output_dict,
        }
        return UIEvent(
            message=message,
            level=level,
            kind=UIEventKind.AGENT,
            data=payload,
        )

    def drain(self) -> None:
        """Dequeue and dispatch all pending events (queued mode only).

        Call periodically from the UI main thread (e.g. via
        ``set_interval``).  No-op in synchronous mode.
        """
        if self._queue is None:
            return
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            self._dispatch(event)

    def _dispatch(self, event: UIEvent) -> None:
        """Call every registered handler for *event*."""
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                pass

    def info(
        self, message: str, *, kind: UIEventKind = UIEventKind.SYSTEM, **data: Any
    ) -> None:
        self.emit(UIEvent.info(message, kind=kind, **data))

    def success(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.success(message, kind=kind, **data))

    def warning(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.warning(message, kind=kind, **data))

    def error(
        self, message: str, *, kind: UIEventKind = UIEventKind.SYSTEM, **data: Any
    ) -> None:
        self.emit(UIEvent.error(message, kind=kind, **data))

    def debug(
        self, message: str, *, kind: UIEventKind = UIEventKind.SYSTEM, **data: Any
    ) -> None:
        self.emit(UIEvent.debug(message, kind=kind, **data))

    def open_view(
        self,
        view_type: str,
        *,
        title: str,
        payload: dict[str, Any] | None = None,
        focus: bool = True,
        reuse_key: str | None = None,
    ) -> None:
        """Broadcast a structured request for the UI to open a view/panel/tab."""
        self.emit(
            UIEvent.info(
                f"Open view: {title}",
                kind=UIEventKind.VIEW,
                action="open",
                view_type=view_type,
                title=title,
                payload=payload or {},
                focus=focus,
                reuse_key=reuse_key,
            )
        )

    def refresh_view(
        self,
        view_type: str,
        *,
        title: str | None = None,
        payload: dict[str, Any] | None = None,
        reuse_key: str | None = None,
    ) -> None:
        """Broadcast a structured request for the UI to refresh a view."""
        self.emit(
            UIEvent.info(
                f"Refresh view: {title or view_type}",
                kind=UIEventKind.VIEW,
                action="refresh",
                view_type=view_type,
                title=title or view_type,
                payload=payload or {},
                reuse_key=reuse_key,
            )
        )


def _safe_lifecycle_context_payload(context: Any) -> dict[str, Any]:
    payload = dict(getattr(context, "payload", {}) or {})
    technical = payload.get("technical")
    if isinstance(technical, dict):
        safe_technical = {
            key: value
            for key, value in technical.items()
            if key != "legacy_context"
        }
        if safe_technical:
            payload["technical"] = safe_technical
        else:
            payload.pop("technical", None)
    return payload


class AgentEventBridge:
    """Republish domain-level agent events onto the UI event bus."""

    def __init__(self, bus: UIEventBus):
        self.bus = bus

    def on_agent_event(self, event: AgentEvent) -> None:
        """Translate an agent event into a UI event envelope."""
        level = UIEventLevel.INFO
        if event.event_type in (
            AgentEventType.ERROR,
            AgentEventType.TOOL_CALL_PROTOCOL_ERROR,
        ):
            level = UIEventLevel.ERROR
        elif event.event_type in (
            AgentEventType.TOOL_CALL_DELTA,
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
            AgentEventType.FILE_CHANGE_STARTED,
            AgentEventType.FILE_CHANGE_PATCH_UPDATED,
            AgentEventType.FILE_CHANGE_APPROVAL_REQUESTED,
            AgentEventType.FILE_CHANGE_APPROVAL_RESOLVED,
            AgentEventType.FILE_CHANGE_COMPLETED,
            AgentEventType.DOCUMENT_DRAFT_STARTED,
            AgentEventType.DOCUMENT_DRAFT_PREVIEW_CHUNK,
            AgentEventType.DOCUMENT_DRAFT_PROGRESS,
            AgentEventType.DOCUMENT_DRAFT_SNAPSHOT,
            AgentEventType.DOCUMENT_DRAFT_COMMIT_REQUESTED,
            AgentEventType.DOCUMENT_DRAFT_COMMITTED,
            AgentEventType.DOCUMENT_DRAFT_FAILED,
            AgentEventType.DOCUMENT_DRAFT_CANCELLED,
        ):
            level = UIEventLevel.DEBUG

        self.bus.emit(
            UIEvent(
                message=event.event_type.value,
                level=level,
                kind=UIEventKind.AGENT,
                data={
                    "agent_event": event,
                    "event_type": event.event_type.value,
                    "tool_name": event.tool_name,
                    "tool_args": event.tool_args,
                    "tool_result": event.tool_result,
                    "error_message": event.error_message,
                    "tool_call_id": event.tool_call_id,
                    "code": event.data.get("code"),
                },
            )
        )
