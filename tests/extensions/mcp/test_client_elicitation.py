from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

from labrastro_server.interfaces.http.remote.service import _SessionRunProjection
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.mcp.adapter import MCPTool
from reuleauxcoder.extensions.mcp.client import MCPClient, MCP_PROTOCOL_VERSION
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.mcp.timeouts import (
    MCP_ELICITATION_TIMEOUT_SEC,
    MCP_REQUEST_TIMEOUT_SEC,
    MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC,
    MCP_TOOL_CALL_TIMEOUT_SEC,
)
from reuleauxcoder.extensions.tools.builtin.capability_execute import (
    CapabilityExecuteTool,
)
from reuleauxcoder.extensions.tools.builtin.tool_search import ToolSearchTool
from reuleauxcoder.extensions.tools.spec import ToolExposure, ToolRisk
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner
from reuleauxcoder.interfaces.events import UIEventBus


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def json_messages(self) -> list[dict[str, Any]]:
        payload = b"".join(self.writes).decode()
        return [json.loads(line) for line in payload.splitlines() if line.strip()]


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        name="docs",
        command="docs-mcp",
        args=[],
        env={},
        cwd=None,
    )


def test_mcp_client_initialize_advertises_latest_form_elicitation_capability(
    monkeypatch,
) -> None:
    async def scenario() -> list[dict[str, Any]]:
        captured_requests: list[dict[str, Any]] = []
        reader = asyncio.StreamReader()
        writer = _FakeWriter()

        class _FakeProcess:
            stdout = reader
            stdin = writer
            returncode = None

            def terminate(self) -> None:
                self.returncode = 0
                reader.feed_eof()

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                self.returncode = -9
                reader.feed_eof()

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        config = _config()
        config.command = sys.executable
        client = MCPClient(
            config,
            elicitation_handler=lambda _request: {"action": "decline", "content": {}},
        )

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            captured_requests.append({"method": method, "params": dict(params)})
            if method == "initialize":
                return {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "docs", "version": "1.0.0"},
                }
            if method == "tools/list":
                return {"tools": []}
            raise AssertionError(f"unexpected MCP request: {method}")

        client._request = fake_request
        try:
            assert await client.connect()
        finally:
            await client.disconnect()
        return captured_requests

    requests = asyncio.run(scenario())

    initialize = requests[0]
    assert initialize["method"] == "initialize"
    assert initialize["params"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert initialize["params"]["capabilities"] == {"elicitation": {"form": {}}}
    assert "tools" not in initialize["params"]["capabilities"]


def test_mcp_client_omits_elicitation_capability_without_unified_handler() -> None:
    assert MCPClient(_config())._client_capabilities() == {}


def test_mcp_client_rejects_unsupported_negotiated_protocol_version(
    monkeypatch,
) -> None:
    async def scenario() -> tuple[bool, list[str]]:
        requested_methods: list[str] = []
        reader = asyncio.StreamReader()
        writer = _FakeWriter()

        class _FakeProcess:
            stdout = reader
            stdin = writer
            returncode = None

            def terminate(self) -> None:
                self.returncode = 0
                reader.feed_eof()

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                self.returncode = -9
                reader.feed_eof()

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        config = _config()
        config.command = sys.executable
        client = MCPClient(
            config,
            elicitation_handler=lambda _request: {"action": "decline", "content": {}},
        )

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            requested_methods.append(method)
            if method == "initialize":
                return {
                    "protocolVersion": "1900-01-01",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "docs", "version": "1.0.0"},
                }
            raise AssertionError(f"unexpected MCP request after failed initialize: {method}")

        client._request = fake_request
        try:
            connected = await client.connect()
        finally:
            await client.disconnect()
        return connected, requested_methods

    connected, requested_methods = asyncio.run(scenario())

    assert connected is False
    assert requested_methods == ["initialize"]


