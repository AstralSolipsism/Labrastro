"""Tests for ToolExecutor, including CWD sync behaviour."""

from types import SimpleNamespace

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookEventContext,
    LifecycleHookOutput,
    LifecycleHookDispatcher,
    LifecycleHookRegistry,
    default_lifecycle_hook_runtime_adapters,
    system_builtin_lifecycle_declarations_from_hook_registry,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import BeforeToolExecuteContext, HookPoint
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool


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


def test_shell_cwd_syncs_to_runtime_working_directory() -> None:
    """After shell tool executes, ToolExecutor syncs _cwd → agent.runtime_working_directory."""
    tool = _ShellToolStub()
    tool._cwd = "/tmp/cool-dir"

    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_1", name="shell", arguments={"command": "echo hi"})
    executor.execute(tc)

    assert getattr(agent, "runtime_working_directory", None) == "/tmp/cool-dir"


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


def test_write_file_missing_required_arguments_returns_tool_error() -> None:
    agent = _AgentStub(WriteFileTool())
    executor = ToolExecutor(agent)

    result = executor.execute(ToolCall(id="call_4", name="write_file", arguments={}))

    assert result.startswith("Error: bad arguments for write_file: invalid arguments")
    assert "$.file_path: expected string, got missing" in result
    assert "$.content: expected string, got missing" in result


def test_edit_file_missing_required_arguments_does_not_raise_from_preflight() -> None:
    agent = _AgentStub(EditFileTool())
    executor = ToolExecutor(agent)

    result = executor.execute(ToolCall(id="call_5", name="edit_file", arguments={}))

    assert result.startswith("Error: bad arguments for edit_file: invalid arguments")
    assert "$.file_path: expected string, got missing" in result
    assert "$.old_string: expected string, got missing" in result
    assert "$.new_string: expected string, got missing" in result


def test_provider_argument_error_returns_tool_error_before_execution() -> None:
    tool = SimpleNamespace(
        name="write_file",
        parameters={"type": "object", "required": ["file_path", "content"]},
        execute=lambda **kwargs: "should not execute",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(
            id="call_6",
            name="write_file",
            arguments={},
            argument_error="missing tool arguments",
        )
    )

    assert result == "Error: bad arguments for write_file: missing tool arguments"


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
        name="write_file",
        parameters={"type": "object", "properties": {}},
        execute=lambda **kwargs: "should not execute",
        preflight_validate=lambda **kwargs: None,
    )
    agent = _AgentStub(tool)
    agent.evaluate_tool_permission = lambda _tool, tool_call=None: PermissionDecision(
        action=PermissionAction.DENY,
        authorized=False,
        reason=(
            "builtin_tool 'write_file' is not authorized by this Agent's "
            "effective_capabilities"
        ),
    )
    executor = ToolExecutor(agent)

    result = executor.execute(
        ToolCall(id="call_blocked", name="write_file", arguments={})
    )

    assert result == (
        "Error: tool 'write_file' denied by permission gateway: builtin_tool "
        "'write_file' is not authorized by this Agent's effective_capabilities"
    )


