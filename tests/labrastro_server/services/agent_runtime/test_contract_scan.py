from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


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
        "agent_runs_protocol": _read(
            "labrastro_server/interfaces/http/remote/protocol/agent_runs.py"
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
        ("admin_routes", "target_branch_binding_id = required_branch_binding_id("),
        ("admin_routes", "branch_binding_id=target_branch_binding_id"),
        ("admin_routes", '"branch_binding_id_required"'),
        ("agent_runs_protocol", "class AgentRunBranchRequest:"),
        ("agent_runs_protocol", "class AgentRunForkRequest:"),
        ("agent_runs_protocol", "required_branch_binding_id(self.branch_binding_id)"),
        ("agent_runs_protocol", "branch_binding_id=required_branch_binding_id(d.get(\"branch_binding_id\"))"),
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
        "chat_protocol": _read("labrastro_server/interfaces/http/remote/protocol/chat.py"),
        "agent_runs_protocol": (
            _read("labrastro_server/interfaces/http/remote/protocol/agent_runs.py")
            if (REPO_ROOT / "labrastro_server/interfaces/http/remote/protocol/agent_runs.py").exists()
            else ""
        ),
        "chat_routes": _read("labrastro_server/interfaces/http/remote/routes/chat.py"),
        "agent_runs_routes": _read("labrastro_server/interfaces/http/remote/routes/agent_runs.py"),
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
        ("agent_runs_protocol", "SessionRunAgentRunSteerRequest"),
        ("chat_protocol", "required_session_run_id"),
        ("agent_runs_protocol", "required_session_run_id"),
        ("chat_routes", '"session_run_id_required"'),
        ("agent_runs_routes", '"session_run_id_required"'),
        ("contracts", '"name": "session_run.continue"'),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]

    assert missing == []


def test_legacy_relay_tool_execution_backend_is_not_production_reachable() -> None:
    scanned = {
        "relay_server": _read("labrastro_server/relay/server.py"),
        "peer_routes": _read("labrastro_server/interfaces/http/remote/routes/peer.py"),
        "remote_service": _read("labrastro_server/interfaces/http/remote/service.py"),
        "runner": _read("reuleauxcoder/interfaces/entrypoint/runner.py"),
        "remote_relay": _read("reuleauxcoder/interfaces/entrypoint/remote_relay.py"),
        "adapter_init": _read("labrastro_server/adapters/reuleauxcoder/__init__.py"),
    }
    remote_backend_path = REPO_ROOT / "labrastro_server/adapters/reuleauxcoder/remote_backend.py"
    if remote_backend_path.exists():
        scanned["remote_backend"] = remote_backend_path.read_text(encoding="utf-8")
    mcp_tools_path = REPO_ROOT / "labrastro_server/adapters/reuleauxcoder/mcp_tools.py"
    if mcp_tools_path.exists():
        scanned["mcp_tools"] = mcp_tools_path.read_text(encoding="utf-8")

    forbidden = [
        "Remote" + "RelayToolBackend",
        "send_" + "exec_request(",
        "send_" + "preview_request(",
        "request_" + "cleanup(",
        "cancel_" + "pending_requests(",
        'parsed.path == "/remote/' + 'poll"',
        'parsed.path == "/remote/' + 'result"',
        "def _handle_" + "poll(",
        "def _handle_" + "result(",
        "def _enqueue_" + "outbound(",
        "def _next_" + "envelope(",
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, source in scanned.items()
        for pattern in forbidden
        if pattern in source
    ]

    assert offenders == []


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


def test_capability_package_session_start_preserves_typed_start_failure() -> None:
    scanned = {
        "admin_routes": _read("labrastro_server/interfaces/http/remote/routes/admin.py"),
        "remote_service": _read("labrastro_server/interfaces/http/remote/service.py"),
        "capability_packages": _read("labrastro_server/services/capability_packages.py"),
    }

    required = [
        ("admin_routes", "start_result = runner.start(session, payload)"),
        ("admin_routes", "if start_result.failure is not None:"),
        ("admin_routes", "session.record_start_failure(start_result.failure)"),
        ("admin_routes", "_session_run_failed_http_error(existing)"),
        ("remote_service", "def record_start_failure("),
        ("remote_service", '"session_run_failed"'),
        ("remote_service", '"operation": "start"'),
        ("capability_packages", "class CapabilityPackageSessionRunStartResult"),
        ("capability_packages", "class CapabilityPackageSessionRunStartFailure"),
    ]
    forbidden = [
        ("admin_routes", "binding = runner.start(session, payload)"),
        ("admin_routes", "def _record_session_run_start_failure("),
        ("admin_routes", '"session_run_start_failure"'),
        ("admin_routes", "session.runtime_state ="),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, pattern in forbidden
        if pattern in scanned[name]
    ]

    assert missing == []
    assert offenders == []


