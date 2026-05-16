"""Minimal JSON-RPC LSP client."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LspClientError(RuntimeError):
    """Raised when a language-server request fails."""


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


@dataclass
class LspClient:
    """Small stdio JSON-RPC client for one language server process."""

    command: str
    args: list[str]
    workspace_root: Path
    init_options: dict[str, Any] | None = None
    process: subprocess.Popen[bytes] | None = None
    _next_id: int = 0
    _write_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: dict[int, queue.Queue[dict[str, Any]]] = field(default_factory=dict)
    _diagnostics: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _diagnostic_events: dict[str, threading.Event] = field(default_factory=dict)
    _reader_thread: threading.Thread | None = None
    _shutdown: bool = False

    def start(self, timeout_sec: float = 5.0) -> None:
        if self.process is not None:
            return
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [self.command, *self.args],
            cwd=str(self.workspace_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": path_to_uri(self.workspace_root),
                "capabilities": {},
                "initializationOptions": self.init_options or {},
            },
            timeout_sec=timeout_sec,
        )
        self.notify("initialized", {})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_sec: float = 5.0,
    ) -> Any:
        if self.process is None:
            self.start(timeout_sec=timeout_sec)
        self._next_id += 1
        request_id = self._next_id
        responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._pending[request_id] = responses
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            response = responses.get(timeout=timeout_sec)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            raise LspClientError(f"LSP request timed out: {method}") from exc
        if "error" in response:
            raise LspClientError(str(response["error"]))
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.process is None:
            self.start()
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def did_open(self, file_path: Path, language_id: str, text: str) -> None:
        uri = path_to_uri(file_path)
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )

    def wait_diagnostics(self, uri: str, timeout_sec: float = 5.0) -> list[dict[str, Any]]:
        event = self._diagnostic_events.setdefault(uri, threading.Event())
        event.wait(timeout=timeout_sec)
        return list(self._diagnostics.get(uri, []))

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self.process is None:
            return
        try:
            self.request("shutdown", {}, timeout_sec=1.0)
        except Exception:
            pass
        try:
            self.notify("exit", {})
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise LspClientError("LSP server is not running")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            self.process.stdin.write(header + body)
            self.process.stdin.flush()

    def _read_loop(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        stream = self.process.stdout
        while not self._shutdown:
            try:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    if line in {b"\r\n", b"\n"}:
                        break
                    key, _, value = line.decode("ascii", errors="replace").partition(":")
                    headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                if length <= 0:
                    continue
                payload = json.loads(stream.read(length).decode("utf-8", errors="replace"))
            except Exception:
                return
            self._handle_message(payload)

    def _handle_message(self, payload: dict[str, Any]) -> None:
        if "id" in payload:
            try:
                request_id = int(payload["id"])
            except (TypeError, ValueError):
                return
            responses = self._pending.pop(request_id, None)
            if responses is not None:
                responses.put(payload)
            return
        if payload.get("method") != "textDocument/publishDiagnostics":
            return
        params = payload.get("params") or {}
        uri = str(params.get("uri") or "")
        if not uri:
            return
        self._diagnostics[uri] = [
            item for item in params.get("diagnostics", []) if isinstance(item, dict)
        ]
        self._diagnostic_events.setdefault(uri, threading.Event()).set()
