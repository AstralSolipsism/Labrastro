from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModeConfig
from reuleauxcoder.domain.files.file_mutation_service import FileChange, FileMutationResult
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
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


class _SchemaTool(_ExecutableTool):
    def __init__(
        self,
        name: str,
        *,
        exposure: ToolExposure,
        parameters: dict,
        namespace: str = "capability",
        tool_id: str | None = None,
    ) -> None:
        super().__init__(
            name,
            exposure=exposure,
            namespace=namespace,
            tool_id=tool_id,
        )
        self.parameters = parameters


class _PreflightFailingTool(_ExecutableTool):
    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return "query rejected by target preflight"


class _PatchTool:
    name = "apply_patch"
    description = "Apply a text patch."
    parameters = {
        "type": "object",
        "properties": {
            "patch": {"type": "string"},
        },
        "required": ["patch"],
        "additionalProperties": False,
    }
    tool_source = "capability"
    uses_workspace_mutation_candidate = True

    def __init__(
        self,
        *,
        exposure: ToolExposure,
        tool_id: str,
    ) -> None:
        self.exposure = exposure
        self.tool_id = tool_id
        self.calls: list[dict] = []

    def tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            namespace="capability",
            description=self.description,
            input_schema=self.parameters,
            output_schema=None,
            output_strategy=ToolOutputStrategy.MUTATION_RESULT,
            risk=ToolRisk.FILE_MUTATION,
            exposure=self.exposure,
            search_text="apply_patch file mutation",
            search_keywords=("apply_patch", "patch"),
            permission=ToolPermissionSpec(policy="workspace_write"),
            mutation=ToolMutationSpec(
                modifies_files=True,
                preview_required=True,
                approved_save_candidate_required=True,
            ),
            execution=ToolExecutionSpec(executor_ref="builtin:apply_patch"),
            provider_surface=ProviderSurface.FUNCTION,
            metadata={"tool_id": self.tool_id},
        )

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None

    def execute(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        return "Applied patch directly"


class _PatchPreviewBackend:
    workspace_id = "workspace-test"
    execution_target = "local"
    path_space = "workspace"

    def __init__(self, *, fail_preview: bool = False) -> None:
        self.fail_preview = fail_preview
        self.preview_calls: list[str] = []
        self.saved_candidates: list[dict] = []

    def preview_text_patch(self, patch: str) -> FileMutationResult:
        self.preview_calls.append(patch)
        if self.fail_preview:
            return FileMutationResult(
                status="failed",
                error="target semantic preview failed",
            )
        preview_identity = {
            "plan_id": "capability-preview-plan",
            "candidate_hash": "candidate-hash",
            "tool_name": "apply_patch",
            "workspace_id": self.workspace_id,
            "execution_target": self.execution_target,
            "path_space": self.path_space,
            "args_hash": "args-hash",
        }
        candidate = {
            "preview_identity": dict(preview_identity),
            "operations": [
                {"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}
            ],
        }
        return FileMutationResult(
            status="in_progress",
            changes=(FileChange(path="docs/a.md", kind="add", diff="+# A\n"),),
            diff="+# A\n",
            message="Preview ready",
            preview_identity=preview_identity,
            approved_save_candidate=candidate,
        )

    def save_candidate(self, candidate: dict) -> FileMutationResult:
        self.saved_candidates.append(dict(candidate))
        return FileMutationResult(status="completed", message="Saved candidate")


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
    source_type: str = "mcp_tool",
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


def test_tool_search_result_uses_model_visible_whitelist_and_call_template() -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer"},
            "score": {"type": "number"},
            "include_archived": {"type": "boolean"},
            "mode": {"enum": ["fast", "accurate"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "filters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "optional_status": {"type": "string"},
                },
                "required": ["owner"],
                "additionalProperties": False,
            },
            "optional_note": {"type": "string"},
        },
        "required": [
            "query",
            "limit",
            "score",
            "include_archived",
            "mode",
            "tags",
            "filters",
        ],
        "additionalProperties": False,
    }
    deferred = _SchemaTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
        parameters=schema,
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="search-contract",
            name="tool_search",
            arguments={"query": "docs", "max_results": 5},
        )
    )

    payload = json.loads(result)
    item = payload["results"][0]
    assert set(item) == {
        "tool_id",
        "call_via",
        "name",
        "description",
        "input_schema",
        "call_template",
        "permission",
        "risk",
    }
    assert item["tool_id"] == "capability:docs:lookup"
    assert item["call_via"] == "capability_execute"
    assert item["input_schema"] == schema
    assert item["call_template"] == {
        "tool_id": "capability:docs:lookup",
        "arguments": {
            "query": "<string>",
            "limit": 0,
            "score": 0,
            "include_archived": False,
            "mode": "<one of: fast | accurate>",
            "tags": ["<string>"],
            "filters": {
                "owner": "<string>",
            },
        },
    }
    assert "optional_note" not in item["call_template"]["arguments"]
    assert "optional_status" not in item["call_template"]["arguments"]["filters"]
    assert "target_tool_ref" not in item
    assert "source_type" not in item
    assert "mutation" not in item


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
        and event.tool_name == "docs_lookup"
    )
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    target_context = {
        "gateway_tool_name": "capability_execute",
        "parent_tool_call_id": "exec-meta",
        "target_tool_call_id": target_start.tool_call_id,
        "target_tool_id": "capability:docs:lookup",
        "target_tool_name": "docs_lookup",
        "target_arguments": {"query": "cache"},
        "target_exposure": "deferred",
        "target_risk": "read_only",
        "target_permission_policy": "read_only",
    }
    assert gateway_start.data["tool_id"] == "builtin:capability_execute"
    assert gateway_start.data["risk"] == "capability"
    assert gateway_start.data["exposure"] == "direct"
    assert gateway_end.data["meta"]["execute_trace"] == {
        "gateway_tool_name": "capability_execute",
        "parent_tool_call_id": "exec-meta",
        "target_tool_call_id": target_start.tool_call_id,
        "tool_id": "capability:docs:lookup",
        "target_tool_name": "docs_lookup",
        "target_tool_id": "capability:docs:lookup",
        "target_exposure": "deferred",
        "target_risk": "read_only",
        "target_permission_policy": "read_only",
    }
    assert target_start.data["tool_id"] == "capability:docs:lookup"
    assert target_start.data["risk"] == "read_only"
    assert target_start.data["exposure"] == "deferred"
    assert target_start.data["capability_target"] == target_context
    assert target_end.data["capability_target"] == target_context
    assert [
        (event.event_type, event.tool_name, event.tool_call_id)
        for event in events
        if event.event_type
        in {AgentEventType.TOOL_CALL_START, AgentEventType.TOOL_CALL_END}
        and event.tool_call_id in {"exec-meta", target_start.tool_call_id}
    ] == [
        (AgentEventType.TOOL_CALL_START, "capability_execute", "exec-meta"),
        (AgentEventType.TOOL_CALL_START, "docs_lookup", target_start.tool_call_id),
        (AgentEventType.TOOL_CALL_END, "docs_lookup", target_start.tool_call_id),
        (AgentEventType.TOOL_CALL_END, "capability_execute", "exec-meta"),
    ]


