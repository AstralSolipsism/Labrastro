from types import SimpleNamespace
import time

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookOutput,
)
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.catalog import ToolCatalog
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
from reuleauxcoder.services.prompt.builder import system_prompt


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}

    def tool_spec(self) -> ToolSpec:
        return _loop_tool_spec(self.name, self.description)


class _LoopSpecOnlyTool:
    def __init__(
        self,
        name: str,
        description: str,
        exposure: ToolExposure = ToolExposure.DIRECT,
    ):
        self.name = name
        self.description = description
        self.exposure = exposure

    def tool_spec(self) -> ToolSpec:
        return _loop_tool_spec(self.name, self.description, self.exposure)


def _loop_tool_spec(
    name: str,
    description: str,
    exposure: ToolExposure = ToolExposure.DIRECT,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        namespace="test",
        description=description,
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        output_strategy=ToolOutputStrategy.TEXT,
        risk=ToolRisk.READ_ONLY,
        exposure=exposure,
        search_text=f"{name}\n{description}",
        search_keywords=(),
        permission=ToolPermissionSpec(policy="read_only"),
        mutation=ToolMutationSpec(),
        execution=ToolExecutionSpec(executor_ref=f"test.{name}"),
        provider_surface=ProviderSurface.FUNCTION,
    )


class _AgentStub:
    def __init__(self) -> None:
        self.active_mode = "coder"
        self.available_modes = {
            "coder": SimpleNamespace(
                description="Default coding mode", prompt_append="Focus on code."
            )
        }
        self.state = SimpleNamespace(messages=[{"role": "user", "content": "hello"}])
        self.runtime_config = SimpleNamespace(
            prompt=SimpleNamespace(system_append="Always answer in Chinese.")
        )
        self.skills_catalog = "# Skills\n- skill-a"
        self.lifecycle_dispatcher = None
        self.current_session_id = "session-1"
        self.runtime_agent_run_id = "run-1"
        self.runtime_turn_id = "turn-1"
        self.locale = "zh-CN"
        self.permission_trigger_source = "chat"
        self.approval_provider = None
        self._events = []

    def _emit_event(self, event) -> None:
        self._events.append(event)

    def get_active_mode_config(self):
        return self.available_modes[self.active_mode]

    def get_active_tools(self):
        return [_Tool("read_file", "Read file")]

    def get_tool(self, name: str):
        for tool in self.get_active_tools():
            if tool.name == name:
                return tool
        if name == "apply_patch":
            return _Tool("apply_patch", "Patch files")
        return None

    def get_blocked_tools(self):
        return []

    def suggest_modes_for_tool(self, _tool_name: str):
        return []


class _RunContextStub:
    _ui_bus = None

    def maybe_compress(self, messages: list[dict], llm: object) -> None:  # noqa: ARG002
        return None

    def get_context_tokens(self, messages: list[dict]) -> int:  # noqa: ARG002
        return 0


class _SequenceLLM:
    model = "test-model"
    max_tokens = 4096

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.call_kwargs: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:  # noqa: ARG002
        self.calls += 1
        self.call_kwargs.append(kwargs)
        response = self.responses.pop(0)
        on_tool_call_delta = kwargs.get("on_tool_call_delta")
        if callable(on_tool_call_delta):
            for delta in response.provider_extra.get("tool_call_deltas", []):
                on_tool_call_delta(delta)
        on_token = kwargs.get("on_token")
        if callable(on_token):
            for token in response.tokens:
                on_token(token)
        return response