def test_mcp_client_skips_tools_list_without_server_tools_capability(
    monkeypatch,
) -> None:
    async def scenario() -> tuple[bool, list[str], list[MCPToolInfo]]:
        requested_methods: list[str] = []
        reader = asyncio.StreamReader()
        writer = _FakeWriter()

        class _FakeProcess:
            stdout = reader
            stdin = writer
            returncode = None

            def terminate(self) -> None:
                self.returncode = 0
                reader.feed_eof()

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                self.returncode = -9
                reader.feed_eof()

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        config = _config()
        config.command = sys.executable
        client = MCPClient(config)

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            requested_methods.append(method)
            if method == "initialize":
                return {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {},
                    "serverInfo": {"name": "docs", "version": "1.0.0"},
                }
            raise AssertionError(f"unexpected MCP request without tools capability: {method}")

        client._request = fake_request
        try:
            connected = await client.connect()
            tools = list(client.tools)
        finally:
            await client.disconnect()
        return connected, requested_methods, tools

    connected, requested_methods, tools = asyncio.run(scenario())

    assert connected is True
    assert requested_methods == ["initialize"]
    assert tools == []


def test_mcp_client_declines_unadvertised_url_elicitation_mode() -> None:
    async def scenario() -> tuple[dict[str, Any], bool, list[Any]]:
        events: list[Any] = []
        bus = UIEventBus()
        bus.subscribe(events.append, replay_history=False)
        handler_called = False

        def elicitation_handler(_request: dict[str, Any]) -> dict[str, Any]:
            nonlocal handler_called
            handler_called = True
            return {"action": "accept", "content": {}}

        client = MCPClient(
            _config(),
            ui_bus=bus,
            elicitation_handler=elicitation_handler,
        )
        result = await client._handle_elicitation_create(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "elicitation/create",
                "params": {
                    "mode": "url",
                    "elicitationId": "url-1",
                    "url": "https://example.test/connect",
                    "message": "Open a URL to continue.",
                },
            }
        )
        return result, handler_called, events

    result, handler_called, events = asyncio.run(scenario())

    assert result == {
        "action": "decline",
        "content": {},
        "reason": "mcp_elicitation_unsupported_mode",
    }
    assert handler_called is False
    result_events = [
        event.data
        for event in events
        if event.data.get("event_name") == "ElicitationResult"
    ]
    assert result_events[0]["diagnostics"] == [
        {
            "code": "mcp_elicitation_unsupported_mode",
            "message": "Unsupported MCP elicitation mode: url",
            "severity": "warning",
        }
    ]


def test_mcp_tool_passes_bound_lifecycle_context_to_client_call() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            lifecycle_context: dict[str, Any] | None = None,
        ) -> str:
            self.calls.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "lifecycle_context": lifecycle_context,
                }
            )
            return "ok"

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    try:
        client = _FakeClient()
        tool = MCPTool(
            client,
            MCPToolInfo(
                name="search",
                description="Search docs",
                input_schema={"type": "object"},
                server_name="docs",
            ),
            loop,
        )
        context = {
            "session_run_id": "session-1",
            "agent_run_id": "agent-run-1",
            "turn_id": "turn-1",
            "tool_call_id": "call-mcp-1",
            "tool_name": "search",
            "mcp_server": "docs",
        }

        restore = tool.bind_lifecycle_context(context)
        assert tool.execute(query="hooks") == "ok"
        restore()
        assert client.calls == [
            {
                "name": "search",
                "arguments": {"query": "hooks"},
                "lifecycle_context": context,
            }
        ]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_mcp_tool_spec_is_deferred_and_server_scoped() -> None:
    tool = MCPTool(
        SimpleNamespace(),
        MCPToolInfo(
            name="search",
            description="Search docs",
            input_schema={"type": "object"},
            server_name="docs",
        ),
        asyncio.new_event_loop(),
    )
    try:
        spec = tool.tool_spec()
    finally:
        tool._loop.close()

    assert spec.exposure == ToolExposure.DEFERRED
    assert spec.risk == ToolRisk.CAPABILITY
    assert spec.metadata["tool_id"] == "mcp:docs:search"
    assert spec.metadata["server_name"] == "docs"
    assert spec.metadata["source_type"] == "local_mcp"
    assert set(spec.search_keywords) >= {"mcp", "local", "docs", "search"}


