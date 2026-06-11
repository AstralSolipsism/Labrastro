# Capability Package Ecosystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the frozen capability package ecosystem architecture so a GitHub link becomes a registered, prepared, installed, verified, and separately activatable capability without free-form LLM config generation.

**Architecture:** Keep AgentRun as execution/audit substrate, but move durable capability-package truth into typed domain models, source snapshots, normalized manifests, dependency graphs, install plans, executor result records, and frontend state projections. The server is the global fact source; local peer facts are reported by the peer through the same typed install-plan/result protocol.

**Tech Stack:** Python backend, ReuleauxCoder domain models, Labrastro server services/admin APIs, config compatibility layer, VS Code extension TypeScript, Solid webview UI, pytest, Vitest, GitNexus.

---

## 0. Execution Rules

This plan implements the frozen matrix:

```text
docs/superpowers/plans/2026-06-11-capability-package-ecosystem-architecture-matrix.md
```

The implementation must update matrix evidence as tasks complete. A task is not complete when code compiles; it is complete only when its tests pass and the matrix rows it covers can point to evidence.

Use these repository roots:

```text
Server repo:
D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem

VS Code extension repo:
D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem-extension
```

The clean source repos may remain indexed as `Labrastro` and `Labrastro-vscode-extension`, but active implementation must use the worktree aliases below. Using the clean aliases during an active worktree task can return false "No changes detected" evidence.

```text
Server GitNexus alias:
Labrastro-capability-package-ecosystem

VS Code extension GitNexus alias:
Labrastro-vscode-extension-capability-package-ecosystem
```

Use these verification command patterns on this Windows machine:

```powershell
# Server focused tests
.\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex

# Extension focused tests
npm run typecheck
npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts

# GitNexus impact check
gitnexus analyze --index-only --name Labrastro-capability-package-ecosystem .
gitnexus detect-changes --repo Labrastro-capability-package-ecosystem --scope all

gitnexus analyze --index-only --name Labrastro-vscode-extension-capability-package-ecosystem .
gitnexus detect-changes --repo Labrastro-vscode-extension-capability-package-ecosystem --scope all
```

Do not execute broad full-suite tests until focused tests pass. If pytest hits Windows temp permission errors, keep using the workspace-local `--basetemp` and cache settings above.

### 0.1 Active Drift-Convergence Rules

The remaining work is not a normal feature tail. It is a drift-convergence pass. Follow these rules before every task:

1. Run GitNexus on both worktrees with explicit aliases and record the affected files, symbols, processes, and risk level in the matrix evidence.
2. For the invariant being changed, run GitNexus `query`, `context`, and when useful `impact --include-tests` against the exact symbol or file.
3. Write or update a failing regression guard before changing implementation.
4. After editing shared helpers, protocol models, state projections, admin/remote services, or frontend view models, refresh the GitNexus index and rerun `detect-changes`.
5. Do not mark a task complete unless the matrix row contains this chain:

```text
GitNexus impact boundary -> files changed -> tests/grep that prove the invariant
```

6. Completion is blocked if any runtime path still decides activation from `package.enabled` alone, any route owns its own materialization logic, any frontend view fabricates state axes, or any protocol endpoint parses aliases independently.

If GitNexus incremental indexing exits non-zero, do not treat the stale index as evidence. Record the failing command and output, then recover with:

```powershell
gitnexus analyze -f --index-only --name Labrastro-capability-package-ecosystem .
gitnexus analyze -f --index-only --name Labrastro-vscode-extension-capability-package-ecosystem .
```

Only use `rg` as a documented supplement to GitNexus. A row cannot be closed by grep alone.

### 0.2 Compression-Safe Active Goal

Complete the reopened Labrastro capability-package ecosystem drift-convergence pass by making every install/update/runtime/frontend invariant structural in code, tests, matrix evidence, and GitNexus audit output.

The goal is not to keep patching individual review comments. The goal is to eliminate the class of drift where documented architecture says one thing but runtime/admin/frontend paths make local decisions. Every remaining task must preserve these boundaries:

- Capability package lifecycle is split into registered, prepared/materialized, installed, and activated states.
- Environment state is split by server and local peer target facts; frontend must not derive both sides from one top-level runtime/check field.
- Capability activation is decided by package state projection and owner activation, not raw `package.enabled`.
- Package-managed skills keep server-managed `SKILL.md` plus controlled file closures; dependencies install on the required runtime side and may be shared or isolated according to policy.
- Config writes, reloads, and SKILL file writes/deletes use one transaction policy with rollback evidence.
- Upstream updates follow GitHub/repository version facts; Labrastro does not mint a parallel user-facing version.
- LLM output remains advisory. It may propose drafts, unmapped findings, and install-plan candidates, but backend typed normalizers decide accepted enums, ids, manifests, credentials, and target facts.
- Installing a package is not activation. Once a package is activated, its hooks are on by default, still gated by owner activation, trust, credentials, and runtime placement.
- Credential rules are multi-tenant: user, workspace, and server-global bindings are resolved consistently, audited by actor, and never expose secret values.
- Conflict and dependency handling uses shared registry/isolation policy. Shared system capabilities are explicit registry facts, not guessed by UI copy or route-local heuristics.
- User-facing UI must show controllable states and concrete required actions. It must not show open-ended "waiting for developer support" states.
- GitNexus is mandatory evidence for every row: baseline impact, targeted symbol/process query, post-edit refresh, final detect-changes, and grep/test corroboration.

Follow-up blockers and closure status from the 2026-06-11 GitNexus audit:

1. C-15 closed the activation projection breach: `apply_update_candidate` and `rollback_update_candidate` now use `capability_package_is_active(current)` instead of `current.get("enabled")`.
2. C-16 closed rollback availability drift: `rollback_capability_package_update` now calls the shared `rollback_update_available` guard before rollback, so empty or consumed rollback metadata returns `rollback_not_available`.
3. C-17 closed frontend state payload drift: `CapabilitiesTab` now builds its state payload through `capabilityPackageStatePayload(item)`, which merges nested `state` with package-level `credential_state`, `target_facts`, and aliases.
4. C-18 replaced the invalidated C-14/final-audit claim with fresh GitNexus, grep, focused test, affected test, typecheck, and matrix evidence.

## 1. File Structure

### Server Repo

Create these focused modules:

```text
reuleauxcoder/domain/capability_packages.py
labrastro_server/services/capability_package_normalizer.py
labrastro_server/services/capability_package_artifacts.py
labrastro_server/services/capability_package_dependencies.py
labrastro_server/services/capability_package_install_plan.py
labrastro_server/services/capability_package_executor.py
labrastro_server/services/capability_package_credentials.py
labrastro_server/services/capability_package_updates.py
```

Modify these existing files:

```text
labrastro_server/services/capability_package_ingest.py
labrastro_server/services/capability_packages.py
labrastro_server/services/admin/service.py
labrastro_server/interfaces/http/remote/service.py
labrastro_server/interfaces/http/remote/routes/admin.py
reuleauxcoder/domain/agent_runtime/models.py
reuleauxcoder/domain/config/models.py
reuleauxcoder/domain/environment_requirements.py
```

Add or modify these server tests:

```text
tests/domain/test_capability_package_domain.py
tests/labrastro_server/services/test_capability_package_normalizer.py
tests/labrastro_server/services/test_capability_package_artifacts.py
tests/labrastro_server/services/test_capability_package_dependencies.py
tests/labrastro_server/services/test_capability_package_install_plan.py
tests/labrastro_server/services/test_capability_package_executor.py
tests/labrastro_server/services/test_capability_package_credentials.py
tests/labrastro_server/services/test_capability_package_updates.py
tests/labrastro_server/services/test_capability_packages.py
tests/labrastro_server/services/test_capability_package_ingest_fields.py
tests/labrastro_server/http/test_remote_service.py
tests/domain/test_config_models.py
tests/domain/hooks/test_lifecycle.py
```

### VS Code Extension Repo

Modify these files:

```text
src/protocol/messages.ts
src/LabrastroRemoteClient.ts
src/LabrastroController.ts
webview-ui/src/settings/capabilityPackageView.ts
webview-ui/src/settings/tabs/CapabilitiesTab.tsx
webview-ui/src/components/chat/SessionTurn.tsx
webview-ui/src/i18n/zh-CN.ts
```

Add or modify these extension tests:

```text
src/LabrastroRemoteClient.test.ts
src/LabrastroController.admin.test.ts
webview-ui/src/settings/capabilityPackageView.test.ts
webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx
webview-ui/src/components/chat/SessionTurn.test.ts
```

