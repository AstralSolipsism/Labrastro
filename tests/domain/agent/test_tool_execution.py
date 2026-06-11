"""Tests for ToolExecutor, including CWD sync behaviour."""

import time
import threading
from types import SimpleNamespace

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.lifecycle import (
    FunctionLifecycleHookRuntimeAdapter,
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookEventContext,
    LifecycleHookOutput,
    LifecycleHookDispatcher,
    LifecycleHookRegistry,
    LifecycleHookRuntimeAdapterRegistry,
    default_lifecycle_hook_runtime_adapters,
    system_builtin_lifecycle_declarations_from_hook_registry,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool


class _ShellToolStub:
    """A minimal stub mimicking ShellTool, with _cwd tracking."""
    name = "shell"
    description = "Run a shell command"
    parameters = {}

    def __init__(self) -> None:
        self._cwd: str | None = None

    def execute(self, command: str, timeout: int = 120) -> str:
        return "(no output)"

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}


class _ChangingShellToolStub(_ShellToolStub):
    def __init__(self, new_cwd: str) -> None:
        super().__init__()
        self.new_cwd = new_cwd
        self.backend = SimpleNamespace(
            backend_id="remote_peer",
            context=SimpleNamespace(execution_target="remote_peer"),
        )

    def execute(self, command: str, timeout: int = 120) -> str:  # noqa: ARG002
        self._cwd = self.new_cwd
        return "(changed cwd)"


class _LifecycleCaptureDispatcher:
    def __init__(self) -> None:
        self.contexts: list[LifecycleHookEventContext] = []

    def dispatch(self, context: LifecycleHookEventContext):
        self.contexts.append(context)
        declaration = LifecycleHookDeclaration.from_dict(
            f"hook:admin:{context.event_name}:capture",
            {
                "event": context.event_name,
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "internal",
                "display_name": f"{context.event_name} capture",
                "summary": f"Captures {context.event_name}.",
                "permissions": [],
                "trust": "trusted",
            },
        )
        return [LifecycleHookDispatchResult(declaration, LifecycleHookOutput())]


class _AgentStub:
    """Minimal agent stub for ToolExecutor."""

    def __init__(self, tool) -> None:
        self._tool = tool
        self.active_mode = "coder"
        self.state = SimpleNamespace(current_round=0)
        self.approval_provider = None
        self.hook_registry = HookRegistry()
        self.lifecycle_dispatcher = LifecycleHookDispatcher(
            LifecycleHookRegistry(),
            runtime_adapters=default_lifecycle_hook_runtime_adapters(
                hook_registry=self.hook_registry,
            ),
        )

    def get_tool(self, name: str):  # noqa: ARG002
        return self._tool

    def is_tool_allowed_in_mode(self, name: str) -> bool:  # noqa: ARG002
        return True

    def suggest_modes_for_tool(self, name: str) -> list[str]:  # noqa: ARG002
        return []

    def get_active_mode_config(self):
        return SimpleNamespace(prompt_append="")

    def _emit_event(self, event) -> None:
        pass


class _CaptureTool:
    name = "mcp_batch"
    description = "Capture arguments"
    parameters = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    def __init__(self) -> None:
        self.received = None

    def execute(self, **kwargs) -> str:
        self.received = kwargs
        return "ok"

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None


class _MCPLifecycleContextTool:
    name = "search"
    description = "Search docs"
    tool_source = "mcp"
    server_name = "docs"
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def __init__(self) -> None:
        self.bound_contexts: list[dict] = []
        self.context_during_execute: dict | None = None
        self.current_context: dict = {}
        self.cleared = False

    def bind_lifecycle_context(self, context: dict):
        self.current_context = dict(context)
        self.bound_contexts.append(dict(context))

        def _clear() -> None:
            self.current_context = {}
            self.cleared = True

        return _clear

    def execute(self, query: str) -> str:  # noqa: ARG002
        self.context_during_execute = dict(self.current_context)
        return "ok"

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None


def _lifecycle_declaration(event_name: str) -> LifecycleHookDeclaration:
    return LifecycleHookDeclaration.from_dict(
        f"hook:{event_name}",
        {
            "event": event_name,
            "source": "admin_managed",
            "placement": "server",
            "handler_type": "command",
            "display_name": f"{event_name} hook",
            "summary": f"Test hook for {event_name}.",
            "permissions": [],
            "trust": "trusted",
        },
    )


class _BeforeToolTransformHook(TransformHook[BeforeToolExecuteContext]):
    def __init__(self, transform) -> None:
        super().__init__(name="test_before_tool_transform")
        self._transform = transform

    def run(self, context: BeforeToolExecuteContext) -> BeforeToolExecuteContext:
        try:
            return self._transform(HookPoint.BEFORE_TOOL_EXECUTE, context)
        except TypeError:
            return self._transform(context)


class _AfterToolMetadataCaptureHook(TransformHook[AfterToolExecuteContext]):
    def __init__(self, bucket: list[dict]) -> None:
        super().__init__(name="test_after_tool_metadata_capture")
        self._bucket = bucket

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        self._bucket.append(dict(context.metadata))
        return context


def _install_legacy_before_tool_transform(agent, transform) -> None:
    hook_registry = HookRegistry()
    hook_registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        _BeforeToolTransformHook(transform),
    )
    agent.hook_registry = hook_registry
    agent.lifecycle_dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry(
            system_builtin_lifecycle_declarations_from_hook_registry(hook_registry)
        ),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(
            hook_registry=hook_registry,
        ),
    )


def _install_legacy_after_tool_metadata_capture(agent, bucket: list[dict]) -> None:
    hook_registry = HookRegistry()
    hook_registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        _AfterToolMetadataCaptureHook(bucket),
    )
    agent.hook_registry = hook_registry
    agent.lifecycle_dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry(
            system_builtin_lifecycle_declarations_from_hook_registry(hook_registry)
        ),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(
            hook_registry=hook_registry,
        ),
    )


def test_shell_cwd_syncs_to_runtime_working_directory() -> None:
    """After shell tool executes, ToolExecutor syncs _cwd → agent.runtime_working_directory."""
    tool = _ShellToolStub()
    tool._cwd = "/tmp/cool-dir"

    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_1", name="shell", arguments={"command": "echo hi"})
    executor.execute(tc)

    assert getattr(agent, "runtime_working_directory", None) == "/tmp/cool-dir"


def test_tool_executor_binds_mcp_lifecycle_context_before_tool_side_effect() -> None:
    tool = _MCPLifecycleContextTool()
    agent = _AgentStub(tool)
    agent.current_session_id = "session-1"
    agent.runtime_agent_run_id = "agent-run-1"
    agent.runtime_turn_id = "turn-1"

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-mcp-1", name="search", arguments={"query": "hooks"})
    )

    assert result == "ok"
    assert len(tool.bound_contexts) == 1
    context = tool.bound_contexts[0]
    emitter = context["_agent_lifecycle_event_emitter"]
    public_context = {
        key: value
        for key, value in context.items()
        if key != "_agent_lifecycle_event_emitter"
    }
    assert public_context == {
        "session_run_id": "session-1",
        "agent_run_id": "agent-run-1",
        "turn_id": "turn-1",
        "tool_call_id": "call-mcp-1",
        "tool_name": "search",
        "mcp_server": "docs",
        "trigger_source": "chat",
    }
    assert callable(emitter)
    assert tool.context_during_execute == context
    assert tool.cleared is True
    assert tool.current_context == {}