def test_local_mcp_tools_are_searchable_and_executable_by_tool_id() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            lifecycle_context: dict[str, Any] | None = None,
        ) -> str:
            self.calls.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "lifecycle_context": lifecycle_context,
                }
            )
            return f"{name}:{arguments['query']}"

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    while not loop.is_running():
        time.sleep(0.01)

    try:
        client = _FakeClient()
        docs_tool = MCPTool(
            client,
            MCPToolInfo(
                name="search",
                description="Search docs",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                server_name="docs",
            ),
            loop,
        )
        github_tool = MCPTool(
            client,
            MCPToolInfo(
                name="search",
                description="Search repositories",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                server_name="github",
            ),
            loop,
        )
        agent = Agent(
            llm=SimpleNamespace(),
            tools=[
                ToolSearchTool(),
                CapabilityExecuteTool(),
                docs_tool,
                github_tool,
            ],
            config=Config(approval=ApprovalConfig(default_mode="allow")),
        )
        executor = ToolExecutor(agent)

        assert [
            item["function"]["name"]
            for item in agent.tool_exposure_plan().direct_provider_schemas()
        ] == ["capability_execute", "tool_search"]
        assert agent.tool_route_plan().get_executor_by_id("mcp:docs:search") is docs_tool
        assert (
            agent.tool_route_plan().get_executor_by_id("mcp:github:search")
            is github_tool
        )

        search_result = executor.execute(
            ToolCall(
                id="search-local-mcp",
                name="tool_search",
                arguments={"query": "docs", "max_results": 5},
            )
        )
        search_payload = json.loads(search_result)
        assert [item["tool_id"] for item in search_payload["results"]] == [
            "mcp:docs:search"
        ]

        execute_result = executor.execute(
            ToolCall(
                id="exec-local-mcp",
                name="capability_execute",
                arguments={
                    "tool_id": "mcp:docs:search",
                    "arguments": {"query": "hooks"},
                },
            )
        )

        assert execute_result == "search:hooks"
        assert client.calls[0]["name"] == "search"
        assert client.calls[0]["arguments"] == {"query": "hooks"}
        assert client.calls[0]["lifecycle_context"]["tool_call_id"] == (
            "exec-local-mcp:mcp:docs:search"
        )
        assert client.calls[0]["lifecycle_context"]["tool_name"] == "search"
        assert client.calls[0]["lifecycle_context"]["mcp_server"] == "docs"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_mcp_tool_call_timeout_exceeds_default_elicitation_wait_and_cancels(
    monkeypatch,
) -> None:
    assert MCP_TOOL_CALL_TIMEOUT_SEC > MCP_ELICITATION_TIMEOUT_SEC

    class _FakeClient:
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            lifecycle_context: dict[str, Any] | None = None,
        ) -> str:
            return "ok"

    class _FakeLoop:
        def is_running(self) -> bool:
            return True

    class _TimeoutFuture:
        def __init__(self) -> None:
            self.timeout: float | None = None
            self.cancelled = False

        def result(self, timeout: float | None = None) -> str:
            self.timeout = timeout
            raise concurrent.futures.TimeoutError()

        def cancel(self) -> bool:
            self.cancelled = True
            return True

    import concurrent.futures

    future = _TimeoutFuture()

    def fake_run_coroutine_threadsafe(coroutine, _loop):
        coroutine.close()
        return future

    monkeypatch.setattr(
        asyncio,
        "run_coroutine_threadsafe",
        fake_run_coroutine_threadsafe,
    )

    tool = MCPTool(
        _FakeClient(),
        MCPToolInfo(
            name="search",
            description="Search docs",
            input_schema={"type": "object"},
            server_name="docs",
        ),
        _FakeLoop(),
    )

    assert tool.execute(query="hooks") == "Error: MCP tool call timed out"
    assert future.timeout == MCP_TOOL_CALL_TIMEOUT_SEC
    assert future.cancelled is True


