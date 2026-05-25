from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.lsp.config import LspConfig


def test_lsp_config_defaults_when_section_missing() -> None:
    config = Config()

    lsp = LspConfig.from_config(config)

    assert lsp.enabled is True
    assert lsp.poll_timeout_ms == 5000
    assert lsp.max_diagnostics == 20
    assert lsp.server_overrides == {}


def test_lsp_config_parses_server_overrides() -> None:
    config = Config(
        lsp={
            "enabled": False,
            "poll_timeout_ms": 1000,
            "max_diagnostics": 5,
            "include_warnings": True,
            "servers": {
                "python": {
                    "cmd": "pyright-langserver",
                    "args": ["--stdio"],
                    "workspace_root": "src",
                    "init_opts": {"typeCheckingMode": "strict"},
                }
            },
        },
    )

    lsp = LspConfig.from_config(config)
    override = lsp.get_override("python")

    assert lsp.enabled is False
    assert lsp.poll_timeout_ms == 1000
    assert lsp.max_diagnostics == 5
    assert lsp.include_warnings is True
    assert override is not None
    assert override.cmd == "pyright-langserver"
    assert override.args == ["--stdio"]
    assert override.workspace_root == "src"
    assert override.init_opts == {"typeCheckingMode": "strict"}
