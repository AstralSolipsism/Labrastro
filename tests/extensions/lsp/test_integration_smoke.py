import os
from pathlib import Path

import pytest

from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.manager import LspManager


pytestmark = pytest.mark.skipif(
    not os.environ.get("RCODER_LSP_REAL_SERVER_SMOKE"),
    reason="set RCODER_LSP_REAL_SERVER_SMOKE=1 to run real language-server smoke tests",
)


def test_python_real_server_reports_syntax_error(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("x =\n")
    manager = LspManager(LspConfig(poll_timeout_ms=10_000), workspace_cwd=tmp_path)
    try:
        block = manager.diagnostics_for_file(target)
    finally:
        manager.shutdown_all()

    assert block is not None
    assert block.items
