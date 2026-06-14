from __future__ import annotations

from types import SimpleNamespace

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.llm.models import ToolCall
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


class _CatalogTool:
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        exposure: ToolExposure,
        *,
        tool_id: str = "",
    ) -> None:
        self.name = name
        self.description = f"{name} tool"
        self.exposure = exposure
        self.tool_id = tool_id
        self.calls = 0

    def tool_spec(self) -> ToolSpec:
        metadata = {"tool_id": self.tool_id} if self.tool_id else {}
        return ToolSpec(
            name=self.name,
            namespace="test",
            description=self.description,
            input_schema=self.parameters,
            output_schema=None,
            output_strategy=ToolOutputStrategy.TEXT,
            risk=ToolRisk.READ_ONLY,
            exposure=self.exposure,
            search_text=f"{self.name}\n{self.description}",
            search_keywords=(),
            permission=ToolPermissionSpec(policy="read_only"),
            mutation=ToolMutationSpec(),
            execution=ToolExecutionSpec(executor_ref=f"test.{self.name}"),
            provider_surface=ProviderSurface.FUNCTION,
            metadata=metadata,
        )

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None

    def execute(self) -> str:
        self.calls += 1
        return f"{self.name}:ok"


def _capability_tool_spec(
    *,
    name: str,
    tool_id: str,
    target_tool_ref: str = "",
) -> dict:
    return {
        "tool_id": tool_id,
        "name": name,
        "namespace": "capability",
        "description": f"{name} capability",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": None,
        "output_strategy": "text",
        "risk": "capability",
        "exposure": "deferred",
        "search_text": f"{name} capability",
        "search_keywords": [name],
        "permission": {"policy": "allow"},
        "mutation": {
            "modifies_files": False,
            "preview_required": False,
            "approved_save_candidate_required": False,
        },
        "execution": {
            "executor_ref": target_tool_ref or tool_id,
            "backend_dispatch": True,
            "supports_parallel": False,
        },
        "provider_surface": "function",
        "source_type": "builtin_tool",
        "target_tool_ref": target_tool_ref or f"builtin:{name}",
        "metadata": {
            "tool_id": tool_id,
            "package_id": "docs",
            "component_id": f"builtin_tool:{name}",
        },
    }


def _agent_with_tools(tools: list[_CatalogTool]) -> Agent:
    return Agent(
        llm=SimpleNamespace(),
        tools=tools,
        config=Config(approval=ApprovalConfig(default_mode="allow")),
    )


def test_agent_builds_tool_exposure_plan_and_routes_executors_from_catalog() -> None:
    direct = _CatalogTool("alpha", ToolExposure.DIRECT)
    deferred = _CatalogTool("capability_docs", ToolExposure.DEFERRED)
    hidden = _CatalogTool("internal_save", ToolExposure.HIDDEN)
    hosted = _CatalogTool("web_search", ToolExposure.HOSTED)
    agent = _agent_with_tools([hidden, direct, hosted, deferred])

    plan = agent.tool_exposure_plan()

    assert [entry.name for entry in plan.direct] == ["alpha"]
    assert [entry.name for entry in plan.deferred] == ["capability_docs"]
    assert [entry.name for entry in plan.hidden] == ["internal_save"]
    assert [entry.name for entry in plan.hosted] == ["web_search"]
    assert agent.get_tool("capability_docs") is deferred
    assert agent.get_tool("internal_save") is hidden
    assert agent.get_model_callable_tool("alpha") is direct
    assert agent.get_model_callable_tool("capability_docs") is None
    assert agent.get_model_callable_tool("internal_save") is None


def test_tool_executor_rejects_hidden_tool_direct_model_call() -> None:
    hidden = _CatalogTool("internal_save", ToolExposure.HIDDEN)
    agent = _agent_with_tools([hidden])

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_hidden", name="internal_save", arguments={})
    )

    assert "not directly exposed to the model" in result
    assert hidden.calls == 0


def test_tool_executor_executes_direct_tool_through_catalog_route() -> None:
    direct = _CatalogTool("alpha", ToolExposure.DIRECT)
    agent = _agent_with_tools([direct])

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_direct", name="alpha", arguments={})
    )

    assert result == "alpha:ok"
    assert direct.calls == 1


def test_agent_catalog_includes_resolved_capability_tool_specs_as_deferred_routes() -> None:
    direct = _CatalogTool("fetch_capabilities", ToolExposure.DIRECT)
    agent = _agent_with_tools([direct])
    agent.resolved_capabilities = {
        "tool_specs": [
            _capability_tool_spec(
                name="fetch_capabilities",
                tool_id="capability:docs:builtin_tool:fetch_capabilities",
                target_tool_ref="builtin:fetch_capabilities",
            )
        ]
    }

    plan = agent.tool_exposure_plan()

    assert [entry.name for entry in plan.direct] == ["fetch_capabilities"]
    assert [entry.tool_id for entry in plan.deferred] == [
        "capability:docs:builtin_tool:fetch_capabilities"
    ]
    assert agent.get_model_callable_tool("fetch_capabilities") is direct
    assert agent.get_tool("fetch_capabilities") is direct
    deferred_tool = agent.tool_route_plan().get_executor_by_id(
        "capability:docs:builtin_tool:fetch_capabilities"
    )
    assert deferred_tool is not None
    assert deferred_tool.tool_spec().exposure == ToolExposure.DEFERRED


def test_agent_catalog_routes_duplicate_deferred_names_only_by_tool_id() -> None:
    first = _CatalogTool(
        "search",
        ToolExposure.DEFERRED,
        tool_id="capability:docs:search",
    )
    second = _CatalogTool(
        "search",
        ToolExposure.DEFERRED,
        tool_id="mcp:github:search",
    )
    agent = _agent_with_tools([first, second])

    plan = agent.tool_exposure_plan()

    assert [entry.tool_id for entry in plan.deferred] == [
        "capability:docs:search",
        "mcp:github:search",
    ]
    assert agent.get_model_callable_tool("search") is None
    assert plan.get_executor_by_id("capability:docs:search") is first
    assert plan.get_executor_by_id("mcp:github:search") is second


def test_agent_catalog_does_not_use_effective_capabilities_as_tool_directory() -> None:
    direct = _CatalogTool("fetch_capabilities", ToolExposure.DIRECT)
    agent = _agent_with_tools([direct])
    agent.effective_capabilities = {
        "tool_specs": [
            _capability_tool_spec(
                name="fetch_capabilities",
                tool_id="capability:docs:builtin_tool:fetch_capabilities",
                target_tool_ref="builtin:fetch_capabilities",
            )
        ]
    }

    plan = agent.tool_route_plan()

    assert plan.get_executor_by_id(
        "capability:docs:builtin_tool:fetch_capabilities"
    ) is None
