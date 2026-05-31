"""Shared locale helpers for user-visible session output."""

from __future__ import annotations


SessionLocale = str


def normalize_session_locale(value: object) -> SessionLocale:
    text = str(value or "").strip().lower()
    return "zh-CN" if text.startswith("zh") else "en"


def session_locale_prompt_append(locale: object) -> str:
    value = str(locale or "").strip()
    if not value:
        return ""
    normalized = normalize_session_locale(value)
    if normalized == "zh-CN":
        return (
            "Language: Use Simplified Chinese for all user-visible generated content, "
            "including assistant replies, progress narration, publicly displayed "
            "reasoning/thinking summaries, and natural-language fields in generated drafts. "
            "Keep JSON keys, identifiers, code, commands, paths, URLs, API names, "
            "and quoted errors unchanged."
        )
    return (
        "Language: Use English for all user-visible generated content, "
        "including assistant replies, progress narration, publicly displayed "
        "reasoning/thinking summaries, and natural-language fields in generated drafts. "
        "Keep JSON keys, identifiers, code, commands, paths, URLs, API names, "
        "and quoted errors unchanged."
    )
