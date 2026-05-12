"""Read-only repository static analysis for Taskflow complexity evidence."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from labrastro_server.taskflow.domain.taskflow_state import (
    ComplexityEvidenceRecord,
    utc_now,
)


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}


@dataclass(slots=True)
class RepoImpactFinding:
    id: str
    kind: str
    dimension: str
    score_delta: int
    path: str = ""
    rationale: str = ""
    confidence: float = 0.75
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoImpactFinding":
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or ""),
            dimension=str(data.get("dimension") or ""),
            score_delta=int(data.get("score_delta") or 0),
            path=str(data.get("path") or ""),
            rationale=str(data.get("rationale") or ""),
            confidence=float(data.get("confidence") or 0.75),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "dimension": self.dimension,
            "score_delta": self.score_delta,
            "path": self.path,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class RepoScanSnapshot:
    id: str
    repository_id: str
    workspace_path: str
    content_hash: str
    findings: list[RepoImpactFinding] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoScanSnapshot":
        return cls(
            id=str(data.get("id") or ""),
            repository_id=str(data.get("repository_id") or ""),
            workspace_path=str(data.get("workspace_path") or ""),
            content_hash=str(data.get("content_hash") or ""),
            findings=[
                RepoImpactFinding.from_dict(dict(item))
                for item in data.get("findings") or []
                if isinstance(item, dict)
            ],
            missing_evidence=[str(item) for item in data.get("missing_evidence") or []],
            created_at=str(data.get("created_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "workspace_path": self.workspace_path,
            "content_hash": self.content_hash,
            "findings": [item.to_dict() for item in self.findings],
            "missing_evidence": list(self.missing_evidence),
            "created_at": self.created_at,
        }

    def to_evidence(self, *, brief_version: int | None = None) -> list[ComplexityEvidenceRecord]:
        records: list[ComplexityEvidenceRecord] = []
        for finding in self.findings:
            records.append(
                ComplexityEvidenceRecord(
                    id=f"complexity-repo-{self.id}-{finding.id}",
                    dimension=finding.dimension,
                    source_type="repo_static_analysis",
                    source_id=finding.id,
                    source_path=finding.path,
                    score_delta=finding.score_delta,
                    confidence=finding.confidence,
                    rationale=finding.rationale,
                    brief_version=brief_version,
                    extracted_by="repo_static_analyzer",
                    metadata={
                        **finding.metadata,
                        "scan_id": self.id,
                        "repository_id": self.repository_id,
                        "finding_kind": finding.kind,
                        "content_hash": self.content_hash,
                    },
                )
            )
        return records


class RepoStaticAnalyzer:
    """Produce formal complexity evidence from local repo structure only."""

    def scan(
        self,
        *,
        workspace_path: str | Path | None,
        repository_id: str = "",
        goal_hints: Iterable[str] = (),
    ) -> RepoScanSnapshot:
        if not workspace_path:
            return self._missing("missing-workspace-path", repository_id)
        root = Path(workspace_path).expanduser()
        if not root.exists() or not root.is_dir():
            return self._missing("workspace-path-not-found", repository_id, str(root))
        root = root.resolve()
        files = self._files(root)
        digest = self._digest(root, files)
        scan_id = f"repo-scan-{digest[:12]}"
        findings: list[RepoImpactFinding] = []
        self._manifest_findings(root, files, findings)
        self._api_findings(root, files, findings, goal_hints)
        self._data_findings(root, files, findings)
        self._ops_findings(root, files, findings)
        self._test_findings(root, files, findings)
        self._dedupe(findings)
        return RepoScanSnapshot(
            id=scan_id,
            repository_id=repository_id or root.name,
            workspace_path=str(root),
            content_hash=digest,
            findings=findings,
            missing_evidence=[],
        )

    def _missing(
        self, reason: str, repository_id: str = "", workspace_path: str = ""
    ) -> RepoScanSnapshot:
        digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        return RepoScanSnapshot(
            id=f"repo-scan-{digest[:12]}",
            repository_id=repository_id,
            workspace_path=workspace_path,
            content_hash=digest,
            missing_evidence=[reason],
        )

    def _files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for path in root.rglob("*"):
            if len(files) >= 2500:
                break
            if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            if path.is_file():
                files.append(path)
        return files

    def _digest(self, root: Path, files: list[Path]) -> str:
        hasher = hashlib.sha256()
        for path in sorted(files):
            rel = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            hasher.update(f"{rel}:{stat.st_size}:{int(stat.st_mtime)}\n".encode("utf-8"))
        return hasher.hexdigest()

    def _manifest_findings(
        self, root: Path, files: list[Path], findings: list[RepoImpactFinding]
    ) -> None:
        names = {path.name for path in files}
        if "package.json" in names:
            self._package_json_findings(root, findings)
        if "pyproject.toml" in names:
            self._add(
                findings,
                "python-project",
                "technical_risk",
                1,
                "pyproject.toml",
                "Python project manifest is present.",
                confidence=0.65,
            )
        if "go.mod" in names:
            self._add(
                findings,
                "go-project",
                "technical_risk",
                1,
                "go.mod",
                "Go module manifest is present.",
                confidence=0.65,
            )
        if "tsconfig.json" in names:
            self._add(
                findings,
                "typescript-project",
                "technical_risk",
                1,
                "tsconfig.json",
                "TypeScript project configuration is present.",
                confidence=0.65,
            )

    def _package_json_findings(self, root: Path, findings: list[RepoImpactFinding]) -> None:
        path = root / "package.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        deps = {
            **dict(data.get("dependencies") or {}),
            **dict(data.get("devDependencies") or {}),
        }
        if any(name in deps for name in ("express", "fastify", "koa", "hono", "next")):
            self._add(
                findings,
                "node-http-framework",
                "interface_impact",
                1,
                "package.json",
                "HTTP framework dependency suggests API surface area.",
                confidence=0.7,
                metadata={"contract": "unknown"},
            )
        if data.get("workspaces"):
            self._add(
                findings,
                "node-workspaces",
                "org_collaboration",
                1,
                "package.json",
                "Package workspaces suggest cross-package coordination.",
                confidence=0.75,
            )

    def _api_findings(
        self,
        root: Path,
        files: list[Path],
        findings: list[RepoImpactFinding],
        goal_hints: Iterable[str],
    ) -> None:
        hints = " ".join(goal_hints).lower()
        for path in files:
            rel = path.relative_to(root).as_posix()
            rel_lower = rel.lower()
            if path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            content = self._read_small(path)
            route_path = any(
                token in rel_lower
                for token in ("/routes/", "/api/", "/controllers/", "route.")
            )
            route_code = any(
                token in content
                for token in (
                    "export async function GET",
                    "export async function POST",
                    "app.get(",
                    "router.get(",
                    "@app.route",
                    "FastAPI(",
                )
            )
            if route_path or route_code:
                self._add(
                    findings,
                    f"api-{self._stable_id(rel)}",
                    "interface_impact",
                    2 if ("public" in hints or "api" in hints or route_code) else 1,
                    rel,
                    "Repo static analysis found API or route surface.",
                    confidence=0.82,
                    metadata={"public": "api" in hints or "public" in hints},
                )
            if self._exports_public_symbol(path, content):
                self._add(
                    findings,
                    f"export-{self._stable_id(rel)}",
                    "interface_impact",
                    1,
                    rel,
                    "Exported symbols may affect module consumers.",
                    confidence=0.58,
                )

    def _data_findings(
        self, root: Path, files: list[Path], findings: list[RepoImpactFinding]
    ) -> None:
        for path in files:
            rel = path.relative_to(root).as_posix()
            rel_lower = rel.lower()
            if (
                "/migration" in rel_lower
                or "/migrations/" in rel_lower
                or path.suffix.lower() == ".sql"
            ):
                self._add(
                    findings,
                    f"migration-{self._stable_id(rel)}",
                    "data_impact",
                    2,
                    rel,
                    "Repo static analysis found schema or migration files.",
                    confidence=0.85,
                    metadata={"migration": True},
                )

    def _ops_findings(
        self, root: Path, files: list[Path], findings: list[RepoImpactFinding]
    ) -> None:
        for path in files:
            rel = path.relative_to(root).as_posix()
            rel_lower = rel.lower()
            if any(
                token in rel_lower
                for token in (
                    ".github/workflows",
                    "dockerfile",
                    "k8s/",
                    "helm/",
                    "deploy",
                    "runbook",
                )
            ):
                self._add(
                    findings,
                    f"ops-{self._stable_id(rel)}",
                    "ops_impact",
                    2 if "deploy" in rel_lower else 1,
                    rel,
                    "Repo static analysis found deployment or operations files.",
                    confidence=0.78,
                    metadata={"ops_surface": True},
                )

    def _test_findings(
        self, root: Path, files: list[Path], findings: list[RepoImpactFinding]
    ) -> None:
        code_files = [
            path for path in files if path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx"}
        ]
        test_files = [
            path
            for path in code_files
            if "test" in path.name.lower()
            or "spec" in path.name.lower()
            or "tests" in path.relative_to(root).parts
        ]
        if code_files and not test_files:
            self._add(
                findings,
                "low-test-signal",
                "technical_risk",
                1,
                "",
                "No test files were found near code files.",
                confidence=0.55,
                metadata={"tests_found": False},
            )

    def _exports_public_symbol(self, path: Path, content: str) -> bool:
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(content or "")
            except SyntaxError:
                return False
            return any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and not node.name.startswith("_")
                for node in tree.body
            )
        return "export " in content

    def _read_small(self, path: Path) -> str:
        try:
            if path.stat().st_size > 128 * 1024:
                return ""
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def _add(
        self,
        findings: list[RepoImpactFinding],
        kind: str,
        dimension: str,
        score_delta: int,
        path: str,
        rationale: str,
        *,
        confidence: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        findings.append(
            RepoImpactFinding(
                id=f"{kind}-{len(findings) + 1}",
                kind=kind,
                dimension=dimension,
                score_delta=score_delta,
                path=path,
                rationale=rationale,
                confidence=confidence,
                metadata=dict(metadata or {}),
            )
        )

    def _stable_id(self, value: str) -> str:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]

    def _dedupe(self, findings: list[RepoImpactFinding]) -> None:
        seen: set[tuple[str, str, str]] = set()
        unique: list[RepoImpactFinding] = []
        for finding in findings:
            key = (finding.kind, finding.dimension, finding.path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(finding)
        findings[:] = unique


__all__ = ["RepoImpactFinding", "RepoScanSnapshot", "RepoStaticAnalyzer"]
