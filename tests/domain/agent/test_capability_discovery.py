from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModeConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.builtin.capability_execute import (
    CapabilityExecuteTool,
)
from reuleauxcoder.extensions.tools.builtin.tool_search import ToolSearchTool
from reuleauxcoder.extensions.tools.spec import (
    ProviderSurface,
    ToolExecutionSpec,
    ToolExposure,
    ToolMutationSpec,
    ToolOutputStrategy,
    ToolPermissionSpec,
    ToolRisk,
    ToolSpec,
)


class _ExecutableTool:
    description = "Executable test tool"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Query text.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        name: str,
        *,
        exposure: ToolExposure,
        namespace: str = "capability",
        tool_id: str | None = None,
        result_prefix: str | None = None,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self.exposure = exposure
        self.tool_id = tool_id
        self.result_prefix = result_prefix or name
        self.calls: list[dict] = []

    def tool_spec(self) -> ToolSpec:
        metadata = {"tool_id": self.tool_id} if self.tool_id else {}
        return ToolSpec(
            name=self.name,
            namespace=self.namespace,
            description=self.description,
            input_schema=self.parameters,
            output_schema=None,
            output_strategy=ToolOutputStrategy.TEXT,
            risk=ToolRisk.READ_ONLY,
            exposure=self.exposure,
            search_text=f"{self.name}\n{self.description}\ndocs lookup evidence",
            search_keywords=(self.name, "docs"),
            permission=ToolPermissionSpec(policy="read_only"),
            mutation=ToolMutationSpec(),
            execution=ToolExecutionSpec(executor_ref=f"{self.namespace}:{self.name}"),
            provider_surface=ProviderSurface.FUNCTION,
            metadata=metadata,
        )

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None

    def execute(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        return f"{self.result_prefix}:{kwargs['query']}"


class _RacyRouteOverrideExecutor(ToolExecutor):
    """Make the old shared route override race deterministic for regression tests."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._route_override_sets = 0
        self._first_override_set = threading.Event()
        self._release_first_override = threading.Event()
        self._first_nested_resolved = threading.Event()

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        if name != "_capability_route_override" or value is None:
            return
        count = getattr(self, "_route_override_sets", 0) + 1
        object.__setattr__(self, "_route_override_sets", count)
        if count == 1:
            self._first_override_set.set()
            self._release_first_override.wait(2)
        elif count == 2:
            self._release_first_override.set()
            self._first_nested_resolved.wait(2)

    def _resolve_model_tool(self, name: str):
        result = super()._resolve_model_tool(name)
        if name == "search" and getattr(self, "_route_override_sets", 0) >= 2:
            self._first_nested_resolved.set()
        return result


def _agent_with_tools(tools: list[object]) -> Agent:
    return Agent(
        llm=SimpleNamespace(),
        tools=tools,
        config=Config(approval=ApprovalConfig(default_mode="allow")),
    )


def _capability_spec(
    *,
    tool_id: str,
    name: str,
    target_tool_ref: str,
    source_type: str = "builtin_tool",
) -> dict:
    return {
        "tool_id": tool_id,
        "name": name,
        "namespace": "capability",
        "description": f"{name} capability",
        "input_schema": _ExecutableTool.parameters,
        "output_schema": None,
        "output_strategy": "text",
        "risk": "capability",
        "exposure": "deferred",
        "search_text": f"{name} capability docs lookup evidence",
        "search_keywords": [name, "docs"],
        "permission": {"policy": "allow"},
        "mutation": {
            "modifies_files": False,
            "preview_required": False,
            "approved_save_candidate_required": False,
        },
        "execution": {
            "executor_ref": target_tool_ref,
            "backend_dispatch": True,
            "supports_parallel": False,
        },
        "provider_surface": "function",
        "source_type": source_type,
        "target_tool_ref": target_tool_ref,
        "metadata": {
            "tool_id": tool_id,
            "target_tool_ref": target_tool_ref,
            "source_type": source_type,
        },
    }


def test_tool_search_returns_only_deferred_specs_and_records_discovery() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    direct = _ExecutableTool(
        "direct_lookup",
        exposure=ToolExposure.DIRECT,
        namespace="builtin",
    )
    hidden = _ExecutableTool(
        "hidden_save",
        exposure=ToolExposure.HIDDEN,
        namespace="internal",
    )
    agent = _agent_with_tools(
        [ToolSearchTool(), CapabilityExecuteTool(), direct, hidden, deferred]
    )
    before_direct_names = [
        item["function"]["name"]
        for item in agent.tool_exposure_plan().direct_provider_schemas()
    ]

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="search-1",
            name="tool_search",
            arguments={"query": "docs", "max_results": 5},
        )
    )

    payload = json.loads(result)
    assert [item["tool_id"] for item in payload["results"]] == [
        "capability:docs:lookup"
    ]
    assert payload["results"][0]["input_schema"] == _ExecutableTool.parameters
    assert getattr(agent, "_discovered_capability_tool_ids") == {
        "capability:docs:lookup"
    }
    assert [
        item["function"]["name"]
        for item in agent.tool_exposure_plan().direct_provider_schemas()
    ] == before_direct_names


def test_tool_search_events_include_spec_metadata_and_search_trace() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)

    ToolExecutor(agent).execute(
        ToolCall(
            id="search-meta",
            name="tool_search",
            arguments={"query": "docs", "max_results": 5},
        )
    )

    start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_call_id == "search-meta"
    )
    end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_call_id == "search-meta"
    )
    assert start.data["tool_id"] == "builtin:tool_search"
    assert start.data["risk"] == "read_only"
    assert start.data["exposure"] == "direct"
    assert end.data["tool_id"] == "builtin:tool_search"
    assert end.data["risk"] == "read_only"
    assert end.data["exposure"] == "direct"
    assert end.data["meta"]["search_trace"] == {
        "query": "docs",
        "result_count": 1,
        "tool_ids": ["capability:docs:lookup"],
    }


def test_capability_execute_runs_registered_deferred_tool_through_executor() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-1",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result == "docs_lookup:cache"
    assert deferred.calls == [{"query": "cache"}]


def test_capability_execute_events_include_gateway_and_target_trace() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-meta",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result == "docs_lookup:cache"
    gateway_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_call_id == "exec-meta"
    )
    gateway_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_call_id == "exec-meta"
    )
    target_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_call_id == "exec-meta:capability:docs:lookup"
    )
    assert gateway_start.data["tool_id"] == "builtin:capability_execute"
    assert gateway_start.data["risk"] == "capability"
    assert gateway_start.data["exposure"] == "direct"
    assert gateway_end.data["meta"]["execute_trace"] == {
        "tool_id": "capability:docs:lookup",
        "target_tool_name": "docs_lookup",
        "target_tool_id": "capability:docs:lookup",
        "target_exposure": "deferred",
    }
    assert target_start.data["tool_id"] == "capability:docs:lookup"
    assert target_start.data["risk"] == "read_only"
    assert target_start.data["exposure"] == "deferred"


def test_capability_execute_parallel_same_name_tools_route_by_tool_id() -> None:
    github_search = _ExecutableTool(
        "search",
        exposure=ToolExposure.DEFERRED,
        namespace="mcp",
        tool_id="mcp:github:search",
        result_prefix="github",
    )
    notion_search = _ExecutableTool(
        "search",
        exposure=ToolExposure.DEFERRED,
        namespace="mcp",
        tool_id="mcp:notion:search",
        result_prefix="notion",
    )
    agent = _agent_with_tools(
        [ToolSearchTool(), CapabilityExecuteTool(), github_search, notion_search]
    )

    results = _RacyRouteOverrideExecutor(agent).execute_parallel(
        [
            ToolCall(
                id="exec-github",
                name="capability_execute",
                arguments={
                    "tool_id": "mcp:github:search",
                    "arguments": {"query": "repo"},
                },
            ),
            ToolCall(
                id="exec-notion",
                name="capability_execute",
                arguments={
                    "tool_id": "mcp:notion:search",
                    "arguments": {"query": "page"},
                },
            ),
        ]
    )

    assert results == ["github:repo", "notion:page"]
    assert github_search.calls == [{"query": "repo"}]
    assert notion_search.calls == [{"query": "page"}]


def test_capability_execute_runs_bound_tool_when_name_matches_gateway() -> None:
    deferred = _ExecutableTool(
        "tool_search",
        exposure=ToolExposure.DEFERRED,
        namespace="mcp",
        tool_id="mcp:custom:tool_search",
        result_prefix="custom_search",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-gateway-name",
            name="capability_execute",
            arguments={
                "tool_id": "mcp:custom:tool_search",
                "arguments": {"query": "docs"},
            },
        )
    )

    assert result == "custom_search:docs"
    assert deferred.calls == [{"query": "docs"}]


def test_capability_execute_resolves_capability_reference_target_executor() -> None:
    target = _ExecutableTool(
        "fetch_capabilities",
        exposure=ToolExposure.DIRECT,
        namespace="builtin",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), target])
    agent.resolved_capabilities = {
        "tool_specs": [
            _capability_spec(
                tool_id="capability:docs:builtin_tool:fetch_capabilities",
                name="fetch_capabilities",
                target_tool_ref="builtin:fetch_capabilities",
            )
        ]
    }

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-ref-1",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:builtin_tool:fetch_capabilities",
                "arguments": {"query": "draft"},
            },
        )
    )

    assert result == "fetch_capabilities:draft"
    assert target.calls == [{"query": "draft"}]


def test_mcp_server_capability_spec_is_authorization_scope_not_discoverable_tool() -> None:
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool()])
    agent.resolved_capabilities = {
        "tool_specs": [
            _capability_spec(
                tool_id="capability:repo:mcp:github",
                name="github",
                target_tool_ref="mcp:github",
                source_type="mcp_server",
            )
        ]
    }

    search_result = ToolExecutor(agent).execute(
        ToolCall(
            id="search-mcp-server",
            name="tool_search",
            arguments={"query": "github", "max_results": 5},
        )
    )
    execute_result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-mcp-server",
            name="capability_execute",
            arguments={
                "tool_id": "capability:repo:mcp:github",
                "arguments": {"query": "repo"},
            },
        )
    )

    assert json.loads(search_result)["results"] == []
    assert "is not available in the active tool exposure plan" in execute_result


def test_capability_execute_rejects_hidden_tool_ids() -> None:
    hidden = _ExecutableTool(
        "hidden_save",
        exposure=ToolExposure.HIDDEN,
        namespace="internal",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), hidden])

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-hidden",
            name="capability_execute",
            arguments={
                "tool_id": "internal:hidden_save",
                "arguments": {"query": "draft"},
            },
        )
    )

    assert "is not a deferred capability tool" in result
    assert hidden.calls == []


def test_discovery_gateway_uses_active_exposure_plan_not_all_routes() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[ToolSearchTool(), CapabilityExecuteTool(), deferred],
        config=Config(approval=ApprovalConfig(default_mode="allow")),
        available_modes={
            "review": ModeConfig(
                name="review",
                tools=["tool_search", "capability_execute"],
            )
        },
        active_mode="review",
    )

    search_result = ToolExecutor(agent).execute(
        ToolCall(
            id="search-blocked",
            name="tool_search",
            arguments={"query": "docs"},
        )
    )
    execute_result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-blocked",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "draft"},
            },
        )
    )

    assert json.loads(search_result)["results"] == []
    assert "is not available in the active tool exposure plan" in execute_result
    assert deferred.calls == []