## 2. Stage Map

| Stage | Matrix rows | Primary output |
| --- | --- | --- |
| 1 | B-01, B-11, T-12 | State axes and legacy config projection. |
| 2 | B-02, B-03, B-04, T-01 | Snapshot, normalized manifest, advisory/open findings split. |
| 3 | B-06, T-02, T-09 | Full skill bundle and controlled file closure. |
| 4 | B-05, B-10, T-03, T-10 | Dependency graph, shared registry, conflict isolation. |
| 5 | B-07, S-01, S-02, S-05 | Typed install plans and server executor result records. |
| 6 | P-01, P-02, P-03, P-04, P-06, T-08 | Local peer install protocol and peer-owned facts. |
| 7 | B-08, P-05, T-06 | Multi-tenant credential requirements and bindings. |
| 8 | S-03, F-02, T-04, T-05 | Activation, hooks, MCP runtime truth. |
| 9 | B-09, F-05, T-07 | Upstream update candidate and manifest diff. |
| 10 | F-01, F-03, F-04, F-06, F-07, T-11 | Frontend package management surfaces and user-facing language. |
| 11 | G-01 through G-12 | Waza regression, migration closure, GitNexus impact evidence. |

## 3. Implementation Tasks

### Task 1: Add State Axes and Legacy Projection

**Matrix rows:** B-01, B-11, T-04, T-12

**Files:**
- Create: `reuleauxcoder/domain/capability_packages.py`
- Modify: `reuleauxcoder/domain/agent_runtime/models.py`
- Test: `tests/domain/test_capability_package_domain.py`
- Test: `tests/domain/test_config_models.py`

- [x] **Step 1: Write state enum tests**

Create `tests/domain/test_capability_package_domain.py` with assertions for the exact state axes:

```python
from reuleauxcoder.domain.capability_packages import (
    ACTIVATION_STATES,
    CHECK_STATES,
    CREDENTIAL_STATES,
    INSTALL_STATES,
    MAPPING_STATES,
    RUNTIME_STATES,
    UPDATE_STATES,
    capability_package_state_projection,
)


def test_state_axes_are_authoritative() -> None:
    assert INSTALL_STATES == {
        "not_installed",
        "registered",
        "materialized",
        "installed",
        "blocked",
        "failed",
    }
    assert ACTIVATION_STATES == {"inactive", "active", "degraded", "blocked"}
    assert RUNTIME_STATES == {
        "not_applicable",
        "stopped",
        "starting",
        "running",
        "connected",
        "failed",
    }
    assert CHECK_STATES == {"unknown", "pending", "passed", "missing", "failed", "stale"}
    assert CREDENTIAL_STATES == {"not_required", "missing", "bound", "verified", "failed"}
    assert UPDATE_STATES == {
        "not_checked",
        "current",
        "update_available",
        "candidate_ready",
        "updating",
        "rollback_available",
        "failed",
    }
    assert MAPPING_STATES == {"mapped", "unmapped", "mapping_required", "invalid"}


def test_legacy_enabled_status_projects_to_state_axes() -> None:
    projection = capability_package_state_projection(
        {
            "enabled": False,
            "status": "installed",
        }
    )
    assert projection["install_state"] == "installed"
    assert projection["activation_state"] == "inactive"
    assert projection["runtime_state"] == "not_applicable"
    assert projection["check_state"] == "unknown"
```

- [x] **Step 2: Run the new test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL with `ModuleNotFoundError: No module named 'reuleauxcoder.domain.capability_packages'`.

- [x] **Step 3: Implement domain constants and projection**

Create `reuleauxcoder/domain/capability_packages.py` with:

```python
"""Capability package domain state and manifest helpers."""

from __future__ import annotations

from typing import Any

INSTALL_STATES = {
    "not_installed",
    "registered",
    "materialized",
    "installed",
    "blocked",
    "failed",
}
ACTIVATION_STATES = {"inactive", "active", "degraded", "blocked"}
RUNTIME_STATES = {"not_applicable", "stopped", "starting", "running", "connected", "failed"}
CHECK_STATES = {"unknown", "pending", "passed", "missing", "failed", "stale"}
CREDENTIAL_STATES = {"not_required", "missing", "bound", "verified", "failed"}
UPDATE_STATES = {
    "not_checked",
    "current",
    "update_available",
    "candidate_ready",
    "updating",
    "rollback_available",
    "failed",
}
MAPPING_STATES = {"mapped", "unmapped", "mapping_required", "invalid"}


def capability_package_state_projection(raw: dict[str, Any]) -> dict[str, str]:
    status = str(raw.get("status") or "").strip().lower()
    enabled = raw.get("enabled") is not False
    install_state = status if status in INSTALL_STATES else "installed" if status else "registered"
    if status in {"failed", "blocked"}:
        install_state = status
    return {
        "install_state": install_state,
        "activation_state": "active" if enabled and install_state == "installed" else "inactive",
        "runtime_state": "not_applicable",
        "check_state": "unknown",
        "credential_state": "not_required",
        "update_state": "not_checked",
        "mapping_state": "mapped",
    }
```

- [x] **Step 4: Preserve compatibility in `CapabilityPackageConfig`**

Modify `reuleauxcoder/domain/agent_runtime/models.py` so `CapabilityPackageConfig.to_dict()` includes a `state` object generated by `capability_package_state_projection(...)` while preserving existing `enabled` and `status` fields.

Expected shape:

```python
result["state"] = capability_package_state_projection(result)
```

- [x] **Step 5: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py tests\domain\test_config_models.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

### Task 2: Add Source Snapshot and Normalized Manifest Models

**Matrix rows:** B-02, B-03

**Files:**
- Modify: `reuleauxcoder/domain/capability_packages.py`
- Create: `tests/domain/test_capability_package_domain.py` additional tests

- [x] **Step 1: Add tests for snapshot and manifest roundtrip**

Append tests:

```python
from reuleauxcoder.domain.capability_packages import (
    CapabilityManifest,
    CapabilitySourceSnapshot,
)


def test_source_snapshot_uses_upstream_version_for_display() -> None:
    snapshot = CapabilitySourceSnapshot.from_dict(
        {
            "package_id": "waza",
            "source_type": "github_repo",
            "source_url": "https://github.com/tw93/Waza",
            "source_ref": "main",
            "commit_sha": "abc1234",
            "upstream_version": "",
            "snapshot_id": "snap-1",
            "snapshot_path": "capability-packages/waza/main-abc1234/source",
            "content_hash": "sha256:1",
        }
    )
    assert snapshot.display_version == "main@abc1234"


def test_manifest_keeps_unmapped_findings_out_of_components() -> None:
    manifest = CapabilityManifest.from_dict(
        {
            "package": {"id": "waza"},
            "components": [{"id": "skill:waza/read", "type": "skill"}],
            "unmapped_findings": {
                "unclassified_requirements": [
                    {"observed": "pip install --user readability-lxml"}
                ]
            },
        }
    )
    assert manifest.components[0]["id"] == "skill:waza/read"
    assert manifest.unmapped_findings["unclassified_requirements"][0]["observed"].startswith("pip install")
```

- [x] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because `CapabilitySourceSnapshot` and `CapabilityManifest` are not defined.

- [x] **Step 3: Implement dataclasses**

Add dataclasses to `reuleauxcoder/domain/capability_packages.py`:

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CapabilitySourceSnapshot:
    package_id: str
    source_type: str
    source_url: str
    source_ref: str
    commit_sha: str
    upstream_version: str
    snapshot_id: str
    snapshot_path: str
    content_hash: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def display_version(self) -> str:
        if self.upstream_version:
            return self.upstream_version
        if self.source_ref and self.commit_sha:
            return f"{self.source_ref}@{self.commit_sha[:7]}"
        return self.commit_sha[:7] or "unversioned"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilitySourceSnapshot":
        return cls(
            package_id=str(value.get("package_id") or ""),
            source_type=str(value.get("source_type") or ""),
            source_url=str(value.get("source_url") or ""),
            source_ref=str(value.get("source_ref") or ""),
            commit_sha=str(value.get("commit_sha") or ""),
            upstream_version=str(value.get("upstream_version") or ""),
            snapshot_id=str(value.get("snapshot_id") or ""),
            snapshot_path=str(value.get("snapshot_path") or ""),
            content_hash=str(value.get("content_hash") or ""),
            provenance=dict(value.get("provenance") or {}),
        )