def test_mcp_client_uses_long_timeout_only_for_tools_call_requests(monkeypatch) -> None:
    async def scenario() -> dict[str, float | None]:
        captured: dict[str, float | None] = {}
        client = MCPClient(_config())
        client._reader = asyncio.StreamReader()
        client._writer = _FakeWriter()

        async def fake_wait_for(_future, timeout: float | None = None):
            captured["tools_call"] = timeout
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        assert await client._request("tools/call", {"name": "search", "arguments": {}}) is None
        return captured

    captured = asyncio.run(scenario())

    assert captured["tools_call"] == MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC
    assert MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC > MCP_ELICITATION_TIMEOUT_SEC
    assert MCP_TOOL_CALL_TIMEOUT_SEC > MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC
    assert MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC > MCP_REQUEST_TIMEOUT_SEC


def test_mcp_tool_keeps_bound_lifecycle_context_isolated_across_threads() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            lifecycle_context: dict[str, Any] | None = None,
        ) -> str:
            self.calls.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "lifecycle_context": lifecycle_context,
                }
            )
            return f"ok-{arguments['query']}"

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    while not loop.is_running():
        time.sleep(0.01)

    try:
        client = _FakeClient()
        tool = MCPTool(
            client,
            MCPToolInfo(
                name="search",
                description="Search docs",
                input_schema={"type": "object"},
                server_name="docs",
            ),
            loop,
        )
        a_bound = threading.Event()
        b_bound = threading.Event()
        a_executed = threading.Event()
        results: dict[str, str] = {}
        errors: list[BaseException] = []

        def run_a() -> None:
            try:
                restore = tool.bind_lifecycle_context(
                    {
                        "session_run_id": "session-a",
                        "agent_run_id": "agent-run-a",
                        "turn_id": "turn-a",
                        "tool_call_id": "call-a",
                        "tool_name": "search",
                        "mcp_server": "docs",
                    }
                )
                try:
                    a_bound.set()
                    if not b_bound.wait(timeout=2):
                        raise AssertionError("thread b did not bind context")
                    results["a"] = tool.execute(query="alpha")
                    a_executed.set()
                finally:
                    restore()
            except BaseException as exc:
                errors.append(exc)
                a_executed.set()

        def run_b() -> None:
            try:
                if not a_bound.wait(timeout=2):
                    raise AssertionError("thread a did not bind context")
                restore = tool.bind_lifecycle_context(
                    {
                        "session_run_id": "session-b",
                        "agent_run_id": "agent-run-b",
                        "turn_id": "turn-b",
                        "tool_call_id": "call-b",
                        "tool_name": "search",
                        "mcp_server": "docs",
                    }
                )
                try:
                    b_bound.set()
                    if not a_executed.wait(timeout=2):
                        raise AssertionError("thread a did not execute")
                    results["b"] = tool.execute(query="beta")
                finally:
                    restore()
            except BaseException as exc:
                errors.append(exc)

        worker_a = threading.Thread(target=run_a)
        worker_b = threading.Thread(target=run_b)
        worker_a.start()
        worker_b.start()
        worker_a.join(timeout=3)
        worker_b.join(timeout=3)

        assert not worker_a.is_alive()
        assert not worker_b.is_alive()
        assert not errors
        assert results == {"a": "ok-alpha", "b": "ok-beta"}

        calls_by_query = {
            call["arguments"]["query"]: call["lifecycle_context"]["tool_call_id"]
            for call in client.calls
        }
        assert calls_by_query == {"alpha": "call-a", "beta": "call-b"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_mcp_client_serializes_tool_calls_so_elicitation_uses_matching_context() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[str], int]:
        captured_requests: list[dict[str, Any]] = []
        active_requests = 0
        max_active_requests = 0

        async def elicitation_handler(request: dict[str, Any]) -> dict[str, Any]:
            captured_requests.append(dict(request))
            return {
                "action": "accept",
                "content": {"tool_call_id": request["tool_call_id"]},
            }

        client = MCPClient(_config(), elicitation_handler=elicitation_handler)
        client._initialized = True
        client.is_connected = lambda: True

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal active_requests, max_active_requests
            assert method == "tools/call"
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            try:
                await asyncio.sleep(0)
                query = params["arguments"]["query"]
                await client._handle_elicitation_create(
                    {
                        "jsonrpc": "2.0",
                        "id": f"elicitation-{query}",
                        "method": "elicitation/create",
                        "params": {
                            "message": f"Choose repository for {query}",
                            "requestedSchema": {"type": "object"},
                        },
                    }
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"done-{query}",
                        }
                    ]
                }
            finally:
                active_requests -= 1

        client._request = fake_request

        results = await asyncio.gather(
            client.call_tool(
                "search",
                {"query": "alpha"},
                lifecycle_context={
                    "session_run_id": "session-1",
                    "agent_run_id": "agent-run-1",
                    "turn_id": "turn-1",
                    "tool_call_id": "call-alpha",
                    "tool_name": "search",
                    "mcp_server": "docs",
                },
            ),
            client.call_tool(
                "search",
                {"query": "beta"},
                lifecycle_context={
                    "session_run_id": "session-1",
                    "agent_run_id": "agent-run-1",
                    "turn_id": "turn-1",
                    "tool_call_id": "call-beta",
                    "tool_name": "search",
                    "mcp_server": "docs",
                },
            ),
        )
        return captured_requests, results, max_active_requests

    requests, results, max_active_requests = asyncio.run(scenario())

    assert results == ["done-alpha", "done-beta"]
    assert max_active_requests == 1
    requests_by_query = {
        request["tool_arguments"]["query"]: request["tool_call_id"]
        for request in requests
    }
    assert requests_by_query == {
        "alpha": "call-alpha",
        "beta": "call-beta",
    }


