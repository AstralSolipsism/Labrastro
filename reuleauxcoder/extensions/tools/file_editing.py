"""Shared file-editing semantics for local tools and approval previews."""

from __future__ import annotations

from pathlib import Path


def read_text_preserve_newlines(path: Path) -> str:
    with path.open("r", newline="") as handle:
        return handle.read()


def write_text_preserve_newlines(path: Path, content: str) -> None:
    with path.open("w", newline="") as handle:
        handle.write(content)


def build_edited_content(
    old_content: str, old_string: str, new_string: str
) -> tuple[str, int]:
    count = old_content.count(old_string)
    if count == 1:
        return old_content.replace(old_string, new_string, 1), 1
    if count != 0:
        return "", count
    return _build_edited_content_by_normalized_line_endings(
        old_content, old_string, new_string
    )


def _build_edited_content_by_normalized_line_endings(
    old_content: str, old_string: str, new_string: str
) -> tuple[str, int]:
    normalized_content, spans = _normalize_line_endings_with_spans(old_content)
    normalized_old = normalize_line_endings(old_string)
    count = normalized_content.count(normalized_old)
    if count != 1:
        return "", count

    start = normalized_content.find(normalized_old)
    if start < 0 or not normalized_old:
        return "", count
    end = start + len(normalized_old) - 1
    if start >= len(spans) or end >= len(spans):
        return "", 0

    original_start = spans[start][0]
    original_end = spans[end][1]
    matched = old_content[original_start:original_end]
    replacement = convert_line_endings(new_string, dominant_line_ending(matched))
    return (
        old_content[:original_start] + replacement + old_content[original_end:],
        1,
    )


def normalize_line_endings(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_line_endings_with_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        if value[index] == "\r":
            if index + 1 < len(value) and value[index + 1] == "\n":
                chars.append("\n")
                spans.append((index, index + 2))
                index += 2
                continue
            chars.append("\n")
            spans.append((index, index + 1))
            index += 1
            continue
        chars.append(value[index])
        spans.append((index, index + 1))
        index += 1
    return "".join(chars), spans


def dominant_line_ending(value: str) -> str:
    crlf = value.count("\r\n")
    without_crlf = value.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    if crlf > 0 and crlf >= lf and crlf >= cr:
        return "\r\n"
    if lf > 0 and lf >= cr:
        return "\n"
    if cr > 0:
        return "\r"
    return "\n"


def convert_line_endings(value: str, newline: str) -> str:
    normalized = normalize_line_endings(value)
    if newline == "\n":
        return normalized
    return normalized.replace("\n", newline)