def test_capability_execute_invalid_target_arguments_returns_target_retry_template() -> None:
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
            id="exec-invalid-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"q": "cache"},
            },
        )
    )

    assert result.startswith("Error: bad arguments for target tool docs_lookup")
    assert "tool_id: capability:docs:lookup" in result
    assert "Retry by calling capability_execute with:" in result
    assert '"tool_id": "capability:docs:lookup"' in result
    assert '"arguments": {' in result
    assert '"query": "<string>"' in result
    assert "bad arguments for capability_execute" not in result
    target_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_name == "docs_lookup"
    )
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    context = target_start.data["capability_target"]
    assert context["parent_tool_call_id"] == "exec-invalid-target"
    assert context["target_tool_call_id"] == target_start.tool_call_id
    assert context["target_tool_id"] == "capability:docs:lookup"
    assert context["target_tool_name"] == "docs_lookup"
    assert context["target_arguments"] == {"q": "cache"}
    assert target_end.data["capability_target"] == context
    assert [
        (event.event_type, event.tool_name, event.tool_call_id)
        for event in events
        if event.event_type
        in {AgentEventType.TOOL_CALL_START, AgentEventType.TOOL_CALL_END}
        and event.tool_call_id in {"exec-invalid-target", target_start.tool_call_id}
    ] == [
        (
            AgentEventType.TOOL_CALL_START,
            "capability_execute",
            "exec-invalid-target",
        ),
        (AgentEventType.TOOL_CALL_START, "docs_lookup", target_start.tool_call_id),
        (AgentEventType.TOOL_CALL_END, "docs_lookup", target_start.tool_call_id),
        (
            AgentEventType.TOOL_CALL_END,
            "capability_execute",
            "exec-invalid-target",
        ),
    ]