@dataclass(frozen=True)
class CapabilityManifest:
    package: dict[str, Any] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)
    dependency_edges: list[dict[str, Any]] = field(default_factory=list)
    environment_requirements: list[dict[str, Any]] = field(default_factory=list)
    credential_requirements: list[dict[str, Any]] = field(default_factory=list)
    install_plans: list[dict[str, Any]] = field(default_factory=list)
    activation_rules: dict[str, Any] = field(default_factory=dict)
    update_metadata: dict[str, Any] = field(default_factory=dict)
    unmapped_findings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    exposed_file_closures: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityManifest":
        return cls(
            package=dict(value.get("package") or {}),
            components=[dict(item) for item in value.get("components") or [] if isinstance(item, dict)],
            dependency_edges=[dict(item) for item in value.get("dependency_edges") or [] if isinstance(item, dict)],
            environment_requirements=[dict(item) for item in value.get("environment_requirements") or [] if isinstance(item, dict)],
            credential_requirements=[dict(item) for item in value.get("credential_requirements") or [] if isinstance(item, dict)],
            install_plans=[dict(item) for item in value.get("install_plans") or [] if isinstance(item, dict)],
            activation_rules=dict(value.get("activation_rules") or {}),
            update_metadata=dict(value.get("update_metadata") or {}),
            unmapped_findings={
                str(key): [dict(item) for item in items if isinstance(item, dict)]
                for key, items in dict(value.get("unmapped_findings") or {}).items()
                if isinstance(items, list)
            },
            exposed_file_closures=[dict(item) for item in value.get("exposed_file_closures") or [] if isinstance(item, dict)],
        )
```

- [x] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

### Task 3: Split LLM Advisory Output From Backend Manifest

**Matrix rows:** B-04, G-09, T-01

**Files:**
- Create: `labrastro_server/services/capability_package_normalizer.py`
- Modify: `labrastro_server/services/capability_package_ingest.py`
- Test: `tests/labrastro_server/services/test_capability_package_normalizer.py`
- Test: `tests/labrastro_server/services/test_capability_package_ingest_fields.py`

- [x] **Step 1: Write Waza-style regression test**

Create `tests/labrastro_server/services/test_capability_package_normalizer.py`:

```python
from labrastro_server.services.capability_package_normalizer import normalize_capability_manifest_candidate


def test_python_package_requirement_becomes_unmapped_runtime_finding() -> None:
    result = normalize_capability_manifest_candidate(
        {
            "package": {"id": "waza"},
            "components": [
                {
                    "id": "skill:waza/read",
                    "kind": "skill",
                    "name": "read",
                    "environment_requirement_refs": ["envreq:python-pkg:readability-lxml"],
                },
                {
                    "id": "envreq:python-pkg:readability-lxml",
                    "kind": "python_package",
                    "name": "readability-lxml",
                    "install": "pip install --user readability-lxml html2text",
                },
            ],
        }
    )
    assert [item["id"] for item in result.components] == ["skill:waza/read"]
    assert result.unmapped_findings["unclassified_requirements"][0]["observed"] == "pip install --user readability-lxml html2text"
    assert result.unmapped_findings["unclassified_requirements"][0]["suggested_kind"] == "python_package"
```

- [x] **Step 2: Run test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because the normalizer module does not exist.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_ingest_fields.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, `ModuleNotFoundError: No module named 'labrastro_server.services.capability_package_normalizer'`.

- [x] **Step 3: Implement normalizer boundary**

Implement `normalize_capability_manifest_candidate(...)` so it:

```text
1. Accepts LLM advisory candidate dict.
2. Keeps only backend-authoritative component types in components.
3. Converts unknown component kinds and unknown envreq id kinds into unmapped_findings.
4. Preserves source evidence fields in unmapped_findings.
5. Returns CapabilityManifest.
```

Use `ENVIRONMENT_REQUIREMENT_KINDS` from `reuleauxcoder.domain.environment_requirements` and component kinds from `reuleauxcoder.domain.agent_runtime.models`.

- [x] **Step 4: Extend ingest field patch support**

Modify `labrastro_server/services/capability_package_ingest.py` so `_is_draft_field_path(...)` accepts:

```text
manifest_candidate
open_findings
target_placement_proposals
exposed_path_candidates
```

Add a test to `tests/labrastro_server/services/test_capability_package_ingest_fields.py` proving these fields are retained in `field_state`.

- [x] **Step 5: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_ingest_fields.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_ingest_fields.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `13 passed in 0.90s`; changed files: `labrastro_server/services/capability_package_normalizer.py`, `labrastro_server/services/capability_package_ingest.py`, `tests/labrastro_server/services/test_capability_package_normalizer.py`, `tests/labrastro_server/services/test_capability_package_ingest_fields.py`.

### Task 4: Implement Full Skill Artifact Bundles and Exposed File Closures

**Matrix rows:** B-02, B-06, T-02, T-09

**Files:**
- Create: `labrastro_server/services/capability_package_artifacts.py`
- Modify: `labrastro_server/services/capability_packages.py`
- Test: `tests/labrastro_server/services/test_capability_package_artifacts.py`
- Test: `tests/labrastro_server/services/test_capability_packages.py`

- [x] **Step 1: Write multi-skill fixture test**

Create `tests/labrastro_server/services/test_capability_package_artifacts.py` with a temp repo-like tree:

```python
from pathlib import Path

from labrastro_server.services.capability_package_artifacts import build_skill_file_closure


def test_skill_file_closure_preserves_nested_and_shared_paths(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "skill-a" / "references").mkdir(parents=True)
    (root / "skill-b" / "scripts").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "skill-a" / "SKILL.md").write_text("Read [ref](references/a.md) and ../shared/rules.md\n", encoding="utf-8")
    (root / "skill-a" / "references" / "a.md").write_text("A\n", encoding="utf-8")
    (root / "skill-b" / "SKILL.md").write_text("Run scripts/b.ps1\n", encoding="utf-8")
    (root / "skill-b" / "scripts" / "b.ps1").write_text("Write-Output b\n", encoding="utf-8")
    (root / "shared" / "rules.md").write_text("shared\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    closure = build_skill_file_closure(
        package_root=root,
        entry_path=root / "skill-a" / "SKILL.md",
        explicit_paths=["shared/rules.md"],
    )

    assert "skill-a/SKILL.md" in closure.included_paths
    assert "skill-a/references/a.md" in closure.included_paths
    assert "shared/rules.md" in closure.included_paths
    assert ".env" in closure.denied_paths
```

- [x] **Step 2: Run and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_artifacts.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because `capability_package_artifacts.py` does not exist.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_artifacts.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, `ModuleNotFoundError: No module named 'labrastro_server.services.capability_package_artifacts'`.

- [x] **Step 3: Implement closure builder**

Implement `build_skill_file_closure(...)` with deterministic include/deny rules from the matrix:

```text
include:
  entry SKILL.md
  entry skill directory files
  explicitly referenced relative paths
  allowlisted package dirs: rules, references, docs, assets
deny:
  .git
  .github
  node_modules
  dist
  build
  coverage
  cache
  .env
  paths containing secret, token, key
```

The return object must include:

```text
included_paths
denied_paths
entry_path
package_root
```

- [x] **Step 4: Adapt skill materialization**

Modify `CapabilityPackageInstaller._materialized_skill_payload(...)` in `labrastro_server/services/capability_packages.py` only as a compatibility bridge. It should continue to support current single-content drafts while new manifest-based installs use the artifact bundle service.

- [x] **Step 5: Run skill tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_packages.py::test_package_installer_materializes_skill_to_canonical_server_path tests\labrastro_server\services\test_capability_packages.py::test_package_installer_keeps_shared_skill_path_stable_when_owner_changes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_packages.py::test_package_installer_materializes_skill_to_canonical_server_path tests\labrastro_server\services\test_capability_packages.py::test_package_installer_keeps_shared_skill_path_stable_when_owner_changes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `4 passed in 2.91s`; closure tests cover nested `SKILL.md`, root `SKILL.md`, shared files, allowlisted roots, and denied sensitive/dependency paths.

### Task 5: Add Dependency Graph and Shared Capability Registry

**Matrix rows:** B-05, B-10, T-03, T-10

**Files:**
- Create: `labrastro_server/services/capability_package_dependencies.py`
- Modify: `reuleauxcoder/domain/capability_packages.py`
- Test: `tests/labrastro_server/services/test_capability_package_dependencies.py`

- [x] **Step 1: Write dependency graph tests**

Create tests:

```python
from labrastro_server.services.capability_package_dependencies import (
    build_dependency_graph,
    default_shared_capability_registry,
)


