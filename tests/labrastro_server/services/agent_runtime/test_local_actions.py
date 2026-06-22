from __future__ import annotations

import pytest

from labrastro_server.interfaces.http.remote.protocol.local_actions import (
    LocalActionCompleteRequest,
    LocalActionRecord,
)
from labrastro_server.services.agent_runtime.local_actions import (
    LocalActionLeaseError,
    LocalActionService,
)
from labrastro_server.services.agent_runtime.session_projection import (
    agent_run_event_to_session_events,
)
from reuleauxcoder.domain.agent_runtime.models import WorkerKind


def test_local_action_record_rejects_half_bound_scopes() -> None:
    invalid_payloads = [
        {
            "scope": "activation_scoped",
            "agent_run_id": "agent-run-1",
            "local_action_id": "local-action-1",
        },
        {
            "scope": "run_scoped",
            "peer_id": "peer-1",
            "workspace_root": "D:\\AboutDEV\\vika_mcp",
            "local_action_id": "local-action-1",
        },
        {
            "scope": "admin_task_scoped",
            "local_action_id": "local-action-1",
            "action_kind": "install_python_packages",
        },
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            LocalActionRecord.from_dict(payload)


def test_local_action_record_accepts_activation_scoped_visible_action() -> None:
    record = LocalActionRecord.from_dict(
        {
            "scope": "activation_scoped",
            "local_action_id": "local-action-1",
            "agent_run_id": "agent-run-1",
            "activation_id": "activation-1",
            "session_run_id": "session-run-1",
            "branch_binding_id": "branch-main",
            "action_kind": "read_workspace_file",
            "status": "waiting_peer",
            "workspace_root": "D:\\AboutDEV\\vika_mcp",
        }
    )

    assert record.scope == "activation_scoped"
    assert record.local_action_id == "local-action-1"
    assert record.to_dict()["branch_binding_id"] == "branch-main"


def test_local_action_protocol_round_trips_record_and_result() -> None:
    record = LocalActionRecord.from_dict(
        {
            "scope": "activation_scoped",
            "local_action_id": "local-action-1",
            "agent_run_id": "agent-run-1",
            "activation_id": "activation-1",
            "session_run_id": "session-run-1",
            "branch_binding_id": "branch-main",
            "action_kind": "read_workspace_file",
            "status": "waiting_peer",
            "workspace_root": "D:\\AboutDEV\\vika_mcp",
        }
    )
    result = LocalActionCompleteRequest.from_dict(
        {
            "local_action_id": "local-action-1",
            "status": "completed",
            "result": {"summary": "read 120 lines"},
        }
    )

    assert LocalActionRecord.from_dict(record.to_dict()).local_action_id == "local-action-1"
    assert record.status == "waiting_peer"
    assert result.local_action_id == "local-action-1"
    assert result.status == "completed"
    assert result.to_dict()["result"] == {"summary": "read 120 lines"}


def test_local_action_claim_matches_workspace_feature_and_requires_valid_lease() -> None:
    service = LocalActionService()
    service.create_local_action(
        LocalActionRecord.from_dict(
            {
                "scope": "activation_scoped",
                "local_action_id": "local-action-1",
                "agent_run_id": "agent-run-1",
                "activation_id": "activation-1",
                "session_run_id": "session-run-1",
                "branch_binding_id": "branch-main",
                "action_kind": "read_workspace_file",
                "status": "waiting_peer",
                "workspace_root": "D:\\AboutDEV\\vika_mcp",
                "payload": {"path": "README.md"},
            }
        )
    )

    missing_feature = service.claim_local_actions(
        peer_id="peer-2",
        worker_kind=WorkerKind.LOCAL_PEER,
        features={"local_actions"},
        workspace_root="D:\\Other",
        max_actions=1,
    )
    assert missing_feature.actions == []

    claim = service.claim_local_actions(
        peer_id="peer-1",
        worker_kind=WorkerKind.LOCAL_PEER,
        features={"local_actions", "local_action:read_workspace_file"},
        workspace_root="D:\\AboutDEV\\vika_mcp",
        max_actions=1,
    )

    assert claim.actions[0].local_action_id == "local-action-1"
    assert claim.actions[0].lease_id

    with pytest.raises(LocalActionLeaseError):
        service.complete_local_action(
            local_action_id="local-action-1",
            peer_id="peer-1",
            lease_id="wrong-lease",
            status="completed",
            result={"summary": "read 120 lines"},
        )

    completed = service.complete_local_action(
        local_action_id="local-action-1",
        peer_id="peer-1",
        lease_id=claim.actions[0].lease_id,
        status="completed",
        result={"summary": "read 120 lines"},
    )
    assert completed.status == "completed"
    assert completed.result == {"summary": "read 120 lines"}


def test_local_action_waiting_event_projects_to_visible_process_item() -> None:
    projected = agent_run_event_to_session_events(
        {
            "type": "local_action_waiting_peer",
            "agent_run_id": "agent-run-1",
            "seq": 1,
            "payload": {
                "local_action_id": "local-action-1",
                "action_kind": "read_workspace_file",
                "workspace_root": "D:\\AboutDEV\\vika_mcp",
                "status": "waiting_peer",
            },
        }
    )

    assert projected
    event_type, item = projected[0]
    assert event_type == "local_action"
    assert item["kind"] == "local_action"
    assert item["status"] == "waiting_peer"
    assert item["message"]
    assert "{" not in item["message"]
