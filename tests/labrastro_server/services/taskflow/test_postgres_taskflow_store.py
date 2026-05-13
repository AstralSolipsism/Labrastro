from __future__ import annotations

import os

import pytest

from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.migration import run_migrations
from labrastro_server.infrastructure.persistence.postgres_taskflow_store import (
    PostgresTaskflowStore,
)
from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.taskflow_service import TaskflowService
from labrastro_server.taskflow.domain.project_state import ProjectState


pytestmark = pytest.mark.skipif(
    not os.environ.get("LABRASTRO_TEST_DATABASE_URL"),
    reason="LABRASTRO_TEST_DATABASE_URL is not configured",
)


def _service() -> TaskflowService:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    store = PostgresTaskflowStore(create_postgres_engine(database_url))
    project_service = ProjectService(store=store)
    project_service.save_project_state(
        ProjectState.new(project_id="project-pg-taskflow", name="Taskflow PG")
    )
    return TaskflowService(project_service=project_service, state_store=store)


def test_postgres_taskflow_store_recovers_complexity_evidence_and_estimate(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "package.json").write_text('{"dependencies":{"express":"^4.0.0"}}', encoding="utf-8")
    routes = workspace / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "health.ts").write_text("export async function GET() { return Response.json({}) }", encoding="utf-8")

    service = _service()
    state = service.start_taskflow(
        project_id="project-pg-taskflow",
        raw_goal="Expose a public health API.",
        taskflow_id="taskflow-pg-complexity",
        goal_id="goal-pg-complexity",
    )
    scanned = service.scan_repo_complexity(
        state.meta.taskflow_id,
        workspace_path=str(workspace),
        repository_id="repo-pg",
    )

    reloaded = _service()
    restored = reloaded.get_taskflow_state("taskflow-pg-complexity")
    project = reloaded.project_service.get_project_state("project-pg-taskflow")

    assert restored.meta.status == scanned.meta.status
    assert restored.compiler.complexity_estimate is not None
    assert restored.compiler.complexity_estimate.scan_refs
    assert any(
        item.source_type == "repo_static_analysis"
        for item in restored.compiler.complexity_estimate.evidence
    )
    assert project is not None
    assert project.project_id == "project-pg-taskflow"
    assert project.knowledge_base.reusable_context.get("repo_scan_snapshots")
