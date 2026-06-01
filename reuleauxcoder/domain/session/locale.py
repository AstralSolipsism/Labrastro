"""Shared locale helpers for user-visible session output."""

from __future__ import annotations


SessionLocale = str

_SESSION_NOTICE_TEXT: dict[str, dict[SessionLocale, str]] = {
    "provider_stream_interrupted.recovering": {
        "zh-CN": "模型输出流中断，正在尝试恢复。",
        "en": "The model output stream was interrupted. Trying to recover.",
    },
    "provider_stream.recovering": {
        "zh-CN": "正在恢复输出。",
        "en": "Recovering output.",
    },
    "provider_stream.continuing": {
        "zh-CN": "正在继续处理",
        "en": "Continuing",
    },
    "provider_stream.continue_generating": {
        "zh-CN": "正在继续生成",
        "en": "Continuing generation",
    },
    "provider_stream.interrupted_can_continue": {
        "zh-CN": "模型输出流中断，可继续生成。",
        "en": "The model output stream was interrupted. You can continue generation.",
    },
    "provider_stream.interrupted_prefix": {
        "zh-CN": "输出中断：",
        "en": "Output interrupted: ",
    },
    "capability_package.session_failed": {
        "zh-CN": "能力包流程执行失败。",
        "en": "Capability package workflow failed.",
    },
}


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
            "语言要求：所有用户可见的生成内容都必须使用简体中文，包括助手回复、"
            "过程叙述、公开展示的思考摘要，以及生成草案中的自然语言字段。"
            "JSON key、标识符、代码、命令、路径、URL、API 名称和引用的原始错误"
            "必须保持原文，不要翻译。"
        )
    return (
        "Language: Use English for all user-visible generated content, "
        "including assistant replies, progress narration, publicly displayed "
        "reasoning/thinking summaries, and natural-language fields in generated drafts. "
        "Keep JSON keys, identifiers, code, commands, paths, URLs, API names, "
        "and quoted errors unchanged."
    )


def session_notice_text(
    locale: object,
    key: object,
    default: object = "",
) -> str:
    normalized = normalize_session_locale(locale)
    notice_key = str(key or "").strip()
    if notice_key:
        labels = _SESSION_NOTICE_TEXT.get(notice_key)
        if labels:
            return labels.get(normalized) or labels.get("en") or next(iter(labels.values()))
    return str(default or notice_key or "")
