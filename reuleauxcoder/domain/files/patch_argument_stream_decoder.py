"""Streaming decoder for JSON-schema apply_patch tool arguments."""

from __future__ import annotations

import json
import re


class PatchArgumentStreamError(ValueError):
    """Raised when streamed apply_patch arguments violate the protocol."""


class PatchArgumentStreamDecoder:
    """Decode provider argument deltas into patch text deltas before JSON completion."""

    _PATCH_START_RE = re.compile(r'"patch"\s*:\s*"')
    _FORBIDDEN_FIELD_RE = re.compile(r'"(content|old_string|new_string)"\s*:')

    def __init__(self) -> None:
        self._buffer = ""
        self._scan_index = 0
        self._started = False
        self._complete = False
        self._escape = False
        self._unicode_escape = ""

    @property
    def complete(self) -> bool:
        return self._complete

    def push_delta(self, delta: str) -> str:
        if self._complete:
            if str(delta or "").strip():
                self._buffer += str(delta or "")
            return ""
        self._buffer += str(delta or "")
        if not self._started:
            self._reject_forbidden_fields()
            match = self._PATCH_START_RE.search(self._buffer)
            if match is None:
                return ""
            self._started = True
            self._scan_index = match.end()
        return self._consume_patch_chars()

    def finish(self) -> str:
        tail = self._consume_patch_chars() if self._started and not self._complete else ""
        if not self._started:
            raise PatchArgumentStreamError("apply_patch arguments must include patch")
        if self._unicode_escape:
            raise PatchArgumentStreamError("incomplete unicode escape in patch argument")
        if self._escape:
            raise PatchArgumentStreamError("incomplete escape in patch argument")
        if not self._complete:
            raise PatchArgumentStreamError("patch argument did not complete")
        try:
            parsed = json.loads(self._buffer)
        except json.JSONDecodeError as exc:
            raise PatchArgumentStreamError(f"invalid apply_patch JSON arguments: {exc}") from exc
        if not isinstance(parsed, dict) or set(parsed) != {"patch"}:
            raise PatchArgumentStreamError("apply_patch arguments may only contain patch")
        if not isinstance(parsed.get("patch"), str):
            raise PatchArgumentStreamError("apply_patch patch must be a string")
        return tail

    def _reject_forbidden_fields(self) -> None:
        match = self._FORBIDDEN_FIELD_RE.search(self._buffer)
        if match:
            raise PatchArgumentStreamError(
                f"apply_patch arguments must not contain {match.group(1)}"
            )

    def _consume_patch_chars(self) -> str:
        output: list[str] = []
        while self._scan_index < len(self._buffer):
            char = self._buffer[self._scan_index]
            self._scan_index += 1
            if self._unicode_escape:
                if not re.match(r"[0-9a-fA-F]", char):
                    raise PatchArgumentStreamError("invalid unicode escape in patch argument")
                self._unicode_escape += char
                if len(self._unicode_escape) == 4:
                    output.append(chr(int(self._unicode_escape, 16)))
                    self._unicode_escape = ""
                continue
            if self._escape:
                self._escape = False
                if char == "u":
                    self._unicode_escape = ""
                    continue
                output.append(_decode_escape(char))
                continue
            if char == "\\":
                self._escape = True
                continue
            if char == '"':
                self._complete = True
                break
            output.append(char)
        return "".join(output)


def _decode_escape(char: str) -> str:
    mapping = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    if char not in mapping:
        raise PatchArgumentStreamError(f"invalid escape in patch argument: \\{char}")
    return mapping[char]
