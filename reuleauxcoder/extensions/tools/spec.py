"""Canonical tool specification model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolExposure(str, Enum):
    """How a tool is exposed to the model/runtime."""

    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"
    HOSTED = "hosted"


class ToolRisk(str, Enum):
    """Architecture-level risk classification for a tool."""

    READ_ONLY = "read_only"
    COMMAND_EXECUTION = "command_execution"
    FILE_MUTATION = "file_mutation"
    DOCUMENT_DRAFT = "document_draft"
    CAPABILITY = "capability"
    INTERNAL = "internal"


class ProviderSurface(str, Enum):
    """Provider-facing representation used for this tool."""

    FUNCTION = "function"
    HOSTED = "hosted"
    NONE = "none"


class ToolOutputStrategy(str, Enum):
    """How callers should interpret tool output."""

    TEXT = "text"
    JSON = "json"
    STRUCTURED = "structured"
    MUTATION_RESULT = "mutation_result"


@dataclass(frozen=True)
class ToolPermissionSpec:
    """Permission policy binding for a tool."""

    policy: str = "read_only"


@dataclass(frozen=True)
class ToolMutationSpec:
    """Mutation contract for tools that can change workspace state."""

    modifies_files: bool = False
    preview_required: bool = False
    approved_save_candidate_required: bool = False


@dataclass(frozen=True)
class ToolExecutionSpec:
    """Runtime execution binding for a tool."""

    executor_ref: str
    backend_dispatch: bool = True
    supports_parallel: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for a tool's architecture and provider schema."""

    name: str
    namespace: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    output_strategy: ToolOutputStrategy
    risk: ToolRisk
    exposure: ToolExposure
    search_text: str
    search_keywords: tuple[str, ...]
    permission: ToolPermissionSpec
    mutation: ToolMutationSpec
    execution: ToolExecutionSpec
    provider_surface: ProviderSurface = ProviderSurface.FUNCTION
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this spec for runtime snapshots and deferred search."""
        return {
            "name": self.name,
            "namespace": self.namespace,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema) if isinstance(self.output_schema, dict) else None,
            "output_strategy": self.output_strategy.value,
            "risk": self.risk.value,
            "exposure": self.exposure.value,
            "search_text": self.search_text,
            "search_keywords": list(self.search_keywords),
            "permission": {"policy": self.permission.policy},
            "mutation": {
                "modifies_files": self.mutation.modifies_files,
                "preview_required": self.mutation.preview_required,
                "approved_save_candidate_required": self.mutation.approved_save_candidate_required,
            },
            "execution": {
                "executor_ref": self.execution.executor_ref,
                "backend_dispatch": self.execution.backend_dispatch,
                "supports_parallel": self.execution.supports_parallel,
            },
            "provider_surface": self.provider_surface.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolSpec":
        """Restore a ToolSpec from a runtime snapshot dictionary."""
        permission = data.get("permission")
        mutation = data.get("mutation")
        execution = data.get("execution")
        input_schema = data.get("input_schema")
        output_schema = data.get("output_schema")
        return cls(
            name=str(data.get("name") or ""),
            namespace=str(data.get("namespace") or "capability"),
            description=str(data.get("description") or ""),
            input_schema=dict(input_schema) if isinstance(input_schema, dict) else {"type": "object", "properties": {}},
            output_schema=dict(output_schema) if isinstance(output_schema, dict) else None,
            output_strategy=_enum_value(
                ToolOutputStrategy,
                data.get("output_strategy"),
                ToolOutputStrategy.TEXT,
            ),
            risk=_enum_value(ToolRisk, data.get("risk"), ToolRisk.CAPABILITY),
            exposure=_enum_value(
                ToolExposure,
                data.get("exposure"),
                ToolExposure.DEFERRED,
            ),
            search_text=str(data.get("search_text") or ""),
            search_keywords=tuple(
                str(item)
                for item in data.get("search_keywords", [])
                if str(item or "").strip()
            )
            if isinstance(data.get("search_keywords"), list)
            else (),
            permission=ToolPermissionSpec(
                policy=str(
                    permission.get("policy")
                    if isinstance(permission, dict)
                    else data.get("permission_policy")
                    or "capability"
                )
            ),
            mutation=ToolMutationSpec(
                modifies_files=bool(mutation.get("modifies_files"))
                if isinstance(mutation, dict)
                else False,
                preview_required=bool(mutation.get("preview_required"))
                if isinstance(mutation, dict)
                else False,
                approved_save_candidate_required=bool(
                    mutation.get("approved_save_candidate_required")
                )
                if isinstance(mutation, dict)
                else False,
            ),
            execution=ToolExecutionSpec(
                executor_ref=str(execution.get("executor_ref") or "")
                if isinstance(execution, dict)
                else "",
                backend_dispatch=bool(execution.get("backend_dispatch", True))
                if isinstance(execution, dict)
                else True,
                supports_parallel=bool(execution.get("supports_parallel", False))
                if isinstance(execution, dict)
                else False,
            ),
            provider_surface=_enum_value(
                ProviderSurface,
                data.get("provider_surface"),
                ProviderSurface.FUNCTION,
            ),
            metadata=dict(data.get("metadata") or {})
            if isinstance(data.get("metadata"), dict)
            else {},
        )

    def to_openai_chat_tool(self) -> dict[str, Any]:
        """Return the provider-compatible function schema derived from this spec."""
        if self.provider_surface != ProviderSurface.FUNCTION:
            raise ValueError(f"Tool '{self.name}' is not exposed as a function tool")
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


def build_tool_search_text(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    keywords: tuple[str, ...] = (),
) -> str:
    """Build stable searchable text from model-visible tool metadata."""
    parts: list[str] = [name, description]
    properties = input_schema.get("properties")
    if isinstance(properties, dict):
        for property_name, property_schema in sorted(properties.items()):
            parts.append(str(property_name))
            if isinstance(property_schema, dict):
                property_description = property_schema.get("description")
                if property_description:
                    parts.append(str(property_description))
    parts.extend(keyword for keyword in keywords if keyword)
    return "\n".join(part.strip() for part in parts if str(part).strip())


def _enum_value(enum_cls, value: Any, default):
    try:
        return enum_cls(str(value))
    except (TypeError, ValueError):
        return default