def test_capability_execute_target_context_uses_repaired_execution_arguments(
    monkeypatch,
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "include_archived": {"type": "boolean"},
            "filters": {
                "type": "object",
                "properties": {"owner": {"type": "string"}},
                "required": ["owner"],
                "additionalProperties": False,
            },
        },
        "required": ["query", "limit", "include_archived", "filters"],
        "additionalProperties": False,
    }
    deferred = _SchemaTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
        parameters=schema,
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    diagnostic_events = []
    agent.add_event_handler(events.append)

    def capture_tool_diagnostic_event(**kwargs):
        diagnostic_events.append(kwargs)

    monkeypatch.setattr(
        "reuleauxcoder.domain.agent.tool_execution.persist_tool_diagnostic_event",
        capture_tool_diagnostic_event,
    )

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-repaired-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {
                    "query": "cache",
                    "limit": "2",
                    "include_archived": "true",
                    "filters": '{"owner":"docs"}',
                },
            },
        )
    )

    expected_arguments = {
        "query": "cache",
        "limit": 2,
        "include_archived": True,
        "filters": {"owner": "docs"},
    }
    assert result == "docs_lookup:cache"
    assert deferred.calls == [expected_arguments]
    target_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_name == "docs_lookup"
    )
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    assert target_start.data["capability_target"]["target_arguments"] == (
        expected_arguments
    )
    assert target_end.data["capability_target"]["target_arguments"] == (
        expected_arguments
    )
    assert target_end.data["meta"]["tool_diagnostics"]
    assert all(
        item["metadata"]["capability_target"]["target_arguments"]
        == expected_arguments
        for item in target_end.data["meta"]["tool_diagnostics"]
    )
    target_diagnostic_events = [
        item
        for item in diagnostic_events
        if isinstance(item.get("metadata"), dict)
        and isinstance(item["metadata"].get("capability_target"), dict)
    ]
    assert target_diagnostic_events
    assert all(
        item["metadata"]["capability_target"]["target_arguments"]
        == expected_arguments
        for item in target_diagnostic_events
    )


def test_capability_execute_clears_target_context_after_execution() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(
            id="exec-cleanup-success",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result == "docs_lookup:cache"
    assert any(
        event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_name == "docs_lookup"
        and event.data["capability_target"]["target_tool_id"]
        == "capability:docs:lookup"
        for event in events
    )
    assert executor._capability_target_context_by_call_id == {}

    invalid_result = executor.execute(
        ToolCall(
            id="exec-cleanup-invalid",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"q": "cache"},
            },
        )
    )

    assert invalid_result.startswith("Error: bad arguments for target tool docs_lookup")
    assert executor._capability_target_context_by_call_id == {}


def test_capability_execute_target_preflight_error_returns_target_retry_template() -> None:
    deferred = _PreflightFailingTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-preflight-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result.startswith("Error: preflight failed for target tool docs_lookup")
    assert "tool_id: capability:docs:lookup" in result
    assert "query rejected by target preflight" in result
    assert "Retry by calling capability_execute with:" in result
    assert '"tool_id": "capability:docs:lookup"' in result
    assert '"query": "<string>"' in result
    target_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_name == "docs_lookup"
    )
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    assert target_end.data["capability_target"] == target_start.data["capability_target"]
    assert [
        (event.event_type, event.tool_name, event.tool_call_id)
        for event in events
        if event.event_type
        in {AgentEventType.TOOL_CALL_START, AgentEventType.TOOL_CALL_END}
        and event.tool_call_id in {"exec-preflight-target", target_start.tool_call_id}
    ] == [
        (
            AgentEventType.TOOL_CALL_START,
            "capability_execute",
            "exec-preflight-target",
        ),
        (AgentEventType.TOOL_CALL_START, "docs_lookup", target_start.tool_call_id),
        (AgentEventType.TOOL_CALL_END, "docs_lookup", target_start.tool_call_id),
        (
            AgentEventType.TOOL_CALL_END,
            "capability_execute",
            "exec-preflight-target",
        ),
    ]


def test_capability_execute_permission_denial_reports_target_tool() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)

    def deny_target(tool, *, tool_call=None, action="execute"):  # noqa: ARG001
        if tool_call is None or getattr(tool_call, "name", "") != "docs_lookup":
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)
        assert getattr(tool, "name", "") == "docs_lookup"
        return PermissionDecision(
            action=PermissionAction.DENY,
            authorized=False,
            reason="docs_lookup denied by policy",
            policy_matched="target-policy",
        )

    agent.evaluate_tool_permission = deny_target

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-denied-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result.startswith("Error: target tool 'docs_lookup' denied by permission gateway")
    assert "tool_id: capability:docs:lookup" in result
    assert "docs_lookup denied by policy" in result
    target_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_name == "docs_lookup"
    )
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    context = target_start.data["capability_target"]
    assert context["target_tool_id"] == "capability:docs:lookup"
    assert target_end.data["capability_target"] == context
    diagnostics = target_end.data["meta"]["tool_diagnostics"]
    assert diagnostics[0]["metadata"]["capability_target"] == context
    assert [
        (event.event_type, event.tool_name, event.tool_call_id)
        for event in events
        if event.event_type
        in {AgentEventType.TOOL_CALL_START, AgentEventType.TOOL_CALL_END}
        and event.tool_call_id in {"exec-denied-target", target_start.tool_call_id}
    ] == [
        (
            AgentEventType.TOOL_CALL_START,
            "capability_execute",
            "exec-denied-target",
        ),
        (AgentEventType.TOOL_CALL_START, "docs_lookup", target_start.tool_call_id),
        (AgentEventType.TOOL_CALL_END, "docs_lookup", target_start.tool_call_id),
        (
            AgentEventType.TOOL_CALL_END,
            "capability_execute",
            "exec-denied-target",
        ),
    ]


def test_capability_execute_require_approval_creates_target_approval_request() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.deny_once("target approval denied")

    approval_provider = ApprovalProvider()
    agent.approval_provider = approval_provider

    def require_target_approval(tool, *, tool_call=None, action="execute"):  # noqa: ARG001
        if tool_call is None or getattr(tool_call, "name", "") != "docs_lookup":
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)
        return PermissionDecision(
            action=PermissionAction.REQUIRE_APPROVAL,
            authorized=True,
            reason="docs_lookup requires approval",
        )

    agent.evaluate_tool_permission = require_target_approval

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-approval-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result == "target approval denied"
    assert len(approval_provider.requests) == 1
    request = approval_provider.requests[0]
    assert request.tool_name == "docs_lookup"
    assert request.tool_args == {"query": "cache"}
    assert request.metadata["tool_call_id"].startswith(
        "exec-approval-target:capability:docs:lookup"
    )
    context = request.metadata["capability_target"]
    assert context["parent_tool_call_id"] == "exec-approval-target"
    assert context["gateway_tool_name"] == "capability_execute"
    assert context["target_tool_id"] == "capability:docs:lookup"
    assert context["target_tool_name"] == "docs_lookup"
    assert context["target_arguments"] == {"query": "cache"}
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    assert target_end.data["capability_target"] == context
    assert "capability_execute" not in result


def test_capability_execute_blocked_review_reports_target_tool() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    events = []
    agent.add_event_handler(events.append)

    def block_target(tool, *, tool_call=None, action="execute"):  # noqa: ARG001
        if tool_call is None or getattr(tool_call, "name", "") != "docs_lookup":
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)
        return PermissionDecision(
            action=PermissionAction.BLOCKED_REVIEW,
            authorized=False,
            reason="docs_lookup requires offline review",
        )

    agent.evaluate_tool_permission = block_target

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-blocked-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "cache"},
            },
        )
    )

    assert result.startswith("Error: target tool 'docs_lookup' blocked pending review")
    assert "tool_id: capability:docs:lookup" in result
    assert "docs_lookup requires offline review" in result
    assert "capability_execute" not in result
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "docs_lookup"
    )
    context = target_end.data["capability_target"]
    assert context["parent_tool_call_id"] == "exec-blocked-target"
    assert context["target_tool_id"] == "capability:docs:lookup"
    assert (
        target_end.data["meta"]["tool_diagnostics"][0]["metadata"]["capability_target"]
        == context
    )


