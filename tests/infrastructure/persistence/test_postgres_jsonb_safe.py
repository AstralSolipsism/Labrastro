from __future__ import annotations

import gzip
import json

from labrastro_server.infrastructure.persistence.postgres_session_store import (
    PostgresSessionStore,
    _json,
    _json_bytes,
)


def test_json_replaces_nul_in_nested_values_and_keys() -> None:
    raw = _json(
        {
            "key\x00name": [
                "a\x00b",
                ("c\x00d",),
                {"nested": "e\x00f"},
            ],
        }
    )

    assert "\\u0000" not in raw
    assert "\x00" not in raw
    decoded = json.loads(raw)
    assert decoded == {"key\ufffdname": ["a\ufffdb", ["c\ufffdd"], {"nested": "e\ufffdf"}]}


def test_json_bytes_replaces_nul_before_encoding() -> None:
    raw = _json_bytes({"tool_result": "a\x00b"})

    assert b"\\u0000" not in raw
    assert b"\x00" not in raw
    assert json.loads(raw.decode("utf-8"))["tool_result"] == "a\ufffdb"


def test_encode_event_payload_sanitizes_compressed_payload() -> None:
    store = object.__new__(PostgresSessionStore)
    store.payload_compress_threshold_bytes = 32

    payload_json, payload_blob, payload_encoding, payload_bytes = (
        store._encode_event_payload({"tool_result": "a\x00" + ("b" * 100)})
    )

    assert payload_json is None
    assert payload_blob is not None
    assert payload_encoding == "json+gzip"
    assert payload_bytes > 32
    decompressed = gzip.decompress(payload_blob)
    assert b"\\u0000" not in decompressed
    assert b"\x00" not in decompressed
    assert json.loads(decompressed.decode("utf-8"))["tool_result"].startswith("a\ufffd")
