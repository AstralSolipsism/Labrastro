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