def test_capability_execute_preview_identity_uses_target_tool_context() -> None:
    deferred = _PatchTool(
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:workspace_patch",
    )
    backend = _PatchPreviewBackend()
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    agent.workspace_mutation_backend = backend
    events = []
    agent.add_event_handler(events.append)

    def allow_target(tool, *, tool_call=None, action="execute"):  # noqa: ARG001
        if tool_call is None or getattr(tool_call, "name", "") != "apply_patch":
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)
        return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent.evaluate_tool_permission = allow_target

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-preview-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:workspace_patch",
                "arguments": {
                    "patch": "*** Begin Patch\n*** Add File: docs/a.md\n+# A\n*** End Patch"
                },
            },
        )
    )

    assert result == "Saved candidate"
    preview_ready = next(
        event
        for event in events
        if event.event_type == AgentEventType.MUTATION_PREVIEW_READY
    )
    context = preview_ready.data["capability_target"]
    assert context["parent_tool_call_id"] == "exec-preview-target"
    assert context["target_tool_name"] == "apply_patch"
    assert context["target_tool_id"] == "capability:docs:workspace_patch"
    assert backend.saved_candidates[0]["capability_target"] == context
    assert (
        backend.saved_candidates[0]["preview_identity"]["capability_target"]
        == context
    )


def test_capability_execute_preview_failure_reports_target_tool_context() -> None:
    deferred = _PatchTool(
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:workspace_patch",
    )
    backend = _PatchPreviewBackend(fail_preview=True)
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    agent.workspace_mutation_backend = backend
    events = []
    agent.add_event_handler(events.append)

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-preview-failed-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:workspace_patch",
                "arguments": {
                    "patch": "*** Begin Patch\n*** Add File: docs/a.md\n+# A\n*** End Patch"
                },
            },
        )
    )

    assert result.startswith("Error: target tool 'apply_patch' semantic preview failed")
    assert "tool_id: capability:docs:workspace_patch" in result
    assert "target semantic preview failed" in result
    assert "capability_execute" not in result
    preview_failed = next(
        event
        for event in events
        if event.event_type == AgentEventType.MUTATION_PREVIEW_FAILED
    )
    context = preview_failed.data["capability_target"]
    assert context["parent_tool_call_id"] == "exec-preview-failed-target"
    assert context["target_tool_id"] == "capability:docs:workspace_patch"
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_name == "apply_patch"
    )
    assert target_end.data["capability_target"] == context
    assert (
        target_end.data["meta"]["tool_diagnostics"][0]["metadata"]["capability_target"]
        == context
    )


def test_capability_execute_approved_save_candidate_uses_target_tool_context() -> None:
    deferred = _PatchTool(
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:workspace_patch",
    )
    backend = _PatchPreviewBackend()
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])
    agent.workspace_mutation_backend = backend
    events = []
    agent.add_event_handler(events.append)

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            candidate = json.loads(json.dumps(request.metadata["approved_save_candidate"]))
            candidate["operations"][0]["new_content"] = "# Approved\n"
            return ApprovalDecision.allow_once(
                "approved",
                meta={"approved_save_candidate": candidate},
            )

    approval_provider = ApprovalProvider()
    agent.approval_provider = approval_provider

    def require_target_approval(tool, *, tool_call=None, action="execute"):  # noqa: ARG001
        if tool_call is None or getattr(tool_call, "name", "") != "apply_patch":
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)
        return PermissionDecision(
            action=PermissionAction.REQUIRE_APPROVAL,
            authorized=True,
            reason="review target patch",
        )

    agent.evaluate_tool_permission = require_target_approval

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-approved-candidate-target",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:workspace_patch",
                "arguments": {
                    "patch": "*** Begin Patch\n*** Add File: docs/a.md\n+# A\n*** End Patch"
                },
            },
        )
    )

    assert result == "Saved candidate"
    assert len(approval_provider.requests) == 1
    request = approval_provider.requests[0]
    context = request.metadata["capability_target"]
    assert request.tool_name == "apply_patch"
    assert context["parent_tool_call_id"] == "exec-approved-candidate-target"
    assert request.metadata["preview_identity"]["capability_target"] == context
    assert request.metadata["approved_save_candidate"]["capability_target"] == context
    assert (
        request.metadata["approved_save_candidate"]["preview_identity"][
            "capability_target"
        ]
        == context
    )
    assert backend.saved_candidates[0]["capability_target"] == context
    assert backend.saved_candidates[0]["operations"][0]["new_content"] == "# Approved\n"
    approval_requested = next(
        event
        for event in events
        if event.event_type == AgentEventType.FILE_CHANGE_APPROVAL_REQUESTED
    )
    assert approval_requested.data["capability_target"] == context


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


