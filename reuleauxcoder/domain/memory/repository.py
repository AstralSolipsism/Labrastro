"""Scoped memory repositories."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import uuid

from reuleauxcoder.domain.memory.models import (
    MemoryCaptureEvent,
    MemoryCaptureJob,
    MemoryCaptureReceipt,
    MemoryItem,
    MemoryQuery,
    MemoryScope,
)

try:  # pragma: no cover - import availability depends on installed extras.
    from sqlalchemy import text as sql_text
except ImportError:  # pragma: no cover
    sql_text = None


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value: str | bytes | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class SQLiteMemoryRepository:
    """SQLite development backend with mandatory agent namespace filtering."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    type TEXT NOT NULL,
                    abstract TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    fields_json TEXT NOT NULL DEFAULT '{}',
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    project_id TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    repo_id TEXT NOT NULL DEFAULT '',
                    goal_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_item_versions (
                    version_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    abstract TEXT NOT NULL DEFAULT '',
                    fields_json TEXT NOT NULL DEFAULT '{}',
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    item_id TEXT PRIMARY KEY,
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_sources (
                    source_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_access_events (
                    access_id TEXT PRIMARY KEY,
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    item_id TEXT,
                    query TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS memory_capture_jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS memory_scope_versions (
                    owner_agent_id TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (owner_agent_id, memory_namespace)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_items_scope_status_updated
                    ON memory_items(owner_agent_id, memory_namespace, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_items_scope_project
                    ON memory_items(owner_agent_id, memory_namespace, project_id, workspace_id);
                CREATE INDEX IF NOT EXISTS idx_memory_item_versions_scope
                    ON memory_item_versions(owner_agent_id, memory_namespace, item_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_access_events_scope
                    ON memory_access_events(owner_agent_id, memory_namespace, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_capture_jobs_scope
                    ON memory_capture_jobs(owner_agent_id, memory_namespace, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_capture_jobs_scope_idempotency
                    ON memory_capture_jobs(owner_agent_id, memory_namespace, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                """
            )

    def scope_version(self, scope: MemoryScope) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT version FROM memory_scope_versions
                WHERE owner_agent_id=? AND memory_namespace=?
                """,
                (scope.owner_agent_id, scope.memory_namespace),
            ).fetchone()
        return int(row["version"]) if row is not None else 0

    def _bump_scope_version(
        self, conn: sqlite3.Connection, owner_agent_id: str, memory_namespace: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_scope_versions (
                owner_agent_id, memory_namespace, version, updated_at
            ) VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(owner_agent_id, memory_namespace) DO UPDATE SET
                version=version + 1,
                updated_at=CURRENT_TIMESTAMP
            """,
            (owner_agent_id, memory_namespace),
        )

    def upsert(self, item: MemoryItem) -> MemoryItem:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT owner_agent_id, memory_namespace, version, created_at
                FROM memory_items WHERE id=?
                """,
                (item.id,),
            ).fetchone()
            if existing is not None and (
                existing["owner_agent_id"] != item.owner_agent_id
                or existing["memory_namespace"] != item.memory_namespace
            ):
                raise ValueError("memory item id already belongs to another agent scope")
            version = int(existing["version"]) + 1 if existing is not None else item.version
            created_at = existing["created_at"] if existing is not None else item.created_at
            item.version = version
            item.created_at = created_at
            conn.execute(
                """
                INSERT INTO memory_items (
                    id, owner_agent_id, memory_namespace, type, abstract, content,
                    fields_json, source_refs_json, confidence, version, status,
                    project_id, workspace_id, repo_id, goal_id, task_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    type=excluded.type,
                    abstract=excluded.abstract,
                    content=excluded.content,
                    fields_json=excluded.fields_json,
                    source_refs_json=excluded.source_refs_json,
                    confidence=excluded.confidence,
                    version=excluded.version,
                    status=excluded.status,
                    project_id=excluded.project_id,
                    workspace_id=excluded.workspace_id,
                    repo_id=excluded.repo_id,
                    goal_id=excluded.goal_id,
                    task_id=excluded.task_id,
                    updated_at=excluded.updated_at
                """,
                (
                    item.id,
                    item.owner_agent_id,
                    item.memory_namespace,
                    item.type,
                    item.abstract,
                    item.content,
                    _json(item.fields),
                    _json(item.source_refs),
                    item.confidence,
                    item.version,
                    item.status,
                    item.project_id,
                    item.workspace_id,
                    item.repo_id,
                    item.goal_id,
                    item.task_id,
                    item.created_at,
                    item.updated_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO memory_item_versions (
                    version_id, item_id, owner_agent_id, memory_namespace, version,
                    content, abstract, fields_json, source_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    f"memver_{uuid.uuid4().hex}",
                    item.id,
                    item.owner_agent_id,
                    item.memory_namespace,
                    item.version,
                    item.content,
                    item.abstract,
                    _json(item.fields),
                    _json(item.source_refs),
                ),
            )
            self._bump_scope_version(conn, item.owner_agent_id, item.memory_namespace)
            conn.commit()
        return item

    def search(self, scope: MemoryScope, query: MemoryQuery) -> list[MemoryItem]:
        clauses = [
            "owner_agent_id=?",
            "memory_namespace=?",
            "status='active'",
            "project_id=?",
            "workspace_id=?",
            "repo_id=?",
        ]
        params: list[Any] = [
            scope.owner_agent_id,
            scope.memory_namespace,
            scope.project_id,
            scope.workspace_id,
            scope.repo_id,
        ]
        if query.type_filter:
            clauses.append("type=?")
            params.append(str(query.type_filter))
        text = str(query.query or "").strip()
        if text:
            clauses.append("(content LIKE ? OR abstract LIKE ?)")
            like = f"%{text}%"
            params.extend([like, like])
        params.append(max(1, int(query.limit or 1)))
        with self._connect() as conn:
            sql = f"""
                SELECT * FROM memory_items
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id ASC
                LIMIT ?
                """
            rows = conn.execute(sql, params).fetchall()
            if not rows and text:
                fallback_clauses = [
                    clause
                    for clause in clauses
                    if clause != "(content LIKE ? OR abstract LIKE ?)"
                ]
                fallback_params = params[:-3] + [params[-1]]
                rows = conn.execute(
                    f"""
                    SELECT * FROM memory_items
                    WHERE {' AND '.join(fallback_clauses)}
                    ORDER BY updated_at DESC, id ASC
                    LIMIT ?
                    """,
                    fallback_params,
                ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO memory_access_events (
                        access_id, owner_agent_id, memory_namespace, item_id, query, event_type
                    ) VALUES (?, ?, ?, ?, ?, 'provide')
                    """,
                    (
                        f"memacc_{uuid.uuid4().hex}",
                        scope.owner_agent_id,
                        scope.memory_namespace,
                        row["id"],
                        text,
                    ),
                )
            conn.commit()
        return [self._item_from_row(row) for row in rows]

    def enqueue_capture_job(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt:
        with self._connect() as conn:
            if event.idempotency_key:
                existing = conn.execute(
                    """
                    SELECT job_id FROM memory_capture_jobs
                    WHERE owner_agent_id=? AND memory_namespace=? AND idempotency_key=?
                    """,
                    (
                        scope.owner_agent_id,
                        scope.memory_namespace,
                        event.idempotency_key,
                    ),
                ).fetchone()
                if existing is not None:
                    return MemoryCaptureReceipt(
                        job_id=str(existing["job_id"]),
                        enqueued=False,
                        scope_version=self.scope_version(scope),
                    )
            job_id = f"memjob_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO memory_capture_jobs (
                    job_id, owner_agent_id, memory_namespace, kind,
                    payload_json, idempotency_key, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    job_id,
                    scope.owner_agent_id,
                    scope.memory_namespace,
                    event.kind,
                    _json(event.payload),
                    event.idempotency_key,
                    event.created_at,
                ),
            )
            conn.commit()
        return MemoryCaptureReceipt(
            job_id=job_id,
            enqueued=True,
            scope_version=self.scope_version(scope),
        )

    def list_capture_jobs(self, scope: MemoryScope) -> list[MemoryCaptureJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_capture_jobs
                WHERE owner_agent_id=? AND memory_namespace=?
                ORDER BY created_at ASC, job_id ASC
                """,
                (scope.owner_agent_id, scope.memory_namespace),
            ).fetchall()
        return [
            MemoryCaptureJob(
                job_id=str(row["job_id"]),
                owner_agent_id=str(row["owner_agent_id"]),
                memory_namespace=str(row["memory_namespace"]),
                kind=str(row["kind"]),
                payload=dict(_json_loads(row["payload_json"], {})),
                idempotency_key=row["idempotency_key"],
                status=str(row["status"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=str(row["id"]),
            owner_agent_id=str(row["owner_agent_id"]),
            memory_namespace=str(row["memory_namespace"]),
            type=str(row["type"]),
            content=str(row["content"]),
            abstract=str(row["abstract"]),
            fields=dict(_json_loads(row["fields_json"], {})),
            source_refs=list(_json_loads(row["source_refs_json"], [])),
            confidence=float(row["confidence"]),
            version=int(row["version"]),
            status=str(row["status"]),
            project_id=str(row["project_id"]),
            workspace_id=str(row["workspace_id"]),
            repo_id=str(row["repo_id"]),
            goal_id=str(row["goal_id"]),
            task_id=str(row["task_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class PostgresMemoryRepository:
    """Postgres backend using the same scoped repository contract."""

    def __init__(self, engine: Any) -> None:
        if sql_text is None:
            raise RuntimeError("Postgres memory repository requires sqlalchemy.")
        self.engine = engine

    def scope_version(self, scope: MemoryScope) -> int:
        with self.engine.connect() as conn:
            row = conn.execute(
                sql_text(
                    """
                    SELECT version FROM memory_scope_versions
                    WHERE owner_agent_id=:owner_agent_id
                      AND memory_namespace=:memory_namespace
                    """
                ),
                {
                    "owner_agent_id": scope.owner_agent_id,
                    "memory_namespace": scope.memory_namespace,
                },
            ).mappings().first()
        return int(row["version"]) if row is not None else 0

    def _bump_scope_version(
        self, conn: Any, owner_agent_id: str, memory_namespace: str
    ) -> None:
        conn.execute(
            sql_text(
                """
                INSERT INTO memory_scope_versions (
                    owner_agent_id, memory_namespace, version, updated_at
                ) VALUES (
                    :owner_agent_id, :memory_namespace, 1, now()
                )
                ON CONFLICT(owner_agent_id, memory_namespace) DO UPDATE SET
                    version=memory_scope_versions.version + 1,
                    updated_at=now()
                """
            ),
            {
                "owner_agent_id": owner_agent_id,
                "memory_namespace": memory_namespace,
            },
        )

    def upsert(self, item: MemoryItem) -> MemoryItem:
        with self.engine.begin() as conn:
            existing = conn.execute(
                sql_text(
                    """
                    SELECT owner_agent_id, memory_namespace, version, created_at
                    FROM memory_items WHERE id=:id
                    """
                ),
                {"id": item.id},
            ).mappings().first()
            if existing is not None and (
                existing["owner_agent_id"] != item.owner_agent_id
                or existing["memory_namespace"] != item.memory_namespace
            ):
                raise ValueError("memory item id already belongs to another agent scope")
            item.version = int(existing["version"]) + 1 if existing is not None else item.version
            if existing is not None:
                item.created_at = _dt_to_text(existing["created_at"])
            conn.execute(
                sql_text(
                    """
                    INSERT INTO memory_items (
                        id, owner_agent_id, memory_namespace, type, abstract, content,
                        fields, source_refs, confidence, version, status,
                        project_id, workspace_id, repo_id, goal_id, task_id,
                        created_at, updated_at
                    ) VALUES (
                        :id, :owner_agent_id, :memory_namespace, :type, :abstract, :content,
                        CAST(:fields AS JSONB), CAST(:source_refs AS JSONB),
                        :confidence, :version, :status,
                        :project_id, :workspace_id, :repo_id, :goal_id, :task_id,
                        CAST(:created_at AS TIMESTAMPTZ), CAST(:updated_at AS TIMESTAMPTZ)
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        type=EXCLUDED.type,
                        abstract=EXCLUDED.abstract,
                        content=EXCLUDED.content,
                        fields=EXCLUDED.fields,
                        source_refs=EXCLUDED.source_refs,
                        confidence=EXCLUDED.confidence,
                        version=EXCLUDED.version,
                        status=EXCLUDED.status,
                        project_id=EXCLUDED.project_id,
                        workspace_id=EXCLUDED.workspace_id,
                        repo_id=EXCLUDED.repo_id,
                        goal_id=EXCLUDED.goal_id,
                        task_id=EXCLUDED.task_id,
                        updated_at=EXCLUDED.updated_at
                    """
                ),
                {
                    "id": item.id,
                    "owner_agent_id": item.owner_agent_id,
                    "memory_namespace": item.memory_namespace,
                    "type": item.type,
                    "abstract": item.abstract,
                    "content": item.content,
                    "fields": _json(item.fields),
                    "source_refs": _json(item.source_refs),
                    "confidence": item.confidence,
                    "version": item.version,
                    "status": item.status,
                    "project_id": item.project_id,
                    "workspace_id": item.workspace_id,
                    "repo_id": item.repo_id,
                    "goal_id": item.goal_id,
                    "task_id": item.task_id,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                },
            )
            conn.execute(
                sql_text(
                    """
                    INSERT INTO memory_item_versions (
                        version_id, item_id, owner_agent_id, memory_namespace, version,
                        content, abstract, fields, source_refs
                    ) VALUES (
                        :version_id, :item_id, :owner_agent_id, :memory_namespace,
                        :version, :content, :abstract,
                        CAST(:fields AS JSONB), CAST(:source_refs AS JSONB)
                    )
                    """
                ),
                {
                    "version_id": f"memver_{uuid.uuid4().hex}",
                    "item_id": item.id,
                    "owner_agent_id": item.owner_agent_id,
                    "memory_namespace": item.memory_namespace,
                    "version": item.version,
                    "content": item.content,
                    "abstract": item.abstract,
                    "fields": _json(item.fields),
                    "source_refs": _json(item.source_refs),
                },
            )
            self._bump_scope_version(conn, item.owner_agent_id, item.memory_namespace)
        return item

    def search(self, scope: MemoryScope, query: MemoryQuery) -> list[MemoryItem]:
        clauses = [
            "owner_agent_id=:owner_agent_id",
            "memory_namespace=:memory_namespace",
            "status='active'",
            "project_id=:project_id",
            "workspace_id=:workspace_id",
            "repo_id=:repo_id",
        ]
        params: dict[str, Any] = {
            "owner_agent_id": scope.owner_agent_id,
            "memory_namespace": scope.memory_namespace,
            "project_id": scope.project_id,
            "workspace_id": scope.workspace_id,
            "repo_id": scope.repo_id,
            "limit": max(1, int(query.limit or 1)),
            "query": str(query.query or "").strip(),
        }
        if query.type_filter:
            clauses.append("type=:type_filter")
            params["type_filter"] = str(query.type_filter)
        if params["query"]:
            clauses.append("(content ILIKE :query_like OR abstract ILIKE :query_like)")
            params["query_like"] = f"%{params['query']}%"
        with self.engine.begin() as conn:
            rows = conn.execute(
                sql_text(
                    f"""
                    SELECT * FROM memory_items
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, id ASC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
            if not rows and params["query"]:
                fallback_clauses = [
                    clause
                    for clause in clauses
                    if clause != "(content ILIKE :query_like OR abstract ILIKE :query_like)"
                ]
                rows = conn.execute(
                    sql_text(
                        f"""
                        SELECT * FROM memory_items
                        WHERE {' AND '.join(fallback_clauses)}
                        ORDER BY updated_at DESC, id ASC
                        LIMIT :limit
                        """
                    ),
                    params,
                ).mappings().all()
            for row in rows:
                conn.execute(
                    sql_text(
                        """
                        INSERT INTO memory_access_events (
                            access_id, owner_agent_id, memory_namespace,
                            item_id, query, event_type
                        ) VALUES (
                            :access_id, :owner_agent_id, :memory_namespace,
                            :item_id, :query, 'provide'
                        )
                        """
                    ),
                    {
                        "access_id": f"memacc_{uuid.uuid4().hex}",
                        "owner_agent_id": scope.owner_agent_id,
                        "memory_namespace": scope.memory_namespace,
                        "item_id": row["id"],
                        "query": params["query"],
                    },
                )
        return [self._item_from_mapping(dict(row)) for row in rows]

    def enqueue_capture_job(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt:
        with self.engine.begin() as conn:
            if event.idempotency_key:
                existing = conn.execute(
                    sql_text(
                        """
                        SELECT job_id FROM memory_capture_jobs
                        WHERE owner_agent_id=:owner_agent_id
                          AND memory_namespace=:memory_namespace
                          AND idempotency_key=:idempotency_key
                        """
                    ),
                    {
                        "owner_agent_id": scope.owner_agent_id,
                        "memory_namespace": scope.memory_namespace,
                        "idempotency_key": event.idempotency_key,
                    },
                ).mappings().first()
                if existing is not None:
                    return MemoryCaptureReceipt(
                        job_id=str(existing["job_id"]),
                        enqueued=False,
                        scope_version=self.scope_version(scope),
                    )
            job_id = f"memjob_{uuid.uuid4().hex}"
            conn.execute(
                sql_text(
                    """
                    INSERT INTO memory_capture_jobs (
                        job_id, owner_agent_id, memory_namespace, kind,
                        payload, idempotency_key, status, created_at
                    ) VALUES (
                        :job_id, :owner_agent_id, :memory_namespace, :kind,
                        CAST(:payload AS JSONB), :idempotency_key, 'queued',
                        CAST(:created_at AS TIMESTAMPTZ)
                    )
                    """
                ),
                {
                    "job_id": job_id,
                    "owner_agent_id": scope.owner_agent_id,
                    "memory_namespace": scope.memory_namespace,
                    "kind": event.kind,
                    "payload": _json(event.payload),
                    "idempotency_key": event.idempotency_key,
                    "created_at": event.created_at,
                },
            )
        return MemoryCaptureReceipt(
            job_id=job_id,
            enqueued=True,
            scope_version=self.scope_version(scope),
        )

    def list_capture_jobs(self, scope: MemoryScope) -> list[MemoryCaptureJob]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                sql_text(
                    """
                    SELECT * FROM memory_capture_jobs
                    WHERE owner_agent_id=:owner_agent_id
                      AND memory_namespace=:memory_namespace
                    ORDER BY created_at ASC, job_id ASC
                    """
                ),
                {
                    "owner_agent_id": scope.owner_agent_id,
                    "memory_namespace": scope.memory_namespace,
                },
            ).mappings().all()
        return [
            MemoryCaptureJob(
                job_id=str(row["job_id"]),
                owner_agent_id=str(row["owner_agent_id"]),
                memory_namespace=str(row["memory_namespace"]),
                kind=str(row["kind"]),
                payload=dict(row["payload"] or {}),
                idempotency_key=row["idempotency_key"],
                status=str(row["status"]),
                created_at=_dt_to_text(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _item_from_mapping(row: dict[str, Any]) -> MemoryItem:
        return MemoryItem(
            id=str(row["id"]),
            owner_agent_id=str(row["owner_agent_id"]),
            memory_namespace=str(row["memory_namespace"]),
            type=str(row["type"]),
            content=str(row["content"]),
            abstract=str(row["abstract"]),
            fields=dict(row.get("fields") or {}),
            source_refs=list(row.get("source_refs") or []),
            confidence=float(row["confidence"]),
            version=int(row["version"]),
            status=str(row["status"]),
            project_id=str(row["project_id"]),
            workspace_id=str(row["workspace_id"]),
            repo_id=str(row["repo_id"]),
            goal_id=str(row["goal_id"]),
            task_id=str(row["task_id"]),
            created_at=_dt_to_text(row["created_at"]),
            updated_at=_dt_to_text(row["updated_at"]),
        )


def _dt_to_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")