class _RunAgentStub(_AgentStub):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.llm = _SequenceLLM(responses)
        self.context = _RunContextStub()
        self.lifecycle_dispatcher = None
        self.max_rounds = 3
        self.approval_provider = None
        self._events = []
        self._stop_requested = False
        self.executed_tool_calls: list[ToolCall] = []
        self.state = SimpleNamespace(
            messages=[{"role": "user", "content": "hello"}],
            current_round=0,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cache_read_tokens=None,
            total_cache_write_tokens=None,
            total_cost_usd=None,
            usage_extra={},
        )
        self._executor = SimpleNamespace(
            execute=self._execute_tool,
            execute_parallel=self._execute_tools,
        )

    def _emit_event(self, event) -> None:
        self._events.append(event)

    def stop_requested(self) -> bool:
        return self._stop_requested

    def request_stop(self, *args) -> None:  # noqa: ANN002
        self._stop_requested = True

    def _collect_pending_tool_calls(self) -> list[tuple[str, str]]:
        return []

    def _execute_tool(self, tool_call: ToolCall, index: int = 0) -> str:  # noqa: ARG002
        self.executed_tool_calls.append(tool_call)
        return "tool-ok"

    def _execute_tools(self, tool_calls: list[ToolCall]) -> list[str]:
        for tool_call in tool_calls:
            self.executed_tool_calls.append(tool_call)
        return ["tool-ok" for _ in tool_calls]


class _ApplyPatchRunAgentStub(_RunAgentStub):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(responses)
        self._apply_patch_tool = ApplyPatchTool()

    def get_active_tools(self):
        return [self._apply_patch_tool]

    def get_tool(self, name: str):
        if name == "apply_patch":
            return self._apply_patch_tool
        return super().get_tool(name)


def test_agent_loop_passes_model_visible_apply_patch_contract_to_llm_chat() -> None:
    agent = _ApplyPatchRunAgentStub([LLMResponse(content="done")])
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "done"
    assert agent.llm.call_kwargs
    call = agent.llm.call_kwargs[0]
    system_content = call["messages"][0]["content"]
    assert "JSON function wrapper" in system_content
    assert "*** Add File:" in system_content
    assert "Do not use *** File:" in system_content
    apply_patch_schema = next(
        tool
        for tool in call["tools"]
        if tool.get("function", {}).get("name") == "apply_patch"
    )
    assert "*** Update File:" in apply_patch_schema["function"]["description"]
    patch_description = apply_patch_schema["function"]["parameters"]["properties"][
        "patch"
    ]["description"]
    assert "draft_document_begin" in patch_description


