from __future__ import annotations

import json

from reuleauxcoder.extensions.tools.mention import MentionAgentTool
from labrastro_server.adapters.reuleauxcoder.taskflow_dispatcher import (
    ReuleauxCoderTaskflowDispatcher,
)
from labrastro_server.services.agent_runtime.control_plane import AgentRunControlPlane
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.taskflow.service import TaskflowService


def test_mention_agent_tool_creates_record_without_dispatching_agent_run() -> None:
    runtime = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {"docs_profile": {"executor": "fake"}},
            "agents": {
                "docs": {
                    "aliases": ["writer"],
                    "runtime_profile": "docs_profile",
                    "dispatch": {"profile": "Best for documentation tasks."},
                }
            },
        }
    )
    taskflow = TaskflowService(dispatcher=ReuleauxCoderTaskflowDispatcher(runtime))
    service = IssueAssignmentService(taskflow_service=taskflow)
    issue = service.create_issue(title="Docs", description="Write docs", peer_id="p1")
    tool = MentionAgentTool(service, peer_id="p1")

    result = json.loads(
        tool.execute(
            "create_mention",
            raw_text="@writer please help",
            issue_id=issue.id,
            prompt="Write docs",
        )
    )

    assert result["ok"] is True
    assert result["mention"]["resolved_agent_id"] == "docs"
    assert result["mention"]["assignment_id"]
    assert runtime.list_agent_runs() == []