def test_after_tool_legacy_context_includes_runtime_boundaries() -> None:
    tool = SimpleNamespace(
        name="apply_patch",
        tool_source="builtin",
        execute=lambda **kwargs: "Applied patch",
        preflight_validate=lambda **kwargs: None,
        schema=lambda: {"type": "function", "function": {"name": "apply_patch"}},
    )
    captured: list[dict] = []
    agent = _AgentStub(tool)
    agent.runtime_agent_run_id = "run-1"
    agent.runtime_workspace_root = "/workspace"
    agent.runtime_working_directory = "/workspace/src"
    agent.permission_trigger_source = "chat"
    _install_legacy_after_tool_metadata_capture(agent, captured)
    executor = ToolExecutor(agent)

    tc = ToolCall(
        id="call_write",
        name="apply_patch",
        arguments={"patch": "*** Begin Patch\n*** End Patch"},
    )
    executor.execute(tc)

    assert len(captured) == 1
    metadata = captured[0]
    assert metadata["agent_run_id"] == "run-1"
    assert metadata["runtime_workspace_root"] == "/workspace"
    assert metadata["runtime_working_directory"] == "/workspace/src"
    assert metadata["path_space"] == "agent_run_worktree"
    assert metadata["trigger_source"] == "chat"


def test_shell_cwd_change_dispatches_cwdchanged_lifecycle_with_runtime_boundaries() -> None:
    tool = _ChangingShellToolStub("/tmp/agent-worktree/subdir")
    agent = _AgentStub(tool)
    agent.runtime_agent_run_id = "run-1"
    agent.runtime_workspace_root = "/tmp/agent-worktree"
    agent.runtime_working_directory = "/tmp/agent-worktree"
    dispatcher = _LifecycleCaptureDispatcher()
    agent.lifecycle_dispatcher = dispatcher
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_cwd", name="shell", arguments={"command": "cd subdir"})
    executor.execute(tc)

    contexts = [
        context for context in dispatcher.contexts if context.event_name == "CwdChanged"
    ]
    assert len(contexts) == 1
    payload = contexts[0].payload
    assert payload["previous_working_directory"] == "/tmp/agent-worktree"
    assert payload["current_working_directory"] == "/tmp/agent-worktree/subdir"
    assert payload["runtime_working_directory"] == "/tmp/agent-worktree/subdir"
    assert payload["runtime_workspace_root"] == "/tmp/agent-worktree"
    assert payload["execution_target"] == "remote_peer"
    assert payload["path_space"] == "agent_run_worktree"


def test_shell_cwd_unchanged_does_not_dispatch_cwdchanged_lifecycle() -> None:
    tool = _ChangingShellToolStub("/tmp/agent-worktree")
    agent = _AgentStub(tool)
    agent.runtime_workspace_root = "/tmp/agent-worktree"
    agent.runtime_working_directory = "/tmp/agent-worktree"
    dispatcher = _LifecycleCaptureDispatcher()
    agent.lifecycle_dispatcher = dispatcher
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_cwd_same", name="shell", arguments={"command": "pwd"})
    executor.execute(tc)

    assert [
        context.event_name for context in dispatcher.contexts
    ] == ["PreToolUse", "PostToolUse"]


def test_non_shell_tool_does_not_set_runtime_working_directory() -> None:
    """A tool without _cwd should not touch runtime_working_directory."""
    tool = SimpleNamespace(
        name="read_file",
        execute=lambda **kwargs: "file content",
        preflight_validate=lambda **kwargs: None,
        schema=lambda: {"type": "function", "function": {"name": "read_file"}},
    )
    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_2", name="read_file", arguments={"file_path": "/tmp/x"})
    executor.execute(tc)

    assert not hasattr(agent, "runtime_working_directory")


def test_shell_tool_without_cwd_does_not_set_runtime_working_directory() -> None:
    """ShellTool with _cwd=None should not set runtime_working_directory."""
    tool = _ShellToolStub()
    tool._cwd = None  # explicitly None

    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_3", name="shell", arguments={"command": "echo hi"})
    executor.execute(tc)

    assert not hasattr(agent, "runtime_working_directory")


def test_shell_tool_initializes_cwd_from_runtime_workspace_root() -> None:
    tool = _ShellToolStub()
    agent = _AgentStub(tool)
    agent.runtime_workspace_root = "/tmp/workspace-root"
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_workspace", name="shell", arguments={"command": "echo hi"})
    executor.execute(tc)

    assert tool._cwd == "/tmp/workspace-root"
    assert getattr(agent, "runtime_working_directory", None) == "/tmp/workspace-root"


def test_apply_patch_missing_required_arguments_returns_tool_error() -> None:
    agent = _AgentStub(ApplyPatchTool())
    executor = ToolExecutor(agent)

    result = executor.execute(ToolCall(id="call_4", name="apply_patch", arguments={}))

    assert result.startswith("Error: bad arguments for apply_patch: invalid arguments")
    assert "$.patch: expected string, got missing" in result


def test_tool_executor_enforces_runtime_max_tool_calls_budget() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            return f"ok:{self.calls}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    tool = CountingTool()
    agent = _AgentStub(tool)
    agent.runtime_budget = {"max_tool_calls": 1}
    executor = ToolExecutor(agent)

    first = executor.execute(ToolCall(id="call_1", name="read_file", arguments={}))
    second = executor.execute(ToolCall(id="call_2", name="read_file", arguments={}))

    assert first == "ok:1"
    assert second == "Error: AgentRun budget exceeded: max_tool_calls=1"
    assert tool.calls == 1
    assert agent.runtime_tool_call_count == 1


def test_tool_executor_budget_lock_is_shared_per_agent_run() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "ok"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    agent = _AgentStub(CountingTool())

    first = ToolExecutor(agent)
    second = ToolExecutor(agent)

    assert first._budget_lock is second._budget_lock
    assert first._budget_lock is agent.runtime_budget_lock


def test_tool_executor_parallel_respects_max_tool_calls_without_racing_side_effects() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            time.sleep(0.02)
            with self._lock:
                self.calls += 1
                return f"ok:{self.calls}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    class EventAgent(_AgentStub):
        def __init__(self, tool) -> None:
            super().__init__(tool)
            self.events = []
            self._event_lock = threading.Lock()

        def _emit_event(self, event) -> None:
            with self._event_lock:
                self.events.append(event)

    tool = CountingTool()
    agent = EventAgent(tool)
    agent.runtime_budget = {"max_tool_calls": 3}
    executor = ToolExecutor(agent)

    results = executor.execute_parallel(
        [
            ToolCall(id=f"call_{index}", name="read_file", arguments={})
            for index in range(8)
        ]
    )

    assert tool.calls == 3
    assert agent.runtime_tool_call_count == 3
    assert sum(1 for result in results if result.startswith("ok:")) == 3
    budget_errors = [
        result
        for result in results
        if result == "Error: AgentRun budget exceeded: max_tool_calls=3"
    ]
    assert len(budget_errors) == 5
    end_events = [
        event for event in agent.events if event.event_type == AgentEventType.TOOL_CALL_END
    ]
    assert len(end_events) == 8
    denied_events = [
        event
        for event in end_events
        if event.tool_result == "Error: AgentRun budget exceeded: max_tool_calls=3"
    ]
    assert len(denied_events) == 5
    for event in denied_events:
        diagnostics = event.data["meta"]["tool_diagnostics"]
        assert diagnostics[0]["code"] == "runtime_budget_exceeded"


def test_tool_executor_budget_rejection_emits_tool_end_diagnostic() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    class EventAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CountingTool())
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

    agent = EventAgent()
    agent.runtime_budget = {"max_tool_calls": 1}
    agent.runtime_tool_call_count = 1

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_2", name="read_file", arguments={})
    )

    assert result == "Error: AgentRun budget exceeded: max_tool_calls=1"
    end_events = [
        event for event in agent.events if event.event_type == AgentEventType.TOOL_CALL_END
    ]
    assert len(end_events) == 1
    assert end_events[0].tool_name == "read_file"
    assert end_events[0].tool_call_id == "call_2"
    assert end_events[0].tool_result == result
    diagnostics = end_events[0].data["meta"]["tool_diagnostics"]
    assert diagnostics[0]["code"] == "runtime_budget_exceeded"