def test_agent_loop_builds_model_visible_tool_schemas_from_sorted_direct_specs() -> None:
    agent = SimpleNamespace(
        get_active_tools=lambda: [
            _LoopSpecOnlyTool("zeta", "last"),
            _LoopSpecOnlyTool("internal_save", "hidden", ToolExposure.HIDDEN),
            _LoopSpecOnlyTool("alpha", "first"),
        ]
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    schemas = loop._tool_schemas()

    assert [schema["function"]["name"] for schema in schemas] == ["alpha", "zeta"]
    assert schemas[0]["function"]["description"] == "first"


def test_agent_loop_reads_model_visible_schemas_from_exposure_plan() -> None:
    plan = ToolCatalog.from_tools(
        [
            _LoopSpecOnlyTool("zeta", "last"),
            _LoopSpecOnlyTool("capability_docs", "deferred", ToolExposure.DEFERRED),
            _LoopSpecOnlyTool("alpha", "first"),
        ]
    ).exposure_plan()

    def _unexpected_active_tool_scan():
        raise AssertionError("AgentLoop must read the exposure plan")

    agent = SimpleNamespace(
        tool_exposure_plan=lambda: plan,
        get_active_tools=_unexpected_active_tool_scan,
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    schemas = loop._tool_schemas()

    assert [schema["function"]["name"] for schema in schemas] == ["alpha", "zeta"]


def test_agent_loop_passes_tool_exposure_metadata_to_llm_request() -> None:
    plan = ToolCatalog.from_tools(
        [
            _LoopSpecOnlyTool("tool_search", "search"),
            _LoopSpecOnlyTool("capability_execute", "execute"),
            _LoopSpecOnlyTool("capability_docs", "deferred", ToolExposure.DEFERRED),
        ]
    ).exposure_plan()
    agent = _RunAgentStub([LLMResponse(content="done")])
    agent.tool_exposure_plan = lambda: plan

    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "done"
    metadata = agent.llm.call_kwargs[0]["metadata"]
    assert metadata["tool_exposure"] == {
        "direct_tool_names": ["capability_execute", "tool_search"],
        "direct_tool_count": 2,
        "deferred_tool_count": 1,
        "hidden_tool_count": 0,
        "hosted_tool_count": 0,
    }


def test_system_prompt_no_longer_contains_runtime_environment_block() -> None:
    prompt = system_prompt([_Tool("read_file", "Read file")])

    assert "# Environment" not in prompt
    assert "- Working directory: " not in prompt
    assert "- Shell: " not in prompt


def test_agent_loop_appends_ephemeral_runtime_context_at_tail() -> None:
    agent = _AgentStub()
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    assert messages[0]["role"] == "system"
    assert "# Tools" in messages[0]["content"]
    assert "# Environment" not in messages[0]["content"]

    assert messages[1:] == [
        {"role": "user", "content": "hello"},
        messages[-1],
    ]
    assert messages[-1]["role"] == "user"
    assert "<system_context>" in messages[-1]["content"]
    assert "- Working directory: " in messages[-1]["content"]
    assert "- Shell: " in messages[-1]["content"]


def test_agent_loop_runtime_working_directory_override() -> None:
    agent = _AgentStub()
    agent.runtime_working_directory = "/tmp/remote-workspace"
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    assert "- Working directory: /tmp/remote-workspace" in messages[-1]["content"]


def _skill_expansion_dispatcher(output: LifecycleHookOutput):
    class Dispatcher:
        def __init__(self) -> None:
            self.contexts = []

        def dispatch(self, context):
            self.contexts.append(context)
            declaration = LifecycleHookDeclaration.from_dict(
                "hook:admin:skill-expansion:UserPromptExpansion:0",
                {
                    "event": "UserPromptExpansion",
                    "source": "admin_managed",
                    "placement": "server",
                    "handler_type": "internal",
                    "display_name": "Skill expansion guard",
                    "summary": "Reviews Skill catalog expansion before prompt injection.",
                    "permissions": [],
                    "trust": "trusted",
                },
            )
            return [LifecycleHookDispatchResult(declaration, output)]

    return Dispatcher()


def test_agent_loop_dispatches_user_prompt_expansion_before_skill_catalog_injection() -> None:
    agent = _AgentStub()
    agent.lifecycle_dispatcher = _skill_expansion_dispatcher(
        LifecycleHookOutput(additional_context=["Use skill-a only for code review."])
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    context = agent.lifecycle_dispatcher.contexts[0]
    assert context.event_name == "UserPromptExpansion"
    assert context.payload["trigger_kind"] == "skill_catalog"
    assert context.payload["skills_catalog"] == "# Skills\n- skill-a"
    assert context.agent_run_id == "run-1"
    system = messages[0]["content"]
    assert "# Skills" in system
    assert "skill-a" in system
    assert "Lifecycle skill context:" in system
    assert "Use skill-a only for code review." in system
    lifecycle_events = [
        event for event in agent._events if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert [event.data["phase"] for event in lifecycle_events] == [
        "dispatch_start",
        "result",
    ]
    assert lifecycle_events[-1].data["event_name"] == "UserPromptExpansion"
    assert lifecycle_events[-1].data["payload"]["trigger_kind"] == "skill_catalog"


def test_agent_loop_denies_skill_catalog_expansion_without_prompt_injection() -> None:
    agent = _AgentStub()
    agent.lifecycle_dispatcher = _skill_expansion_dispatcher(
        LifecycleHookOutput(
            decision="deny",
            continue_flow=False,
            user_message="Skill catalog blocked.",
        )
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    system = messages[0]["content"]
    assert "skill-a" not in system
    lifecycle_events = [
        event for event in agent._events if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert lifecycle_events[-1].data["event_name"] == "UserPromptExpansion"
    assert lifecycle_events[-1].data["decision"] == "deny"
    assert lifecycle_events[-1].data["message"] == "Skill catalog blocked."


def test_agent_loop_reuses_skill_catalog_expansion_within_turn_without_reexecuting_hooks() -> None:
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", arguments={}),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    agent.lifecycle_dispatcher = _skill_expansion_dispatcher(
        LifecycleHookOutput(additional_context=["Use skill-a once per turn."])
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "done"
    assert len(agent.lifecycle_dispatcher.contexts) == 1


def test_agent_loop_routes_document_draft_stream_to_preview_chunk_not_assistant_delta(
    tmp_path,
) -> None:
    declaration = "\n".join(
        [
            "Draft document declared: Architecture",
            "draft_id: draft-test",
            "target_path: docs/architecture.md",
            "Continue the document body in assistant markdown stream.",
        ]
    )
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="draft_document_begin",
                        arguments={"target_path": "docs/architecture.md"},
                    ),
                ],
            ),
            LLMResponse(
                content="# Architecture\n\nBody\n",
                tokens=["# Architecture\n", "\nBody\n"],
            ),
        ]
    )
    agent.runtime_workspace_root = str(tmp_path)
    agent._executor = SimpleNamespace(
        execute=lambda _tool_call, index=0: declaration,  # noqa: ARG005
        execute_parallel=agent._execute_tools,
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "# Architecture\n\nBody\n"
    event_types = [event.event_type.value for event in agent._events]
    assert "document_draft_preview_chunk" in event_types
    assert "document_draft_progress" in event_types
    assert "document_draft_snapshot" in event_types
    assert "document_draft_delta" not in event_types
    assert "stream_token" not in event_types
    preview_chunks = [
        event.data
        for event in agent._events
        if event.event_type.value == "document_draft_preview_chunk"
    ]
    snapshots = [
        event.data
        for event in agent._events
        if event.event_type.value == "document_draft_snapshot"
    ]
    assert "".join(chunk["content"] for chunk in preview_chunks) == "# Architecture\n\nBody\n"
    assert all(chunk["draft_id"] == "draft-test" for chunk in preview_chunks)
    assert snapshots[-1]["content"] == "# Architecture\n\nBody\n"
    assert snapshots[-1]["final"] is True
    assert loop.last_response_streamed is True


def test_agent_loop_keeps_apply_patch_argument_delta_out_of_file_change_stream() -> None:
    agent = _RunAgentStub(
        [
            LLMResponse(
                content="done",
                provider_extra={
                    "tool_call_deltas": [
                        {
                            "index": 0,
                            "tool_call_id": "call_patch",
                            "tool_name": "apply_patch",
                            "arguments_delta": (
                                '{"patch":"*** Begin Patch\\n'
                                "*** Update File: src/app.py\\n"
                                "@@\\n-old"
                            ),
                            "arguments_preview": '{"patch":"*** Begin Patch...',
                        }
                    ]
                },
            )
        ]
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "done"
    event_types = [event.event_type.value for event in agent._events]
    assert "tool_call_delta" in event_types
    assert "file_change_started" not in event_types
    assert "file_change_patch_updated" not in event_types
    assert "file_change_completed" not in event_types


def test_agent_loop_batches_tiny_document_draft_stream_tokens(tmp_path) -> None:
    declaration = "\n".join(
        [
            "Draft document declared: Architecture",
            "draft_id: draft-test",
            "target_path: docs/architecture.md",
            "Continue the document body in assistant markdown stream.",
        ]
    )
    content = "0123456789" * 90
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="draft_document_begin",
                        arguments={"target_path": "docs/architecture.md"},
                    ),
                ],
            ),
            LLMResponse(content=content, tokens=list(content)),
        ]
    )
    agent.runtime_workspace_root = str(tmp_path)
    agent._executor = SimpleNamespace(
        execute=lambda _tool_call, index=0: declaration,  # noqa: ARG005
        execute_parallel=agent._execute_tools,
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    preview_chunks = [
        event.data
        for event in agent._events
        if event.event_type.value == "document_draft_preview_chunk"
    ]
    assert result == content
    assert "".join(chunk["content"] for chunk in preview_chunks) == content
    assert len(preview_chunks) < len(content) // 50


def test_agent_loop_flushes_document_draft_before_interrupt_cancel(tmp_path) -> None:
    declaration = "\n".join(
        [
            "Draft document declared: Architecture",
            "draft_id: draft-test",
            "target_path: docs/architecture.md",
            "Continue the document body in assistant markdown stream.",
        ]
    )
    content = "partial draft body"
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="draft_document_begin",
                        arguments={"target_path": "docs/architecture.md"},
                    ),
                ],
            ),
            LLMResponse(
                content=content,
                tokens=list(content),
                stream_status="interrupted",
                interruption={"message": "provider stream exceeded wall time limit (600s)"},
                recovery={"attempted": True, "failed": True},
            ),
        ]
    )
    agent.runtime_workspace_root = str(tmp_path)
    agent._executor = SimpleNamespace(
        execute=lambda _tool_call, index=0: declaration,  # noqa: ARG005
        execute_parallel=agent._execute_tools,
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    event_types = [event.event_type.value for event in agent._events]
    snapshots = [
        event.data
        for event in agent._events
        if event.event_type.value == "document_draft_snapshot"
    ]
    assert result == content
    assert snapshots[-1]["content"] == content
    assert snapshots[-1]["final"] is True
    assert "draft_body_stalled" in event_types
    assert "draft_interrupted_recoverable" in event_types
    assert "document_draft_cancelled" not in event_types
    checkpoint = getattr(agent, "pending_document_draft_checkpoint", None)
    assert checkpoint["draft_id"] == "draft-test"
    assert checkpoint["target_path"] == "docs/architecture.md"
    assert checkpoint["content"] == content


def test_agent_loop_resumes_document_draft_from_interrupted_checkpoint(tmp_path) -> None:
    declaration = "\n".join(
        [
            "Draft document declared: Architecture",
            "draft_id: draft-test",
            "target_path: docs/architecture.md",
            "Continue the document body in assistant markdown stream.",
        ]
    )
    first_part = "# Architecture\n"
    second_part = "\nContinued\n"
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="draft_document_begin",
                        arguments={"target_path": "docs/architecture.md"},
                    ),
                ],
            ),
            LLMResponse(
                content=first_part,
                tokens=[first_part],
                stream_status="interrupted",
                interruption={"message": "provider stream interrupted"},
            ),
        ]
    )
    agent.runtime_workspace_root = str(tmp_path)
    agent.approval_provider = SimpleNamespace(
        request_approval=lambda _request: ApprovalDecision.allow_once("ok")
    )
    agent._executor = SimpleNamespace(
        execute=lambda _tool_call, index=0: declaration,  # noqa: ARG005
        execute_parallel=agent._execute_tools,
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    assert loop.run() == first_part
    agent.llm.responses.append(LLMResponse(content=second_part, tokens=[second_part]))

    assert loop.run() == second_part

    assert (tmp_path / "docs" / "architecture.md").read_text() == first_part + second_part
    assert getattr(agent, "pending_document_draft_checkpoint", None) is None
    assert all(tool_call.name != "apply_patch" for tool_call in agent.executed_tool_calls)


def test_agent_loop_runtime_workspace_root_falls_back_to_working_directory() -> None:
    agent = _AgentStub()
    agent.runtime_workspace_root = "/tmp/agent-run-workspace"
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    assert "- Working directory: /tmp/agent-run-workspace" in messages[-1]["content"]


def test_agent_loop_remote_peer_runtime_context_uses_peer_registration() -> None:
    agent = _AgentStub()
    agent.runtime_execution_target = "remote_peer"
    agent.runtime_peer_context = {
        "cwd": "D:\\work\\repo",
        "workspace_root": "D:\\work\\repo",
        "features": ["shell", "read_file"],
        "host_info_min": {
            "os": "windows",
            "arch": "amd64",
            "shell": "bash",
            "hostname": "devbox",
        },
    }
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="server-shell")

    messages = loop._full_messages()

    content = messages[-1]["content"]
    assert "- Execution target: remote_peer" in content
    assert "- Working directory: D:\\work\\repo" in content
    assert "- Workspace root: D:\\work\\repo" in content
    assert "- OS: windows (amd64)" in content
    assert "- Shell: bash" in content
    assert "server-shell" not in content


