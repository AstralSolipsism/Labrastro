from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_removed_session_fork_remote_contract_does_not_reappear() -> None:
    scanned = {
        "registry": _read("labrastro_server/interfaces/http/remote/protocol/registry.py"),
        "session_protocol": _read(
            "labrastro_server/interfaces/http/remote/protocol/sessions.py"
        ),
        "session_protocol_init": _read(
            "labrastro_server/interfaces/http/remote/protocol/__init__.py"
        ),
        "session_routes": _read(
            "labrastro_server/interfaces/http/remote/routes/sessions.py"
        ),
        "remote_relay": _read("reuleauxcoder/interfaces/entrypoint/remote_relay.py"),
    }

    forbidden = [
        "/remote/sessions/fork",
        "sessions.fork",
        "SessionForkRequest",
        'action == "fork"',
        "SessionStore.clone",
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []


def test_relation_decision_fields_are_not_written_to_free_metadata() -> None:
    scanned = {
        "control_plane": _read(
            "labrastro_server/services/agent_runtime/control_plane.py"
        ),
        "postgres_store": _read(
            "labrastro_server/services/agent_runtime/postgres_store.py"
        ),
        "runtime_store": _read(
            "labrastro_server/services/agent_runtime/runtime_store.py"
        ),
    }

    forbidden = [
        "relation_metadata = {",
        'relation.metadata.get("conversation_scope"',
        'relation.metadata.get("wait"',
        'relation.metadata.get("thread_key"',
        'relation.metadata.get("parent_session_id"',
        'relation.metadata.get("workspace_root"',
        'relation.metadata.get("branch_git_ref"',
        'relation.metadata.get("branch_worktree_ref"',
        'relation.metadata.get("fork_workspace_ref"',
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []


def test_activation_steer_waiting_or_terminal_paths_are_forbidden() -> None:
    scanned = {
        "control_plane": _read(
            "labrastro_server/services/agent_runtime/control_plane.py"
        ),
        "postgres_store": _read(
            "labrastro_server/services/agent_runtime/postgres_store.py"
        ),
    }

    forbidden = [
        '_ACTIVE_STEER_AGENT_RUN_STATUSES = {"queued", "dispatched", "running", "waiting"}',
        "activation_steer_pending_feedback",
        "same_activation_steer_delivery_unavailable",
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []


def test_branch_and_fork_use_agent_run_relation_control_plane_path() -> None:
    scanned = {
        "models": _read("reuleauxcoder/domain/agent_runtime/models.py"),
        "control_plane": _read(
            "labrastro_server/services/agent_runtime/control_plane.py"
        ),
        "admin_routes": _read(
            "labrastro_server/interfaces/http/remote/routes/admin.py"
        ),
        "registry": _read("labrastro_server/interfaces/http/remote/protocol/registry.py"),
        "worktree": _read("labrastro_server/services/agent_runtime/worktree.py"),
    }

    required = [
        ("models", "BRANCH = \"branch\""),
        ("models", "\"branch_worktree_ref\""),
        ("models", "\"fork_workspace_ref\""),
        ("models", "\"target_owner_session_run_id\""),
        ("control_plane", "def branch_agent_run("),
        ("control_plane", "def fork_agent_run("),
        ("control_plane", "relation_type=AgentRunRelationType.BRANCH"),
        ("control_plane", "relation_type=AgentRunRelationType.FORK"),
        ("admin_routes", 'path == "/remote/admin/agent-runs/branch"'),
        ("admin_routes", 'path == "/remote/admin/agent-runs/fork"'),
        ("registry", "admin.agent_runs.branch"),
        ("registry", "admin.agent_runs.fork"),
        ("worktree", "git\", \"-C\""),
        ("worktree", "def create_branch_worktree("),
        ("worktree", "def cleanup_branch_worktree("),
        ("worktree", "\"add\""),
        ("worktree", "\"remove\""),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]

    assert missing == []


def test_activation_steer_mailbox_delivery_path_is_not_feedback_fallback() -> None:
    scanned = {
        "control_plane": _read(
            "labrastro_server/services/agent_runtime/control_plane.py"
        ),
        "postgres_store": _read(
            "labrastro_server/services/agent_runtime/postgres_store.py"
        ),
        "agent_routes": _read(
            "labrastro_server/interfaces/http/remote/routes/agent_runs.py"
        ),
        "projection": _read(
            "labrastro_server/services/agent_runtime/session_projection.py"
        ),
    }

    required = [
        ("control_plane", "activation_steer_delivering"),
        ("control_plane", "activation_steer_delivered"),
        ("control_plane", "delivered_steer_ids"),
        ("postgres_store", "activation_steer_delivering"),
        ("postgres_store", "activation_steer_delivered"),
        ("agent_routes", "delivered_steer_ids"),
        ("projection", "activation_steer_delivering"),
        ("projection", "activation_steer_delivered"),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]

    assert missing == []


def test_agent_thread_binding_cleanup_is_control_plane_owned() -> None:
    scanned = {
        "control_plane": _read(
            "labrastro_server/services/agent_runtime/control_plane.py"
        ),
        "postgres_store": _read(
            "labrastro_server/services/agent_runtime/postgres_store.py"
        ),
        "runtime_store": _read(
            "labrastro_server/services/agent_runtime/runtime_store.py"
        ),
    }

    required = [
        ("control_plane", "def close_agent_thread_binding("),
        ("control_plane", "def mark_agent_thread_binding_unavailable("),
        ("control_plane", "def delete_agent_thread_bindings_for_owner_session("),
        ("control_plane", "def invalidate_agent_thread_bindings("),
        ("control_plane", "agent_thread_binding_deleted"),
        ("postgres_store", "def set_agent_thread_binding_status("),
        ("postgres_store", "def delete_agent_thread_bindings_for_owner_session("),
        ("runtime_store", "def set_agent_thread_binding_status("),
        ("runtime_store", "def delete_agent_thread_bindings_for_owner_session("),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]

    assert missing == []


def test_legacy_session_run_guidance_family_and_agent_loop_injection_are_forbidden() -> None:
    scanned = {
        "registry": _read("labrastro_server/interfaces/http/remote/protocol/registry.py"),
        "chat_protocol": _read("labrastro_server/interfaces/http/remote/protocol/chat.py"),
        "protocol_init": _read("labrastro_server/interfaces/http/remote/protocol/__init__.py"),
        "chat_routes": _read("labrastro_server/interfaces/http/remote/routes/chat.py"),
        "remote_service": _read("labrastro_server/interfaces/http/remote/service.py"),
        "remote_relay": _read("reuleauxcoder/interfaces/entrypoint/remote_relay.py"),
        "agent_loop": _read("reuleauxcoder/domain/agent/loop.py"),
    }

    guidance = "follow" + "_up"
    dashed_guidance = "follow" + "-up"
    forbidden = [
        "/remote/session-runs/" + dashed_guidance,
        "/remote/session-runs/" + dashed_guidance + "/cancel",
        "session_run." + guidance,
        "session_run." + guidance + "_cancel",
        "SessionRun" + "FollowUpRequest",
        "SessionRun" + "FollowUpCancelRequest",
        "SessionRun" + "FollowUpResponse",
        "session_run_" + guidance + "_",
        "submit_" + guidance,
        "queue_" + guidance,
        "cancel_" + guidance,
        "consume_" + "follow" + "_ups",
        "_inject_pending_" + "follow" + "_ups",
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []


def test_session_run_continue_and_agent_run_protocol_contract_are_required() -> None:
    scanned = {
        "registry": _read("labrastro_server/interfaces/http/remote/protocol/registry.py"),
        "protocol_init": _read("labrastro_server/interfaces/http/remote/protocol/__init__.py"),
        "agent_runs_protocol": (
            _read("labrastro_server/interfaces/http/remote/protocol/agent_runs.py")
            if (REPO_ROOT / "labrastro_server/interfaces/http/remote/protocol/agent_runs.py").exists()
            else ""
        ),
        "contracts": _read("labrastro_server/interfaces/http/remote/protocol/contracts.json"),
    }

    required = [
        ("registry", "session_run.continue"),
        ("registry", "/remote/session-runs/continue"),
        ("registry", "SessionRunContinueRequest"),
        ("registry", "SessionRunContinueResponse"),
        ("protocol_init", "SessionRunContinueRequest"),
        ("protocol_init", "SessionRunContinueResponse"),
        ("agent_runs_protocol", "AgentRunActivationHeartbeatRequest"),
        ("agent_runs_protocol", "AgentRunActivationHeartbeatResponse"),
        ("agent_runs_protocol", "AgentRunSteerRequest"),
        ("agent_runs_protocol", "AgentRunSteerResponse"),
        ("contracts", '"name": "session_run.continue"'),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]

    assert missing == []


def test_public_remote_interfaces_do_not_accept_camel_case_aliases() -> None:
    scanned = {
        "chat_protocol": _read("labrastro_server/interfaces/http/remote/protocol/chat.py"),
        "admin_routes": _read("labrastro_server/interfaces/http/remote/routes/admin.py"),
        "http_protocol_tests": _read("tests/labrastro_server/http/test_protocol.py"),
    }

    forbidden = [
        "client" + "RequestId",
        "session" + "Id",
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []


def test_capability_package_ingest_has_single_public_start_route() -> None:
    scanned = {
        "admin_routes": _read("labrastro_server/interfaces/http/remote/routes/admin.py"),
        "remote_service_tests": _read("tests/labrastro_server/http/test_remote_service.py"),
        "backend_runtime_docs": _read("docs/agent-context/backend-runtime-map.md"),
    }

    required = [
        ("admin_routes", "/remote/admin/capability-packages/ingest/session/start"),
    ]
    forbidden = [
        "/remote/admin/capability-packages/ingest/start",
        "/remote/admin/capability-packages/ingest/status",
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert missing == []
    assert offenders == []


def test_session_run_routes_do_not_drive_direct_prompt_execution() -> None:
    scanned = {
        "chat_routes": _read("labrastro_server/interfaces/http/remote/routes/chat.py"),
        "remote_service": _read("labrastro_server/interfaces/http/remote/service.py"),
        "remote_relay": _read("reuleauxcoder/interfaces/entrypoint/remote_relay.py"),
        "remote_service_tests": _read("tests/labrastro_server/http/test_remote_service.py"),
    }

    forbidden = [
        "session_run_" + "events_handler",
        "_Remote" + "SessionRun",
        "_stream_" + "session_run",
        "set_session_run_" + "events_handler",
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []
