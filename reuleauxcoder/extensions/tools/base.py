"""Base class and backend dispatch helpers for tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from reuleauxcoder.extensions.tools.spec import (
    ProviderSurface,
    ToolExecutionSpec,
    ToolExposure,
    ToolMutationSpec,
    ToolOutputStrategy,
    ToolPermissionSpec,
    ToolRisk,
    ToolSpec,
    build_tool_search_text,
)


BackendHandler = Callable[..., str]


def backend_handler(backend_id: str) -> Callable[[BackendHandler], BackendHandler]:
    """Mark a tool method as the implementation for a specific backend."""

    def decorator(func: BackendHandler) -> BackendHandler:
        setattr(func, "_tool_backend_id", backend_id)
        return func

    return decorator


class Tool(ABC):
    """Minimal tool interface with backend-aware dispatch helpers."""

    name: str
    description: str
    parameters: dict[str, Any]
    namespace: str = "builtin"
    output_schema: dict[str, Any] | None = None
    output_strategy: ToolOutputStrategy = ToolOutputStrategy.TEXT
    risk: ToolRisk = ToolRisk.READ_ONLY
    exposure: ToolExposure = ToolExposure.DIRECT
    provider_surface: ProviderSurface = ProviderSurface.FUNCTION
    search_text: str = ""
    search_keywords: tuple[str, ...] = ()
    permission_policy: str = "read_only"
    mutates_files: bool = False
    preview_required: bool = False
    approved_save_candidate_required: bool = False
    supports_parallel_tool_calls: bool = False
    _backend_handlers: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        handlers: dict[str, str] = {}
        for base in reversed(cls.__mro__[1:]):
            handlers.update(getattr(base, "_backend_handlers", {}))
        for attr_name, value in cls.__dict__.items():
            backend_id = getattr(value, "_tool_backend_id", None)
            if backend_id:
                handlers[backend_id] = attr_name
        cls._backend_handlers = handlers

    def __init__(self, backend: Any = None):
        self.backend = backend

    def preflight_validate(self, **kwargs) -> str | None:
        """Optional lightweight validation before approval/execution.

        Return an error string to short-circuit execution, or None if valid.
        """
        return None

    @property
    def backend_id(self) -> str:
        return getattr(self.backend, "backend_id", "local")

    def run_backend(self, *args, **kwargs) -> str:
        """Dispatch to a tool-local implementation for the active backend."""
        handler_name = self._backend_handlers.get(self.backend_id)
        if handler_name is None:
            handler_name = self._backend_handlers.get("local")
        if handler_name is None:
            raise RuntimeError(
                f"Tool '{self.name}' has no handler for backend '{self.backend_id}' and no local fallback"
            )
        handler = getattr(self, handler_name)
        return handler(*args, **kwargs)

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Run the tool and return a text result."""
        ...

    def tool_spec(self) -> ToolSpec:
        """Return the canonical specification for this tool."""
        return ToolSpec(
            name=self.name,
            namespace=self.namespace,
            description=self.description,
            input_schema=self.parameters,
            output_schema=self.output_schema,
            output_strategy=self.output_strategy,
            risk=self.risk,
            exposure=self.exposure,
            search_text=self.search_text
            or build_tool_search_text(
                name=self.name,
                description=self.description,
                input_schema=self.parameters,
                keywords=self.search_keywords,
            ),
            search_keywords=tuple(self.search_keywords),
            permission=ToolPermissionSpec(policy=self.permission_policy),
            mutation=ToolMutationSpec(
                modifies_files=self.mutates_files,
                preview_required=self.preview_required,
                approved_save_candidate_required=self.approved_save_candidate_required,
            ),
            execution=ToolExecutionSpec(
                executor_ref=f"{self.__class__.__module__}.{self.__class__.__qualname__}",
                backend_dispatch=bool(self._backend_handlers),
                supports_parallel=self.supports_parallel_tool_calls,
            ),
            provider_surface=self.provider_surface,
        )

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return self.tool_spec().to_openai_chat_tool()
