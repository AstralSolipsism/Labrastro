from __future__ import annotations

import pytest

from labrastro_server.services.agent_runtime.session_branch_tree import (
    SessionBranchBinding,
    SessionBranchTree,
    SessionBranchTreeError,
    SessionTreeEntry,
    compose_selected_transcript,
)


def _entry(
    entry_id: str,
    *,
    parent_id: str | None,
    branch_binding_id: str,
    kind: str = "user",
    timestamp: str = "2026-06-20T00:00:00+00:00",
) -> SessionTreeEntry:
    return SessionTreeEntry(
        id=entry_id,
        parent_id=parent_id,
        branch_binding_id=branch_binding_id,
        kind=kind,
        timestamp=timestamp,
        payload={"id": entry_id},
    )


def branch_tree_with_main_and_child_branch() -> SessionBranchTree:
    entries = {
        "root-user": _entry("root-user", parent_id=None, branch_binding_id="main"),
        "root-assistant": _entry(
            "root-assistant",
            parent_id="root-user",
            branch_binding_id="main",
            kind="assistant",
        ),
        "main-tail": _entry(
            "main-tail",
            parent_id="root-assistant",
            branch_binding_id="main",
            kind="assistant",
        ),
        "branch-a-user": _entry(
            "branch-a-user",
            parent_id="root-assistant",
            branch_binding_id="branch-a",
        ),
    }
    return SessionBranchTree(
        session_run_id="run-1",
        root_branch_binding_id="main",
        selected_branch_binding_id="branch-a",
        entries_by_id=entries,
        branch_bindings_by_id={
            "main": SessionBranchBinding(
                branch_binding_id="main",
                agent_run_id="agent-main",
                relation="main",
                status="open",
            ),
            "branch-a": SessionBranchBinding(
                branch_binding_id="branch-a",
                parent_branch_binding_id="main",
                base_session_item_id="root-assistant",
                source_agent_run_id="agent-main",
                agent_run_id="agent-branch-a",
                relation="branch",
                status="open",
            ),
        },
    )


def branch_tree_with_missing_base() -> SessionBranchTree:
    tree = branch_tree_with_main_and_child_branch()
    tree.branch_bindings_by_id["branch-a"] = SessionBranchBinding(
        branch_binding_id="branch-a",
        parent_branch_binding_id="main",
        base_session_item_id="missing-base-item",
        source_agent_run_id="agent-main",
        agent_run_id="agent-branch-a",
        relation="branch",
        status="open",
    )
    return tree


def branch_tree_with_cyclic_branch_chain() -> SessionBranchTree:
    tree = branch_tree_with_main_and_child_branch()
    tree.branch_bindings_by_id["branch-a"] = SessionBranchBinding(
        branch_binding_id="branch-a",
        parent_branch_binding_id="branch-b",
        base_session_item_id="root-assistant",
        source_agent_run_id="agent-main",
        agent_run_id="agent-branch-a",
        relation="branch",
        status="open",
    )
    tree.branch_bindings_by_id["branch-b"] = SessionBranchBinding(
        branch_binding_id="branch-b",
        parent_branch_binding_id="branch-a",
        base_session_item_id="root-assistant",
        source_agent_run_id="agent-main",
        agent_run_id="agent-branch-b",
        relation="branch",
        status="open",
    )
    return tree


def test_branch_tree_composes_selected_branch_from_parent_prefix_and_branch_delta() -> None:
    tree = branch_tree_with_main_and_child_branch()

    selected = compose_selected_transcript(tree, "branch-a")

    assert [item["id"] for item in selected] == [
        "root-user",
        "root-assistant",
        "branch-a-user",
    ]


def test_branch_tree_orders_sibling_entries_by_explicit_timestamp_not_rebuild_order() -> None:
    tree = branch_tree_with_main_and_child_branch()
    tree.entries_by_id = {
        "branch-second": _entry(
            "branch-second",
            parent_id="root-assistant",
            branch_binding_id="branch-a",
            timestamp="2026-06-20T00:00:04+00:00",
        ),
        "root-assistant": _entry(
            "root-assistant",
            parent_id="root-user",
            branch_binding_id="main",
            kind="assistant",
            timestamp="2026-06-20T00:00:02+00:00",
        ),
        "branch-first": _entry(
            "branch-first",
            parent_id="root-assistant",
            branch_binding_id="branch-a",
            timestamp="2026-06-20T00:00:03+00:00",
        ),
        "root-user": _entry(
            "root-user",
            parent_id=None,
            branch_binding_id="main",
            timestamp="2026-06-20T00:00:01+00:00",
        ),
    }

    selected = compose_selected_transcript(tree, "branch-a")

    assert [item["id"] for item in selected] == [
        "root-user",
        "root-assistant",
        "branch-first",
        "branch-second",
    ]


def test_branch_tree_rejects_sibling_timestamp_without_timezone() -> None:
    tree = branch_tree_with_main_and_child_branch()
    tree.entries_by_id = {
        "root-user": _entry(
            "root-user",
            parent_id=None,
            branch_binding_id="main",
            timestamp="2026-06-20T00:00:01+00:00",
        ),
        "root-assistant": _entry(
            "root-assistant",
            parent_id="root-user",
            branch_binding_id="main",
            kind="assistant",
            timestamp="2026-06-20T00:00:02+00:00",
        ),
        "branch-naive": _entry(
            "branch-naive",
            parent_id="root-assistant",
            branch_binding_id="branch-a",
            timestamp="2026-06-20T00:00:03",
        ),
        "branch-aware": _entry(
            "branch-aware",
            parent_id="root-assistant",
            branch_binding_id="branch-a",
            timestamp="2026-06-20T00:00:04+00:00",
        ),
    }

    with pytest.raises(SessionBranchTreeError, match="timezone"):
        compose_selected_transcript(tree, "branch-a")


def test_branch_tree_rejects_unknown_base_item() -> None:
    tree = branch_tree_with_missing_base()

    with pytest.raises(SessionBranchTreeError, match="base_session_item_id"):
        compose_selected_transcript(tree, "branch-a")


def test_branch_tree_rejects_unknown_branch_binding() -> None:
    tree = branch_tree_with_main_and_child_branch()

    with pytest.raises(SessionBranchTreeError, match="branch_binding_id"):
        compose_selected_transcript(tree, "missing-branch")


def test_branch_tree_rejects_empty_branch_binding_without_selected_fallback() -> None:
    tree = branch_tree_with_main_and_child_branch()

    with pytest.raises(SessionBranchTreeError, match="branch_binding_id"):
        compose_selected_transcript(tree, "")


def test_branch_tree_rejects_cyclic_parent_branch_chain() -> None:
    tree = branch_tree_with_cyclic_branch_chain()

    with pytest.raises(SessionBranchTreeError, match="cycle"):
        compose_selected_transcript(tree, "branch-a")
