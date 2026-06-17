"""Safe worktree planning helpers for daemon-owned Agent runtime roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess


_SAFE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def sanitize_branch_segment(value: str, *, fallback: str = "agent") -> str:
    """Return a git-branch-safe segment without path traversal semantics."""

    text = _SAFE_SEGMENT_RE.sub("-", value.strip()).strip(".-_/\\")
    return text[:64] if text else fallback


@dataclass(frozen=True)
class WorktreePlan:
    """A side-effect-free description of one daemon-owned worktree."""

    runtime_root: Path
    workspace_id: str
    task_id: str
    agent_id: str
    branch_name: str
    worktree_path: Path
    cache_path: Path | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "runtime_root": str(self.runtime_root),
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "branch_name": self.branch_name,
            "worktree_path": str(self.worktree_path),
            "cache_path": str(self.cache_path) if self.cache_path else None,
        }


@dataclass(frozen=True)
class WorktreeBranchResult:
    """A prepared git branch and daemon-owned worktree."""

    runtime_root: Path
    source_repo: Path
    branch_name: str
    base_git_ref: str
    base_tree_ref: str
    branch_git_ref: str
    branch_worktree_ref: str
    worktree_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "runtime_root": str(self.runtime_root),
            "source_repo": str(self.source_repo),
            "branch_name": self.branch_name,
            "base_git_ref": self.base_git_ref,
            "base_tree_ref": self.base_tree_ref,
            "branch_git_ref": self.branch_git_ref,
            "branch_worktree_ref": self.branch_worktree_ref,
            "worktree_path": str(self.worktree_path),
        }


@dataclass(frozen=True)
class WorktreeCleanupResult:
    """Result of a best-effort daemon-owned worktree cleanup."""

    worktree_path: Path
    branch_name: str
    removed_worktree: bool = False
    deleted_branch: bool = False
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, str | bool | list[str]]:
        return {
            "worktree_path": str(self.worktree_path),
            "branch_name": self.branch_name,
            "removed_worktree": self.removed_worktree,
            "deleted_branch": self.deleted_branch,
            "errors": list(self.errors),
            "ok": self.ok,
        }


class WorktreeOwnershipError(ValueError):
    """Raised when code tries to operate outside the daemon-owned runtime root."""


class WorktreeGitError(RuntimeError):
    """Raised when git cannot prepare or clean a daemon-owned worktree."""


class WorktreeManager:
    """Build worktree plans constrained to a single daemon-owned root."""

    def __init__(self, runtime_root: str | Path) -> None:
        root = Path(runtime_root).expanduser()
        if not str(root).strip():
            raise ValueError("runtime root is required")
        self.runtime_root = root.resolve()

    def plan(
        self,
        *,
        workspace_id: str,
        task_id: str,
        agent_id: str,
        repo_url: str | None = None,
        branch_name: str | None = None,
    ) -> WorktreePlan:
        workspace = sanitize_branch_segment(workspace_id, fallback="workspace")
        task = sanitize_branch_segment(task_id, fallback="task")
        agent = sanitize_branch_segment(agent_id, fallback="agent")
        branch = branch_name or f"agent/{agent}/{task[:12]}"
        worktree_path = (
            self.runtime_root / "worktrees" / workspace / f"{agent}-{task[:12]}"
        ).resolve()
        self.assert_owned(worktree_path)
        cache_path = None
        if repo_url:
            cache_path = (self.runtime_root / "repos" / workspace / _repo_cache_name(repo_url)).resolve()
            self.assert_owned(cache_path)
        return WorktreePlan(
            runtime_root=self.runtime_root,
            workspace_id=workspace,
            task_id=task,
            agent_id=agent,
            branch_name=branch,
            worktree_path=worktree_path,
            cache_path=cache_path,
        )

    def create_branch_worktree(
        self,
        *,
        source_repo: str | Path,
        plan: WorktreePlan,
        base_ref: str = "HEAD",
    ) -> WorktreeBranchResult:
        """Create a real git branch and worktree under the daemon runtime root."""

        repo = Path(source_repo).expanduser().resolve()
        self.assert_owned(plan.worktree_path)
        self._git(repo, "rev-parse", "--is-inside-work-tree")
        self._git(repo, "check-ref-format", "--branch", plan.branch_name)
        base_git_ref = self._git(
            repo,
            "rev-parse",
            "--verify",
            f"{str(base_ref or 'HEAD').strip()}^{{commit}}",
        )
        base_tree_ref = self._git(repo, "rev-parse", f"{base_git_ref}^{{tree}}")
        branch_ref = f"refs/heads/{plan.branch_name}"
        branch_existed = self._git_ok(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            branch_ref,
        )
        if branch_existed:
            raise WorktreeGitError(f"branch already exists: {plan.branch_name}")
        if plan.worktree_path.exists():
            raise WorktreeGitError(f"worktree path already exists: {plan.worktree_path}")
        try:
            self._git(
                repo,
                "worktree",
                "add",
                "-b",
                plan.branch_name,
                str(plan.worktree_path),
                base_git_ref,
            )
            return WorktreeBranchResult(
                runtime_root=self.runtime_root,
                source_repo=repo,
                branch_name=plan.branch_name,
                base_git_ref=base_git_ref,
                base_tree_ref=base_tree_ref,
                branch_git_ref=branch_ref,
                branch_worktree_ref=str(plan.worktree_path),
                worktree_path=plan.worktree_path,
            )
        except Exception:
            cleanup_errors: list[str] = []
            if plan.worktree_path.exists():
                try:
                    self.cleanup_branch_worktree(
                        source_repo=repo,
                        branch_name=plan.branch_name,
                        worktree_path=plan.worktree_path,
                        delete_branch=True,
                    )
                except Exception as cleanup_exc:  # pragma: no cover - defensive
                    cleanup_errors.append(str(cleanup_exc))
            if not branch_existed and self._git_ok(
                repo,
                "show-ref",
                "--verify",
                "--quiet",
                branch_ref,
            ):
                self._git_ok(repo, "branch", "-D", plan.branch_name)
            if cleanup_errors:
                raise WorktreeGitError("; ".join(cleanup_errors))
            raise

    def cleanup_branch_worktree(
        self,
        *,
        source_repo: str | Path,
        branch_name: str,
        worktree_path: str | Path,
        delete_branch: bool = False,
    ) -> WorktreeCleanupResult:
        """Remove a daemon-owned git worktree and optionally its branch."""

        repo = Path(source_repo).expanduser().resolve()
        resolved_worktree = self.assert_owned(worktree_path)
        removed_worktree = False
        deleted_branch = False
        errors: list[str] = []
        if resolved_worktree.exists():
            result = self._run_git(
                repo,
                "worktree",
                "remove",
                "--force",
                str(resolved_worktree),
            )
            if result.returncode == 0:
                removed_worktree = True
            elif resolved_worktree.exists():
                try:
                    shutil.rmtree(resolved_worktree)
                    removed_worktree = True
                except OSError as exc:
                    errors.append(str(exc))
        normalized_branch = str(branch_name or "").strip()
        if delete_branch and normalized_branch:
            self._git(repo, "check-ref-format", "--branch", normalized_branch)
            result = self._run_git(repo, "branch", "-D", normalized_branch)
            if result.returncode == 0:
                deleted_branch = True
            elif self._git_ok(
                repo,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{normalized_branch}",
            ):
                stderr = (result.stderr or result.stdout or "").strip()
                errors.append(stderr or f"failed to delete branch: {normalized_branch}")
        return WorktreeCleanupResult(
            worktree_path=resolved_worktree,
            branch_name=normalized_branch,
            removed_worktree=removed_worktree,
            deleted_branch=deleted_branch,
            errors=tuple(errors),
        )

    def assert_owned(self, path: str | Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        try:
            resolved.relative_to(self.runtime_root)
        except ValueError as exc:
            raise WorktreeOwnershipError(
                f"path is outside agent runtime root: {resolved}"
            ) from exc
        return resolved

    def _run_git(
        self,
        repo: Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
            text=True,
        )

    def _git(self, repo: Path, *args: str) -> str:
        result = self._run_git(repo, *args)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise WorktreeGitError(stderr or "git command failed")
        return result.stdout.strip()

    def _git_ok(self, repo: Path, *args: str) -> bool:
        return self._run_git(repo, *args).returncode == 0


def _repo_cache_name(repo_url: str) -> str:
    text = repo_url.strip().rstrip("/")
    if not text:
        return "repo.git"
    text = text.replace(":", "+").replace("@", "+").replace("\\", "/")
    parts = [sanitize_branch_segment(part, fallback="repo") for part in text.split("/")]
    name = "+".join(part for part in parts if part)
    if not name.endswith(".git"):
        name += ".git"
    return name


__all__ = [
    "WorktreeBranchResult",
    "WorktreeCleanupResult",
    "WorktreeGitError",
    "WorktreeManager",
    "WorktreeOwnershipError",
    "WorktreePlan",
    "sanitize_branch_segment",
]
