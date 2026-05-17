from types import SimpleNamespace

from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.services.prompt.builder import system_prompt


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}


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

    def get_active_mode_config(self):
        return self.available_modes[self.active_mode]

    def get_active_tools(self):
        return [_Tool("read_file", "Read file")]

    def get_blocked_tools(self):
        return []

    def suggest_modes_for_tool(self, _tool_name: str):
        return []


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


def test_agent_loop_injects_consumed_follow_up_before_next_llm_call() -> None:
    agent = _AgentStub()
    agent.consume_follow_ups = lambda: [
        SimpleNamespace(followup_id="follow-1", text="prefer the shorter path")
    ]
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    loop._inject_pending_follow_ups()
    messages = loop._full_messages()

    assert messages[-2]["role"] == "user"
    assert "<conversation_guidance>" in messages[-2]["content"]
    assert "prefer the shorter path" in messages[-2]["content"]


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
