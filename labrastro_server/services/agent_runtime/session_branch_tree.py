from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class SessionBranchTreeError(ValueError):
    pass


@dataclass
class SessionTreeEntry:
    id: str
    parent_id: str | None
    branch_binding_id: str
    kind: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionBranchBinding:
    branch_binding_id: str
    agent_run_id: str
    relation: str
    status: str
    parent_branch_binding_id: str = ""
    base_session_item_id: str = ""
    source_agent_run_id: str = ""


@dataclass
class SessionBranchTree:
    session_run_id: str
    root_branch_binding_id: str
    selected_branch_binding_id: str
    entries_by_id: dict[str, SessionTreeEntry]
    branch_bindings_by_id: dict[str, SessionBranchBinding]


def compose_selected_transcript(
    tree: SessionBranchTree,
    branch_binding_id: str,
) -> list[dict[str, Any]]:
    transcript: list[SessionTreeEntry] = []
    for binding in branch_ancestor_chain(tree, branch_binding_id):
        base_item_id = _normalized(binding.base_session_item_id)
        if not binding.parent_branch_binding_id:
            transcript = _branch_descendants(tree, binding.branch_binding_id, parent_id=None)
            continue
        if not base_item_id:
            raise SessionBranchTreeError("base_session_item_id is required for branch bindings")
        if base_item_id not in tree.entries_by_id:
            raise SessionBranchTreeError(f"base_session_item_id not found: {base_item_id}")
        base_index = _entry_index(transcript, base_item_id)
        if base_index is None:
            raise SessionBranchTreeError(
                f"base_session_item_id is not in parent branch transcript: {base_item_id}"
            )
        transcript = [
            *transcript[: base_index + 1],
            *_branch_descendants(tree, binding.branch_binding_id, parent_id=base_item_id),
        ]
    return [_entry_payload(entry) for entry in transcript]


def branch_ancestor_chain(
    tree: SessionBranchTree,
    branch_binding_id: str,
) -> list[SessionBranchBinding]:
    binding = validate_branch_binding(tree, branch_binding_id)
    chain: list[SessionBranchBinding] = []
    seen: set[str] = set()
    while True:
        current_id = _normalized(binding.branch_binding_id)
        if current_id in seen:
            raise SessionBranchTreeError(f"branch_binding_id parent cycle detected: {current_id}")
        seen.add(current_id)
        chain.append(binding)
        parent_id = _normalized(binding.parent_branch_binding_id)
        if not parent_id:
            break
        binding = validate_branch_binding(tree, parent_id)
    chain.reverse()
    return chain


def validate_branch_binding(
    tree: SessionBranchTree,
    branch_binding_id: str,
) -> SessionBranchBinding:
    normalized = _normalized(branch_binding_id)
    if not normalized:
        raise SessionBranchTreeError("branch_binding_id is required")
    binding = tree.branch_bindings_by_id.get(normalized)
    if binding is None:
        raise SessionBranchTreeError(f"branch_binding_id not found: {normalized}")
    return binding


def _branch_descendants(
    tree: SessionBranchTree,
    branch_binding_id: str,
    *,
    parent_id: str | None,
) -> list[SessionTreeEntry]:
    branch_id = _normalized(branch_binding_id)
    children_by_parent: dict[str | None, list[SessionTreeEntry]] = {}
    for entry in tree.entries_by_id.values():
        if _normalized(entry.branch_binding_id) != branch_id:
            continue
        children_by_parent.setdefault(entry.parent_id, []).append(entry)
    for current_parent_id, children in children_by_parent.items():
        seen_timestamps: set[datetime] = set()
        for child in children:
            timestamp = _entry_timestamp(child)
            if timestamp in seen_timestamps:
                raise SessionBranchTreeError(
                    "duplicate sibling timestamp for transcript ordering: "
                    f"{current_parent_id or '__root__'}"
                )
            seen_timestamps.add(timestamp)
        children.sort(key=_entry_timestamp)

    out: list[SessionTreeEntry] = []

    def walk(current_parent_id: str | None) -> None:
        for child in children_by_parent.get(current_parent_id, []):
            out.append(child)
            walk(child.id)

    walk(parent_id)
    return out


def _entry_index(entries: list[SessionTreeEntry], entry_id: str) -> int | None:
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return index
    return None


def _entry_payload(entry: SessionTreeEntry) -> dict[str, Any]:
    payload = dict(entry.payload)
    payload.setdefault("id", entry.id)
    payload.setdefault("kind", entry.kind)
    payload.setdefault("timestamp", entry.timestamp)
    payload.setdefault("branch_binding_id", entry.branch_binding_id)
    return payload


def _entry_timestamp(entry: SessionTreeEntry) -> datetime:
    raw = _normalized(entry.timestamp)
    if not raw:
        raise SessionBranchTreeError(
            f"entry timestamp is required for transcript ordering: {entry.id}"
        )
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionBranchTreeError(
            f"entry timestamp is invalid for transcript ordering: {entry.id}"
        ) from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SessionBranchTreeError(
            f"entry timestamp timezone is required for transcript ordering: {entry.id}"
        )
    return timestamp


def _normalized(value: str | None) -> str:
    return str(value or "").strip()
