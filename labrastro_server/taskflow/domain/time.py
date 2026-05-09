"""Time helpers for neutral Taskflow state models."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> str:
    """Return a UTC timestamp string for Taskflow state snapshots."""

    return datetime.now(timezone.utc).isoformat()


__all__ = ["utc_now"]
