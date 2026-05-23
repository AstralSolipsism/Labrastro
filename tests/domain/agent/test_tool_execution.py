"""Tests for ToolExecutor, including CWD sync behaviour."""

from types import SimpleNamespace

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
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
        self.hook_registry = SimpleNamespace(
            run_guards=lambda point, ctx: [],
            run_transforms=lambda point, ctx: ctx,
            run_observers=lambda point, ctx: None,
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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

        def get_tool(self, name: str):
            return self._tools.get(name)

        def _transform(self, _point, ctx):
            ctx.tool_call = ToolCall(id="call-transformed", name="write_file", arguments={})
            return ctx

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
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


def test_tool_executor_reevaluates_permission_after_in_place_argument_transform() -> None:
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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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
    assert agent.permission_checks == ["/tmp/ok.txt", "/private/secret.txt"]
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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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


def test_tool_executor_reapproves_when_transform_changes_arguments_after_approval() -> None:
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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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
        {"file_path": "/tmp/before.txt"},
        {"file_path": "/tmp/after.txt"},
    ]
    assert agent.tool.received == {"file_path": "/tmp/after.txt"}


def test_tool_executor_reapproves_when_transform_changes_tool_after_approval() -> None:
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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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
        "read_file",
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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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
            self.hook_registry = SimpleNamespace(
                run_guards=lambda point, ctx: [],
                run_transforms=self._transform,
                run_observers=lambda point, ctx: None,
            )

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
    assert agent.permission_checks == ["read_file", "write_file"]
    assert agent._tools["read_file"].executed is False
    assert agent._tools["write_file"].executed is True