def test_capability_package_start_runtime_state_is_scoped_runtime_owned() -> None:
    source = _read("labrastro_server/services/capability_packages.py")

    required = [
        "apply_selected_runtime_scope(",
        "runtime_state_updates=",
    ]
    forbidden = [
        "def _set_session_runtime_state(",
        "_set_session_runtime_state(",
        "session.runtime_state = dict(runtime_state)",
    ]

    missing = [pattern for pattern in required if pattern not in source]
    offenders = [pattern for pattern in forbidden if pattern in source]

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


def test_session_run_ui_routes_are_scope_resolved_before_branch_runtime_access() -> None:
    chat_routes = _read("labrastro_server/interfaces/http/remote/routes/chat.py")

    status_response = _section(
        chat_routes,
        "def _send_session_run_status_response",
        "def _session_run_start_mainline_response",
    )
    start_mainline_response = _section(
        chat_routes,
        "def _session_run_start_mainline_response",
        "def _handle_session_run_start",
    )
    events_handler = _section(
        chat_routes,
        "def _handle_session_run_events",
        "def _handle_session_run_cancel",
    )
    branch_select_handler = _section(
        chat_routes,
        "def _handle_session_run_branch_select",
        "def _handle_session_run_cancel",
    )
    cancel_handler = _section(
        chat_routes,
        "def _handle_session_run_cancel",
        "def _handle_session_run_recover",
    )
    recover_handler = _section(
        chat_routes,
        "def _handle_session_run_recover",
        "def _handle_session_run_user_input_reply",
    )

    required = [
        (status_response, "writer = session.scoped_writer("),
        (status_response, "branch_binding_id=scope.branch_binding_id"),
        (status_response, "status_payload = writer.status_response_payload("),
        (status_response, "selected=scope.selected"),
        (start_mainline_response, "writer = session.scoped_writer("),
        (start_mainline_response, "status_payload = writer.status_response_payload("),
        (events_handler, "self._resolve_session_run_control("),
        (events_handler, "req.branch_binding_id"),
        (events_handler, "batch_payload = writer.events_response_payload("),
        (events_handler, "runtime_snapshot=runtime_snapshot"),
        (events_handler, "selected=scope.selected"),
        (branch_select_handler, "selected_writer.apply_selected_runtime_scope("),
        (branch_select_handler, "runtime_status=selected_runtime_status"),
        (branch_select_handler, "terminal=selected_terminal"),
        (cancel_handler, "self._resolve_session_run_control("),
        (cancel_handler, "req.branch_binding_id"),
        (cancel_handler, "session.request_branch_cancel("),
        (cancel_handler, "binding.branch_binding_id"),
        (cancel_handler, "scope.scope_id"),
        (recover_handler, "self._resolve_session_run_control("),
        (recover_handler, "req.branch_binding_id"),
        (recover_handler, "session.consume_recovery("),
        (recover_handler, "branch_binding_id=binding.branch_binding_id"),
        (recover_handler, "selected_agent_run = self.service.runtime_control_plane.get_agent_run("),
        (recover_handler, "scope.scope_id"),
    ]
    missing = [
        pattern
        for section, pattern in required
        if pattern not in section
    ]
    forbidden = [
        (status_response, "runtime_state.update("),
        (status_response, "status_payload.update("),
        (status_response, "_branch_summaries_with_target_status("),
        (status_response, "_session_run_branch_status_from_agent_run("),
        (start_mainline_response, 'runtime_state["branch_binding_id"]'),
        (start_mainline_response, 'runtime_state["agent_run_id"]'),
        (start_mainline_response, 'runtime_state["scope_id"]'),
        (start_mainline_response, 'runtime_state["activation_id"]'),
        (events_handler, "writer.wait_events("),
        (events_handler, "_branch_summaries_with_target_status("),
        (events_handler, "_session_run_branch_status_from_agent_run("),
        (chat_routes, "def _branch_summaries_with_target_status("),
        (chat_routes, "def _session_run_branch_status_from_agent_run("),
        (recover_handler, "and session.running"),
    ]
    offenders = [
        pattern
        for section, pattern in forbidden
        if pattern in section
    ]

    assert missing == []
    assert offenders == []


