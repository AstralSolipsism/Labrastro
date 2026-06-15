from __future__ import annotations

from reuleauxcoder.extensions.tools.catalog import ToolCatalog
from reuleauxcoder.extensions.tools.registry import build_tool_catalog, build_tool_exposure_plan
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


class _SpecTool:
    def __init__(
        self,
        name: str,
        exposure: ToolExposure,
        *,
        namespace: str = "test",
        tool_id: str = "",
    ) -> None:
        self.name = name
        self.description = f"{name} description"
        self.exposure = exposure
        self.namespace = namespace
        self.tool_id = tool_id

    def tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            namespace=self.namespace,
            description=self.description,
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
            output_strategy=ToolOutputStrategy.TEXT,
            risk=ToolRisk.READ_ONLY,
            exposure=self.exposure,
            search_text=self.description,
            search_keywords=(),
            permission=ToolPermissionSpec(policy="read_only"),
            mutation=ToolMutationSpec(),
            execution=ToolExecutionSpec(executor_ref=f"test.{self.name}"),
            provider_surface=ProviderSurface.FUNCTION,
            metadata={"tool_id": self.tool_id} if self.tool_id else {},
        )

    def execute(self) -> str:
        return self.name


def test_tool_catalog_builds_stable_exposure_plan_and_route_table() -> None:
    direct_b = _SpecTool("zeta", ToolExposure.DIRECT)
    hidden = _SpecTool("internal_save", ToolExposure.HIDDEN)
    deferred = _SpecTool("capability_docs", ToolExposure.DEFERRED)
    hosted = _SpecTool("web_search", ToolExposure.HOSTED)
    direct_a = _SpecTool("alpha", ToolExposure.DIRECT)

    plan = ToolCatalog.from_tools(
        [direct_b, hidden, deferred, hosted, direct_a]
    ).exposure_plan()

    assert [entry.name for entry in plan.direct] == ["alpha", "zeta"]
    assert [entry.name for entry in plan.deferred] == ["capability_docs"]
    assert [entry.name for entry in plan.hidden] == ["internal_save"]
    assert [entry.name for entry in plan.hosted] == ["web_search"]
    assert [schema["function"]["name"] for schema in plan.direct_provider_schemas()] == [
        "alpha",
        "zeta",
    ]
    assert plan.get_model_callable_tool("alpha") is direct_a
    assert plan.get_model_callable_tool("capability_docs") is None
    assert plan.get_model_callable_tool("internal_save") is None
    assert plan.get_executor("capability_docs") is deferred
    assert plan.get_executor("internal_save") is hidden
    assert plan.get_executor_by_id("test:capability_docs") is deferred


def test_tool_catalog_rejects_duplicate_direct_model_visible_names() -> None:
    first = _SpecTool("read_file", ToolExposure.DIRECT)
    second = _SpecTool("read_file", ToolExposure.DIRECT, namespace="other")

    try:
        ToolCatalog.from_tools([first, second]).exposure_plan()
    except ValueError as exc:
        assert "duplicate direct tool name" in str(exc)
    else:
        raise AssertionError("duplicate tool names must fail before provider exposure")


def test_tool_catalog_rejects_duplicate_tool_ids() -> None:
    first = _SpecTool(
        "docs_lookup",
        ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    second = _SpecTool(
        "repo_lookup",
        ToolExposure.DEFERRED,
        namespace="other",
        tool_id="capability:docs:lookup",
    )

    try:
        ToolCatalog.from_tools([first, second]).exposure_plan()
    except ValueError as exc:
        assert "duplicate tool id" in str(exc)
    else:
        raise AssertionError("duplicate tool ids must fail before routing")


def test_tool_catalog_allows_duplicate_non_direct_names_by_tool_id() -> None:
    first = _SpecTool(
        "search",
        ToolExposure.DEFERRED,
        namespace="capability",
        tool_id="capability:docs:search",
    )
    second = _SpecTool(
        "search",
        ToolExposure.DEFERRED,
        namespace="mcp",
        tool_id="mcp:github:search",
    )

    plan = ToolCatalog.from_tools([first, second]).exposure_plan()

    assert [entry.tool_id for entry in plan.deferred] == [
        "capability:docs:search",
        "mcp:github:search",
    ]
    assert plan.get_model_callable_tool("search") is None
    assert plan.get_executor_by_id("capability:docs:search") is first
    assert plan.get_executor_by_id("mcp:github:search") is second


def test_tool_catalog_allows_deferred_capability_to_share_direct_tool_name_by_id() -> None:
    direct = _SpecTool("lookup", ToolExposure.DIRECT, namespace="builtin")
    deferred = _SpecTool(
        "lookup",
        ToolExposure.DEFERRED,
        namespace="capability",
        tool_id="capability:docs:lookup",
    )

    plan = ToolCatalog.from_tools([deferred, direct]).exposure_plan()

    assert [entry.name for entry in plan.direct] == ["lookup"]
    assert [entry.tool_id for entry in plan.deferred] == ["capability:docs:lookup"]
    assert plan.get_model_callable_tool("lookup") is direct
    assert plan.get_executor("lookup") is direct
    assert plan.get_executor_by_id("capability:docs:lookup") is deferred


def test_registry_builds_builtin_catalog_and_exposure_plan() -> None:
    catalog = build_tool_catalog()
    plan = build_tool_exposure_plan()

    assert [entry.name for entry in catalog.entries] == sorted(
        entry.name for entry in catalog.entries
    )
    assert "apply_patch" in {entry.name for entry in plan.direct}
    assert "shell" in {entry.name for entry in plan.direct}
    assert "draft_document_begin" in {entry.name for entry in plan.direct}
    assert [schema["function"]["name"] for schema in plan.direct_provider_schemas()] == [
        entry.name
        for entry in plan.direct
        if entry.spec.provider_surface == ProviderSurface.FUNCTION
    ]
