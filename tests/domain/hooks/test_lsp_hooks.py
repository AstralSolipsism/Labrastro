from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import LspEditObserverHook
from reuleauxcoder.domain.hooks.builtin.lsp_injector import LspDiagnosticInjectorHook
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock


class FakeManager:
    config = LspConfig()

    def __init__(self):
        self.changed = []
        self.block = DiagnosticBlock(
            file_path="main.py",
            items=[Diagnostic(line=1, character=1, message="bad")],
        )

    def notify_file_changed(self, file_path):
        self.changed.append(file_path)
        return self.block

    def render_cached_diagnostics(self):
        return '<diagnostics file="main.py">\n  ERROR [1:1] bad\n</diagnostics>'


def test_lsp_edit_observer_appends_local_diagnostics() -> None:
    manager = FakeManager()
    hook = LspEditObserverHook()
    hook.set_lsp_manager(manager)
    context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="call-1",
            name="edit_file",
            arguments={"file_path": "main.py"},
        ),
        result="Edited main.py",
        metadata={"execution_target": "local"},
    )

    result = hook.run(context)

    assert "Edited main.py" in result.result
    assert '<diagnostics file="main.py">' in result.result
    assert manager.changed == ["main.py"]


def test_lsp_edit_observer_skips_remote_peer_results() -> None:
    manager = FakeManager()
    hook = LspEditObserverHook()
    hook.set_lsp_manager(manager)
    context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="call-1",
            name="write_file",
            arguments={"file_path": "main.py"},
        ),
        result="Wrote main.py",
        metadata={"execution_target": "remote_peer"},
    )

    result = hook.run(context)

    assert result.result == "Wrote main.py"
    assert manager.changed == []


def test_lsp_injector_adds_cached_diagnostics_to_request() -> None:
    hook = LspDiagnosticInjectorHook()
    hook.set_lsp_manager(FakeManager())
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[{"role": "user", "content": "fix it"}],
    )

    result = hook.run(context)

    assert result.messages[-1]["role"] == "system"
    assert "Current LSP diagnostics" in result.messages[-1]["content"]