def test_shared_gh_requirement_is_single_registry_backed_node() -> None:
    registry = default_shared_capability_registry()
    graph = build_dependency_graph(
        components=[
            {"id": "skill:pkg-a/review", "environment_requirement_refs": ["envreq:executable:gh"]},
            {"id": "skill:pkg-b/issues", "environment_requirement_refs": ["envreq:executable:gh"]},
        ],
        requirements=[
            {"id": "envreq:executable:gh", "kind": "executable", "name": "gh"},
        ],
        registry=registry,
    )
    assert graph.requirements["envreq:executable:gh"]["shared_registry_id"] == "shared:executable:gh"
    assert sorted(edge["from_component_id"] for edge in graph.edges) == [
        "skill:pkg-a/review",
        "skill:pkg-b/issues",
    ]


def test_invalid_dependency_edge_blocks_only_dependent_component() -> None:
    graph = build_dependency_graph(
        components=[{"id": "skill:pkg/read", "environment_requirement_refs": ["envreq:unknown:thing"]}],
        requirements=[],
        registry=default_shared_capability_registry(),
    )
    assert graph.edges[0]["status"] == "invalid"
    assert graph.blocked_component_ids == ["skill:pkg/read"]
```

- [x] **Step 2: Run and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because module does not exist.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, `ModuleNotFoundError: No module named 'labrastro_server.services.capability_package_dependencies'`.

- [x] **Step 3: Implement registry and graph builder**

The initial registry must include at least:

```text
shared:executable:git
shared:executable:gh
shared:executable:bash
shared:executable:sh
shared:executable:python3
shared:executable:node
shared:executable:npm
shared:executable:pnpm
shared:executable:yarn
shared:executable:docker
shared:executable:jq
shared:executable:curl
shared:executable:wget
shared:executable:rg
```

Each registry entry must include `version_check_action`, `install_action_policy`, `platforms`, `credential_interaction`, `conflict_policy`, and `evidence_required`.

- [x] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `3 passed in 0.86s`. Additional guard: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `9 passed in 0.84s`.

### Task 6: Add Typed InstallPlan and Server Executor Result Records

**Matrix rows:** B-07, S-01, S-02, S-05

**Files:**
- Create: `labrastro_server/services/capability_package_install_plan.py`
- Create: `labrastro_server/services/capability_package_executor.py`
- Test: `tests/labrastro_server/services/test_capability_package_install_plan.py`
- Test: `tests/labrastro_server/services/test_capability_package_executor.py`

- [x] **Step 1: Write action catalog tests**

Create `tests/labrastro_server/services/test_capability_package_install_plan.py`:

```python
from labrastro_server.services.capability_package_install_plan import (
    INSTALL_ACTION_TYPES,
    InstallAction,
    InstallPlan,
)


def test_install_action_catalog_is_typed() -> None:
    assert "install_python_packages" in INSTALL_ACTION_TYPES
    action = InstallAction.from_dict(
        {
            "id": "act-1",
            "type": "install_python_packages",
            "target": "server",
            "params": {"packages": ["readability-lxml"], "venv": "venvs/waza"},
        }
    )
    assert action.type == "install_python_packages"


def test_unknown_install_action_is_rejected() -> None:
    try:
        InstallAction.from_dict({"id": "act-1", "type": "shell", "target": "server"})
    except ValueError as exc:
        assert "unknown install action type" in str(exc)
    else:
        raise AssertionError("unknown shell action should be rejected")
```

- [x] **Step 2: Write executor safety tests**

Create `tests/labrastro_server/services/test_capability_package_executor.py`:

```python
from labrastro_server.services.capability_package_executor import CapabilityPackageServerExecutor
from labrastro_server.services.capability_package_install_plan import InstallAction


def test_executor_records_check_executable_result_without_marking_peer_verified(tmp_path) -> None:
    executor = CapabilityPackageServerExecutor(runtime_root=tmp_path)
    result = executor.execute_action(
        InstallAction.from_dict(
            {
                "id": "check-gh",
                "type": "check_executable",
                "target": "server",
                "params": {"executable": "gh"},
            }
        )
    )
    assert result.action_id == "check-gh"
    assert result.target == "server"
    assert result.status in {"passed", "missing", "failed"}


def test_python_packages_install_to_package_local_venv_path(tmp_path) -> None:
    executor = CapabilityPackageServerExecutor(runtime_root=tmp_path)
    result = executor.plan_runtime_path(
        package_id="waza",
        action_type="install_python_packages",
    )
    assert str(result).startswith(str(tmp_path))
    assert "waza" in str(result)
```

- [x] **Step 3: Run and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because modules do not exist.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, `ModuleNotFoundError` for `capability_package_install_plan` and `capability_package_executor`.

- [x] **Step 4: Implement InstallPlan and executor skeleton**

Implement:

```text
InstallAction
InstallPlan
InstallActionResult
CapabilityPackageServerExecutor.execute_action
CapabilityPackageServerExecutor.plan_runtime_path
```

Executor methods may start with safe checks and path planning only. Do not implement network package installation until typed parameters, path isolation, and result recording are passing.

- [x] **Step 5: Run focused tests**

Run the same pytest command from Step 3.

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `5 passed in 0.10s`; server executor currently records safe checks and isolated runtime paths only, with no network package installation.

### Task 7: Refactor Accept/Install/Activation API Boundaries

**Matrix rows:** B-11, S-04, F-02, T-04, G-10

**Files:**
- Modify: `labrastro_server/services/admin/service.py`
- Modify: `labrastro_server/interfaces/http/remote/routes/admin.py`
- Modify: `labrastro_server/interfaces/http/remote/service.py`
- Test: `tests/labrastro_server/services/test_capability_packages.py`
- Test: `tests/labrastro_server/http/test_remote_service.py`

- [x] **Step 1: Write service tests for install vs activation**

Add a test proving accepted install remains inactive until activation:

```python
def test_accept_capability_package_installs_without_activation(tmp_path) -> None:
    manager = _admin_manager_with_temp_config(tmp_path)
    result = manager.accept_capability_package_draft(
        {
            "package_id": "review",
            "draft": {
                "id": "review",
                "name": "Review",
                "components": [
                    {
                        "kind": "skill",
                        "name": "code-review",
                        "skill_content": "---\nname: code-review\n---\nReview.\n",
                    }
                ],
                "evidence": [{"title": "fixture", "excerpt": "review"}],
                "risk_level": "low",
            },
        }
    )
    assert result.ok is True
    package = result.payload["capability_package"]
    assert package["state"]["install_state"] in {"materialized", "installed"}
    assert package["state"]["activation_state"] == "inactive"
```

Use the existing test helper pattern in `tests/labrastro_server/services/test_capability_packages.py`; if there is no `_admin_manager_with_temp_config`, add a local helper in that test file.

- [x] **Step 2: Write route/projection test**

Add an HTTP-level test in `tests/labrastro_server/http/test_remote_service.py` proving package view contains separate state axes.

- [x] **Step 3: Run focused tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_packages.py::test_accept_capability_package_installs_without_activation tests\labrastro_server\http\test_remote_service.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because current accept path returns `enabled=True` and `status=installed`.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_admin_service.py::test_accept_capability_package_installs_without_activation tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_accept_returns_separate_state_axes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, both tests hit missing `capability_package.state`.

- [x] **Step 4: Implement compatibility state projection**

Modify `RemoteAdminConfigManager.accept_capability_package_draft(...)` so install acceptance:

```text
1. records installed/materialized state,
2. keeps activation inactive by default,
3. returns state axes in response payload,
4. does not auto-add package refs to agents.
```

Keep `enable_capability_package(...)` as a compatibility wrapper, but route it to activation state semantics internally.

- [x] **Step 5: Run tests**

Run the same focused tests plus:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\services\config\test_agent_runtime_config_loader.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_admin_service.py::test_accept_capability_package_installs_without_activation tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_accept_returns_separate_state_axes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `2 passed in 4.50s`. Additional guards: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\services\config\test_agent_runtime_config_loader.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `31 passed in 1.64s`; `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\test_config_models.py::test_capability_package_config_roundtrip_preserves_runtime_footprint tests\domain\test_config_models.py::test_capability_component_and_package_config_roundtrip_preserve_lifecycle_hooks --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `2 passed in 0.77s`. Service test was added to `tests/labrastro_server/services/test_admin_service.py`, the existing admin manager test home, rather than `test_capability_packages.py`.

### Task 8: Add Local Peer Install Protocol and Peer-Owned Facts

**Matrix rows:** P-01, P-02, P-03, P-04, P-06, T-08