def test_agent_loop_remote_peer_runtime_context_requires_peer_registration() -> None:
    agent = _AgentStub()
    agent.runtime_execution_target = "remote_peer"
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    try:
        loop._full_messages()
    except RuntimeError as exc:
        assert "remote peer runtime context is missing" in str(exc)
    else:
        raise AssertionError("remote_peer context without registration must fail")


def test_agent_loop_does_not_start_llm_after_runtime_timeout() -> None:
    agent = _RunAgentStub([LLMResponse(content="should not run")])
    agent.runtime_budget = {"timeout_sec": 1}
    agent.runtime_timeout_sec = 1
    agent.runtime_deadline = time.monotonic() - 0.1
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "(AgentRun budget exceeded: timeout_sec=1)"
    assert agent.llm.calls == 0


def test_agent_loop_stops_before_tool_side_effects_when_token_budget_exceeded() -> None:
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", arguments={}),
                ],
                prompt_tokens=6,
                completion_tokens=5,
            )
        ]
    )
    agent.runtime_budget = {"token_budget": 10}
    agent.runtime_token_budget = 10
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "(AgentRun budget exceeded: token_budget=10)"
    assert agent.llm.calls == 1
    assert agent.executed_tool_calls == []


def test_agent_loop_budget_max_turns_does_not_call_summary_llm() -> None:
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", arguments={}),
                ],
            ),
            LLMResponse(content="summary should not run"),
        ]
    )
    agent.max_rounds = 1
    agent.runtime_budget = {"max_turns": 1}
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "(AgentRun budget exceeded: max_turns=1)"
    assert agent.llm.calls == 1
    assert [call.id for call in agent.executed_tool_calls] == ["call_1"]