def test_mcp_client_routes_elicitation_request_to_unified_audit_and_returns_result() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any], list[Any]]:
        events: list[Any] = []
        bus = UIEventBus()
        bus.subscribe(events.append, replay_history=False)
        captured: dict[str, Any] = {}

        async def elicitation_handler(request: dict[str, Any]) -> dict[str, Any]:
            captured["request"] = request
            return {"action": "accept", "content": {"repo": "Labrastro"}}

        client = MCPClient(
            _config(),
            ui_bus=bus,
            elicitation_handler=elicitation_handler,
        )
        client._active_tool_name = "search"
        client._active_tool_arguments = {"query": "hooks"}
        reader = asyncio.StreamReader()
        writer = _FakeWriter()
        client._reader = reader
        client._writer = writer
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "elicitation/create",
                        "params": {
                            "message": "Choose repository",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"repo": {"type": "string"}},
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()

        await client._receive_loop()
        return writer.json_messages(), captured["request"], events

    messages, request, events = asyncio.run(scenario())

    assert messages == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"action": "accept", "content": {"repo": "Labrastro"}},
        }
    ]
    assert request["mcp_server"] == "docs"
    assert request["tool_name"] == "search"
    assert request["message"] == "Choose repository"
    assert request["input_schema"] == {
        "type": "object",
        "properties": {"repo": {"type": "string"}},
    }
    assert request["tool_arguments"] == {"query": "hooks"}

    lifecycle_events = [
        event.data for event in events if event.data.get("event_type") == "lifecycle_hook"
    ]
    assert [event["event_name"] for event in lifecycle_events] == [
        "Elicitation",
        "ElicitationResult",
    ]
    assert lifecycle_events[0]["payload"]["mcp_server"] == "docs"
    assert lifecycle_events[0]["payload"]["tool_name"] == "search"
    assert lifecycle_events[0]["payload"]["input_schema"] == request["input_schema"]
    assert lifecycle_events[1]["payload"]["result_action"] == "accept"
    assert lifecycle_events[1]["payload"]["result_content"] == {"repo": "Labrastro"}