def test_tool_executor_blocks_side_effects_after_runtime_timeout() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            return "ok"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    tool = CountingTool()
    agent = _AgentStub(tool)
    agent.runtime_budget = {"timeout_sec": 1}
    agent.runtime_timeout_sec = 1
    agent.runtime_deadline = time.monotonic() - 0.1
    executor = ToolExecutor(agent)

    result = executor.execute(ToolCall(id="call_1", name="read_file", arguments={}))

    assert result == "Error: AgentRun budget exceeded: timeout_sec=1"
    assert tool.calls == 0


def test_tool_executor_parallel_blocks_all_side_effects_after_runtime_timeout() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            return "ok"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    class EventAgent(_AgentStub):
        def __init__(self, tool) -> None:
            super().__init__(tool)
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

    tool = CountingTool()
    agent = EventAgent(tool)
    agent.runtime_budget = {"timeout_sec": 1}
    agent.runtime_timeout_sec = 1
    agent.runtime_deadline = time.monotonic() - 0.1

    results = ToolExecutor(agent).execute_parallel(
        [
            ToolCall(id="call_1", name="read_file", arguments={}),
            ToolCall(id="call_2", name="read_file", arguments={}),
        ]
    )

    assert results == [
        "Error: AgentRun budget exceeded: timeout_sec=1",
        "Error: AgentRun budget exceeded: timeout_sec=1",
    ]
    assert tool.calls == 0
    end_events = [
        event for event in agent.events if event.event_type == AgentEventType.TOOL_CALL_END
    ]
    end_events_by_call_id = {event.tool_call_id: event for event in end_events}
    assert set(end_events_by_call_id) == {"call_1", "call_2"}
    for event in end_events:
        diagnostics = event.data["meta"]["tool_diagnostics"]
        assert diagnostics[0]["code"] == "runtime_budget_exceeded"


def test_tool_executor_reports_budget_timeout_when_tool_finishes_after_deadline() -> None:
    class SlowTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            time.sleep(0.05)
            return "late success"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    class EventAgent(_AgentStub):
        def __init__(self, tool) -> None:
            super().__init__(tool)
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

    tool = SlowTool()
    agent = EventAgent(tool)
    agent.runtime_budget = {"timeout_sec": 1}
    agent.runtime_timeout_sec = 1
    agent.runtime_deadline = time.monotonic() + 0.01

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_1", name="read_file", arguments={})
    )

    assert tool.calls == 1
    assert result == "Error: AgentRun budget exceeded: timeout_sec=1"
    end_events = [
        event for event in agent.events if event.event_type == AgentEventType.TOOL_CALL_END
    ]
    assert end_events[-1].tool_result == result


def test_tool_executor_blocks_side_effects_after_token_budget() -> None:
    class CountingTool:
        name = "read_file"
        description = "Read"
        parameters = {}

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            return "ok"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    tool = CountingTool()
    agent = _AgentStub(tool)
    agent.runtime_budget = {"token_budget": 10}
    agent.state.total_prompt_tokens = 6
    agent.state.total_completion_tokens = 4
    executor = ToolExecutor(agent)

    result = executor.execute(ToolCall(id="call_1", name="read_file", arguments={}))

    assert result == "Error: AgentRun budget exceeded: token_budget=10"
    assert tool.calls == 0


def test_apply_patch_missing_required_arguments_does_not_raise_from_preflight() -> None:
    agent = _AgentStub(ApplyPatchTool())
    executor = ToolExecutor(agent)

    result = executor.execute(ToolCall(id="call_5", name="apply_patch", arguments={}))

    assert result.startswith("Error: bad arguments for apply_patch: invalid arguments")
    assert "$.patch: expected string, got missing" in result


def test_provider_argument_error_returns_tool_error_before_execution() -> None:
    tool = SimpleNamespace(
        name="apply_patch",
        parameters={"type": "object", "required": ["patch"]},
        execute=lambda **kwargs: "should not execute",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(
            id="call_6",
            name="apply_patch",
            arguments={},
            argument_error="missing tool arguments",
        )
    )

    assert result == "Error: bad arguments for apply_patch: missing tool arguments"


def test_tool_executor_repairs_deepseek_bare_string_array_before_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    tool = _CaptureTool()
    agent = _AgentStub(tool)
    agent.llm = SimpleNamespace(
        provider_id="deepseek",
        provider_type="openai_chat",
        provider_config=SimpleNamespace(compat="deepseek"),
        model="deepseek-v4-pro",
    )
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(id="call_7", name="mcp_batch", arguments={"paths": "demo.md"})
    )

    assert result == "ok"
    assert tool.received == {"paths": ["demo.md"]}


def test_tool_executor_does_not_persist_clean_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    tool = _CaptureTool()
    agent = _AgentStub(tool)
    agent.llm = SimpleNamespace(
        provider_id="openai",
        provider_type="openai_chat",
        provider_config=SimpleNamespace(compat="generic"),
        model="gpt-demo",
    )
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(id="call_clean", name="mcp_batch", arguments={"paths": ["demo.md"]})
    )

    assert result == "ok"
    assert not (tmp_path / ".rcoder" / "diagnostics" / "tool_diagnostics.jsonl").exists()


def test_tool_executor_respects_disabled_tool_argument_telemetry(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    tool = _CaptureTool()
    agent = _AgentStub(tool)
    agent.llm = SimpleNamespace(
        provider_id="deepseek",
        provider_type="openai_chat",
        provider_config=SimpleNamespace(compat="deepseek"),
        model="deepseek-v4-pro",
    )
    agent.config = SimpleNamespace(
        diagnostics=SimpleNamespace(
            tool_diagnostics=SimpleNamespace(
                enabled=False,
                record_clean=False,
            )
        )
    )
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(id="call_disabled", name="mcp_batch", arguments={"paths": "{}"})
    )

    assert result.startswith("Error: bad arguments for mcp_batch")
    assert not (tmp_path / ".rcoder" / "diagnostics" / "tool_diagnostics.jsonl").exists()


def test_tool_executor_keeps_unrepairable_placeholder_as_tool_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    tool = _CaptureTool()
    agent = _AgentStub(tool)
    agent.llm = SimpleNamespace(
        provider_id="deepseek",
        provider_type="openai_chat",
        provider_config=SimpleNamespace(compat="deepseek"),
        model="deepseek-v4-pro",
    )
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(id="call_8", name="mcp_batch", arguments={"paths": "{}"})
    )

    assert result.startswith("Error: bad arguments for mcp_batch")
    assert "$.paths: expected array, got string" in result
    assert tool.received is None


def test_tool_executor_blocks_tools_outside_effective_capabilities() -> None:
    tool = SimpleNamespace(
        name="apply_patch",
        parameters={"type": "object", "properties": {}},
        execute=lambda **kwargs: "should not execute",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.evaluate_tool_permission = lambda _tool, tool_call=None: PermissionDecision(
        action=PermissionAction.DENY,
        authorized=False,
        reason=(
            "builtin_tool 'apply_patch' is not authorized by this Agent's "
            "effective_capabilities"
        ),
    )
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(id="call_blocked", name="apply_patch", arguments={})
    )

    assert result == (
        "Error: tool 'apply_patch' denied by permission gateway: builtin_tool "
        "'apply_patch' is not authorized by this Agent's effective_capabilities"
    )