def test_capability_execute_preserves_capability_identity_when_resolving_target_executor() -> None:
    target = _ExecutableTool(
        "search",
        exposure=ToolExposure.DIRECT,
        namespace="mcp",
        tool_id="mcp:docs:search",
        result_prefix="docs-search",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), target])
    agent.resolved_capabilities = {
        "tool_specs": [
            _capability_spec(
                tool_id="capability:docs:lookup",
                name="docs_lookup",
                target_tool_ref="mcp:docs:search",
                source_type="mcp_tool",
            )
        ]
    }
    events = []
    agent.add_event_handler(events.append)

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-ref-1",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "draft"},
            },
        )
    )

    assert result == "docs-search:draft"
    assert target.calls == [{"query": "draft"}]
    target_start = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_START
        and event.tool_call_id == "exec-ref-1:capability:docs:lookup"
    )
    target_end = next(
        event
        for event in events
        if event.event_type == AgentEventType.TOOL_CALL_END
        and event.tool_call_id == "exec-ref-1:capability:docs:lookup"
    )
    assert target_start.tool_name == "docs_lookup"
    assert target_start.data["tool_id"] == "capability:docs:lookup"
    assert target_start.data["risk"] == "capability"
    assert target_start.data["exposure"] == "deferred"
    assert target_start.data["capability_target"]["target_tool_id"] == (
        "capability:docs:lookup"
    )
    assert target_start.data["capability_target"]["target_tool_name"] == "docs_lookup"
    assert target_end.data["tool_id"] == "capability:docs:lookup"
    assert target_end.data["capability_target"] == target_start.data["capability_target"]


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


def test_capability_execute_does_not_require_prior_tool_search() -> None:
    deferred = _ExecutableTool(
        "docs_lookup",
        exposure=ToolExposure.DEFERRED,
        tool_id="capability:docs:lookup",
    )
    agent = _agent_with_tools([ToolSearchTool(), CapabilityExecuteTool(), deferred])

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-without-search",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "draft"},
            },
        )
    )

    assert result == "docs_lookup:draft"
    assert deferred.calls == [{"query": "draft"}]
    assert not getattr(agent, "_discovered_capability_tool_ids", set())


def test_tool_search_result_does_not_authorize_later_inactive_tool_id() -> None:
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
            "discover": ModeConfig(
                name="discover",
                tools=["tool_search", "capability_execute", "docs_lookup"],
            ),
            "review": ModeConfig(
                name="review",
                tools=["tool_search", "capability_execute"],
            ),
        },
        active_mode="discover",
    )

    search_result = ToolExecutor(agent).execute(
        ToolCall(
            id="search-before-mode-change",
            name="tool_search",
            arguments={"query": "docs"},
        )
    )
    agent.active_mode = "review"
    execute_result = ToolExecutor(agent).execute(
        ToolCall(
            id="exec-after-mode-change",
            name="capability_execute",
            arguments={
                "tool_id": "capability:docs:lookup",
                "arguments": {"query": "draft"},
            },
        )
    )

    payload = json.loads(search_result)
    assert payload["results"][0]["tool_id"] == "capability:docs:lookup"
    assert getattr(agent, "_discovered_capability_tool_ids") == {
        "capability:docs:lookup"
    }
    assert "is not available in the active tool exposure plan" in execute_result
    assert "Executable test tool" not in execute_result
    assert "input_schema" not in execute_result
    assert "permission" not in execute_result
    assert "Query text" not in execute_result
    assert deferred.calls == []


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
