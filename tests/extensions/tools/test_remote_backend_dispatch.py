"""Tests that builtin tools dispatch to the remote backend correctly."""

from __future__ import annotations

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
    ToolMutationPreviewState,
)
from labrastro_server.relay.server import RelayServer
from labrastro_server.adapters.reuleauxcoder.mcp_tools import RemotePeerMCPTool
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.builtin.draft_document import DraftDocumentBeginTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool


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
            assert "no remote peer" in result.lower()
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
            backend.remember_approved_preview(
                "draft_document_commit",
                {"target_path": "docs/a.md", "content": "# A\n"},
                ToolMutationPreviewState(
                    plan_hash="plan-hash",
                    operations=[
                        {
                            "path": "docs/a.md",
                            "old_exists": False,
                        }
                    ],
                ),
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
            assert env.payload["args"] == {
                "target_path": "docs/a.md",
                "content": "# A\n",
            }
            assert env.payload["expected_state"]["plan_hash"] == "plan-hash"
            assert env.payload["expected_state"]["operations"] == [
                {
                    "path": "docs/a.md",
                    "old_exists": False,
                }
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

    def test_remote_backend_carries_document_preview_state_to_commit_without_approval_args(
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
                meta={
                    "plan_hash": "plan-hash",
                    "operations": [
                        {
                            "path": "docs/a.md",
                            "old_exists": False,
                        }
                    ],
                },
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
            assert env.payload["args"] == {
                "target_path": "docs/a.md",
                "content": "# A\n",
            }
            assert env.payload["expected_state"]["plan_hash"] == "plan-hash"
            assert env.payload["expected_state"]["operations"] == [
                {
                    "path": "docs/a.md",
                    "old_exists": False,
                }
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

    def test_remote_backend_does_not_send_empty_expected_state(self) -> None:
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
            backend.remember_approved_preview(
                "draft_document_commit",
                {"target_path": "docs/a.md", "content": "# A\n"},
                ToolMutationPreviewState(operations=[]),
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
            assert env.payload["expected_state"] == {}
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
