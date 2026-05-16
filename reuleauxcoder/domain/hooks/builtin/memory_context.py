"""Built-in hooks for ReuleauxCoder core private memory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from reuleauxcoder.domain.hooks.base import ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    HookPoint,
    SessionSaveContext,
)
from reuleauxcoder.domain.memory import (
    MemoryBackendUnavailable,
    MemoryBundle,
    MemoryCaptureEvent,
    MemoryProvider,
    MemoryProvideRequest,
    MemoryScope,
    PostgresMemoryRepository,
    SQLiteMemoryRepository,
)

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config


def _last_user_query(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        if "<system_context>" in content:
            continue
        return content
    return ""


def _render_bundle(bundle: MemoryBundle) -> str:
    lines = [
        "## Private Agent Memory",
        "These are private memories for this agent only. Treat them as helpful context, not shared project state.",
    ]
    for item in bundle.items:
        title = item.abstract or item.type
        lines.append(f"- [{item.type}] {title}: {item.content}")
    return "\n".join(lines)


def _memory_item_event_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "abstract": item.abstract,
        "content": item.content,
        "confidence": item.confidence,
        "version": item.version,
        "updated_at": item.updated_at,
    }


def _memory_context_event_payload(
    *,
    bundle: MemoryBundle,
    request: MemoryProvideRequest,
    rendered_context: str,
    context: BeforeLLMRequestContext,
) -> dict[str, Any]:
    return {
        "schema": "memory_context.v1",
        "context_kind": "memory_injection",
        "status": "provided",
        "round_index": context.metadata.get("round_index"),
        "scope": bundle.scope.to_dict(),
        "scope_version": bundle.provenance.get("scope_version", 0),
        "query": request.query,
        "provided_items": len(bundle.items),
        "token_estimate": bundle.token_estimate,
        "items": [_memory_item_event_payload(item) for item in bundle.items],
        "rendered_context": rendered_context,
    }


def _emit_memory_context_event(
    context: BeforeLLMRequestContext, payload: dict[str, Any]
) -> None:
    if context.ui_bus is None:
        return
    try:
        from reuleauxcoder.interfaces.events import UIEventKind

        context.ui_bus.info(
            "Injected private memory context.",
            kind=UIEventKind.CONTEXT,
            **payload,
        )
    except Exception:
        pass


def _insert_after_system_messages(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        index += 1
    messages.insert(index, message)


def _provider_from_config(config: "Config") -> MemoryProvider:
    memory_config = getattr(config, "memory", None)
    backend = str(getattr(memory_config, "backend", "sqlite") or "sqlite")
    if backend == "postgres":
        database_url = str(getattr(getattr(config, "persistence", None), "database_url", "") or "")
        if not database_url:
            return _UnavailableMemoryProvider("memory.backend=postgres requires persistence.database_url")
        from labrastro_server.infrastructure.persistence.db import create_postgres_engine

        return MemoryProvider(PostgresMemoryRepository(create_postgres_engine(database_url)))
    store_path = getattr(memory_config, "store_path", ".rcoder/memory.sqlite3")
    path = Path(str(store_path or ".rcoder/memory.sqlite3"))
    if not path.is_absolute():
        path = Path.cwd() / path
    return MemoryProvider(SQLiteMemoryRepository(path))


class _UnavailableMemoryProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def provide(self, scope: MemoryScope, request: MemoryProvideRequest) -> MemoryBundle:
        raise MemoryBackendUnavailable(self.reason)

    def capture(self, scope: MemoryScope, event: MemoryCaptureEvent) -> None:
        raise MemoryBackendUnavailable(self.reason)


@register_hook(HookPoint.BEFORE_LLM_REQUEST, priority=40)
class MemoryContextHook(TransformHook[BeforeLLMRequestContext]):
    """Inject private agent-scoped memory into core LLM requests."""

    def __init__(
        self,
        *,
        provider: Any,
        enabled: bool = True,
        default_agent_id: str = "",
        default_namespace: str = "",
        token_budget: int = 800,
        priority: int = 40,
    ) -> None:
        super().__init__(name="memory_context", priority=priority, extension_name="core")
        self.provider = provider
        self.enabled = enabled
        self.default_agent_id = default_agent_id
        self.default_namespace = default_namespace
        self.token_budget = token_budget

    @classmethod
    def create_from_config(cls, config: "Config") -> "MemoryContextHook":
        memory_config = getattr(config, "memory", None)
        enabled = bool(getattr(memory_config, "enabled", True))
        return cls(
            provider=_provider_from_config(config),
            enabled=enabled,
            default_agent_id=str(getattr(memory_config, "default_agent_id", "core") or "core"),
            default_namespace=str(getattr(memory_config, "default_namespace", "") or ""),
            token_budget=int(getattr(memory_config, "token_budget", 800) or 800),
            priority=40,
        )

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        if not self.enabled:
            return context
        scope = MemoryScope.from_metadata(
            context.metadata,
            default_agent_id=self.default_agent_id,
            default_namespace=self.default_namespace,
        )
        request = MemoryProvideRequest(
            query=_last_user_query(context.messages),
            token_budget=self.token_budget,
        )
        try:
            bundle = self.provider.provide(scope, request)
        except MemoryBackendUnavailable as exc:
            context.metadata["memory"] = {
                "status": "unavailable",
                "warning": str(exc),
                "owner_agent_id": scope.owner_agent_id,
                "memory_namespace": scope.memory_namespace,
            }
            return context
        if not bundle.items:
            context.metadata["memory"] = {
                "status": "empty",
                "provided_items": 0,
                "owner_agent_id": scope.owner_agent_id,
                "memory_namespace": scope.memory_namespace,
                "scope_version": bundle.provenance.get("scope_version", 0),
            }
            return context
        rendered_context = _render_bundle(bundle)
        _insert_after_system_messages(
            context.messages,
            {"role": "system", "content": rendered_context},
        )
        context.metadata["memory"] = {
            "status": "provided",
            "provided_items": len(bundle.items),
            "owner_agent_id": scope.owner_agent_id,
            "memory_namespace": scope.memory_namespace,
            "scope_version": bundle.provenance.get("scope_version", 0),
            "token_estimate": bundle.token_estimate,
        }
        _emit_memory_context_event(
            context,
            _memory_context_event_payload(
                bundle=bundle,
                request=request,
                rendered_context=rendered_context,
                context=context,
            ),
        )
        return context


@register_hook(HookPoint.SESSION_SAVE, priority=0)
class MemorySessionSaveHook(ObserverHook[SessionSaveContext]):
    """Enqueue saved sessions for async private memory extraction."""

    def __init__(
        self,
        *,
        provider: Any,
        enabled: bool = True,
        capture_enabled: bool = True,
        default_agent_id: str = "",
        default_namespace: str = "",
        priority: int = 0,
    ) -> None:
        super().__init__(name="memory_session_capture", priority=priority, extension_name="core")
        self.provider = provider
        self.enabled = enabled
        self.capture_enabled = capture_enabled
        self.default_agent_id = default_agent_id
        self.default_namespace = default_namespace

    @classmethod
    def create_from_config(cls, config: "Config") -> "MemorySessionSaveHook":
        memory_config = getattr(config, "memory", None)
        return cls(
            provider=_provider_from_config(config),
            enabled=bool(getattr(memory_config, "enabled", True)),
            capture_enabled=bool(getattr(memory_config, "capture_enabled", True)),
            default_agent_id=str(getattr(memory_config, "default_agent_id", "core") or "core"),
            default_namespace=str(getattr(memory_config, "default_namespace", "") or ""),
        )

    def run(self, context: SessionSaveContext) -> None:
        if not self.enabled or not self.capture_enabled:
            return
        metadata = dict(context.metadata or {})
        metadata.update(dict(context.session_data.get("memory_scope") or {}))
        scope = MemoryScope.from_metadata(
            metadata,
            default_agent_id=self.default_agent_id,
            default_namespace=self.default_namespace,
        )
        session_id = context.session_id or context.session_data.get("session_id") or ""
        self.provider.capture(
            scope,
            MemoryCaptureEvent(
                kind="session_save",
                payload=dict(context.session_data),
                idempotency_key=(
                    f"session_save:{scope.owner_agent_id}:{scope.memory_namespace}:{session_id}"
                    if session_id
                    else None
                ),
            ),
        )


@register_hook(HookPoint.AFTER_TOOL_EXECUTE, priority=0)
class MemoryToolCaptureHook(ObserverHook[AfterToolExecuteContext]):
    """Enqueue tool outcomes as scoped capture events."""

    def __init__(
        self,
        *,
        provider: Any,
        enabled: bool = True,
        capture_enabled: bool = True,
        default_agent_id: str = "",
        default_namespace: str = "",
        priority: int = 0,
    ) -> None:
        super().__init__(name="memory_tool_capture", priority=priority, extension_name="core")
        self.provider = provider
        self.enabled = enabled
        self.capture_enabled = capture_enabled
        self.default_agent_id = default_agent_id
        self.default_namespace = default_namespace

    @classmethod
    def create_from_config(cls, config: "Config") -> "MemoryToolCaptureHook":
        memory_config = getattr(config, "memory", None)
        return cls(
            provider=_provider_from_config(config),
            enabled=bool(getattr(memory_config, "enabled", True)),
            capture_enabled=bool(getattr(memory_config, "capture_enabled", True)),
            default_agent_id=str(getattr(memory_config, "default_agent_id", "core") or "core"),
            default_namespace=str(getattr(memory_config, "default_namespace", "") or ""),
        )

    def run(self, context: AfterToolExecuteContext) -> None:
        if not self.enabled or not self.capture_enabled:
            return
        scope = MemoryScope.from_metadata(
            context.metadata,
            default_agent_id=self.default_agent_id,
            default_namespace=self.default_namespace,
        )
        tool_call = context.tool_call
        tool_call_id = getattr(tool_call, "id", "") if tool_call is not None else ""
        self.provider.capture(
            scope,
            MemoryCaptureEvent(
                kind="tool_result",
                payload={
                    "session_id": context.session_id,
                    "round_index": context.round_index,
                    "tool_call": {
                        "id": tool_call_id,
                        "name": getattr(tool_call, "name", "") if tool_call else "",
                        "arguments": getattr(tool_call, "arguments", {}) if tool_call else {},
                    },
                    "result": context.result,
                },
                idempotency_key=(
                    f"tool_result:{scope.owner_agent_id}:{scope.memory_namespace}:{context.session_id}:{tool_call_id}"
                    if context.session_id and tool_call_id
                    else None
                ),
            ),
        )
