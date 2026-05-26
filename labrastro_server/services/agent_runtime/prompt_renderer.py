"""Render canonical Agent context into executor-native prompt files."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9._-]+"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[A-Za-z0-9._-]+"),
]


def _redact_secret_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


@dataclass
class CanonicalAgentContext:
    """Executor-neutral context generated from server Agent config."""

    agent_id: str
    agent_name: str = ""
    agent_md: str | None = None
    system_append: str = ""
    dispatch: dict[str, Any] = field(default_factory=dict)
    capability_refs: list[str] = field(default_factory=list)
    resolved_capabilities: dict[str, Any] = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)
    credential_refs: dict[str, str] = field(default_factory=dict)


@dataclass
class RenderedPrompt:
    """Rendered prompt files and metadata for an executor runtime."""

    files: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutorPromptRenderer:
    """Render canonical Agent context to the instruction file each CLI expects."""

    _FILES_BY_EXECUTOR = {
        "codex": "AGENTS.md",
        "claude": "CLAUDE.md",
        "gemini": "GEMINI.md",
        "reuleauxcoder": "AGENT_RUNTIME.md",
    }

    def render(self, executor: str, context: CanonicalAgentContext) -> RenderedPrompt:
        executor_key = str(executor).strip().lower()
        filename = self._FILES_BY_EXECUTOR.get(executor_key, "AGENT_RUNTIME.md")
        markdown = self._render_markdown(context)
        return RenderedPrompt(
            files={filename: markdown},
            metadata={
                "executor": executor_key,
                "agent_id": context.agent_id,
                "credential_refs": dict(context.credential_refs),
                "system_prompt": markdown,
            },
        )

    def _render_markdown(self, context: CanonicalAgentContext) -> str:
        lines = [
            "# Agent Runtime Context",
            "",
            f"- Agent ID: `{context.agent_id}`",
        ]
        if context.agent_name:
            lines.append(f"- Agent Name: {context.agent_name}")
        if context.agent_md:
            lines.append(f"- Agent Instructions: `{context.agent_md}`")
        if context.dispatch:
            lines.append("")
            lines.append("## Dispatch Profile")
            profile = str(context.dispatch.get("profile") or "").strip()
            if profile:
                lines.append(_redact_secret_text(profile))
            examples = _string_list(context.dispatch.get("examples"))
            if examples:
                lines.append("")
                lines.append("Example fit:")
                lines.extend(f"- {_redact_secret_text(item)}" for item in examples)
            avoid = _string_list(context.dispatch.get("avoid"))
            if avoid:
                lines.append("")
                lines.append("Avoid assigning:")
                lines.extend(f"- {_redact_secret_text(item)}" for item in avoid)
        if context.capability_refs or context.resolved_capabilities:
            lines.append("")
            lines.append("## Granted Capabilities")
            if context.capability_refs:
                lines.append("")
                lines.append("Capability packages:")
                lines.extend(f"- `{ref}`" for ref in context.capability_refs)
            self._append_resolved(lines, context.resolved_capabilities)
        if context.mcp_servers:
            lines.append("")
            lines.append("## MCP Servers")
            lines.extend(f"- `{server}`" for server in context.mcp_servers)
        if context.system_append:
            lines.append("")
            lines.append("## Additional Instructions")
            lines.append(_redact_secret_text(context.system_append))
        return "\n".join(lines).strip() + "\n"

    def _append_resolved(self, lines: list[str], resolved: dict[str, Any]) -> None:
        packages = resolved.get("packages")
        if isinstance(packages, list) and packages:
            lines.append("")
            lines.append("Resolved packages:")
            for package in packages:
                if isinstance(package, dict):
                    package_id = str(package.get("id") or "").strip()
                    name = str(package.get("name") or package_id).strip()
                    label = name if not package_id or package_id == name else f"{name} (`{package_id}`)"
                    lines.append(f"- {label}")
                else:
                    lines.append(f"- `{package}`")
        components = resolved.get("components")
        if isinstance(components, list) and components:
            lines.append("")
            lines.append("Installed components:")
            for component in components:
                if not isinstance(component, dict):
                    continue
                component_id = str(component.get("id") or "").strip()
                kind = str(component.get("kind") or "").strip()
                name = str(component.get("name") or component_id).strip()
                label = f"{kind}:{name}" if kind and name else name or component_id
                lines.append(f"- `{label}`")
        sections = (
            ("mcp_servers", "MCP servers"),
            ("skills", "Skills"),
            ("builtin_tool_grants", "Built-in tool grants"),
        )
        for key, title in sections:
            values = resolved.get(key)
            if not isinstance(values, list) or not values:
                continue
            lines.append("")
            lines.append(f"{title}:")
            lines.extend(f"- `{value}`" for value in values)
        requirements = resolved.get("environment_requirements")
        if isinstance(requirements, list) and requirements:
            lines.append("")
            lines.append("Environment requirements:")
            for requirement in requirements:
                if not isinstance(requirement, dict):
                    continue
                requirement_id = str(requirement.get("id") or "").strip()
                kind = str(requirement.get("kind") or "").strip()
                name = str(requirement.get("name") or requirement_id).strip()
                label = f"{kind}:{name}" if kind and name else name or requirement_id
                lines.append(f"- `{label}`")
        fragments = resolved.get("prompt_fragments")
        if isinstance(fragments, list) and fragments:
            lines.append("")
            lines.append("Prompt fragments:")
            for fragment in fragments:
                if isinstance(fragment, dict):
                    name = str(fragment.get("name") or fragment.get("id") or "").strip()
                    if name:
                        lines.append(f"- `{name}`")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]