def test_session_run_routes_do_not_directly_mutate_selected_runtime_projection() -> None:
    chat_routes = _read("labrastro_server/interfaces/http/remote/routes/chat.py")
    admin_routes = _read("labrastro_server/interfaces/http/remote/routes/admin.py")

    required = [
        "writer.apply_selected_runtime_scope(",
    ]
    forbidden = [
        "session.runtime_state.update(",
        "session.agent_run_id =",
        "session.branch_binding_id =",
        "session.done =",
        "session.finished_at =",
    ]

    missing = [pattern for pattern in required if pattern not in chat_routes]
    offenders = [
        f"chat_routes: {pattern}" for pattern in forbidden if pattern in chat_routes
    ]
    offenders.extend(
        f"admin_routes: {pattern}"
        for pattern in ["runtime_state.update("]
        if pattern in admin_routes
    )

    assert missing == []
    assert offenders == []


def test_session_branch_tree_transcript_order_requires_explicit_entry_timestamp() -> None:
    source = _read("labrastro_server/services/agent_runtime/session_branch_tree.py")
    branch_descendants = _section(
        source,
        "def _branch_descendants(",
        "def _entry_index(",
    )

    required = [
        "children.sort(key=_entry_timestamp)",
        "duplicate sibling timestamp for transcript ordering",
        "def _entry_timestamp(",
        "entry timestamp is required for transcript ordering",
        "entry timestamp is invalid for transcript ordering",
        "entry timestamp timezone is required for transcript ordering",
    ]
    missing = [
        pattern
        for pattern in required
        if pattern not in source
        and pattern not in branch_descendants
    ]

    assert missing == []


def test_session_run_mutating_service_apis_fail_closed_without_branch_proof() -> None:
    remote_service = _read("labrastro_server/interfaces/http/remote/service.py")
    legacy_cancel = _section(
        remote_service,
        "def request_cancel(",
        "def request_branch_cancel(",
    )
    cancel = _section(
        remote_service,
        "def request_branch_cancel(",
        "def register_recovery(",
    )
    register_recovery = _section(
        remote_service,
        "def register_recovery(",
        "def consume_recovery(",
    )
    recovery = _section(
        remote_service,
        "def consume_recovery(",
        "def _recovery_ticket_for_branch_locked(",
    )
    recovery_lookup = _section(
        remote_service,
        "def _recovery_ticket_for_branch_locked(",
        "def _recovery_prompt(",
    )

    required = [
        (legacy_cancel, 'raise ValueError("branch_binding_id_required")'),
        (legacy_cancel, "self.request_branch_cancel("),
        (cancel, 'raise ValueError("branch_binding_id_required")'),
        (recovery, 'raise ValueError("branch_binding_id_required")'),
        (recovery_lookup, "if not branch_id:"),
        (recovery_lookup, "return None"),
    ]
    forbidden = [
        (legacy_cancel, "self.cancel_requested = True"),
        (legacy_cancel, "self._cancel_pending_approvals_locked("),
        (legacy_cancel, "self.cancel_callback"),
        (cancel, 'branch_id = "main"'),
        (cancel, "or self.branch_binding_id or"),
        (cancel, 'or "main"'),
        (register_recovery, "or self.selected_branch_binding_id"),
        (register_recovery, "or self.branch_binding_id"),
        (register_recovery, 'or "main"'),
        (recovery, "or self.selected_branch_binding_id"),
        (recovery, 'or "main"'),
        (recovery_lookup, "or self.selected_branch_binding_id"),
        (recovery_lookup, "or self.branch_binding_id"),
        (recovery_lookup, 'or "main"'),
    ]
    missing = [
        pattern
        for section, pattern in required
        if pattern not in section
    ]
    offenders = [
        pattern
        for section, pattern in forbidden
        if pattern in section
    ]

    assert missing == []
    assert offenders == []