def test_tool_executor_permission_denied_lifecycle_feedback_enters_user_error() -> None:
    tool = SimpleNamespace(
        name="apply_patch",
        parameters={"type": "object", "properties": {}},
        execute=lambda **kwargs: "should not execute",
        preflight_validate=lambda **kwargs: None,
    )

    class RecordingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(tool)
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason="apply_patch denied by policy",
                policy_matched="effective_capabilities",
                audit={
                    "permission_denied_lifecycle": [
                        {
                            "hook_id": "hook:permission-denied-feedback",
                            "user_message": "Use read_file or ask for apply_patch capability.",
                            "diagnostics": [
                                {"code": "recoverable_permission_denied"}
                            ],
                        }
                    ]
                },
            )

    agent = RecordingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_blocked", name="apply_patch", arguments={})
    )

    assert result == (
        "Error: tool 'apply_patch' denied by permission gateway: "
        "apply_patch denied by policy\n"
        "Permission feedback: Use read_file or ask for apply_patch capability."
    )
    tool_events = [
        event
        for event in agent.events
        if event.event_type == AgentEventType.TOOL_CALL_END
    ]
    diagnostics = tool_events[0].data["meta"]["tool_diagnostics"]
    permission_audit = diagnostics[0]["metadata"]["permission"]["audit"]
    assert permission_audit["permission_denied_lifecycle"][0]["hook_id"] == (
        "hook:permission-denied-feedback"
    )


def test_tool_executor_permission_deny_emits_end_only_with_index_and_diagnostics() -> None:
    tool = SimpleNamespace(
        name="apply_patch",
        parameters={"type": "object", "properties": {}},
        execute=lambda **kwargs: "should not execute",
        preflight_validate=lambda **kwargs: None,
    )

    class RecordingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(tool)
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason="apply_patch denied by policy",
                policy_matched="effective_capabilities",
            )

    agent = RecordingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_blocked", name="apply_patch", arguments={}),
        index=2,
    )

    assert result == (
        "Error: tool 'apply_patch' denied by permission gateway: "
        "apply_patch denied by policy"
    )
    tool_events = [
        event
        for event in agent.events
        if event.event_type in {
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
        }
    ]
    assert [(event.event_type, event.data.get("index")) for event in tool_events] == [
        (AgentEventType.TOOL_CALL_END, 2),
    ]
    diagnostics = tool_events[0].data["meta"]["tool_diagnostics"]
    assert diagnostics[0]["code"] == "permission_deny"
    assert diagnostics[0]["severity"] == "error"


def test_tool_executor_preflight_failure_emits_end_only_with_index_and_diagnostics() -> None:
    class PreflightTool:
        name = "read_file"
        description = "Read a file"
        parameters = {"type": "object", "properties": {}}
        tool_source = "builtin"

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "should not execute"

        def preflight_validate(self, **kwargs) -> str:  # noqa: ARG002
            return "path must stay inside workspace"

    class RecordingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(PreflightTool())
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            raise AssertionError("preflight failures should stop before permission")

    agent = RecordingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_preflight", name="read_file", arguments={}),
        index=3,
    )

    assert result == "path must stay inside workspace"
    tool_events = [
        event
        for event in agent.events
        if event.event_type in {
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
        }
    ]
    assert [(event.event_type, event.data.get("index")) for event in tool_events] == [
        (AgentEventType.TOOL_CALL_END, 3),
    ]
    diagnostics = tool_events[0].data["meta"]["tool_diagnostics"]
    assert diagnostics[0]["code"] == "preflight_failed"
    assert diagnostics[0]["severity"] == "error"


def test_tool_executor_approval_deny_emits_end_only_with_index_and_failure_meta() -> None:
    class CaptureTool:
        name = "apply_patch"
        description = "Write a file"
        parameters = {"type": "object", "properties": {}}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def request_approval(self, request):  # noqa: ARG002
            return ApprovalDecision.deny_once("denied by reviewer")

    class RecordingAgent(_AgentStub):
        def __init__(self) -> None:
            self.tool = CaptureTool()
            super().__init__(self.tool)
            self.approval_provider = ApprovalProvider()
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.REQUIRE_APPROVAL,
                authorized=True,
                reason="apply_patch requires approval",
            )

    agent = RecordingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_approval", name="apply_patch", arguments={}),
        index=4,
    )

    assert result == "denied by reviewer"
    assert agent.tool.executed is False
    tool_events = [
        event
        for event in agent.events
        if event.event_type in {
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
        }
    ]
    assert [(event.event_type, event.data.get("index")) for event in tool_events] == [
        (AgentEventType.TOOL_CALL_END, 4),
    ]
    meta = tool_events[0].data["meta"]
    assert meta["failure_kind"] == "approval_denied"
    assert meta["tool_diagnostics"][0]["code"] == "approval_denied"


def test_tool_executor_applies_pre_tool_lifecycle_update_then_reevaluates_permission() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            if context.event_name != "PreToolUse":
                return []
            updated_input = dict(context.payload)
            updated_input["tool_call"] = {
                "id": "call-transformed",
                "name": "apply_patch",
                "arguments": {},
            }
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(updated_input=updated_input),
                )
            ]

    class LifecycleAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            self.permission_tool_call_ids: list[str] = []
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_tool_call_ids.append(getattr(tool_call, "id", ""))
            if getattr(tool, "name", "") == "apply_patch":
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="apply_patch denied after lifecycle update",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == (
        "Error: tool 'apply_patch' denied by permission gateway: "
        "apply_patch denied after lifecycle update"
    )
    assert agent.lifecycle_dispatcher.contexts[0].event_name == "PreToolUse"
    assert (
        agent.lifecycle_dispatcher.contexts[0].payload["technical"]["tool_call"]["name"]
        == "read_file"
    )
    assert agent.lifecycle_dispatcher.contexts[0].payload["tool_names"] == ["read_file"]
    assert agent.lifecycle_dispatcher.contexts[0].payload["tool_call_ids"] == ["call-original"]
    assert agent.lifecycle_dispatcher.contexts[0].payload["tool_sources"] == ["builtin"]
    assert agent.lifecycle_dispatcher.contexts[0].payload["mcp_servers"] == []
    assert agent.permission_tool_call_ids == ["call-original"]
    for legacy_field in (
        "tool_" + "name",
        "tool_" + "call_id",
        "tool_" + "source",
        "mcp_" + "server",
    ):
        assert legacy_field not in agent.lifecycle_dispatcher.contexts[0].payload


def test_tool_executor_does_not_apply_permission_before_pre_tool_lifecycle_update() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return f"executed:{self.name}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        updated_input={
                            "tool_call": {
                                "id": "call-transformed",
                                "name": "read_file",
                                "arguments": {},
                            }
                        }
                    ),
                )
            ]

    class LifecycleAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("apply_patch"))
            self._tools = {
                "apply_patch": CaptureTool("apply_patch"),
                "read_file": CaptureTool("read_file"),
            }
            self.permission_checks = []
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            tool_name = getattr(tool, "name", "")
            self.permission_checks.append(tool_name)
            if tool_name == "apply_patch":
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="apply_patch denied before lifecycle update",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="apply_patch", arguments={})
    )

    assert result == "executed:read_file"
    assert agent.permission_checks == ["read_file"]
    assert agent._tools["apply_patch"].executed is False
    assert agent._tools["read_file"].executed is True


def test_tool_executor_blocks_pre_tool_lifecycle_defer_without_execution() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="defer",
                        reason="read_file deferred by lifecycle",
                    ),
                )
            ]

    tool = CaptureTool()
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-defer", name="read_file", arguments={})
    )

    assert result == "read_file deferred by lifecycle"
    assert tool.executed is False


def test_tool_executor_blocks_pre_tool_lifecycle_continue_flow_false_without_execution() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        continue_flow=False,
                        reason="read_file stopped by lifecycle",
                    ),
                )
            ]

    tool = CaptureTool()
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-stop", name="read_file", arguments={})
    )

    assert result == "read_file stopped by lifecycle"
    assert tool.executed is False


def test_tool_executor_blocks_pre_tool_lifecycle_ask_without_execution() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="ask",
                        reason="read_file requires lifecycle review",
                    ),
                )
            ]

    tool = CaptureTool()
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-ask", name="read_file", arguments={})
    )

    assert result == "read_file requires lifecycle review"
    assert tool.executed is False


