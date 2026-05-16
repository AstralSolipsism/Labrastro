from pathlib import Path

from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import LspManager


class FakeClient:
    def __init__(self, command, args, root, init_opts):
        self.command = command
        self.args = args
        self.root = root
        self.init_opts = init_opts
        self.opened = []
        self.requests = []

    def did_open(self, file_path, language_id, text):
        self.opened.append((file_path, language_id, text))

    def wait_diagnostics(self, uri, timeout_sec):
        return [
            {
                "range": {"start": {"line": 0, "character": 2}},
                "severity": 1,
                "message": "syntax error",
            }
        ]

    def request(self, method, params, timeout_sec):
        self.requests.append((method, params, timeout_sec))
        return [{"uri": params["textDocument"]["uri"], "range": {"start": {"line": 1, "character": 0}}}]

    def shutdown(self):
        pass


def test_manager_caches_diagnostics_from_client(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("x =\n")
    config = LspConfig(
        server_overrides={
            "python": LspServerOverride(
                language="python",
                cmd="python",
                args=["-m", "fake"],
            )
        }
    )
    manager = LspManager(
        config,
        workspace_cwd=tmp_path,
        client_factory=lambda command, args, root, init_opts: FakeClient(
            command, args, root, init_opts
        ),
    )

    block = manager.diagnostics_for_file("main.py")

    assert block is not None
    assert block.file_path == "main.py"
    assert block.items[0].message == "syntax error"
    assert "syntax error" in (manager.render_cached_diagnostics() or "")


def test_manager_request_uses_workspace_relative_file(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("def f():\n    pass\n")
    manager = LspManager(
        LspConfig(
            server_overrides={
                "python": LspServerOverride(language="python", cmd="python")
            }
        ),
        workspace_cwd=tmp_path,
        client_factory=lambda command, args, root, init_opts: FakeClient(
            command, args, root, init_opts
        ),
    )

    result = manager.request(
        "textDocument/definition",
        "main.py",
        {"textDocument": {"uri": target.as_uri()}},
    )

    assert result