def test_tool_executor_permission_deny_emits_end_only_with_index_and_diagnostics() -> None:
    tool = SimpleNamespace(
        name="write_file",
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
                reason="write_file denied by policy",
                policy_matched="effective_capabilities",
            )

    agent = RecordingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_blocked", name="write_file", arguments={}),
        index=2,
    )

    assert result == (
        "Error: tool 'write_file' denied by permission gateway: "
        "write_file denied by policy"
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
        name = "write_file"
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
                reason="write_file requires approval",
            )

    agent = RecordingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_approval", name="write_file", arguments={}),
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
                "name": "write_file",
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
                "write_file": CaptureTool("write_file"),
            }
            self.permission_tool_call_ids: list[str] = []
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_tool_call_ids.append(getattr(tool_call, "id", ""))
            if getattr(tool, "name", "") == "write_file":
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="write_file denied after lifecycle update",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == (
        "Error: tool 'write_file' denied by permission gateway: "
        "write_file denied after lifecycle update"
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
            super().__init__(CaptureTool("write_file"))
            self._tools = {
                "write_file": CaptureTool("write_file"),
                "read_file": CaptureTool("read_file"),
            }
            self.permission_checks = []
            self.lifecycle_dispatcher = LifecycleDispatcher()

        def get_tool(self, name: str):
            return self._tools.get(name)

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            tool_name = getattr(tool, "name", "")
            self.permission_checks.append(tool_name)
            if tool_name == "write_file":
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="write_file denied before lifecycle update",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = LifecycleAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="write_file", arguments={})
    )

    assert result == "executed:read_file"
    assert agent.permission_checks == ["read_file"]
    assert agent._tools["write_file"].executed is False
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
                                "name": "write_file",
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
                "write_file": CaptureTool("write_file"),
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

    assert result == "executed:write_file"
    tool_events = [
        event for event in agent.events
        if event.event_type in {
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
        }
    ]
    assert [(event.event_type, event.tool_name, event.tool_call_id) for event in tool_events] == [
        (AgentEventType.TOOL_CALL_START, "write_file", "call-original"),
        (AgentEventType.TOOL_CALL_END, "write_file", "call-original"),
    ]
    assert tool_events[0].tool_args == {"path": "after.txt"}
    assert tool_events[0].data["index"] == 7
    assert agent._tools["write_file"].executed_arguments == {"path": "after.txt"}


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
                    output=LifecycleHookOutput(decision="allow"),
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
                "write_file": CaptureTool("write_file"),
            }
            self.permission_tool_call_ids: list[str] = []
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_tool_call_ids.append(getattr(tool_call, "id", ""))
            if getattr(tool, "name", "") == "write_file":
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason="write_file denied after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    result = ToolExecutor(TransformingAgent()).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert (
        result
        == "Error: tool 'write_file' denied by permission gateway: write_file denied after transform"
    )


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
                "write_file": CaptureTool("write_file"),
            }
            self.permission_tool_call_ids: list[str] = []
            self.end_tool_call_ids: list[str] = []
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
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
                "write_file": CaptureTool("write_file"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            if getattr(tool, "name", "") == "write_file":
                return PermissionDecision(
                    action=PermissionAction.REQUIRE_APPROVAL,
                    authorized=True,
                    reason="write_file requires approval after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "executed"
    assert len(agent.approval_provider.requests) == 1
    assert agent.approval_provider.requests[0].tool_name == "write_file"


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
        name = "write_file"
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
                reason="write_file requires approval",
            )

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-original",
            name="write_file",
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
                "write_file": CaptureTool("write_file"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
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

    assert result == "executed:write_file"
    assert [request.tool_name for request in agent.approval_provider.requests] == [
        "write_file",
    ]
    assert agent._tools["read_file"].executed is False
    assert agent._tools["write_file"].executed is True


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
                "write_file": CaptureTool("write_file"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            if getattr(tool, "name", "") == "write_file":
                return PermissionDecision(
                    action=PermissionAction.BLOCKED_REVIEW,
                    authorized=False,
                    reason="write_file requires background review",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == (
        "Error: tool 'write_file' blocked pending review: "
        "write_file requires background review"
    )
    assert agent._tools["write_file"].executed is False


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
                "write_file": CaptureTool("write_file"),
            }
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            if getattr(tool, "name", "") == "write_file":
                return PermissionDecision(
                    action=PermissionAction.REQUIRE_APPROVAL,
                    authorized=True,
                    reason="write_file requires approval after transform",
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "Tool 'write_file' approval interrupted by user"
    assert agent._tools["write_file"].executed is False


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
                "write_file": CaptureTool("write_file"),
            }
            self.permission_checks = []
            _install_legacy_before_tool_transform(self, self._transform)

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            self.permission_checks.append(getattr(tool, "name", ""))
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()
    result = ToolExecutor(agent).execute(
        ToolCall(id="call-original", name="read_file", arguments={})
    )

    assert result == "executed:write_file"
    assert agent.permission_checks == ["write_file"]
    assert agent._tools["read_file"].executed is False
    assert agent._tools["write_file"].executed is True


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
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
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
                                "name": "write_file",
                                "arguments": {"file_path": "/tmp/out.txt"},
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
                "write_file": CaptureTool("write_file"),
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

    assert result.startswith("Error: bad arguments for write_file: invalid arguments")
    assert "$.content: expected string, got missing" in result
    assert agent._tools["write_file"].executed is False


def test_tool_executor_reruns_preflight_after_legacy_tool_call_transform() -> None:
    class WriteTool:
        name = "write_file"
        description = "Write a file"
        parameters = {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        }
        tool_source = "builtin"

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **kwargs) -> str:  # noqa: ARG002
            self.executed = True
            return "should not execute"

        def preflight_validate(self, **kwargs) -> str | None:
            if kwargs.get("file_path") == "/blocked.txt":
                return "blocked path"
            return None

    class TransformingAgent(_AgentStub):
        def __init__(self) -> None:
            self.tool = WriteTool()
            super().__init__(self.tool)
            _install_legacy_before_tool_transform(self, self._transform)

        def _transform(self, _point, ctx):
            ctx.tool_call.arguments["file_path"] = "/blocked.txt"
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    agent = TransformingAgent()

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="call-original",
            name="write_file",
            arguments={"file_path": "/allowed.txt"},
        )
    )

    assert result == "blocked path"
    assert agent.tool.executed is False