def test_tool_executor_routes_pre_tool_lifecycle_ask_through_approval_provider() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "executed"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("approved")

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="ask",
                        reason="read_file requires lifecycle review",
                    ),
                )
            ]

    tool = CaptureTool()
    agent = _AgentStub(tool)
    agent.approval_provider = ApprovalProvider()
    agent.permission_interactive = True
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-ask", name="read_file", arguments={})
    )

    assert result == "executed"
    assert tool.executed is True
    assert len(agent.approval_provider.requests) == 1
    request = agent.approval_provider.requests[0]
    assert request.tool_name == "read_file"
    assert request.tool_args == {}
    assert request.tool_source == "lifecycle_hook"
    assert request.reason == "read_file requires lifecycle review"
    assert request.metadata["lifecycle_event"] == "PreToolUse"


def test_tool_executor_pre_tool_lifecycle_ask_approval_preserves_all_hook_identity() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "executed"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("approved")

    def declaration(hook_id: str, display_name: str) -> LifecycleHookDeclaration:
        return LifecycleHookDeclaration.from_dict(
            hook_id,
            {
                "event": "PreToolUse",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "command",
                "display_name": display_name,
                "summary": f"{display_name} summary.",
                "permissions": [],
                "trust": "trusted",
            },
        )

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.first = declaration("hook:review-risk", "Review risk")
            self.second = declaration("hook:review-owner", "Review owner")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.first,
                    output=LifecycleHookOutput(
                        decision="ask",
                        reason="risk team review required",
                    ),
                ),
                LifecycleHookDispatchResult(
                    declaration=self.second,
                    output=LifecycleHookOutput(
                        decision="ask",
                        user_message="owner review required",
                    ),
                ),
            ]

    tool = CaptureTool()
    agent = _AgentStub(tool)
    agent.approval_provider = ApprovalProvider()
    agent.permission_interactive = True
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-ask", name="read_file", arguments={})
    )

    assert result == "executed"
    assert tool.executed is True
    request = agent.approval_provider.requests[0]
    assert request.reason == "risk team review required\nowner review required"
    assert request.metadata["lifecycle_event"] == "PreToolUse"
    assert request.metadata["lifecycle_hooks"] == [
        {
            "hook_id": "hook:review-risk",
            "display_name": "Review risk",
            "handler_type": "command",
            "reason": "risk team review required",
        },
        {
            "hook_id": "hook:review-owner",
            "display_name": "Review owner",
            "handler_type": "command",
            "reason": "owner review required",
        },
    ]


def test_tool_executor_blocks_background_pre_tool_lifecycle_ask_as_review() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def request_approval(self, request):  # noqa: ARG002
            raise AssertionError("background lifecycle ask must not prompt")

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="ask",
                        reason="read_file requires lifecycle review",
                    ),
                )
            ]

    tool = CaptureTool()
    agent = _AgentStub(tool)
    agent.approval_provider = ApprovalProvider()
    agent.permission_interactive = False
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-ask", name="read_file", arguments={})
    )

    assert result == (
        "Error: tool 'read_file' blocked pending review: "
        "read_file requires lifecycle review"
    )
    assert tool.executed is False


def test_tool_executor_pre_tool_lifecycle_background_ask_audits_hook_identity() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "executed"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def request_approval(self, request):  # noqa: ARG002
            raise AssertionError("background lifecycle ask must not prompt")

    def declaration(hook_id: str, display_name: str) -> LifecycleHookDeclaration:
        return LifecycleHookDeclaration.from_dict(
            hook_id,
            {
                "event": "PreToolUse",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "command",
                "display_name": display_name,
                "summary": f"{display_name} summary.",
                "permissions": [],
                "trust": "trusted",
            },
        )

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.first = declaration("hook:review-risk", "Review risk")
            self.second = declaration("hook:review-owner", "Review owner")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.first,
                    output=LifecycleHookOutput(
                        decision="ask",
                        reason="risk team review required",
                    ),
                ),
                LifecycleHookDispatchResult(
                    declaration=self.second,
                    output=LifecycleHookOutput(
                        decision="ask",
                        user_message="owner review required",
                    ),
                ),
            ]

    class EventAgent(_AgentStub):
        def __init__(self, tool) -> None:
            super().__init__(tool)
            self.events = []

        def _emit_event(self, event) -> None:
            self.events.append(event)

    tool = CaptureTool()
    agent = EventAgent(tool)
    agent.approval_provider = ApprovalProvider()
    agent.permission_interactive = False
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-ask", name="read_file", arguments={})
    )

    assert result == (
        "Error: tool 'read_file' blocked pending review: "
        "risk team review required\nowner review required"
    )
    assert tool.executed is False
    end_events = [
        event for event in agent.events if event.event_type == AgentEventType.TOOL_CALL_END
    ]
    diagnostics = end_events[0].data["meta"]["tool_diagnostics"]
    assert diagnostics[0]["code"] == "permission_blocked_review"
    assert diagnostics[0]["metadata"]["permission"]["audit"]["lifecycle_event"] == "PreToolUse"
    assert diagnostics[0]["metadata"]["permission"]["audit"]["lifecycle_hooks"] == [
        {
            "hook_id": "hook:review-risk",
            "display_name": "Review risk",
            "handler_type": "command",
            "reason": "risk team review required",
        },
        {
            "hook_id": "hook:review-owner",
            "display_name": "Review owner",
            "handler_type": "command",
            "reason": "owner review required",
        },
    ]


def test_tool_executor_pre_tool_lifecycle_update_is_visible_to_next_matching_hook() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return f"executed:{self.name}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    declarations = [
        LifecycleHookDeclaration.from_dict(
            "hook:rewrite",
            {
                "event": "PreToolUse",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "mcp_tool",
                "handler_ref": "rewrite",
                "matcher": "*",
                "display_name": "Rewrite tool",
                "summary": "Rewrite the tool call.",
                "permissions": [],
                "trust": "trusted",
            },
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:observe-read",
            {
                "event": "PreToolUse",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "mcp_tool",
                "handler_ref": "observe",
                "matcher": {"tool_names": "read_file"},
                "display_name": "Observe read",
                "summary": "Observe the rewritten tool call.",
                "permissions": [],
                "trust": "trusted",
            },
        ),
    ]
    seen_tool_names: list[list[str]] = []

    def handler(declaration, context):
        if declaration.id == "hook:rewrite":
            return LifecycleHookOutput(
                updated_input={
                    "tool_call": {
                        "id": "call-transformed",
                        "name": "read_file",
                        "arguments": {},
                    }
                }
            )
        seen_tool_names.append(list(context.payload["tool_names"]))
        return LifecycleHookOutput(decision="allow")

    class LifecycleAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("apply_patch"))
            self._tools = {
                "apply_patch": CaptureTool("apply_patch"),
                "read_file": CaptureTool("read_file"),
            }
            self.lifecycle_dispatcher = LifecycleHookDispatcher(
                LifecycleHookRegistry(declarations),
                runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
                    FunctionLifecycleHookRuntimeAdapter(
                        handler_type="mcp_tool",
                        handler=handler,
                    )
                ]),
            )

        def get_tool(self, name: str):
            return self._tools.get(name)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="apply_patch", arguments={})
    )

    assert result == "executed:read_file"
    assert seen_tool_names == [["read_file"]]
    assert agent._tools["apply_patch"].executed is False
    assert agent._tools["read_file"].executed is True