def test_live_stdio_mcp_elicitation_roundtrips_through_session_user_input(tmp_path) -> None:
    server_script = tmp_path / "live_mcp_server.py"
    server_script.write_text(
        """
import json
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


def read_message():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "initialize":
        params = message.get("params") or {}
        if params.get("protocolVersion") != "__MCP_PROTOCOL_VERSION__":
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": -32602,
                    "message": "unexpected protocol version",
                    "data": {"requested": params.get("protocolVersion")},
                },
            })
            continue
        if params.get("capabilities") != {"elicitation": {"form": {}}}:
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": -32602,
                    "message": "unexpected client capabilities",
                    "data": {"capabilities": params.get("capabilities")},
                },
            })
            continue
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "live-docs", "version": "1.0.0"},
            },
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "tools": [
                    {
                        "name": "search",
                        "description": "Search docs",
                        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                    }
                ]
            },
        })
    elif method == "tools/call":
        tool_call_id = message["id"]
        send({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "elicitation/create",
            "params": {
                "message": "Choose repository",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"repo": {"type": "string"}},
                },
            },
        })
        elicitation_response = {}
        while True:
            response = read_message()
            if response is None:
                break
            if response.get("id") == 99:
                elicitation_response = response.get("result") or {}
                break
        repo = (elicitation_response.get("content") or {}).get("repo", "")
        send({
            "jsonrpc": "2.0",
            "id": tool_call_id,
            "result": {
                "content": [{"type": "text", "text": "repo=" + repo}],
            },
        })
""".replace("__MCP_PROTOCOL_VERSION__", MCP_PROTOCOL_VERSION),
        encoding="utf-8",
    )

    session = _SessionRunProjection(session_run_id="run-live", peer_id="peer-live")
    runner = AppRunner()
    runner._relay_http_service = SimpleNamespace(
        _get_session_run=lambda session_run_id: (
            session if session_run_id == "run-live" else None
        )
    )

    async def scenario() -> tuple[str, list[dict[str, Any]]]:
        client = MCPClient(
            SimpleNamespace(
                name="docs",
                command=sys.executable,
                args=[str(server_script)],
                env={},
                cwd=str(tmp_path),
            ),
            elicitation_handler=runner._mcp_elicitation_handler,
        )
        assert await client.connect()

        def resolve_user_input() -> None:
            deadline = time.time() + 3
            while time.time() < deadline:
                request_event = next(
                    (
                        event
                        for event in session.events
                        if event["type"] == "user_input_request"
                    ),
                    None,
                )
                if request_event is not None:
                    session.resolve_user_input(
                        request_event["payload"]["input_id"],
                        "accept",
                        {"repo": "Labrastro"},
                        "selected",
                    )
                    return
                time.sleep(0.01)
            raise AssertionError("MCP elicitation user input request was not emitted")

        resolver = threading.Thread(target=resolve_user_input)
        resolver.start()
        try:
            output = await client.call_tool(
                "search",
                {"query": "hooks"},
                lifecycle_context={
                    "session_run_id": "run-live",
                    "agent_run_id": "agent-run-live",
                    "turn_id": "turn-live",
                    "tool_call_id": "tool-call-live",
                    "tool_name": "search",
                    "mcp_server": "docs",
                },
            )
        finally:
            resolver.join(timeout=3)
            await client.disconnect()
        assert not resolver.is_alive()
        return output, session.events

    output, events = asyncio.run(scenario())

    assert output == "repo=Labrastro"
    request_events = [event for event in events if event["type"] == "user_input_request"]
    assert request_events
    assert request_events[-1]["payload"]["message"] == "Choose repository"
    assert request_events[-1]["payload"]["session_run_id"] == "run-live"
    assert request_events[-1]["payload"]["tool_call_id"] == "tool-call-live"
    resolved_events = [event for event in events if event["type"] == "user_input_resolved"]
    resolved_payload = resolved_events[-1]["payload"]
    assert {
        key: resolved_payload[key]
        for key in ("input_id", "kind", "action", "content", "reason")
    } == {
        "input_id": request_events[-1]["payload"]["input_id"],
        "kind": "mcp_elicitation",
        "action": "accept",
        "content": {"repo": "Labrastro"},
        "reason": "selected",
    }