def test_session_run_projection_does_not_fabricate_branch_scope_from_selected_state() -> None:
    remote_service = _read("labrastro_server/interfaces/http/remote/service.py")
    merged_status = _section(
        remote_service,
        "def _merged_branch_summary_status(",
        "def _raw_event_ref_keys(",
    )
    event_visibility = _section(
        remote_service,
        "def _event_visible_in_branch_locked(",
        "def _selected_branch_ancestor_limits_locked(",
    )
    event_payload = _section(
        remote_service,
        "def _session_event_payload_locked(",
        "def _event_branch_binding_id_locked(",
    )
    event_branch_scope = _section(
        remote_service,
        "def _event_branch_binding_id_locked(",
        "def _append_live_only_event_locked(",
    )
    payload_scope = _section(
        remote_service,
        "def _payload_branch_binding_id_locked(",
        "def _waiter_branch_binding_id(",
    )
    waiter_match = _section(
        remote_service,
        "def _waiter_matches_branch(",
        "def register_approval(",
    )
    wait_approval = _section(
        remote_service,
        "def wait_approval(",
        "def cancel_pending_approvals(",
    )
    wait_user_input = _section(
        remote_service,
        "def wait_user_input(",
        "def cancel_pending_user_inputs(",
    )
    approval_payload = _section(
        remote_service,
        "def _approval_payload(",
        "def _pending_approvals_locked(",
    )
    user_input_payload = _section(
        remote_service,
        "def _user_input_payload(",
        "def _record_user_input_resolution_locked(",
    )
    live_event_key = _section(
        remote_service,
        "def _live_event_key_locked(",
        "def _update_status_for_event_locked(",
    )
    update_status = _section(
        remote_service,
        "def _update_status_for_event_locked(",
        "def _persist_or_queue_trace_event(",
    )
    cancel_approvals = _section(
        remote_service,
        "def cancel_pending_approvals(",
        "def _cancel_pending_approvals_locked(",
    )
    cancel_approvals_locked = _section(
        remote_service,
        "def _cancel_pending_approvals_locked(",
        "def _approval_resolved_event_payload_locked(",
    )
    cancel_user_inputs = _section(
        remote_service,
        "def cancel_pending_user_inputs(",
        "def _cancel_pending_user_inputs_locked(",
    )
    cancel_user_inputs_locked = _section(
        remote_service,
        "def _cancel_pending_user_inputs_locked(",
        "def _pending_user_inputs_locked(",
    )
    approval_resolved_event = _section(
        remote_service,
        "def _append_approval_resolved_event_locked(",
        "def _append_user_input_resolved_event_locked(",
    )
    user_input_resolved_event = _section(
        remote_service,
        "def _append_user_input_resolved_event_locked(",
        "def _append_live_event_locked(",
    )
    revision_feedback = _section(
        remote_service,
        "def submit_revision_feedback(",
        "def request_cancel(",
    )
    status_payload = _section(
        remote_service,
        "def status_payload(",
        "def set_cancel_callback(",
    )
    status_response_payload = _section(
        remote_service,
        "def status_response_payload(",
        "def events_response_payload(",
    )
    branch_summaries = _section(
        remote_service,
        "def _branch_summaries_locked(",
        "def _status_next_cursor_locked(",
    )
    events_after = _section(
        remote_service,
        "def _events_after_locked(",
        "def _event_visible_in_branch_locked(",
    )
    events_wait_done = _section(
        remote_service,
        "def _events_wait_done_locked(",
        "def mark_running(",
    )
    mark_branch_binding_status = _section(
        remote_service,
        "def _mark_branch_binding_status_locked(",
        "def _branch_runtime_status_locked(",
    )
    branch_runtime = _section(
        remote_service,
        "def _branch_runtime_status_locked(",
        "def _require_branch_binding_locked(",
    )
    apply_branch_runtime_status = _section(
        remote_service,
        "def _apply_branch_runtime_status_locked(",
        "def is_stale(",
    )
    branch_close_reason = _section(
        remote_service,
        "def _branch_close_reason_locked(",
        "def mark_running(",
    )
    mark_running = _section(
        remote_service,
        "def mark_running(",
        "def mark_done(",
    )
    mark_done = _section(
        remote_service,
        "def mark_done(",
        "def apply_selected_runtime_scope(",
    )
    event_buffer_fields = _section(
        remote_service,
        "_ENVELOPE_PAYLOAD_FIELDS = {",
        "    def __init__(",
    )

    required = [
        (merged_status, "if current in _TERMINAL_BRANCH_BINDING_STATUSES:"),
        (merged_status, "return current"),
        (event_visibility, 'payload.get("branch_binding_id") or ""'),
        (event_visibility, "return False"),
        (event_payload, 'if event_type != "session_run_start":'),
        (event_payload, 'raise ValueError("session_run_branch_binding_not_found")'),
        (event_payload, "self._require_branch_binding_locked(branch_id)"),
        (event_branch_scope, 'raise ValueError("branch_binding_id_required")'),
        (payload_scope, 'raise ValueError("branch_binding_id_required")'),
        (payload_scope, "self._require_branch_binding_locked(branch_id)"),
        (waiter_match, "return actual == expected"),
        (wait_approval, 'raise ValueError("branch_binding_id_required")'),
        (wait_approval, "self.approval_waiters.get(state_key)"),
        (wait_user_input, 'raise ValueError("branch_binding_id_required")'),
        (wait_user_input, "self.user_input_waiters.get(state_key)"),
        (approval_payload, 'raise ValueError("approval_id_mismatch")'),
        (approval_payload, 'raise ValueError("approval_id_required")'),
        (user_input_payload, 'raise ValueError("input_id_mismatch")'),
        (user_input_payload, 'raise ValueError("input_id_required")'),
        (live_event_key, "self._scoped_state_key_locked("),
        (live_event_key, "self._require_branch_binding_locked(branch_id)"),
        (live_event_key, "branch_id"),
        (update_status, "self._mark_branch_binding_status_locked("),
        (update_status, "self.running = False"),
        (update_status, "self.done = True"),
        (update_status, "self.finished_at = time.time()"),
        (cancel_approvals, 'raise ValueError("branch_binding_id_required")'),
        (cancel_approvals_locked, 'raise ValueError("branch_binding_id_required")'),
        (cancel_user_inputs, 'raise ValueError("branch_binding_id_required")'),
        (cancel_user_inputs_locked, 'raise ValueError("branch_binding_id_required")'),
        (approval_resolved_event, "self._payload_scoped_state_key_locked("),
        (user_input_resolved_event, "self._payload_scoped_state_key_locked("),
        (revision_feedback, '"revision_feedback"'),
        (revision_feedback, "self.revision_feedback_tickets[state_key]"),
        (revision_feedback, 'raise ValueError("branch_binding_id_required")'),
        (revision_feedback, "self._require_branch_binding_locked(branch_id)"),
        (status_payload, 'raise ValueError("branch_binding_id_required")'),
        (status_payload, 'raise ValueError("session_run_branch_binding_not_found")'),
        (status_payload, 'raise ValueError("session_run_branch_agent_run_required")'),
        (status_payload, "branch_runtime = self._branch_runtime_status_locked(target_branch_id)"),
        (status_payload, 'branch_agent_run_id = ('),
        (status_payload, '"scope_id": scope_id_for(self.session_run_id, target_branch_id)'),
        (status_payload, 'raise ValueError("session_run_branch_agent_run_required")'),
        (status_response_payload, 'response_running = bool(status_payload.get("running"))'),
        (branch_summaries, 'selected_branch_id = str(self.selected_branch_binding_id or "").strip()'),
        (branch_summaries, "for branch_id, binding in self.branch_bindings.items()"),
        (branch_summaries, 'str(binding.get("agent_run_id") or "").strip()'),
        (branch_summaries, '"finished_at": binding.get("finished_at")'),
        (events_after, 'raise ValueError("branch_binding_id_required")'),
        (events_after, "self._scope_events_lost_event(event, target_branch_id)"),
        (events_wait_done, 'raise ValueError("branch_binding_id_required")'),
        (mark_branch_binding_status, 'binding["finished_at"] = updated_at'),
        (mark_branch_binding_status, 'binding.pop("finished_at", None)'),
        (branch_runtime, 'finished_at = binding.get("finished_at")'),
        (branch_runtime, "running = normalized in _ACTIVE_BRANCH_BINDING_STATUSES"),
        (branch_runtime, '"finished_at": finished_at if done else None'),
        (apply_branch_runtime_status, "merged_status in _ACTIVE_BRANCH_BINDING_STATUSES"),
        (branch_close_reason, "self.cancel_requests_by_branch.get(branch_binding_id)"),
        (branch_close_reason, 'binding.get("last_error")'),
        (mark_running, "self._require_branch_binding_locked(branch_id)"),
        (mark_done, "self._require_branch_binding_locked(branch_id)"),
        (mark_done, "self._branch_close_reason_locked(branch_id, reason)"),
        (event_buffer_fields, '"branch_binding_id"'),
    ]
    forbidden = [
        (remote_service, "def _branch_summaries_with_target_status("),
        (remote_service, "_branch_summaries_with_target_status("),
        (merged_status, 'current == "error" and runtime_status == "done"'),
        (branch_runtime, 'normalized == "running"'),
        (remote_service, "self.branch_binding_id or self.selected_branch_binding_id"),
        (event_visibility, "or selected_branch_id"),
        (event_payload, "self.branch_bindings.setdefault("),
        (event_branch_scope, "or self.selected_branch_binding_id"),
        (event_branch_scope, 'or "main"'),
        (payload_scope, "or self.selected_branch_binding_id"),
        (payload_scope, "or self.branch_binding_id"),
        (payload_scope, 'or "main"'),
        (waiter_match, "not actual or actual == expected"),
        (wait_approval, "approval_waiters.setdefault"),
        (wait_approval, "self.approval_waiters.get(approval_id)"),
        (wait_user_input, "user_input_waiters.setdefault"),
        (wait_user_input, "self.user_input_waiters.get(input_id)"),
        (approval_payload, 'out["approval_id"] = str(out.get("approval_id") or approval_id)'),
        (user_input_payload, 'out["input_id"] = str(out.get("input_id") or input_id)'),
        (live_event_key, "return event_type"),
        (live_event_key, '":".join'),
        (update_status, "self.done = bool("),
        (cancel_approvals_locked, "_waiter_matches_branch(waiter, branch_binding_id)"),
        (cancel_user_inputs_locked, "_waiter_matches_branch(waiter, branch_binding_id)"),
        (approval_resolved_event, "approval_id in self._approval_resolved_event_ids"),
        (user_input_resolved_event, "input_id in self._user_input_resolved_event_ids"),
        (revision_feedback, "self.revision_feedback_tickets.get(normalized_id)"),
        (revision_feedback, "self.revision_feedback_tickets[normalized_id]"),
        (status_payload, "or self.selected_branch_binding_id"),
        (status_payload, "or self.branch_binding_id"),
        (status_payload, '"status": self.status'),
        (status_payload, '"done": self.done'),
        (status_payload, '"running": self.running'),
        (status_payload, '"error": self.last_error'),
        (status_payload, '"agent_run_id": self.agent_run_id'),
        (status_payload, '"finished_at": self.finished_at'),
        (status_payload, "branch_agent_run_id = str(self.agent_run_id or \"\")"),
        (status_response_payload, 'response_status == "running"'),
        (branch_summaries, "selected_branch_id not in bindings"),
        (branch_summaries, 'str(self.agent_run_id or "")'),
        (events_after, "_event_visible_in_selected_branch_locked(event)"),
        (events_wait_done, "return self.done"),
        (mark_done, "or self.cancel_reason"),
        (mark_done, "or self.last_error"),
    ]
    missing = [
        pattern
        for section, pattern in required
        if pattern not in section
    ]
    offenders = [
        pattern
        for section, pattern in forbidden
        if pattern in section
    ]

    assert missing == []
    assert offenders == []