def test_tool_executor_emits_canonical_start_after_pre_tool_lifecycle_update() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed_arguments = None

        def execute(self, **kwargs) -> str:
            self.executed_arguments = kwargs
            return f"executed:{self.name}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        updated_input={
                            "tool_call": {
                                "id": "call-transformed",
                                "name": "apply_patch",
                                "arguments": {"path": "after.txt"},
                            }
                        }
                    ),
                )
            ]

    class LifecycleAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self.events = []
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _emit_event(self, event) -> None:
            self.events.append(event)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={"path": "before.txt"}),
        index=7,
    )

    assert result == "executed:apply_patch"
    tool_events = [
        event for event in agent.events
        if event.event_type in {
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
        }
    ]
    assert [(event.event_type, event.tool_name, event.tool_call_id) for event in tool_events] == [
        (AgentEventType.TOOL_CALL_START, "apply_patch", "call-original"),
        (AgentEventType.TOOL_CALL_END, "apply_patch", "call-original"),
    ]
    assert tool_events[0].tool_args == {"path": "after.txt"}
    assert tool_events[0].data["index"] == 7
    assert agent._tools["apply_patch"].executed_arguments == {"path": "after.txt"}


def test_tool_executor_emits_lifecycle_hook_observation_events() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Read a file"
        parameters = {}
        tool_source = "builtin"

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "ok"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="allow",
                        reason="Tool call reviewed.",
                        user_message="Tool call approved by lifecycle review.",
                        artifacts=[{"kind": "review", "id": "artifact-1"}],
                    ),
                )
            ]

    class LifecycleAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool())
            self.events = []
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def _emit_event(self, event) -> None:
            self.events.append(event)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-read", name="read_file", arguments={})
    )

    assert result == "ok"
    lifecycle_events = [
        event for event in agent.events
        if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert [event.data["phase"] for event in lifecycle_events[:2]] == [
        "dispatch_start",
        "result",
    ]
    assert lifecycle_events[0].data["event_name"] == "PreToolUse"
    assert lifecycle_events[1].data["hook_id"] == "hook:PreToolUse"
    assert lifecycle_events[1].data["decision"] == "allow"
    assert lifecycle_events[1].data["reason"] == "Tool call reviewed."
    assert (
        lifecycle_events[1].data["user_message"]
        == "Tool call approved by lifecycle review."
    )
    assert lifecycle_events[1].data["artifacts"] == [
        {"kind": "review", "id": "artifact-1"}
    ]


def test_tool_executor_dispatches_post_tool_lifecycle_with_result_payload() -> None:
    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            return []

    tool = SimpleNamespace(
        name="read_file",
        parameters={},
        tool_source="builtin",
        execute=lambda **kwargs: "file content",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-read", name="read_file", arguments={})
    )

    assert result == "file content"
    assert [context.event_name for context in agent.lifecycle_dispatcher.contexts] == [
        "PreToolUse",
        "PostToolUse",
    ]
    post_context = agent.lifecycle_dispatcher.contexts[1]
    assert post_context.payload["technical"]["tool_call"]["name"] == "read_file"
    assert post_context.payload["tool_names"] == ["read_file"]
    assert post_context.payload["tool_call_ids"] == ["call-read"]
    assert post_context.payload["tool_sources"] == ["builtin"]
    assert post_context.payload["mcp_servers"] == []
    assert post_context.payload["technical"]["result"] == "file content"


def test_tool_executor_lifecycle_context_populates_authoritative_fields() -> None:
    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            return []

    tool = SimpleNamespace(
        name="read_file",
        parameters={},
        tool_source="builtin",
        execute=lambda **kwargs: f"file {kwargs.get('file_path', '')}",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()
    agent.current_session_id = "session-1"
    agent.runtime_agent_run_id = "agent-run-1"
    agent.runtime_turn_id = "turn-1"
    agent.permission_trigger_source = "taskflow"
    agent.locale = "zh-CN"

    results = ToolExecutor(agent).execute_parallel(
        [
            ToolCall(id="call-a", name="read_file", arguments={"file_path": "a.txt"}),
            ToolCall(id="call-b", name="read_file", arguments={"file_path": "b.txt"}),
        ]
    )

    assert results == ["file a.txt", "file b.txt"]
    assert {context.event_name for context in agent.lifecycle_dispatcher.contexts} == {
        "PreToolUse",
        "PostToolUse",
        "PostToolBatch",
    }
    for context in agent.lifecycle_dispatcher.contexts:
        assert context.session_run_id == "session-1"
        assert context.agent_run_id == "agent-run-1"
        assert context.turn_id == "turn-1"
        assert context.source == "taskflow"
        assert context.origin == "agent"
        assert context.locale == "zh-CN"
        assert context.placement == "server"
        assert context.timestamp
        assert context.metadata["round_index"] == 0
        assert context.payload["session_run_id"] == "session-1"
        assert context.payload["agent_run_id"] == "agent-run-1"
        assert context.payload["turn_id"] == "turn-1"
        assert context.payload["trigger_source"] == "taskflow"
        assert context.payload["timestamp"] == context.timestamp


def test_tool_executor_post_tool_lifecycle_failure_preserves_result_and_emits_diagnostic() -> None:
    calls: list[str] = []
    declarations = [
        LifecycleHookDeclaration.from_dict(
            "hook:broken-observer",
            {
                "event": "PostToolUse",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "mcp_tool",
                "handler_ref": "broken-observer",
                "matcher": "*",
                "permissions": [],
                "display_name": "Broken observer",
                "summary": "Fails while observing a tool result.",
                "trust": "trusted",
            },
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:second-observer",
            {
                "event": "PostToolUse",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "mcp_tool",
                "handler_ref": "second-observer",
                "matcher": "*",
                "permissions": [],
                "display_name": "Second observer",
                "summary": "Runs after a failed observer.",
                "trust": "trusted",
            },
        ),
    ]

    def lifecycle_handler(declaration, _context):
        calls.append(declaration.id)
        if declaration.id == "hook:broken-observer":
            raise RuntimeError("observer unavailable")
        return LifecycleHookOutput.from_dict({"diagnostics": [{"code": "second"}]})

    class CaptureAgent(_AgentStub):
        def __init__(self, tool) -> None:
            super().__init__(tool)
            self.events = []
            self.lifecycle_dispatcher = LifecycleHookDispatcher(
                LifecycleHookRegistry(declarations),
                runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
                    FunctionLifecycleHookRuntimeAdapter(
                        handler_type="mcp_tool",
                        handler=lifecycle_handler,
                    )
                ]),
            )

        def _emit_event(self, event) -> None:
            self.events.append(event)

    tool = SimpleNamespace(
        name="read_file",
        parameters={},
        tool_source="builtin",
        execute=lambda **kwargs: "file content",
        preflight_validate=lambda **kwargs: None,
    )
    agent = CaptureAgent(tool)

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-read", name="read_file", arguments={})
    )

    assert result == "file content"
    assert calls == ["hook:broken-observer", "hook:second-observer"]
    post_results = [
        event.data
        for event in agent.events
        if event.event_type == AgentEventType.LIFECYCLE_HOOK
        and event.data["event_name"] == "PostToolUse"
        and event.data["phase"] == "result"
    ]
    assert [event["hook_id"] for event in post_results] == [
        "hook:broken-observer",
        "hook:second-observer",
    ]
    assert post_results[0]["continue_flow"] is True
    assert post_results[0]["decision"] == "none"
    assert post_results[0]["diagnostics"][0]["code"] == "lifecycle_hook_failed_open"
    assert post_results[0]["diagnostics"][0]["failure_policy"] == "fail_open"
    assert post_results[1]["diagnostics"] == [{"code": "second"}]


def test_tool_executor_dispatches_post_tool_failure_lifecycle_on_execution_error() -> None:
    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            return []

    def _raise_error(**kwargs) -> str:  # noqa: ARG001
        raise RuntimeError("boom")

    tool = SimpleNamespace(
        name="read_file",
        parameters={},
        tool_source="builtin",
        execute=_raise_error,
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-read", name="read_file", arguments={})
    )

    assert result == "Error executing read_file: boom"
    assert [context.event_name for context in agent.lifecycle_dispatcher.contexts] == [
        "PreToolUse",
        "PostToolUseFailure",
    ]
    failure_context = agent.lifecycle_dispatcher.contexts[1]
    assert failure_context.payload["technical"]["tool_call"]["name"] == "read_file"
    assert failure_context.payload["tool_names"] == ["read_file"]
    assert failure_context.payload["tool_call_ids"] == ["call-read"]
    assert failure_context.payload["tool_sources"] == ["builtin"]
    assert failure_context.payload["mcp_servers"] == []
    assert failure_context.payload["technical"]["error"]["message"] == "boom"
    assert failure_context.payload["technical"]["error"]["type"] == "RuntimeError"