**Files:**
- Server modify: `labrastro_server/interfaces/http/remote/service.py`
- Server modify: `labrastro_server/interfaces/http/remote/protocol/__init__.py`
- Extension modify: `src/protocol/messages.ts`
- Extension modify: `src/LabrastroRemoteClient.ts`
- Extension no-op: `src/LabrastroController.ts` remains unchanged until Task 12 UI projection; Task 8 only adds peer HTTP client/protocol guard.
- Server test: `tests/labrastro_server/http/test_remote_service.py`
- Extension test: `src/LabrastroRemoteClient.test.ts`

- [x] **Step 1: Add protocol contract tests**

Server test must prove:

```text
server desired install target local_peer
-> response shows peer check_state unknown/pending
-> server does not mark local_peer installed until peer result arrives
```

Extension test must prove `LabrastroRemoteClient` can send and receive:

```text
capability_package_install_plan
capability_package_install_result
target: local_peer
```

- [x] **Step 2: Run expected failing tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Then in the extension repo:

```powershell
npx vitest run src/LabrastroRemoteClient.test.ts
```

Expected: FAIL because the install-plan peer protocol is not implemented.

Evidence: server RED command `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_install_plan_waits_for_peer_result --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` failed before implementation because `/remote/capability-packages/install/plan` was not routed. Extension RED command initially reached test bootstrap failure because the worktree lacked `node_modules` (`Cannot find module 'vitest/config'`); after `npm ci`, protocol tests ran and exposed only an existing worktree fixture-path assumption before helper compatibility was added.

- [x] **Step 3: Implement protocol message shapes**

Use these wire names:

```text
capabilityPackage.installPlan
capabilityPackage.installResult
capabilityPackage.peerStatus
```

Each result record must include:

```text
plan_id
action_id
package_id
component_id
target
status
version
content_hash
message
timestamp
```

- [x] **Step 4: Implement peer result aggregation**

Server aggregation must:

```text
1. store desired state separately from peer result,
2. mark missing peer result as unknown or pending,
3. mark stale peer result as stale,
4. never infer peer installed from server config.
```

- [x] **Step 5: Run protocol tests**

Run the server and extension commands from Step 2.

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_install_plan_waits_for_peer_result --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `1 passed in 4.19s`. Extension evidence: `npm ci` completed in the extension worktree, then `npx vitest run src/LabrastroRemoteClient.test.ts src/protocol/messages.test.ts` -> PASS, `2 passed`, `52 passed` in `4.10s`. Changed files: server `labrastro_server/interfaces/http/remote/protocol/capability_packages.py`, `protocol/__init__.py`, `protocol/registry.py`, `routes/capability_packages.py`, `routes/__init__.py`, `service.py`, `tests/labrastro_server/http/test_remote_service.py`; extension `src/LabrastroRemoteClient.ts`, `src/LabrastroRemoteClient.test.ts`, `src/protocol/messages.ts`, `src/protocol/messages.test.ts`.

### Task 9: Add Credential Requirements and Multi-Tenant Bindings

**Matrix rows:** B-08, P-05, T-06

**Files:**
- Create: `labrastro_server/services/capability_package_credentials.py`
- Modify: `reuleauxcoder/domain/capability_packages.py`
- Modify: `labrastro_server/services/admin/service.py`
- Modify extension: `src/LabrastroRemoteClient.ts`
- Modify extension: `webview-ui/src/settings/capabilityPackageView.ts`
- Test: `tests/labrastro_server/services/test_capability_package_credentials.py`
- Test extension: `webview-ui/src/settings/capabilityPackageView.test.ts`

- [x] **Step 1: Write credential resolution tests**

Create server tests:

```python
from labrastro_server.services.capability_package_credentials import resolve_credential_binding


def test_user_binding_wins_over_workspace_and_global() -> None:
    binding = resolve_credential_binding(
        requirement_id="credreq:github",
        user_id="user-a",
        workspace_id="workspace-1",
        bindings=[
            {"scope": "server_global", "requirement_id": "credreq:github", "secret_ref_id": "global"},
            {"scope": "workspace", "workspace_id": "workspace-1", "requirement_id": "credreq:github", "secret_ref_id": "workspace"},
            {"scope": "user", "user_id": "user-a", "requirement_id": "credreq:github", "secret_ref_id": "user"},
        ],
    )
    assert binding["secret_ref_id"] == "user"


def test_secret_value_is_rejected_in_public_payload() -> None:
    try:
        resolve_credential_binding(
            requirement_id="credreq:github",
            user_id="user-a",
            workspace_id="workspace-1",
            bindings=[{"scope": "user", "secret_value": "ghp_secret"}],
        )
    except ValueError as exc:
        assert "secret values must not enter capability package payloads" in str(exc)
    else:
        raise AssertionError("plaintext secret should be rejected")
```

- [x] **Step 2: Run and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_credentials.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because credential service does not exist.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_credentials.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, `ModuleNotFoundError: No module named 'labrastro_server.services.capability_package_credentials'`.

- [x] **Step 3: Implement credential service**

Implement:

```text
credential requirement model
binding scopes: user, workspace, server_global
resolution order: user -> workspace -> server_global
credential_actor: user_delegated | service_account
audit payload without secret values
```

- [x] **Step 4: Add frontend projection tests**

Add `capabilityPackageView.test.ts` coverage that displays:

```text
默认使用当前用户凭据
工作区共享凭据
服务端全局凭据
```

and does not include any secret value.

- [x] **Step 5: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_credentials.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Then in extension repo:

```powershell
npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_admin_service.py::test_server_settings_projects_capability_package_credential_bindings tests\labrastro_server\services\test_admin_service.py::test_accept_capability_package_preserves_credential_requirements --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `9 passed in 1.31s`; `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/useSettingsController.test.tsx` -> PASS, `2 passed`, `56 passed` in `1.22s`. Credential service now resolves `user -> workspace -> server_global`, rejects plaintext secret fields in capability payloads, requires admin authorization for global bindings, preserves `credential_requirements` through accept/install, and frontend projection renders `默认使用当前用户凭据`, `工作区共享凭据`, `服务端全局凭据` without retaining secret values.

### Task 10: Implement Activation, Hook Following, and MCP Runtime Truth

**Matrix rows:** S-03, F-02, T-04, T-05

**Files:**
- Modify: `labrastro_server/services/admin/service.py`
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Modify: `reuleauxcoder/domain/config/models.py`
- Test: `tests/domain/hooks/test_lifecycle.py`
- Test: `tests/labrastro_server/services/test_capability_packages.py`

- [x] **Step 1: Add hook activation tests**

Add tests proving:

```text
installed package with hooks + activation_state inactive
-> hooks visible as package contains hooks
-> hooks not active in runtime

same package activated
-> valid hooks active through parent package/component
```

Use existing lifecycle hook config helpers in `tests/domain/hooks/test_lifecycle.py`.

- [x] **Step 2: Add MCP runtime truth test**

Add a test proving:

```text
activation_state active
runtime_state failed
```

is a valid displayable state and is not collapsed into activation failure.

- [x] **Step 3: Run failing tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\domain\hooks\test_lifecycle.py tests\labrastro_server\services\test_capability_packages.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL until activation state gates hook runtime projection.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_capability_package_hooks_by_activation_state tests\labrastro_server\services\test_capability_packages.py::test_capability_package_state_keeps_activation_active_when_mcp_runtime_failed --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation: hook dashboard lacked `owner_activation_state`, and explicit `runtime_state=failed` was collapsed to `not_applicable`.

- [x] **Step 4: Implement activation projection**

Implement activation projection so:

```text
install_state controls whether activation can be requested
activation_state controls whether valid hooks/MCP resources enter runtime desired state
runtime_state reports actual process/connectivity state
invalid hooks remain blockers and never enter activatable manifest
```

- [x] **Step 5: Run tests**

Run the same pytest command from Step 3.

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\hooks\test_lifecycle.py tests\labrastro_server\services\test_capability_packages.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `264 passed in 5.98s`. Lifecycle declarations and dashboard items now include `owner_activation_state`; inactive package hooks remain visible but not executable with `unavailable_reason=owner_activation:inactive`; active package hooks can execute when runtime adapters exist; explicit package `state.runtime_state=failed` is preserved alongside `activation_state=active`.

### Task 11: Add Upstream Update Candidate Flow

**Matrix rows:** B-09, F-05, T-07

**Files:**
- Create: `labrastro_server/services/capability_package_updates.py`
- Modify: `labrastro_server/services/admin/service.py`
- Modify: `labrastro_server/interfaces/http/remote/routes/admin.py`
- Test: `tests/labrastro_server/services/test_capability_package_updates.py`
- Test: `tests/labrastro_server/http/test_remote_service.py`

- [x] **Step 1: Write update candidate tests**

Create tests:

```python
from labrastro_server.services.capability_package_updates import (
    detect_upstream_version,
    manifest_diff,
)


