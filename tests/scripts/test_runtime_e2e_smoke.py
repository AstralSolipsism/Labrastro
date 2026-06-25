from __future__ import annotations

from pathlib import Path
import tarfile
from types import SimpleNamespace

from scripts.runtime_e2e_smoke import (
    DEFAULT_SMOKE_DATABASE,
    REQUIRED_TABLES,
    ServerRunner,
    create_source_archive,
    postgres_database_url,
)


def _args(tmp_path: Path, *, database_name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp="20260508T000000Z",
        root=str(tmp_path),
        host_container="labrastro-host",
        pg_container="Postgresql",
        pg_user="user_rBrNr5",
        pg_password_env="LABRASTRO_PG_PASSWORD",
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
        ("DROP DATABASE IF EXISTS labrastro_smoke WITH (FORCE)", "postgres"),
        ("CREATE DATABASE labrastro_smoke", "postgres"),
    ]
    assert dsn.endswith("/labrastro_smoke")


def test_create_database_url_encodes_postgres_password(tmp_path: Path) -> None:
    runner = ServerRunner(_args(tmp_path), {"pg_password": "qwer@4321"})
    runner.psql = lambda sql, *, database: SimpleNamespace(stdout="")  # type: ignore[method-assign]

    dsn = runner.create_database()

    assert dsn == "postgresql://user_rBrNr5:qwer%404321@Postgresql:5432/labrastro_smoke"


def test_preflight_requires_superadmin_password_before_deployment(tmp_path: Path) -> None:
    runner = ServerRunner(_args(tmp_path), {"pg_password": "pw"})

    try:
        runner.preflight()
    except RuntimeError as exc:
        assert "LABRASTRO_SUPERADMIN_PASSWORD is required before deployment" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("preflight accepted missing superadmin password")


def test_postgres_database_url_encodes_reserved_password_characters() -> None:
    assert postgres_database_url(
        user="user_rBrNr5",
        password="pa@ss/word#1",
        host="Postgresql",
        database="labrastro_smoke",
    ) == "postgresql://user_rBrNr5:pa%40ss%2Fword%231@Postgresql:5432/labrastro_smoke"


def test_required_tables_match_current_taskflow_snapshot_schema() -> None:
    assert "labrastro_taskflow_projects" in REQUIRED_TABLES
    assert "labrastro_taskflow_states" in REQUIRED_TABLES
    assert "labrastro_taskflow_events" in REQUIRED_TABLES
    assert "labrastro_taskflow_goals" not in REQUIRED_TABLES


def test_runtime_smoke_uses_activation_claim_endpoint() -> None:
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "runtime_e2e_smoke.py"
    ).read_text(encoding="utf-8")

    legacy_claim_path = '"/remote/agent-runs' + '/claim"'
    assert '"/remote/agent-run-activations/claim"' in script
    assert legacy_claim_path not in script


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
