"""Artifact closure helpers for capability package skill bundles."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_ALLOWLISTED_PACKAGE_DIRS = {"assets", "docs", "references", "rules"}
_DENIED_DIR_NAMES = {
    ".cache",
    ".git",
    ".github",
    "__pycache__",
    "build",
    "cache",
    "coverage",
    "dist",
    "node_modules",
}
_DENIED_FILE_NAMES = {".env"}
_DENIED_PATH_MARKERS = ("secret", "token", "key")
_MARKDOWN_LINK_RE = re.compile(r"\]\(([^)#?]+)(?:[)#?][^)]*)?\)")
_BARE_PATH_RE = re.compile(
    r"(?<![\w./-])((?:\.\./|\./)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9][\w.-]*)"
)


@dataclass(frozen=True)
class SkillFileClosure:
    """Runtime-visible file closure for one package-managed skill entry."""

    package_root: str
    entry_path: str
    included_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "package_root": self.package_root,
            "entry_path": self.entry_path,
            "included_paths": list(self.included_paths),
            "denied_paths": list(self.denied_paths),
        }


def build_skill_file_closure(
    *,
    package_root: str | Path,
    entry_path: str | Path,
    explicit_paths: Iterable[str | Path] | None = None,
) -> SkillFileClosure:
    root = Path(package_root).expanduser().resolve()
    entry = Path(entry_path).expanduser().resolve()
    entry_rel = _relative_posix(entry, root)
    if entry.name != "SKILL.md":
        raise ValueError("entry_path must point to a SKILL.md file")
    if _is_denied_relative_path(entry_rel):
        raise ValueError(f"entry_path is denied by artifact closure rules: {entry_rel}")

    included: set[str] = {entry_rel}
    denied = _denied_paths(root)

    if entry.parent != root:
        included.update(_included_files_under(entry.parent, root))
    included.update(_included_allowlisted_package_dirs(root))
    included.update(_included_referenced_paths(root=root, entry=entry))
    for value in explicit_paths or ():
        included.update(_included_explicit_path(root=root, entry=entry, value=value))

    included.difference_update(denied)
    return SkillFileClosure(
        package_root=str(root),
        entry_path=entry_rel,
        included_paths=_sorted_paths(included),
        denied_paths=_sorted_paths(denied),
    )


def _included_files_under(path: Path, root: Path) -> set[str]:
    result: set[str] = set()
    if not path.exists():
        return result
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        rel = _relative_posix(item.resolve(), root)
        if not _is_denied_relative_path(rel):
            result.add(rel)
    return result


def _included_allowlisted_package_dirs(root: Path) -> set[str]:
    result: set[str] = set()
    for name in _ALLOWLISTED_PACKAGE_DIRS:
        result.update(_included_files_under(root / name, root))
    return result


def _included_referenced_paths(*, root: Path, entry: Path) -> set[str]:
    try:
        content = entry.read_text(encoding="utf-8")
    except OSError:
        return set()
    candidates: list[str] = []
    candidates.extend(match.group(1) for match in _MARKDOWN_LINK_RE.finditer(content))
    candidates.extend(match.group(1) for match in _BARE_PATH_RE.finditer(content))
    result: set[str] = set()
    for candidate in candidates:
        result.update(_included_candidate_path(root=root, base=entry.parent, value=candidate))
    return result


def _included_explicit_path(*, root: Path, entry: Path, value: str | Path) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    result = _included_candidate_path(root=root, base=root, value=text)
    if result:
        return result
    return _included_candidate_path(root=root, base=entry.parent, value=text)


def _included_candidate_path(*, root: Path, base: Path, value: str | Path) -> set[str]:
    text = str(value or "").strip().replace("\\", "/")
    if not text or "://" in text or text.startswith("#"):
        return set()
    path = (base / text).resolve()
    try:
        rel = _relative_posix(path, root)
    except ValueError:
        return set()
    if _is_denied_relative_path(rel):
        return set()
    if path.is_file():
        return {rel}
    if path.is_dir():
        return _included_files_under(path, root)
    return set()


def _denied_paths(root: Path) -> set[str]:
    result: set[str] = set()
    if not root.exists():
        return result
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        rel = _relative_posix(item.resolve(), root)
        if _is_denied_relative_path(rel):
            result.add(rel)
    return result


def _is_denied_relative_path(rel: str) -> bool:
    normalized = rel.replace("\\", "/").strip("/")
    parts = [part.lower() for part in normalized.split("/") if part]
    if any(part in _DENIED_DIR_NAMES for part in parts[:-1]):
        return True
    if parts and parts[-1] in _DENIED_FILE_NAMES:
        return True
    lower_path = normalized.lower()
    return any(marker in lower_path for marker in _DENIED_PATH_MARKERS)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sorted_paths(values: set[str]) -> list[str]:
    return sorted(values, key=lambda item: (item.count("/"), item))


__all__ = ["SkillFileClosure", "build_skill_file_closure"]