def test_mcp_elicitation_and_entrypoint_producers_require_scoped_branch_proof() -> None:
    scanned = {
        "runner": _read("reuleauxcoder/interfaces/entrypoint/runner.py"),
        "remote_relay": _read("reuleauxcoder/interfaces/entrypoint/remote_relay.py"),
        "mcp_client": _read("reuleauxcoder/extensions/mcp/client.py"),
        "tool_execution": _read("reuleauxcoder/domain/agent/tool_execution.py"),
        "executor_backend": _read(
            "labrastro_server/services/agent_runtime/executor_backend.py"
        ),
        "control_plane": _read(
            "labrastro_server/services/agent_runtime/control_plane.py"
        ),
        "remote_service": _read("labrastro_server/interfaces/http/remote/service.py"),
    }

    required = [
        ("remote_service", "class _ScopedSessionRunWriter:"),
        ("remote_service", 'raise ValueError("agent_run_id_required")'),
        ("remote_service", 'raise ValueError("session_run_branch_binding_not_found")'),
        ("remote_service", 'raise ValueError("session_run_branch_agent_run_required")'),
        ("runner", 'branch_binding_id = str(request.get("branch_binding_id") or "").strip()'),
        ("runner", 'reason": "branch_binding_id_required"'),
        ("runner", "writer = session.scoped_writer("),
        ("runner", 'writer.append_event("user_input_request", payload)'),
        ("runner", 'writer.append_event("user_input_resolved", resolved_payload)'),
        ("mcp_client", '"branch_binding_id": str(context.get("branch_binding_id") or "")'),
        ("mcp_client", '"branch_binding_id": str(payload.get("branch_binding_id") or "")'),
        ("tool_execution", "runtime_branch_binding_id(self.agent)"),
        ("tool_execution", 'event_payload.setdefault("branch_binding_id", context["branch_binding_id"])'),
        ("executor_backend", 'setattr(agent, "runtime_branch_binding_id", branch_binding_id)'),
        ("control_plane", "def _session_run_binding_metadata_for_task_locked("),
        ("control_plane", '"branch_binding_id": branch_binding_id'),
    ]
    forbidden = [
        ("runner", "session.register_user_input("),
        ("runner", "session.append_event("),
        ("remote_relay", "remote_session.append_event("),
        ("remote_relay", "remote_session.register_user_input("),
        ("remote_service", "self._agent_run_id = binding_agent_run_id"),
        ("remote_service", "self._agent_run_id and binding_agent_run_id"),
        ("tool_execution", '"branch_binding_id": "main"'),
        ("mcp_client", '"branch_binding_id": "main"'),
    ]
    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern not in scanned[name]
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, pattern in forbidden
        if pattern in scanned[name]
    ]

    assert missing == []
    assert offenders == []


