from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.extensions.auth.cli import run_auth_cli
from reuleauxcoder.extensions.db import cli as db_cli


def test_db_cli_requires_authoritative_config(monkeypatch, capsys) -> None:
    monkeypatch.delenv("RCODER_CONFIG_PATH", raising=False)

    result = db_cli.run_db_cli(
        SimpleNamespace(config=None, db_command="status", retention_days=None)
    )

    assert result == 1
    assert "requires --config or RCODER_CONFIG_PATH" in capsys.readouterr().err


def test_db_cli_uses_rcoder_config_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "host.yaml"
    config_path.write_text(
        """
remote_exec:
  enabled: true
  host_mode: true
persistence:
  database_url: postgresql://user:pass@example.test/db
""".strip(),
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def fake_current_revision(database_url: str) -> str:
        captured["database_url"] = database_url
        return "rev_1"

    monkeypatch.setenv("RCODER_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(db_cli, "current_revision", fake_current_revision)

    result = db_cli.run_db_cli(
        SimpleNamespace(config=None, db_command="status", retention_days=None)
    )

    assert result == 0
    assert captured["database_url"] == "postgresql://user:pass@example.test/db"
    assert "rev_1" in capsys.readouterr().out


def test_auth_verify_config_requires_authoritative_config(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("RCODER_CONFIG_PATH", raising=False)

    result = run_auth_cli(SimpleNamespace(config=None, auth_command="verify-config"))

    assert result == 1
    assert "requires --config or RCODER_CONFIG_PATH" in capsys.readouterr().out

