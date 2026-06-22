"""Scope proof helpers for remote protocol models."""

from __future__ import annotations

from typing import Any


def required_branch_binding_id(value: Any) -> str:
    branch_binding_id = value.strip() if isinstance(value, str) else ""
    if not branch_binding_id:
        raise ValueError("branch_binding_id_required")
    return branch_binding_id


def required_session_run_id(value: Any) -> str:
    session_run_id = value.strip() if isinstance(value, str) else ""
    if not session_run_id:
        raise ValueError("session_run_id_required")
    return session_run_id
