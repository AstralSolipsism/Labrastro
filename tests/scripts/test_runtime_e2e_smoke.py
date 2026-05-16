from __future__ import annotations

from pathlib import Path
import tarfile
from types import SimpleNamespace

from scripts.runtime_e2e_smoke import (
    DEFAULT_SMOKE_DATABASE,
    REQUIRED_TABLES,
    ServerRunner,
    create_source_archive,
)


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


def test_required_tables_match_current_taskflow_snapshot_schema() -> None:
    assert "labrastro_taskflow_projects" in REQUIRED_TABLES
    assert "labrastro_taskflow_states" in REQUIRED_TABLES
    assert "labrastro_taskflow_events" in REQUIRED_TABLES
    assert "labrastro_taskflow_goals" not in REQUIRED_TABLES


def test_source_archive_embeds_deploy_revision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".deploy-revision").write_text("abc123\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ignored", encoding="utf-8")
    (repo / "docker").mkdir()
    (repo / "docker" / "entrypoint.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")

    archive = create_source_archive(repo, "20260516T000000Z", tmp_path)

    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        revision = tar.extractfile(".deploy-revision")
        assert revision is not None
        revision_text = revision.read().decode("utf-8")

    assert ".deploy-revision" in names
    assert ".git/HEAD" not in names
    assert revision_text == "abc123\n"
