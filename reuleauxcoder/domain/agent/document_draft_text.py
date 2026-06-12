"""Shared text units for document draft wire offsets."""

from __future__ import annotations


def draft_text_units(text: str) -> int:
    """Return the UTF-16 code unit length used by VS Code/TypeScript strings."""

    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2