def test_mcp_client_emits_elicitation_lifecycle_to_bound_agent_event_emitter() -> None:
    async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        emitted: list[dict[str, Any]] = []

        async def elicitation_handler(_request: dict[str, Any]) -> dict[str, Any]:
            return {"action": "accept", "content": {"repo": "Labrastro"}}

        client = MCPClient(
            _config(),
            elicitation_handler=elicitation_handler,
        )
        client._active_tool_name = "search"
        client._active_tool_arguments = {"query": "hooks"}
        client._active_lifecycle_context = {
            "session_run_id": "session-1",
            "agent_run_id": "agent-run-1",
            "turn_id": "turn-1",
            "tool_call_id": "call-mcp-1",
            "tool_name": "search",
            "mcp_server": "docs",
            "_agent_lifecycle_event_emitter": emitted.append,
        }

        result = await client._handle_elicitation_create(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "elicitation/create",
                "params": {
                    "message": "Choose repository",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"repo": {"type": "string"}},
                    },
                },
            }
        )
        return result, emitted

    result, emitted = asyncio.run(scenario())

    assert result == {"action": "accept", "content": {"repo": "Labrastro"}}
    assert [event["event_name"] for event in emitted] == [
        "Elicitation",
        "ElicitationResult",
    ]
    assert emitted[0]["session_run_id"] == "session-1"
    assert emitted[0]["agent_run_id"] == "agent-run-1"
    assert emitted[0]["turn_id"] == "turn-1"
    assert emitted[0]["tool_call_id"] == "call-mcp-1"
    assert emitted[0]["tool_name"] == "search"
    assert emitted[0]["mcp_server"] == "docs"
    assert emitted[0]["payload"]["message"] == "Choose repository"
    assert emitted[1]["payload"]["result_action"] == "accept"
    assert emitted[1]["payload"]["result_content"] == {"repo": "Labrastro"}


def test_mcp_client_elicitation_without_unified_handler_declines_and_audits() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[Any]]:
        events: list[Any] = []
        bus = UIEventBus()
        bus.subscribe(events.append, replay_history=False)
        client = MCPClient(_config(), ui_bus=bus)
        client._active_tool_name = "search"
        reader = asyncio.StreamReader()
        writer = _FakeWriter()
        client._reader = reader
        client._writer = writer
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "elicitation/create",
                        "params": {
                            "message": "Enter token",
                            "requestedSchema": {"type": "object"},
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()

        await client._receive_loop()
        return writer.json_messages(), events

    messages, events = asyncio.run(scenario())

    assert messages == [
        {
            "jsonrpc": "2.0",
            "id": 8,
            "result": {
                "action": "decline",
                "content": {},
                "reason": "mcp_elicitation_unhandled",
            },
        }
    ]
    result_events = [
        event.data
        for event in events
        if event.data.get("event_name") == "ElicitationResult"
    ]
    assert result_events[0]["payload"]["result_action"] == "decline"
    assert result_events[0]["diagnostics"] == [
        {
            "code": "mcp_elicitation_unhandled",
            "message": "MCP elicitation has no unified input handler.",
            "severity": "warning",
        }
    ]
