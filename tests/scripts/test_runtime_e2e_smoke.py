from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.runtime_e2e_smoke import DEFAULT_SMOKE_DATABASE, ServerRunner


def _args(tmp_path: Path, *, database_name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp="20260508T000000Z",
        root=str(tmp_path),
        host_container="ezcode-host",
        pg_container="Postgresql",
        pg_user="user_rBrNr5",
        pg_password_env="EZCODE_PG_PASSWORD",
        database_name=database_name,
    )


def test_server_runner_uses_fixed_default_smoke_database(tmp_path: Path) -> None:
    runner = ServerRunner(_args(tmp_path), {"pg_password": "pw"})

    assert runner.db_name == DEFAULT_SMOKE_DATABASE


def test_create_database_recreates_smoke_database(tmp_path: Path) -> None:
    runner = ServerRunner(_args(tmp_path), {"pg_password": "pw"})
    calls: list[tuple[str, str]] = []

    def fake_psql(sql: str, *, database: str):
        calls.append((sql, database))
        return SimpleNamespace(stdout="")

    runner.psql = fake_psql  # type: ignore[method-assign]

    dsn = runner.create_database()

    assert calls[:2] == [
        ("DROP DATABASE IF EXISTS ezcode_smoke WITH (FORCE)", "postgres"),
        ("CREATE DATABASE ezcode_smoke", "postgres"),
    ]
    assert dsn.endswith("/ezcode_smoke")