def test_agent_loop_stores_lifecycle_transformed_tool_call_in_history() -> None:
    agent = _RunAgentStub(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "before.txt"},
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )

    def execute_transformed(tool_call: ToolCall, index: int = 0) -> str:  # noqa: ARG001
        tool_call.name = "apply_patch"
        tool_call.arguments = {"path": "after.txt"}
        return "tool-ok"

    agent._executor = SimpleNamespace(
        execute=execute_transformed,
        execute_parallel=agent._execute_tools,
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    result = loop.run()

    assert result == "done"
    assistant_tool_message = agent.state.messages[1]
    stored_tool_call = assistant_tool_message["tool_calls"][0]
    assert stored_tool_call["function"]["name"] == "apply_patch"
    assert stored_tool_call["function"]["arguments"] == '{"path": "after.txt"}'


def test_agent_loop_has_no_legacy_guidance_injection_hook() -> None:
    agent = _AgentStub()
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    assert not hasattr(agent, "consume_" + "follow" + "_ups")
    assert not hasattr(loop, "_inject_pending_" + "follow" + "_ups")


def test_system_prompt_includes_taskflow_only_when_workflow_is_active() -> None:
    normal = system_prompt([_Tool("read_file", "Read file")])
    taskflow = system_prompt(
        [_Tool("read_file", "Read file")],
        workflow_mode="taskflow",
        workflow_prompt_append="Current Taskflow taskflow_id: `taskflow-1`.",
    )

    assert "Active Workflow" not in normal
    assert "taskflow" in taskflow
    assert "taskflow-1" in taskflow


def test_stream_interruption_payload_keeps_transport_message_diagnostic_only() -> None:
    payload = AgentLoop._stream_interruption_payload(
        LLMResponse(
            content="partial",
            stream_status="interrupted",
            interruption={
                "recoverable": True,
                "classification": "text_interrupted",
                "partial_kind": "text",
                "retry_action": "continue",
                "message": "peer closed connection without sending complete message body",
                "diagnostic_path": "logs/llm-error.json",
            },
            recovery={"attempted": True, "action": "continue"},
        )
    )

    assert payload["notice_code"] == "provider_stream_interrupted"
    assert payload["message_key"] == "provider_stream.interrupted_can_continue"
    assert "message" not in payload
    assert payload["diagnostic_message"] == "peer closed connection without sending complete message body"
    assert payload["diagnostic_path"] == "logs/llm-error.json"


def test_stream_interruption_payload_uses_recovering_key_only_for_successful_recovery() -> None:
    payload = AgentLoop._stream_interruption_payload(
        LLMResponse(
            content="complete",
            stream_status="completed",
            interruption={"message": "peer closed connection"},
            recovery={"attempted": True, "failed": False},
        )
    )

    assert payload["message_key"] == "provider_stream_interrupted.recovering"
