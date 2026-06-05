from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from reuleauxcoder.extensions.mcp import manager as manager_module
from reuleauxcoder.extensions.mcp.manager import MCPManager


def test_mcp_manager_passes_unified_elicitation_handler_to_clients(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeMCPClient:
        def __init__(self, config, *, ui_bus=None, elicitation_handler=None):
            captured["config"] = config
            captured["ui_bus"] = ui_bus
            captured["elicitation_handler"] = elicitation_handler
            self.tools = []

        async def connect(self) -> bool:
            return True

    monkeypatch.setattr(manager_module, "MCPClient", FakeMCPClient)

    ui_bus = object()

    def elicitation_handler(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "accept", "content": {"answer": request.get("message")}}

    manager = MCPManager(ui_bus=ui_bus, elicitation_handler=elicitation_handler)
    try:
        assert manager.connect_server(
            SimpleNamespace(name="server-a", enabled=True)
        ) is True
    finally:
        manager.stop()

    assert captured["ui_bus"] is ui_bus
    assert captured["elicitation_handler"] is elicitation_handler
