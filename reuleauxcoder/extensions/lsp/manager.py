"""LSP manager with lazy language-server startup."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from reuleauxcoder.extensions.lsp.client import LspClient, LspClientError, path_to_uri
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import (
    DiagnosticBlock,
    diagnostic_from_lsp,
    render_blocks,
)
from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    detect_language,
    get_language_id_string,
    get_server_command,
    iter_supported_languages,
    resolve_workspace_root,
)


ClientFactory = Callable[[str, list[str], Path, dict[str, Any] | None], LspClient]


class LspError(RuntimeError):
    """Raised for user-facing LSP tool errors."""


@dataclass(slots=True)
class LspServerStatus:
    language: str
    command: str
    available: bool
    reason: str = ""


@dataclass(slots=True)
class LspHealthReport:
    statuses: list[LspServerStatus]

    @property
    def total(self) -> int:
        return len(self.statuses)

    @property
    def available(self) -> int:
        return sum(1 for status in self.statuses if status.available)


class LspManager:
    """Owns LSP client instances and diagnostic cache."""

    def __init__(
        self,
        config: LspConfig,
        *,
        workspace_cwd: Path | None = None,
        client_factory: ClientFactory | None = None,
    ):
        self.config = config
        self.workspace_cwd = (workspace_cwd or Path.cwd()).resolve()
        self._client_factory = client_factory or (
            lambda command, args, root, init_opts: LspClient(
                command=command,
                args=args,
                workspace_root=root,
                init_options=init_opts,
            )
        )
        self._clients: dict[tuple[LanguageId, str], LspClient] = {}
        self._cached_blocks: dict[str, DiagnosticBlock] = {}
        self._lock = threading.Lock()

    def start_worker(self) -> None:
        """Compatibility hook for upstream lifecycle; clients start lazily."""

    def health_check(self) -> LspHealthReport:
        statuses: list[LspServerStatus] = []
        for language in iter_supported_languages():
            override = self.config.get_override(language.value)
            command, _ = get_server_command(language)
            command = override.cmd if override and override.cmd else command
            available = bool(command and shutil.which(command))
            statuses.append(
                LspServerStatus(
                    language=language.value,
                    command=command,
                    available=available,
                    reason="" if available else "not found on PATH",
                )
            )
        return LspHealthReport(statuses=statuses)

    def diagnostics_for_file(self, file_path: str | Path) -> DiagnosticBlock | None:
        path, language = self._resolve_file_and_language(file_path)
        client = self._client_for(path, language)
        language_id = get_language_id_string(language)
        try:
            text = path.read_text(errors="replace")
            uri = path_to_uri(path)
            client.did_open(path, language_id, text)
            raw_items = client.wait_diagnostics(
                uri,
                timeout_sec=self.config.poll_timeout_ms / 1000,
            )
        except (OSError, LspClientError) as exc:
            raise LspError(str(exc)) from exc

        block = DiagnosticBlock(
            file_path=self._workspace_relative(path),
            items=[diagnostic_from_lsp(item) for item in raw_items],
        )
        self.record_diagnostics(block)
        return block

    def notify_file_changed(self, file_path: str | Path) -> DiagnosticBlock | None:
        return self.diagnostics_for_file(file_path)

    def request(
        self,
        method: str,
        file_path: str | Path,
        params: dict[str, Any],
    ) -> Any:
        path, language = self._resolve_file_and_language(file_path)
        client = self._client_for(path, language)
        try:
            if path.exists():
                client.did_open(path, get_language_id_string(language), path.read_text(errors="replace"))
            return client.request(
                method,
                params,
                timeout_sec=self.config.poll_timeout_ms / 1000,
            )
        except (OSError, LspClientError) as exc:
            raise LspError(str(exc)) from exc

    def record_diagnostics(self, block: DiagnosticBlock) -> None:
        with self._lock:
            self._cached_blocks[block.file_path] = block

    def cached_diagnostic_blocks(self) -> list[DiagnosticBlock]:
        with self._lock:
            return list(self._cached_blocks.values())

    def render_cached_diagnostics(self) -> str | None:
        return render_blocks(
            self.cached_diagnostic_blocks(),
            max_diagnostics=self.config.max_diagnostics,
            include_warnings=self.config.include_warnings,
        )

    def shutdown_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            client.shutdown()

    def _resolve_file_and_language(self, file_path: str | Path) -> tuple[Path, LanguageId]:
        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace_cwd / path
        path = path.resolve()
        language = detect_language(path)
        if language is None:
            raise LspError(f"unsupported file type for LSP: {path.suffix or path.name}")
        return path, language

    def _client_for(self, path: Path, language: LanguageId) -> LspClient:
        override = self.config.get_override(language.value)
        command, args = get_server_command(language)
        init_opts = None
        workspace_override = None
        if override is not None:
            if override.cmd:
                command = override.cmd
            if override.args is not None:
                args = override.args
            workspace_override = override.workspace_root
            init_opts = override.init_opts
        if not command:
            raise LspError(f"no language server configured for {language.value}")
        if shutil.which(command) is None:
            raise LspError(f"language server command not found on PATH: {command}")
        root = resolve_workspace_root(
            path,
            language,
            cwd=self.workspace_cwd,
            override=workspace_override,
        )
        key = (language, root.as_posix())
        if key not in self._clients:
            self._clients[key] = self._client_factory(command, list(args), root, init_opts)
        return self._clients[key]

    def _workspace_relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace_cwd).as_posix()
        except ValueError:
            return path.as_posix()
