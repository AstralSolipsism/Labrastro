from pathlib import Path

from labrastro_server.services.capability_package_artifacts import (
    build_skill_file_closure,
)


def test_skill_file_closure_preserves_nested_and_shared_paths(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "skill-a" / "references").mkdir(parents=True)
    (root / "skill-b" / "scripts").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "skill-a" / "SKILL.md").write_text(
        "Read [ref](references/a.md) and ../shared/rules.md\n",
        encoding="utf-8",
    )
    (root / "skill-a" / "references" / "a.md").write_text("A\n", encoding="utf-8")
    (root / "skill-a" / "token.txt").write_text("token\n", encoding="utf-8")
    (root / "skill-b" / "SKILL.md").write_text("Run scripts/b.ps1\n", encoding="utf-8")
    (root / "skill-b" / "scripts" / "b.ps1").write_text(
        "Write-Output b\n",
        encoding="utf-8",
    )
    (root / "shared" / "rules.md").write_text("shared\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("module\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    closure = build_skill_file_closure(
        package_root=root,
        entry_path=root / "skill-a" / "SKILL.md",
        explicit_paths=["shared/rules.md"],
    )

    assert closure.entry_path == "skill-a/SKILL.md"
    assert closure.package_root == str(root)
    assert "skill-a/SKILL.md" in closure.included_paths
    assert "skill-a/references/a.md" in closure.included_paths
    assert "shared/rules.md" in closure.included_paths
    assert "skill-b/SKILL.md" not in closure.included_paths
    assert ".env" in closure.denied_paths
    assert "skill-a/token.txt" in closure.denied_paths
    assert "node_modules/pkg/index.js" in closure.denied_paths


def test_root_skill_file_closure_exposes_allowlisted_roots_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    (root / "docs").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "skill-b" / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "Use docs/root.md and rules/global.md\n",
        encoding="utf-8",
    )
    (root / "docs" / "root.md").write_text("root docs\n", encoding="utf-8")
    (root / "rules" / "global.md").write_text("global rules\n", encoding="utf-8")
    (root / "skill-b" / "SKILL.md").write_text("Run scripts/b.ps1\n", encoding="utf-8")
    (root / "skill-b" / "scripts" / "b.ps1").write_text(
        "Write-Output b\n",
        encoding="utf-8",
    )

    closure = build_skill_file_closure(
        package_root=root,
        entry_path=root / "SKILL.md",
    )

    assert closure.included_paths == ["SKILL.md", "docs/root.md", "rules/global.md"]
    assert "skill-b/SKILL.md" not in closure.included_paths


def test_waza_like_repo_keeps_eight_skill_file_closures_controlled(
    tmp_path: Path,
) -> None:
    root = tmp_path / "waza"
    skill_names = [
        "read",
        "summarize",
        "translate",
        "search",
        "extract",
        "outline",
        "rewrite",
        "publish",
    ]
    (root / "references").mkdir(parents=True)
    (root / "references" / "shared.md").write_text("shared rules\n", encoding="utf-8")
    (root / "requirements.txt").write_text(
        "readability-lxml\nhtml2text\n",
        encoding="utf-8",
    )
    for name in skill_names:
        skill_dir = root / "skills" / name
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"# {name}\nUse scripts/{name}.py and ../../references/shared.md\n",
            encoding="utf-8",
        )
        (skill_dir / "scripts" / f"{name}.py").write_text(
            f"print('{name}')\n",
            encoding="utf-8",
        )
    (root / "skills" / "read" / "secret-token.txt").write_text(
        "do not expose\n",
        encoding="utf-8",
    )

    closures = [
        build_skill_file_closure(
            package_root=root,
            entry_path=root / "skills" / name / "SKILL.md",
            explicit_paths=["references/shared.md"],
        )
        for name in skill_names
    ]

    assert [closure.entry_path for closure in closures] == [
        f"skills/{name}/SKILL.md" for name in skill_names
    ]
    assert len(closures) == 8
    for name, closure in zip(skill_names, closures):
        assert f"skills/{name}/SKILL.md" in closure.included_paths
        assert f"skills/{name}/scripts/{name}.py" in closure.included_paths
        assert "references/shared.md" in closure.included_paths
        assert "requirements.txt" not in closure.included_paths
    assert "skills/read/secret-token.txt" in closures[0].denied_paths