def test_backend_session_run_ui_producers_use_scoped_writer_ports() -> None:
    scanned = {
        "chat_routes": _read("labrastro_server/interfaces/http/remote/routes/chat.py"),
        "admin_routes": _read("labrastro_server/interfaces/http/remote/routes/admin.py"),
        "capability_packages": _read("labrastro_server/services/capability_packages.py"),
        "runner": _read("reuleauxcoder/interfaces/entrypoint/runner.py"),
        "remote_relay": _read("reuleauxcoder/interfaces/entrypoint/remote_relay.py"),
    }
    remote_service = _read("labrastro_server/interfaces/http/remote/service.py")
    abort_peer_session_runs = _section(
        remote_service,
        "def _abort_peer_session_runs(",
        "def _build_handler(",
    )

    mutators = [
        "append_event",
        "append_live_event",
        "register_user_input",
        "register_approval",
        "mark_running",
        "mark_done",
        "resolve_approval",
        "resolve_user_input",
        "cancel_pending_approvals",
        "cancel_pending_user_inputs",
        "status_payload",
        "wait_events",
    ]
    required = [
        ("chat_routes", "writer = session.scoped_writer("),
        ("admin_routes", "writer = session.scoped_writer("),
        ("capability_packages", "def _session_scoped_writer(session: Any) -> Any:"),
        ("capability_packages", "return _session_scoped_writer(session).append_event("),
        ("runner", "writer = session.scoped_writer("),
        ("abort_peer_session_runs", "for binding in session.branch_bindings.values():"),
        ("abort_peer_session_runs", "session.scoped_writer("),
        ("abort_peer_session_runs", "writer.mark_done(reason)"),
    ]
    forbidden = [
        (name, f"session.{mutator}(")
        for name in scanned
        for mutator in mutators
    ]
    forbidden.extend(
        [
            ("abort_peer_session_runs", "session.branch_binding_id"),
            ("abort_peer_session_runs", "session.append_event("),
            ("abort_peer_session_runs", "session.cancel_pending_approvals("),
            ("abort_peer_session_runs", "session.cancel_pending_user_inputs("),
            ("abort_peer_session_runs", "session.mark_done("),
            ("abort_peer_session_runs", "session.mark_orphaned_done("),
            ("remote_service", "def mark_orphaned_done("),
        ]
    )

    missing = [
        f"{name}: {pattern}"
        for name, pattern in required
        if pattern
        not in (
            abort_peer_session_runs
            if name == "abort_peer_session_runs"
            else scanned[name]
        )
    ]
    offenders = [
        f"{name}: {pattern}"
        for name, pattern in forbidden
        if pattern
        in (
            abort_peer_session_runs
            if name == "abort_peer_session_runs"
            else remote_service
            if name == "remote_service"
            else scanned[name]
        )
    ]

    assert missing == []
    assert offenders == []