def test_main_branch_version_displays_as_main_at_commit() -> None:
    assert detect_upstream_version({"source_ref": "main", "commit_sha": "abcdef123"}) == "main@abcdef1"


def test_manifest_diff_is_backend_computed() -> None:
    diff = manifest_diff(
        {"components": [{"id": "skill:waza/read"}]},
        {"components": [{"id": "skill:waza/read"}, {"id": "skill:waza/write"}]},
    )
    assert diff["added_components"] == ["skill:waza/write"]
    assert diff["removed_components"] == []
```

- [x] **Step 2: Run and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: FAIL because update module does not exist.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> FAIL before implementation, `ModuleNotFoundError: No module named 'labrastro_server.services.capability_package_updates'`.

- [x] **Step 3: Implement update candidate service**

Implement:

```text
upstream version detection
candidate snapshot metadata
backend-computed manifest diff
no auto-activation
rollback snapshot reference
```

- [x] **Step 4: Add admin endpoints**

Add endpoints for:

```text
check update
prepare update candidate
approve candidate switch
rollback to previous snapshot
```

These endpoints must not reuse `enable_capability_package` as update activation.

- [x] **Step 5: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\http\test_remote_service.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_admin_service.py::test_prepare_capability_package_update_records_candidate_without_activation tests\labrastro_server\services\test_admin_service.py::test_apply_capability_package_update_candidate_does_not_auto_activate tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `8 passed in 4.17s`; update candidate flow now preserves upstream version display, backend-computed manifest diff, rollback snapshot references, and no-auto-activation semantics.

### Task 12: Implement Frontend State Projection and Copy Boundaries

**Matrix rows:** F-01, F-03, F-04, F-06, F-07, T-11, G-12

**Files:**
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\settings\capabilityPackageView.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\settings\tabs\CapabilitiesTab.tsx`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\components\chat\SessionTurn.tsx`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\i18n\zh-CN.ts`
- Test: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\settings\capabilityPackageView.test.ts`
- Test: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\settings\tabs\CapabilitiesTab.test.tsx`
- Test: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\components\chat\SessionTurn.test.ts`

- [x] **Step 1: Add view-model tests**

In `capabilityPackageView.test.ts`, assert internal states render as action-oriented user text:

```ts
expect(capabilityPackageUserStateLabel({
  mapping_state: "mapping_required",
  manual_step: "manual_command_review_required",
})).toContain("需要确认命令")
expect(capabilityPackageUserStateLabel({
  mapping_state: "mapping_required",
})).not.toContain("不支持")
expect(capabilityPackageUserStateLabel({
  mapping_state: "mapping_required",
})).not.toContain("等待开发者")
```

- [x] **Step 2: Add CapabilitiesTab tests**

Assert package cards show:

```text
安装状态
激活状态
服务端状态
本地端状态
凭据状态
更新状态
```

and do not show raw `mapping_required`.

- [x] **Step 3: Add chat card tests**

Assert the install card title and body reflect package-level install intent:

```text
确认安装能力
包含 hooks
需要确认命令
完成后重新检查
```

Do not add per-hook approve buttons.

- [x] **Step 4: Run expected failing frontend tests**

In extension repo:

```powershell
npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts
```

Expected: FAIL until labels and projection helpers exist.

Evidence: `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts` -> FAIL before implementation: `capabilityPackageStateView is not a function`, package cards lacked `安装状态`, and install decision cards did not show `包含 hooks`.

- [x] **Step 5: Implement projection helpers and UI copy**

Implement helpers in `capabilityPackageView.ts`:

```text
capabilityPackageStateView
capabilityPackageUserStateLabel
manualStepUserActionLabel
targetStatusLabel
credentialStateLabel
updateStateLabel
```

Update `CapabilitiesTab.tsx` and `SessionTurn.tsx` to use these helpers instead of raw status strings.

- [x] **Step 6: Run frontend verification**

Run:

```powershell
npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts
npm run typecheck
```

Expected: PASS.

Evidence: `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts` -> PASS, `3 passed`, `48 passed` in `4.00s`; `npm run typecheck` -> PASS. UI now projects install/activation/server/local/credential/update states into user-facing labels, avoids raw `mapping_required`, and install confirmation cards show hooks/manual command/re-check semantics without per-hook approval controls.

### Task 13: End-to-End Regression Matrix Closure

**Matrix rows:** G-01 through G-12

**Files:**
- Modify: `docs/superpowers/plans/2026-06-11-capability-package-ecosystem-architecture-matrix.md`
- Modify: `tests/labrastro_server/services/test_capability_package_ingest_fields.py`
- Modify: `tests/labrastro_server/services/test_capability_package_normalizer.py`
- Modify: `tests/labrastro_server/services/test_capability_package_artifacts.py`
- Modify: `tests/labrastro_server/services/test_capability_package_dependencies.py`
- Modify: `tests/labrastro_server/services/test_capability_package_install_plan.py`
- Modify: `tests/labrastro_server/services/test_capability_package_executor.py`
- Modify: `tests/labrastro_server/services/test_capability_package_credentials.py`
- Modify: `tests/labrastro_server/services/test_capability_package_updates.py`
- Modify: `tests/labrastro_server/services/test_capability_packages.py`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\LabrastroRemoteClient.test.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\LabrastroController.admin.test.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\settings\capabilityPackageView.test.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\settings\tabs\CapabilitiesTab.test.tsx`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\components\chat\SessionTurn.test.ts`

- [x] **Step 1: Add Waza regression fixture**

Add a fixture in server tests that represents:

```text
source: https://github.com/tw93/Waza
8 skill files
python package evidence: readability-lxml, html2text
```

The assertion must be:

```text
package is not discarded
8 skills remain materializable
python package finding maps to isolated runtime or open finding
dependent components degrade/block only when unresolved
```

Evidence: Waza fixture added across `test_capability_package_normalizer.py`, `test_capability_package_artifacts.py`, and `test_capability_package_dependencies.py`; focused command `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `10 passed in 1.13s`.

- [x] **Step 2: Run server regression set**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_ingest_fields.py tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_package_dependencies.py tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_capability_packages.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

Evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_ingest_fields.py tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_package_dependencies.py tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_capability_packages.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `147 passed in 3.33s`.

- [x] **Step 3: Run extension regression set**

In extension repo:

```powershell
npx vitest run src/LabrastroRemoteClient.test.ts src/LabrastroController.admin.test.ts webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts
npm run typecheck
```

Expected: PASS.

Evidence: `npx vitest run src/LabrastroRemoteClient.test.ts src/LabrastroController.admin.test.ts webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts` -> PASS, `5 passed`, `117 passed` in `4.39s`; `npm run typecheck` -> PASS.

- [x] **Step 4: Run GitNexus impact checks**

Run:

```powershell
$env:TEMP = 'D:\AboutDEV\Labrastro\.gitnexus-temp'
$env:TMP = 'D:\AboutDEV\Labrastro\.gitnexus-temp'
gitnexus detect-changes --scope all -r Labrastro
gitnexus detect-changes --scope all -r Labrastro-vscode-extension
```

Expected: output maps changed hunks to known capability-package, admin, HTTP, peer, and frontend surfaces; there should be no unexpected unrelated subsystem impact.

Evidence: Original aliases `Labrastro` and `Labrastro-vscode-extension` point to clean source paths and returned `No changes detected`, so worktrees were indexed with aliases `Labrastro-capability-package-ecosystem` and `Labrastro-vscode-extension-capability-package-ecosystem`. `gitnexus detect-changes --scope all -r Labrastro-capability-package-ecosystem` -> `Changes: 15 files, 89 symbols; Affected processes: 31; Risk level: critical`, concentrated in capability package/admin/remote/config flows. `gitnexus detect-changes --scope all -r Labrastro-vscode-extension-capability-package-ecosystem` -> `Changes: 12 files, 41 symbols; Affected processes: 9; Risk level: high`, concentrated in remote client/protocol/CapabilitiesTab/SessionTurn.

- [x] **Step 5: Update matrix evidence**

Update the matrix rows B-01 through B-11, S-01 through S-05, P-01 through P-06, F-01 through F-07, T-01 through T-12, and G-01 through G-12 with links or notes to passing test evidence.