def test_tool_executor_post_tool_failure_context_populates_authoritative_fields() -> None:
    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            return []

    def _raise_error(**kwargs) -> str:  # noqa: ARG001
        raise RuntimeError("boom")

    tool = SimpleNamespace(
        name="read_file",
        parameters={},
        tool_source="builtin",
        execute=_raise_error,
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()
    agent.current_session_id = "session-1"
    agent.runtime_agent_run_id = "agent-run-1"
    agent.runtime_turn_id = "turn-1"
    agent.permission_trigger_source = "taskflow"
    agent.locale = "zh-CN"

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-read", name="read_file", arguments={})
    )

    assert result == "Error executing read_file: boom"
    failure_context = agent.lifecycle_dispatcher.contexts[1]
    assert failure_context.event_name == "PostToolUseFailure"
    assert failure_context.session_run_id == "session-1"
    assert failure_context.agent_run_id == "agent-run-1"
    assert failure_context.turn_id == "turn-1"
    assert failure_context.source == "taskflow"
    assert failure_context.origin == "agent"
    assert failure_context.locale == "zh-CN"
    assert failure_context.placement == "server"
    assert failure_context.timestamp
    assert failure_context.metadata["round_index"] == 0
    assert failure_context.payload["session_run_id"] == "session-1"
    assert failure_context.payload["agent_run_id"] == "agent-run-1"
    assert failure_context.payload["turn_id"] == "turn-1"
    assert failure_context.payload["trigger_source"] == "taskflow"
    assert failure_context.payload["timestamp"] == failure_context.timestamp


def test_tool_executor_dispatches_post_tool_batch_lifecycle_after_parallel_execution() -> None:
    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            return []

    tool = SimpleNamespace(
        name="read_file",
        parameters={},
        tool_source="builtin",
        execute=lambda **kwargs: f"file {kwargs.get('file_path', '')}",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.lifecycle_dispatcher = LifecycleDispatcher()

    results = ToolExecutor(agent).execute_parallel(
        [
            ToolCall(id="call-a", name="read_file", arguments={"file_path": "a.txt"}),
            ToolCall(id="call-b", name="read_file", arguments={"file_path": "b.txt"}),
        ]
    )

    assert results == ["file a.txt", "file b.txt"]
    assert agent.lifecycle_dispatcher.contexts[-1].event_name == "PostToolBatch"
    batch_payload = agent.lifecycle_dispatcher.contexts[-1].payload
    assert [item["id"] for item in batch_payload["technical"]["tool_calls"]] == ["call-a", "call-b"]
    assert batch_payload["technical"]["results"] == ["file a.txt", "file b.txt"]
    assert batch_payload["event_name"] == "PostToolBatch"
    assert batch_payload["placement"] == "server"
    assert batch_payload["tool_names"] == ["read_file", "read_file"]
    assert batch_payload["tool_call_ids"] == ["call-a", "call-b"]
    assert batch_payload["tool_sources"] == ["builtin", "builtin"]
    assert batch_payload["mcp_servers"] == []
    assert batch_payload["trigger_source"] == "chat"


def test_tool_executor_post_tool_batch_uses_lifecycle_transformed_tool_calls() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            return f"executed:{self.name}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")
            self.batch_payloads: list[dict] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if (
                context.event_name == "PreToolUse"
                and context.payload.get("tool_names") == ["read_file"]
            ):
                return [
                    LifecycleHookDispatchResult(
                        declaration=self.declaration,
                        output=LifecycleHookOutput.from_dict(
                            {
                                "updated_input": {
                                    "tool_call": {
                                        "id": "call-read",
                                        "name": "apply_patch",
                                        "arguments": {},
                                    }
                                }
                            }
                        ),
                    )
                ]
            if context.event_name == "PostToolBatch":
                self.batch_payloads.append(context.payload)
            return []

    class MultiToolAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
                "noop": CaptureTool("noop"),
            }
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

    agent = MultiToolAgent()

    results = ToolExecutor(agent).execute_parallel(
        [
            ToolCall(id="call-read", name="read_file", arguments={}),
            ToolCall(id="call-noop", name="noop", arguments={}),
        ]
    )

    assert results == ["executed:apply_patch", "executed:noop"]
    assert agent._tools["read_file"].calls == 0
    assert agent._tools["apply_patch"].calls == 1
    batch_payload = agent.lifecycle_dispatcher.batch_payloads[-1]
    assert batch_payload["tool_names"] == ["apply_patch", "noop"]
    assert batch_payload["technical"]["tool_calls"] == [
        {"id": "call-read", "name": "apply_patch", "arguments": {}},
        {"id": "call-noop", "name": "noop", "arguments": {}},
    ]


def test_tool_executor_reevaluates_permission_after_tool_call_transform() -> None:
    class CaptureTool:
        description = "Write a file"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            self.permission_tool_call_ids: list[str] = []
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_tool_call_ids.append(getattr(tool_call, "id", ""))
            if getattr(tool, "name", "") == "apply_patch":
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="apply_patch denied after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    result = ToolExecutor(TransformingAgent()).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert (
        result
        == "Error: tool 'apply_patch' denied by permission gateway: apply_patch denied after transform"
    )


def test_tool_executor_pre_tool_allow_cannot_bypass_permission_gateway() -> None:
    class CaptureTool:
        name = "apply_patch"
        description = "Write"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.calls += 1
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")
            self.contexts: list[LifecycleHookEventContext] = []

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            if context.event_name == "PreToolUse":
                return [
                    LifecycleHookDispatchResult(
                        declaration=self.declaration,
                        output=LifecycleHookOutput.from_dict({"decision": "allow"}),
                    )
                ]
            return []

    class DenyingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool())
            self.lifecycle_dispatcher = LifecycleDispatcher()
            self.permission_tool_call_ids: list[str] = []

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_tool_call_ids.append(getattr(tool_call, "id", ""))
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason="permission gateway denied apply_patch",
            )

    agent = DenyingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-write", name="apply_patch", arguments={})
    )

    assert result == (
        "Error: tool 'apply_patch' denied by permission gateway: "
        "permission gateway denied apply_patch"
    )
    assert agent._tool.calls == 0
    assert agent.permission_tool_call_ids == ["call-write"]
    assert [context.event_name for context in agent.lifecycle_dispatcher.contexts] == [
        "PreToolUse"
    ]


def test_legacy_tool_transform_cannot_rewrite_provider_tool_call_id() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "executed"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            self.permission_tool_call_ids: list[str] = []
            self.end_tool_call_ids: list[str] = []
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_tool_call_ids.append(getattr(tool_call, "id", ""))
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

        def _emit_event(self, event) -> None:
            if event.event_type == AgentEventType.TOOL_CALL_END:
                self.end_tool_call_ids.append(event.tool_call_id or "")

    agent = TransformingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "executed"
    assert agent.permission_tool_call_ids == ["call-original"]
    assert agent.end_tool_call_ids == ["call-original"]


def test_tool_executor_evaluates_permission_after_in_place_argument_transform() -> None:
    class CaptureTool:
        name = "read_file"
        description = "Read a file"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            self.tool = CaptureTool()
            super().__init__(self.tool)
            self.permission_checks = []
            _install_legacy_before_tool_transform(self, self._transform)

        def _transform(self, _point, ctx):
            ctx.tool_call.arguments["file_path"] = "/private/secret.txt"
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            file_path = str((tool_call.arguments or {}).get("file_path") or "")
            self.permission_checks.append(file_path)
            if file_path.startswith("/private/"):
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="private path denied after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-original",
            name="read_file",
            arguments={"file_path": "/tmp/ok.txt"},
        )
    )

    assert result == (
        "Error: tool 'read_file' denied by permission gateway: "
        "private path denied after transform"
    )
    assert agent.permission_checks == ["/private/secret.txt"]
    assert agent.tool.executed is False


