"""Tests that builtin tools dispatch to the remote backend correctly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from labrastro_server.relay.errors import (
    PeerDisconnectedError,
    PeerNotFoundError,
)
from labrastro_server.interfaces.http.remote.protocol import (
    ExecToolRequest,
    ExecToolResult,
    RemoteMCPToolInfo,
)
from labrastro_server.relay.server import RelayServer
from labrastro_server.adapters.reuleauxcoder.mcp_tools import RemotePeerMCPTool
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.builtin.capability_execute import CapabilityExecuteTool
from reuleauxcoder.extensions.tools.builtin.draft_document import DraftDocumentBeginTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.tool_search import ToolSearchTool


_CONTRACT_FIXTURE = Path(__file__).parents[2] / "fixtures" / "apply_patch_contract.json"


def _load_patch_contract_fixture() -> dict:
    return json.loads(_CONTRACT_FIXTURE.read_text(encoding="utf-8"))


def _patch_text(item: dict) -> str:
    return "\n".join(item["patch"])


def _approved_candidate(tool_name: str = "apply_patch") -> dict:
    if tool_name == "draft_document_commit":
        operations = [{"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}]
        diff = "--- /dev/null\n+++ b/docs/a.md\n+# A\n"
    else:
        operations = [{"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}]
        diff = "+# A\n"
    preview_identity = {
        "plan_id": f"{tool_name}-plan",
        "candidate_hash": f"{tool_name}-candidate",
        "tool_name": tool_name,
        "workspace_id": "/tmp",
        "execution_target": "remote_peer",
        "path_space": "remote_peer_workspace",
        "args_hash": f"{tool_name}-args",
    }
    return {
        "plan_id": preview_identity["plan_id"],
        "tool_name": tool_name,
        "workspace_id": "/tmp",
        "execution_target": "remote_peer",
        "path_space": "remote_peer_workspace",
        "args_hash": preview_identity["args_hash"],
        "operations": operations,
        "changes": [{"path": "docs/a.md", "kind": "add", "diff": diff}],
        "diff": diff,
        "candidate_hash": preview_identity["candidate_hash"],
        "preview_identity": preview_identity,
    }


def _preview_meta(tool_name: str = "apply_patch") -> dict:
    candidate = _approved_candidate(tool_name)
    return {
        "preview_identity": candidate["preview_identity"],
        "approved_save_candidate": candidate,
    }


class TestRemoteBackendDispatch:
    def test_shell_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ShellTool(backend=backend)
            result = tool.execute(command="ls", intent="列出当前目录内容。")
            assert "no remote peer" in result.lower()
        finally:
            srv.stop()

    def test_read_file_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ReadFileTool(backend=backend)
            result = tool.execute(file_path="/tmp/foo")
            assert "no remote peer" in result.lower()

            result = tool.execute(file_path="/tmp/foo", offset=0)
            assert "positive integer" in result.lower()

            result = tool.execute(file_path="/tmp/foo", limit=0)
            assert "positive integer" in result.lower()

            result = tool.execute(file_path="/tmp/foo", override="yes")
            assert "boolean" in result.lower()
        finally:
            srv.stop()

    def test_apply_patch_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ApplyPatchTool(backend=backend)
            result = tool.execute(
                patch="*** Begin Patch\n*** Add File: foo.txt\n+bar\n*** End Patch\n"
            )
            assert "no online peer" in result.lower()
        finally:
            srv.stop()

    def test_draft_document_begin_is_runtime_declaration(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = DraftDocumentBeginTool(backend=backend)
            result = tool.execute(
                target_path="docs/architecture.md",
                title="Architecture",
                format="markdown",
            )
            assert "draft document declared" in result.lower()
            assert "target_path: docs/architecture.md" in result

            result = tool.execute(
                target_path="../outside.md",
                title="Bad",
                format="markdown",
            )
            assert "workspace-relative" in result.lower()
        finally:
            srv.stop()

    def test_glob_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = GlobTool(backend=backend)
            result = tool.execute(pattern="*.py")
            assert "no remote peer" in result.lower()

            result = tool.execute(pattern="*.py", path="")
            assert "non-empty string" in result.lower()
        finally:
            srv.stop()

    def test_grep_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = GrepTool(backend=backend)
            result = tool.execute(pattern="foo")
            assert "no remote peer" in result.lower()

            result = tool.execute(pattern="foo", path="")
            assert "non-empty string" in result.lower()

            result = tool.execute(pattern="foo", include=123)
            assert "must be a string" in result.lower()
        finally:
            srv.stop()

    def test_list_file_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ListFileTool(backend=backend)
            result = tool.execute(path="/tmp")
            assert "no remote peer" in result.lower()

            result = tool.execute(path="")
            assert "non-empty string" in result.lower()

            result = tool.execute(path="/tmp", all="yes")
            assert "boolean" in result.lower()

            result = tool.execute(path="/tmp", pattern=123)
            assert "must be a string" in result.lower()
        finally:
            srv.stop()

    def test_lsp_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = LspTool(backend=backend)
            result = tool.execute(operation="documentSymbol", filePath="main.py")
            assert "no remote peer" in result.lower()
        finally:
            srv.stop()

    def test_lsp_requires_peer_feature(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    features=["read_file"],
                )
            )
            backend = RemoteRelayToolBackend(relay_server=srv)
            backend.context.peer_id = resp.peer_id
            tool = LspTool(backend=backend)

            result = tool.execute(operation="documentSymbol", filePath="main.py")

            assert "does not advertise lsp support" in result.lower()
        finally:
            srv.stop()

    def test_shell_invalid_args(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ShellTool(backend=backend)
            result = tool.execute(command="", intent="验证空命令会被拒绝。")
            assert "non-empty string" in result.lower()

            result = tool.execute(command="echo ok", intent="输出 ok 用于连通性检查。", timeout=0)
            assert "positive integer" in result.lower()
        finally:
            srv.stop()

    def test_remote_backend_exec_forwards_to_server(self) -> None:
        """Simulate a full round-trip: register peer, inject response, verify result."""
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            # register peer
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(RegisterRequest(bootstrap_token=bt, cwd="/tmp"))

            backend = RemoteRelayToolBackend(relay_server=srv)
            backend.context.peer_id = resp.peer_id
            tool = ShellTool(backend=backend)

            import threading

            result_holder = {}

            def run_tool():
                result_holder["result"] = tool.execute(command="echo hello", intent="输出 hello 用于远端执行测试。")

            t = threading.Thread(target=run_tool)
            t.start()
            import time

            time.sleep(0.1)

            # inject tool result
            assert len(received) == 1
            outbound = received[0][1]
            req_id = outbound.request_id
            assert outbound.payload["tool_call_id"].startswith("manual-shell-")
            from labrastro_server.interfaces.http.remote.protocol import RelayEnvelope

            env = RelayEnvelope(
                type="tool_result",
                request_id=req_id,
                peer_id=resp.peer_id,
                payload=ExecToolResult(ok=True, result="hello").to_dict(),
            )
            srv.handle_inbound(resp.peer_id, env)
            t.join(timeout=2)

            assert result_holder["result"] == "hello"
        finally:
            srv.stop()

    def test_remote_peer_mcp_tool_forwards_to_peer(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                )
            )
            backend = RemoteRelayToolBackend(relay_server=srv)
            backend.context.peer_id = resp.peer_id
            backend.context.current_tool_call_id = "call-mcp-1"
            backend.context.permission_context = {
                "agent_id": "reviewer",
                "decision": {"action": "allow", "authorized": True},
            }
            tool = RemotePeerMCPTool(
                backend,
                RemoteMCPToolInfo(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                    server_name="docs",
                ),
            )
            spec = tool.tool_spec()
            assert spec.exposure.value == "deferred"
            assert spec.risk.value == "capability"
            assert spec.metadata["tool_id"] == "mcp:docs:search"
            assert spec.metadata["server_name"] == "docs"

            import threading
            import time
            from labrastro_server.interfaces.http.remote.protocol import RelayEnvelope

            holder: dict[str, object] = {}

            def run_tool() -> None:
                holder["result"] = tool.execute(query="hello")

            t = threading.Thread(target=run_tool)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            assert env.payload["tool_call_id"] == "call-mcp-1"
            assert env.payload["permission_context"] == {
                "agent_id": "reviewer",
                "decision": {"action": "allow", "authorized": True},
            }
            assert env.payload["tool_name"] == "mcp"
            assert env.payload["args"]["server_name"] == "docs"
            assert env.payload["args"]["tool_name"] == "search"
            assert env.payload["args"]["arguments"] == {"query": "hello"}
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=env.request_id,
                    peer_id=resp.peer_id,
                    payload=ExecToolResult(ok=True, result="mcp-ok").to_dict(),
                ),
            )
            t.join(timeout=2)
            assert holder["result"] == "mcp-ok"
        finally:
            srv.stop()

    def test_remote_peer_mcp_tool_is_searched_and_executed_through_capability_gateway(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                )
            )
            backend = RemoteRelayToolBackend(relay_server=srv)
            backend.context.peer_id = resp.peer_id
            tool = RemotePeerMCPTool(
                backend,
                RemoteMCPToolInfo(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                    server_name="docs",
                ),
            )
            agent = Agent(
                llm=object(),
                tools=[ToolSearchTool(), CapabilityExecuteTool(), tool],
                config=Config(approval=ApprovalConfig(default_mode="allow")),
            )
            executor = ToolExecutor(agent)

            search_result = json.loads(
                executor.execute(
                    ToolCall(
                        id="search-remote-mcp",
                        name="tool_search",
                        arguments={"query": "docs search"},
                    )
                )
            )
            assert [item["tool_id"] for item in search_result["results"]] == [
                "mcp:docs:search"
            ]
            assert [item["function"]["name"] for item in agent.tool_exposure_plan().direct_provider_schemas()] == [
                "capability_execute",
                "tool_search",
            ]

            import threading
            import time
            from labrastro_server.interfaces.http.remote.protocol import RelayEnvelope

            holder: dict[str, object] = {}

            def run_tool() -> None:
                holder["result"] = executor.execute(
                    ToolCall(
                        id="exec-remote-mcp",
                        name="capability_execute",
                        arguments={
                            "tool_id": "mcp:docs:search",
                            "arguments": {"query": "hello"},
                        },
                    )
                )

            t = threading.Thread(target=run_tool)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            assert env.payload["tool_call_id"] == "exec-remote-mcp:mcp:docs:search"
            assert env.payload["tool_name"] == "mcp"
            assert env.payload["args"]["server_name"] == "docs"
            assert env.payload["args"]["tool_name"] == "search"
            assert env.payload["args"]["arguments"] == {"query": "hello"}
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=env.request_id,
                    peer_id=resp.peer_id,
                    payload=ExecToolResult(ok=True, result="mcp-ok").to_dict(),
                ),
            )
            t.join(timeout=2)
            assert holder["result"] == "mcp-ok"
        finally:
            srv.stop()

    def test_remote_backend_forwards_permission_context(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                )
            )
            backend = RemoteRelayToolBackend(
                relay_server=srv,
                context=ExecutionContext(
                    peer_id=resp.peer_id,
                    permission_context={
                        "agent_id": "worker",
                        "decision": {"action": "allow", "authorized": True},
                    },
                ),
            )
            tool = ShellTool(backend=backend)

            import threading
            import time
            from labrastro_server.interfaces.http.remote.protocol import RelayEnvelope

            holder: dict[str, object] = {}

            def run_tool() -> None:
                holder["result"] = tool.execute(command="echo hello", intent="输出 hello 用于权限上下文测试。")

            t = threading.Thread(target=run_tool)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            assert env.payload["permission_context"] == {
                "agent_id": "worker",
                "decision": {"action": "allow", "authorized": True},
            }
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=env.request_id,
                    peer_id=resp.peer_id,
                    payload=ExecToolResult(ok=True, result="ok").to_dict(),
                ),
            )
            t.join(timeout=2)
            assert holder["result"] == "ok"
        finally:
            srv.stop()

    def test_remote_backend_carries_apply_patch_save_candidate_to_execute(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import (
                RegisterRequest,
                RelayEnvelope,
            )

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    features=["tool_preview"],
                )
            )
            backend = RemoteRelayToolBackend(
                relay_server=srv,
                context=ExecutionContext(peer_id=resp.peer_id, cwd="/tmp"),
            )
            patch = "*** Begin Patch\n*** Add File: docs/a.md\n+# A\n*** End Patch"
            backend.remember_approved_candidate(
                "apply_patch",
                {"patch": patch},
                _approved_candidate("apply_patch"),
            )

            import threading
            import time

            holder: dict[str, object] = {}

            def run_patch() -> None:
                holder["result"] = backend.exec_tool("apply_patch", {"patch": patch})

            t = threading.Thread(target=run_patch)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            assert env.payload["tool_name"] == "apply_patch"
            assert env.payload["args"] == {"patch": patch}
            assert ("expected" + "_state") not in env.payload
            assert env.payload["preview_identity"]["tool_name"] == "apply_patch"
            assert env.payload["approved_save_candidate"]["operations"] == [
                {"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}
            ]
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=env.request_id,
                    peer_id=resp.peer_id,
                    payload=ExecToolResult(ok=True, result="Applied patch").to_dict(),
                ),
            )
            t.join(timeout=2)

            assert holder["result"] == "Applied patch"
        finally:
            srv.stop()

    def test_remote_backend_carries_apply_patch_save_candidate_without_manual_approval_memory(self) -> None:
        from labrastro_server.interfaces.http.remote.protocol import (
            ExecToolResult,
            RegisterRequest,
            RelayEnvelope,
            ToolPreviewResult,
        )

        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    features=["tool_preview"],
                )
            )
            backend = RemoteRelayToolBackend(
                relay_server=srv,
                context=ExecutionContext(peer_id=resp.peer_id, cwd="/tmp"),
            )
            backend.preview_tool = lambda _tool_name, _args: ToolPreviewResult(
                ok=True,
                sections=[{"path": "docs/a.md", "change_kind": "add"}],
                meta=_preview_meta("apply_patch"),
            )
            patch = "*** Begin Patch\n*** Add File: docs/a.md\n+# A\n*** End Patch"

            preview = backend.preview_text_patch(patch)
            assert preview.status == "in_progress"

            import threading
            import time

            holder: dict[str, object] = {}

            def run_patch() -> None:
                holder["result"] = backend.apply_text_patch(patch)

            t = threading.Thread(target=run_patch)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            try:
                assert ("expected" + "_state") not in env.payload
                assert env.payload["preview_identity"]["tool_name"] == "apply_patch"
                assert env.payload["approved_save_candidate"]["operations"] == [
                    {"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}
                ]
            finally:
                srv.handle_inbound(
                    resp.peer_id,
                    RelayEnvelope(
                        type="tool_result",
                        request_id=env.request_id,
                        peer_id=resp.peer_id,
                        payload=ExecToolResult(ok=True, result="Applied patch").to_dict(),
                    ),
                )
            t.join(timeout=2)

            assert holder["result"].status == "completed"
        finally:
            srv.stop()

    def test_remote_backend_commits_document_through_peer_owner(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import (
                RegisterRequest,
                RelayEnvelope,
            )

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    features=["tool_preview"],
                )
            )
            backend = RemoteRelayToolBackend(
                relay_server=srv,
                context=ExecutionContext(peer_id=resp.peer_id, cwd="/tmp"),
            )
            backend.remember_approved_candidate(
                "draft_document_commit",
                {"target_path": "docs/a.md", "content": "# A\n"},
                _approved_candidate("draft_document_commit"),
            )

            import threading
            import time

            holder: dict[str, object] = {}

            def run_commit() -> None:
                holder["result"] = backend.commit_document("docs/a.md", "# A\n")

            t = threading.Thread(target=run_commit)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            assert env.payload["tool_name"] == "draft_document_commit"
            assert env.payload["args"] == {}
            assert ("expected" + "_state") not in env.payload
            assert env.payload["preview_identity"]["tool_name"] == "draft_document_commit"
            assert env.payload["approved_save_candidate"]["operations"] == [
                {"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}
            ]
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=env.request_id,
                    peer_id=resp.peer_id,
                    payload=ExecToolResult(ok=True, result="Committed document docs/a.md").to_dict(),
                ),
            )
            t.join(timeout=2)

            result = holder["result"]
            assert result.status == "completed"
            assert result.message == "Committed document docs/a.md"
        finally:
            srv.stop()

    def test_remote_backend_maps_semantic_apply_patch_preview_failures_from_shared_fixture(
        self,
    ) -> None:
        from labrastro_server.interfaces.http.remote.protocol import ToolPreviewResult

        backend = RemoteRelayToolBackend(relay_server=RelayServer())
        contract = _load_patch_contract_fixture()

        for item in contract["semantic_invalid"]:
            backend.preview_tool = lambda _tool_name, _args, item=item: ToolPreviewResult(
                ok=False,
                error_code="REMOTE_TOOL_ERROR",
                error_message=item["error_contains"],
            )

            result = backend.preview_text_patch(_patch_text(item))

            assert result.status == "failed", item["name"]
            assert result.error is not None
            assert item["error_contains"] in result.error

    def test_remote_backend_rejects_mutation_preview_without_required_save_candidate(
        self,
    ) -> None:
        from labrastro_server.interfaces.http.remote.protocol import ToolPreviewResult

        backend = RemoteRelayToolBackend(relay_server=RelayServer())
        backend.preview_tool = lambda _tool_name, _args: ToolPreviewResult(
            ok=True,
            sections=[{"path": "docs/a.md", "change_kind": "add"}],
            meta={},
        )

        apply_result = backend.preview_text_patch(
            "*** Begin Patch\n*** Add File: docs/a.md\n+# A\n*** End Patch"
        )
        draft_result = backend.preview_document_commit("docs/a.md", "# A\n")

        for result in (apply_result, draft_result):
            assert result.status == "failed"
            assert result.error is not None
            assert "approved_save_candidate" in result.error
            assert "preview_identity" in result.error
            assert "operations" in result.error

    def test_remote_backend_carries_document_save_candidate_to_commit_without_approval_args(
        self,
    ) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import (
                RegisterRequest,
                RelayEnvelope,
                ToolPreviewResult,
            )

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    features=["tool_preview"],
                )
            )
            backend = RemoteRelayToolBackend(
                relay_server=srv,
                context=ExecutionContext(peer_id=resp.peer_id, cwd="/tmp"),
            )
            backend.preview_tool = lambda _tool_name, _args: ToolPreviewResult(
                ok=True,
                diff="--- /dev/null\n+++ b/docs/a.md\n+# A\n",
                meta=_preview_meta("draft_document_commit"),
            )

            preview = backend.preview_document_commit("docs/a.md", "# A\n")

            assert preview.status == "in_progress"

            import threading
            import time

            holder: dict[str, object] = {}

            def run_commit() -> None:
                holder["result"] = backend.commit_document("docs/a.md", "# A\n")

            t = threading.Thread(target=run_commit)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            env = received[0][1]
            assert env.payload["tool_name"] == "draft_document_commit"
            assert env.payload["args"] == {}
            assert ("expected" + "_state") not in env.payload
            assert env.payload["preview_identity"]["tool_name"] == "draft_document_commit"
            assert env.payload["approved_save_candidate"]["operations"] == [
                {"kind": "add", "path": "docs/a.md", "new_content": "# A\n"}
            ]
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=env.request_id,
                    peer_id=resp.peer_id,
                    payload=ExecToolResult(ok=True, result="Committed document docs/a.md").to_dict(),
                ),
            )
            t.join(timeout=2)

            result = holder["result"]
            assert result.status == "completed"
        finally:
            srv.stop()

    def test_remote_backend_does_not_send_empty_save_candidate(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    features=["tool_preview"],
                )
            )
            backend = RemoteRelayToolBackend(
                relay_server=srv,
                context=ExecutionContext(peer_id=resp.peer_id, cwd="/tmp"),
            )
            result = backend.save_candidate({})

            assert received == []
            assert result.status == "failed"
            assert result.error is not None
            assert "approved_save_candidate" in result.error
        finally:
            srv.stop()

    def test_relay_server_fails_inflight_requests_on_disconnect(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            from labrastro_server.interfaces.http.remote.protocol import RegisterRequest

            resp = srv._on_register(RegisterRequest(bootstrap_token=bt, cwd="/tmp"))

            import threading
            import time
            from labrastro_server.interfaces.http.remote.protocol import RelayEnvelope

            result_holder: dict[str, object] = {}

            def run_request() -> None:
                try:
                    srv.send_exec_request(
                        resp.peer_id,
                        ExecToolRequest(
                            tool_name="shell", args={"command": "echo hello"}
                        ),
                        timeout_sec=5,
                    )
                except Exception as exc:
                    result_holder["error"] = exc

            t = threading.Thread(target=run_request)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="disconnect",
                    peer_id=resp.peer_id,
                    payload={"reason": "peer_initiated"},
                ),
            )
            t.join(timeout=2)

            assert isinstance(result_holder.get("error"), PeerDisconnectedError)
        finally:
            srv.stop()