Use this evidence format. The command text must be the real command that passed:

```text
Evidence: .\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex -> PASS on 2026-06-11; changed files: labrastro_server/services/capability_package_normalizer.py, tests/labrastro_server/services/test_capability_package_normalizer.py
```

Evidence: Matrix Section 14, Section 15.6, and Acceptance Gate Section 18 updated with Task 11-13 evidence and GitNexus worktree aliases.

## 3.1 Active Drift-Convergence Execution Progress

This section is the handoff anchor for continuing Stage 13 and reopened Stage 14 after conversation compaction. Keep it current after every C row. Any item marked superseded is historical evidence only and must not be counted as completion.

- [x] C-09 / G-21 runtime activation truth: `resolve_capability_refs`, environment manifest filtering, fallback capability catalog, and config hook validation now use package activation projection instead of deciding runtime visibility from `package.enabled`. Passing focused evidence: `tests\services\config\test_loader.py::test_config_validate_projects_capability_package_hook_activation_state`, `tests\services\config\test_agent_runtime_config_loader.py::test_agent_effective_capability_scope_excludes_disabled_package_runtime_resources`, `tests\domain\agent_runtime\test_runtime_models.py::test_resolve_capability_refs_uses_activation_state_not_enabled_only`, `tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_environment_manifest_endpoint_returns_structured_manifest`, and lifecycle activation tests passed together as `6 passed in 4.41s`.
- [x] C-10 / G-22 component/resource convergence: `RemoteAdminConfigManager._sync_capability_components_from_package_manifest` is the shared convergence helper for accept, delete, enable/disable, apply update, and rollback. It recalculates owner-derived `enabled`, updates `package_ids`, and calls `CapabilityPackageInstaller.materialize_component` / `remove_materialized_component` so `capability_components`, environment requirements, MCP servers, skills, and canonical SKILL.md files converge together. Passing focused evidence: `tests\labrastro_server\services\test_admin_service.py` plus shared package config/runtime guards passed as `64 passed in 4.35s`.
- [x] GitNexus recovery for current worktree: the explicit alias `Labrastro-capability-package-ecosystem` was absent from `~/.gitnexus/registry.json` after compaction; `gitnexus analyze -f --index-only --name Labrastro-capability-package-ecosystem .` rebuilt the index after an `lbug.wal without lbug.shadow` incremental failure. A later C-13 incremental run also exited non-zero after `+201 importer(s) added to writable set`; a forced rebuild restored a known-good server index. Current detect-changes evidence is server `22 files, 128 symbols, 18 affected processes, critical` and extension `14 files, 45 symbols, 9 affected processes, high`.
- [x] C-11 / G-23 transaction policy: capability package accept/delete/enable/apply/rollback now use `_commit_capability_package_config_and_files`. Policy is config commit first, then SKILL file operations; if a file operation fails, restore file snapshots and roll config back to `previous_data`. Passing focused evidence: `tests\labrastro_server\services\test_admin_service.py`, `test_admin_accept_does_not_write_skill_file_when_config_commit_fails`, `test_admin_delete_does_not_remove_skill_file_when_config_commit_fails`, and shared component tests passed as `62 passed in 1.95s`. Current server `detect-changes` after C-11 is `22 files, 127 symbols, 18 affected processes, critical`.
- [x] C-12 / G-24 full stable manifest diff: `manifest_diff` keeps legacy component/delta fields and adds stable `changed_sections` covering package, components, dependency edges, environment requirements, credential requirements, install plans, activation rules, exposed file closures, and update metadata. Passing evidence: update service/admin/http route tests passed as `10 passed in 4.66s`.
- [x] C-13 / G-25 protocol/action/credential/source alias normalizers: `InstallAction.from_dict`, `CapabilityCredentialBinding.from_dict`, `CapabilitySourceSnapshot.from_dict`, `normalize_update_candidate_payload`, and admin update check/prepare now centralize action ids, credential binding ids/secret refs, source snapshot aliases, and update candidate payload aliases. Passing evidence: install-plan, credential, update, peer install/result, and admin update focused tests passed as `26 passed in 6.75s`. Grep shows no remaining route-local parsing for `action_id`, `candidate_snapshot`, `candidate_manifest`, `secret_ref`, or `credential_ref_id`; the only remaining `requirement_id` hit is normalized dictionary lookup.
- [x] C-14 / G-26 frontend backend fact alignment is superseded by follow-up audit and reclosed through C-17/G-29. Historical C-14 evidence remains useful for target labels, but current closure depends on the package payload helper because backend `credential_state` and `target_facts` are package-level facts.
- [x] Previous final server/extension GitNexus + grep audit is invalidated as completion evidence and replaced by C-18/G-30. It remains historical baseline only; it cannot override current grep, GitNexus, or tests.
- [x] C-15 / G-27 update lifecycle activation truth: failing-before tests for `apply_update_candidate` and `rollback_update_candidate` reproduced the bug where `enabled=True` plus projected `activation_state=inactive` reactivated the package. Passing implementation uses `capability_package_is_active(current)`, and grep no longer finds `current.get("enabled")` in `capability_package_updates.py`.
- [x] C-16 / G-28 rollback availability guard: failing-before admin tests reproduced empty `{}` rollback metadata and repeated rollback after `update_state=current` entering rollback. Passing implementation uses `rollback_update_available`, which requires `state.update_state == rollback_available` and rollback snapshot/manifest metadata before calling `rollback_update_candidate`.
- [x] C-17 / G-29 frontend package state payload owner: failing-before frontend fixture with package-level `credential_state` and `target_facts` proved `CapabilitiesTab` rendered missing server/check and credential facts. Passing implementation adds `capabilityPackageStatePayload(item)` and keeps fixtures in the real backend shape. `useSettingsController` does not expose this state payload, so no second view-model owner was added there.
- [x] C-18 / G-30 evidence reclosure: matrix and this plan now record passing C-15 through C-17 evidence, targeted GitNexus query/impact, refreshed indexes, final `detect-changes`, grep audits, and affected server/extension tests.

## 3.2 Reopened Compression-Safe Execution Goal

Execute the reopened capability-package drift-convergence closure C-15 through C-18 without changing the architecture direction. The target is not a minimal patch set; the target is to make the documented state/projection architecture structurally enforceable in server code, frontend view-model code, tests, GitNexus evidence, and this matrix.

Current status: completed on 2026-06-11. Reuse the goal below for any future replay or continuation, but do not reclassify it as complete unless the listed evidence is refreshed against current code.

Start from these worktrees and GitNexus aliases:

```text
Server worktree: D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem
Server GitNexus alias: Labrastro-capability-package-ecosystem
Extension worktree: D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem-extension
Extension GitNexus alias: Labrastro-vscode-extension-capability-package-ecosystem
```

Mandatory execution sequence:

1. Baseline with GitNexus, not grep alone:
   - Query server activation drift around `apply_update_candidate rollback_update_candidate capability_package_is_active capability_package_state_projection enabled`.
   - Query server rollback drift around `rollback_capability_package_update rollback_update_candidate rollback_not_available update_state`.
   - Query extension frontend fact drift around `CapabilitiesTab installedCapabilityPackages capabilityPackageStateView credential_state target_facts useSettingsController`.
   - Run `gitnexus impact --include-tests` for `apply_update_candidate`, `rollback_update_candidate`, `rollback_capability_package_update`, and `installedCapabilityPackages`.
2. C-15 first: write failing tests that prove update apply/rollback cannot reactivate a package when shared state projection says inactive even if legacy `enabled=True`; then change `capability_package_updates.py` to use shared projection/transition logic instead of raw `current.get("enabled")`.
3. C-16 second: write failing tests for empty `{}` rollback metadata and a second rollback after `update_state=current`; then add a shared rollback availability guard used by admin rollback before calling `rollback_update_candidate`.
4. C-17 third: write failing frontend tests using real backend shape with `credential_state` and `target_facts` at package top level only; then add a single frontend helper that builds the state payload from package-level facts plus nested `state`, and use it in `CapabilitiesTab` and any controller-facing package view that exposes state.
5. C-18 last: refresh both GitNexus indexes, rerun `detect-changes`, rerun focused and affected regression commands, run grep audits, and update both this plan and the architecture matrix with current evidence. Do not claim final closure if any evidence contradicts code.

Hard boundaries:

- Do not rewrite AgentRun, the install architecture, or the GitHub-version strategy.
- Do not treat `enabled` as activation truth except as compatibility serialization or explicit admin write input that immediately updates state projection.
- Do not make rollback available from a non-empty dict check; availability must be a named transition guard.
- Do not fix frontend tests by moving backend package-level facts into nested `state` fixtures.
- Do not let `useSettingsController` and `CapabilitiesTab` diverge into two incompatible package view models when they expose the same package state facts.
- Do not close C-18/G-30 without fresh GitNexus query/impact/detect, grep, focused tests, affected regression tests, and updated matrix evidence.

Minimum focused verification commands:

```powershell
# Server targeted tests
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_admin_service.py::test_rollback_capability_package_update_rejects_empty_rollback_metadata tests\labrastro_server\services\test_admin_service.py::test_rollback_capability_package_update_rejects_consumed_rollback tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex

# Extension targeted tests
npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/settings/useSettingsController.test.tsx

# Extension typecheck
npm run typecheck
```

Required grep audits before final closure:

```powershell
rg -n 'current\.get\("enabled"\)|raw_package\.get\("enabled"\)|package\.enabled' labrastro_server/services/capability_package_updates.py labrastro_server/services/admin/service.py reuleauxcoder/domain
rg -n 'raw_package\.get\("rollback"\)|get\("rollback"\)|rollback_not_available|rollback_available' labrastro_server/services tests/labrastro_server
rg -n 'capabilityPackageStateView\(|credential_state|target_facts|const state = objectValue|capabilityPackageValue' webview-ui/src/settings
```

Executed closure evidence:

- Server focused verification: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_admin_service.py::test_rollback_capability_package_update_rejects_empty_rollback_metadata tests\labrastro_server\services\test_admin_service.py::test_rollback_capability_package_update_rejects_consumed_rollback tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `14 passed in 4.97s`.
- Extension focused/affected verification: `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/settings/useSettingsController.test.tsx` -> PASS, `67 passed in 3.71s`; `npm run typecheck` -> PASS.
- GitNexus refresh: `gitnexus analyze -f --index-only --name Labrastro-capability-package-ecosystem .` -> `16,653 nodes | 37,110 edges | 649 clusters | 300 flows`; `gitnexus analyze -f --index-only --name Labrastro-vscode-extension-capability-package-ecosystem .` -> `5,156 nodes | 16,145 edges | 326 clusters | 300 flows`.
- GitNexus detect: server -> `22 files, 130 symbols, 10 affected processes, high`; extension -> `14 files, 50 symbols, 10 affected processes, high`.
- Grep audits: update lifecycle no longer reads `current.get("enabled")`; rollback path routes availability through `rollback_update_available`; frontend state payload is built by `capabilityPackageStatePayload(item)`.

## 4. Review Checklist

Before creating a new implementation goal, verify:

- [ ] The matrix status is frozen and points to this plan.
- [ ] GitNexus is version `1.6.7` or newer.
- [ ] Both repos are indexed and up to date.
- [ ] The plan does not require rewriting AgentRun.
- [ ] The plan separates install, activation, runtime, check, credential, update, and mapping states.
- [ ] The plan includes both server and local peer target facts.
- [ ] The plan preserves old config compatibility until migration tests pass.
- [ ] The plan rejects LLM enum expansion and direct shell execution.
- [ ] The plan includes frontend wording tests for internal state leakage.

## 5. Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-11-capability-package-ecosystem-implementation-plan.md`.

Two execution options:

1. Subagent-Driven recommended: dispatch a fresh subagent per task and review between tasks.
2. Inline Execution: execute tasks in this session using checkpoint reviews after each stage.

Do not start implementation until the user chooses an execution mode or gives direct permission to proceed.

## 6. Current Execution Goal: Local-Peer Install Execution Reclosure

This section supersedes the old handoff whenever the active work is the local-peer install execution contract. The implementation goal is:

```text
In the Labrastro capability package ecosystem, close the local-peer install execution drift without broadening the architecture. Treat the architecture matrix as the control document and complete C-19 through C-22 / G-31 through G-34:

1. Server install action identity must be canonical. All action/status/result/stale paths in capability package remote routes must derive package_id, plan_id, action_id, component_id, and expected content/hash aliases through one helper. id/action_id, package_id, component_id, plan_id, content_hash/expected_content_hash/lock_hash, and top-level vs params aliases must not be parsed differently by separate functions.
2. Local peer execution must be idempotent. The VS Code local peer runner must skip canonical actions whose peer_status says install_state=installed and check_state is empty or passed. Polling or repeated runOnce must not rerun install_python_packages/check actions or resubmit identical results unless the canonical action identity or expected content hash changed.
3. Local peer runner lifecycle must be an authorization boundary. Runner start requires authenticated=true, status=ready, peerConnected=true, and a stable host/account/device/peer generation key. Logout, unauthenticated connection, host change, account change, device change, or peer change must stop the previous runner before any new runner starts.
4. Evidence must be failing-first and current. Before claiming closure, rerun GitNexus query/detect on server and extension, grep for residual hand-built keys and peerConnected-only runner starts, run targeted red/green tests, broad server capability-package regression, affected extension tests, extension typecheck, and git diff --check. Update the architecture matrix evidence row with the exact current commands and results.
```

Do not solve this by adding route-local special cases, by filtering only in the UI, by weakening rollback/update gates, or by treating server config as proof of local peer installation.

Minimum verification commands for this goal:

```powershell
# Server targeted identity tests
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_result_matches_params_component_id tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_result_with_stale_hash_requires_retry --basetemp .pytest-contract-green-tmp -o cache_dir=.pytest-cache-codex-contract-green

# Server capability-package regression
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_ingest_fields.py tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_package_dependencies.py tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_capability_packages.py tests\labrastro_server\services\test_admin_service.py tests\services\config\test_agent_runtime_config_loader.py::test_agent_effective_capability_scope_excludes_disabled_package_runtime_resources tests\services\config\test_loader.py::test_config_validate_projects_capability_package_hook_activation_state tests\domain\agent_runtime\test_runtime_models.py::test_resolve_capability_refs_uses_activation_state_not_enabled_only tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_capability_package_hooks_by_activation_state tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_package_component_hooks_by_owner_activation tests\domain\test_capability_package_domain.py --basetemp .pytest-contract-full-tmp -o cache_dir=.pytest-cache-codex-contract-full

# Extension targeted and affected tests
npx vitest run src/CapabilityPackageLocalPeerRunner.test.ts src/LabrastroController.admin.test.ts --testNamePattern "skips actions|does not rerun|gates the capability package local peer runner"
npx vitest run src/CapabilityPackageLocalPeerRunner.test.ts src/LabrastroController.admin.test.ts src/LabrastroRemoteClient.test.ts src/protocol/messages.test.ts webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts
npm run typecheck
```

Required residual audits:

```powershell
rg -n -F '_install_action_identity' labrastro_server\interfaces\http\remote\routes\capability_packages.py tests\labrastro_server\http\test_remote_service.py
rg -n -F 'params.get("component_id")' labrastro_server\interfaces\http\remote\routes\capability_packages.py
rg -n -F '"|".join' labrastro_server\interfaces\http\remote\routes\capability_packages.py
rg -n "peerConnected === true|capabilityPackageLocalPeerRunner\.start|actionAlreadyInstalled|installActionKey|install_state|check_state|capabilityPackageLocalPeerRunnerKey" src\CapabilityPackageLocalPeerRunner.ts src\LabrastroController.ts src\LabrastroController.admin.test.ts src\CapabilityPackageLocalPeerRunner.test.ts
```

GitNexus must be used before final closure:

```powershell
gitnexus analyze -f --index-only --name Labrastro-capability-package-ecosystem .
gitnexus query --repo Labrastro-capability-package-ecosystem "_install_action_identity _first_action_string _peer_action_key _peer_result_key _peer_result_is_stale component_id params expected_content_hash content_hash" --context "post-fix canonical identity audit" --goal "confirm install action and result keys share one canonical alias path" --limit 20
gitnexus detect-changes --repo Labrastro-capability-package-ecosystem --scope all

gitnexus analyze -f --index-only --name Labrastro-vscode-extension-capability-package-ecosystem .
gitnexus query --repo Labrastro-vscode-extension-capability-package-ecosystem "actionAlreadyInstalled installActionKey peer_status peerStatus check_state install_state updateCapabilityPackageLocalPeerRunner authenticated status ready peerConnected capabilityPackageLocalPeerRunnerKey" --context "post-fix runner idempotency and auth gate audit" --goal "confirm local runner skips installed actions and controller gates on authenticated ready peer connection" --limit 20
gitnexus detect-changes --repo Labrastro-vscode-extension-capability-package-ecosystem --scope all
```