def test_tool_executor_requests_approval_after_tool_call_transform() -> None:
    class CaptureTool:
        description = "Write a file"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "executed"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("ok")

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self.approval_provider = ApprovalProvider()
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            if getattr(tool, "name", "") == "apply_patch":
                return PermissionDecision(
                    action=PermissionAction.REQUIRE_APPROVAL,
                    authorized=True,
                    reason="apply_patch requires approval after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "executed"
    assert len(agent.approval_provider.requests) == 1
    assert agent.approval_provider.requests[0].tool_name == "apply_patch"


def test_tool_executor_separates_shell_intent_from_approval_tool_args() -> None:
    class ShellLikeTool:
        name = "shell"
        description = "Execute a shell command"
        parameters = {}
        tool_source = "builtin"

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "executed"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("ok")

    class ApprovingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(ShellLikeTool())
            self.approval_provider = ApprovalProvider()

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.REQUIRE_APPROVAL,
                authorized=True,
                reason="shell requires approval",
            )

    agent = ApprovingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-shell",
            name="shell",
            arguments={
                "command": "npm view demo version",
                "intent": "查询 npm 包 demo 的版本信息。",
                "timeout": 10,
            },
        )
    )

    assert result == "executed"
    assert len(agent.approval_provider.requests) == 1
    request = agent.approval_provider.requests[0]
    assert request.intent == "查询 npm 包 demo 的版本信息。"
    assert request.tool_args == {"command": "npm view demo version", "timeout": 10}


def test_tool_executor_rejects_shell_without_intent_before_approval() -> None:
    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("ok")

    class ApprovingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(ShellTool())
            self.approval_provider = ApprovalProvider()

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.REQUIRE_APPROVAL,
                authorized=True,
                reason="shell requires approval",
            )

    agent = ApprovingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-shell",
            name="shell",
            arguments={"command": "npm test"},
        )
    )

    assert "intent" in result.lower()
    assert agent.approval_provider.requests == []


def test_tool_executor_requests_approval_for_final_arguments_after_transform() -> None:
    class CaptureTool:
        name = "apply_patch"
        description = "Write a file"
        parameters = {}
        tool_source = "builtin"

        def __init__(self) -> None:
            self.received = None

        def execute(self, **kwargs) -> str:
            self.received = kwargs
            return f"executed:{kwargs.get('file_path')}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("ok")

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            self.tool = CaptureTool()
            super().__init__(self.tool)
            self.approval_provider = ApprovalProvider()
            _install_legacy_before_tool_transform(self, self._transform)

        def _transform(self, _point, ctx):
            ctx.tool_call.arguments["file_path"] = "/tmp/after.txt"
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.REQUIRE_APPROVAL,
                authorized=True,
                reason="apply_patch requires approval",
            )

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-original",
            name="apply_patch",
            arguments={"file_path": "/tmp/before.txt"},
        )
    )

    assert result == "executed:/tmp/after.txt"
    assert [request.tool_args for request in agent.approval_provider.requests] == [
        {"file_path": "/tmp/after.txt"},
    ]
    assert agent.tool.received == {"file_path": "/tmp/after.txt"}


def test_tool_executor_requests_approval_for_final_tool_after_transform() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return f"executed:{self.name}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("ok")

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self.approval_provider = ApprovalProvider()
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(
                action=PermissionAction.REQUIRE_APPROVAL,
                authorized=True,
                reason=f"{getattr(tool, 'name', '')} requires approval",
            )

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "executed:apply_patch"
    assert [request.tool_name for request in agent.approval_provider.requests] == [
        "apply_patch",
    ]
    assert agent._tools["read_file"].executed is False
    assert agent._tools["apply_patch"].executed is True


def test_tool_executor_blocks_background_approval_after_transform_without_prompting() -> None:
    class CaptureTool:
        description = "Write a file"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def request_approval(self, request):  # noqa: ARG002
            raise AssertionError("background blocked_review must not prompt")

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self.approval_provider = ApprovalProvider()
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            if getattr(tool, "name", "") == "apply_patch":
                return PermissionDecision(
                    action=PermissionAction.BLOCKED_REVIEW,
                    authorized=False,
                    reason="apply_patch requires background review",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == (
        "Error: tool 'apply_patch' blocked pending review: "
        "apply_patch requires background review"
    )
    assert agent._tools["apply_patch"].executed is False


def test_tool_executor_handles_interrupted_approval_after_transform() -> None:
    class CaptureTool:
        description = "Write a file"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class ApprovalProvider:
        def request_approval(self, request):  # noqa: ARG002
            raise EOFError

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self.approval_provider = ApprovalProvider()
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            if getattr(tool, "name", "") == "apply_patch":
                return PermissionDecision(
                    action=PermissionAction.REQUIRE_APPROVAL,
                    authorized=True,
                    reason="apply_patch requires approval after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "Tool 'apply_patch' approval interrupted by user"
    assert agent._tools["apply_patch"].executed is False


def test_tool_executor_executes_transformed_tool_when_permission_allows() -> None:
    class CaptureTool:
        description = "Capture"
        parameters = {}
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return f"executed:{self.name}"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(CaptureTool("read_file"))
            self._tools = {
                "read_file": CaptureTool("read_file"),
                "apply_patch": CaptureTool("apply_patch"),
            }
            self.permission_checks = []
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="apply_patch", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_checks.append(getattr(tool, "name", ""))
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "executed:apply_patch"
    assert agent.permission_checks == ["apply_patch"]
    assert agent._tools["read_file"].executed is False
    assert agent._tools["apply_patch"].executed is True


def test_tool_executor_revalidates_lifecycle_updated_tool_arguments_before_execution() -> None:
    class ReadTool:
        name = "read_file"
        description = "Read a file"
        parameters = {}
        tool_source = "builtin"

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class CaptureTool:
        description = "Write a file"
        parameters = {
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
            },
            "required": ["patch"],
        }
        tool_source = "builtin"

        def __init__(self, name: str) -> None:
            self.name = name
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> None:  # noqa: ARG002
            return None

    class LifecycleDispatcher:
        def __init__(self) -> None:
            self.declaration = _lifecycle_declaration("PreToolUse")

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            if context.event_name != "PreToolUse":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        updated_input={
                            "tool_call": {
                                "id": "call-transformed",
                                "name": "apply_patch",
                                "arguments": {"path": "/tmp/out.txt"},
                            }
                        }
                    ),
                )
            ]

    class LifecycleAgent(_AgentStub):
        def __init__(self) -> None:
            super().__init__(ReadTool())
            self._tools = {
                "read_file": ReadTool(),
                "apply_patch": CaptureTool("apply_patch"),
            }
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result.startswith("Error: bad arguments for apply_patch: invalid arguments")
    assert "$.patch: expected string, got missing" in result
    assert agent._tools["apply_patch"].executed is False


def test_tool_executor_reruns_preflight_after_legacy_tool_call_transform() -> None:
    class PatchTool:
        name = "apply_patch"
        description = "Write a file"
        parameters = {
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
        }
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> str | None:
            if kwargs.get("patch") == "blocked":
                return "blocked patch"
            return None

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            self.tool = PatchTool()
            super().__init__(self.tool)
            _install_legacy_before_tool_transform(self, self._transform)

        def _transform(self, _point, ctx):
            ctx.tool_call.arguments["patch"] = "blocked"
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-original",
            name="apply_patch",
            arguments={"patch": "allowed"},
        )
    )

    assert result == "blocked patch"
    assert agent.tool.executed is False
