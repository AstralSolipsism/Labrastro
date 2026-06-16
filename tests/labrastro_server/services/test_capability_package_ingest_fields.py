from __future__ import annotations

import json

from labrastro_server.services.capability_package_ingest import (
    CapabilityDraftAssembler,
    CapabilityDraftFieldPatch,
    CapabilityFailureCode,
    extract_capability_draft_field_patches,
)


def _source_bundle() -> dict[str, object]:
    return {
        "source": {
            "type": "github_repo",
            "url": "https://github.com/greensock/gsap-skills",
        },
        "documents": [
            {
                "source_document_id": "cap-src-doc-gsap-core",
                "title": "skills/gsap-core/SKILL.md",
                "source_path": "skills/gsap-core/SKILL.md",
                "content": "---\nname: gsap-core\n---\n\nUse GSAP core.\n",
            }
        ],
        "evidence": [
            {"title": "GSAP core", "excerpt": "Use GSAP core."},
        ],
    }


def _patch_json(field_path: str, value: object) -> str:
    return json.dumps(
        {
            "capability_draft_patch": {
                "field_path": field_path,
                "value": value,
            }
        }
    )


def test_extracts_newline_separated_field_patch_objects() -> None:
    patches = extract_capability_draft_field_patches(
        "\n".join(
            [
                _patch_json("id", "gsap-skills"),
                _patch_json("name", "GSAP Skills"),
            ]
        ),
        producer_event_refs=[{"agent_run_id": "run-1", "seq": 8, "type": "text"}],
    )

    assert [patch.field_path for patch in patches] == ["id", "name"]
    assert [patch.value for patch in patches] == ["gsap-skills", "GSAP Skills"]
    assert patches[0].producer_event_refs == [
        {"agent_run_id": "run-1", "seq": 8, "type": "text"}
    ]


def test_extracts_field_patches_from_mixed_text_and_fenced_json() -> None:
    patches = extract_capability_draft_field_patches(
        "\n".join(
            [
                "Repository summary is ready.",
                "```json",
                _patch_json("repo_summary", "GSAP skill repository."),
                "```",
                "Risk classification is also ready.",
                _patch_json("risk_level", "low"),
            ]
        )
    )

    assert [patch.field_path for patch in patches] == ["repo_summary", "risk_level"]
    assert patches[0].value == "GSAP skill repository."
    assert patches[1].value == "low"


def test_extracts_batch_field_patch_array() -> None:
    patches = extract_capability_draft_field_patches(
        json.dumps(
            {
                "capability_draft_patches": [
                    {"field_path": "id", "value": "gsap-skills"},
                    {"field_path": "name", "value": "GSAP Skills"},
                ]
            }
        )
    )

    assert [patch.field_path for patch in patches] == ["id", "name"]


def test_normalizes_string_source_refs_from_prompt_protocol() -> None:
    patches = extract_capability_draft_field_patches(
        json.dumps(
            {
                "capability_draft_patch": {
                    "field_path": "repo_summary",
                    "value": "GSAP skill repository.",
                    "source_refs": [
                        "cap-src-doc-gsap-core",
                        "skills/gsap-core/SKILL.md",
                        "https://github.com/greensock/gsap-skills",
                        "read-gsap-core",
                        {"source_path": "README.md"},
                    ],
                }
            }
        )
    )

    assert patches[0].source_refs == [
        {"source_document_id": "cap-src-doc-gsap-core"},
        {"source_path": "skills/gsap-core/SKILL.md"},
        {"url": "https://github.com/greensock/gsap-skills"},
        {"content_ref": "read-gsap-core"},
        {"source_path": "README.md"},
    ]


def test_extracts_legacy_full_draft_as_full_draft_patch() -> None:
    draft = {
        "id": "legacy",
        "name": "Legacy",
        "source": {"type": "project_notes"},
        "contributions": {},
        "evidence": [{"title": "Project notes", "excerpt": "Use legacy draft."}],
        "risk_level": "low",
    }

    patches = extract_capability_draft_field_patches(json.dumps(draft))

    assert len(patches) == 1
    assert patches[0].field_path == "full_draft"
    assert patches[0].value == draft


def test_assembler_records_repo_summary_patch_without_draft_ready() -> None:
    result = CapabilityDraftAssembler().assemble(
        source_bundle=_source_bundle(),
        patches=[
            CapabilityDraftFieldPatch(
                field_path="repo_summary",
                value="GSAP skill repository with multiple animation skills.",
                source_refs=[{"source_document_id": "cap-src-doc-gsap-core"}],
                producer_event_refs=[{"agent_run_id": "run-1", "seq": 8, "type": "text"}],
            )
        ],
    )

    assert result.draft is None
    assert result.failure_code == CapabilityFailureCode.FIELD_GENERATION_INCOMPLETE
    assert result.field_state["repo_summary"]["status"] == "filled"
    assert result.field_state["repo_summary"]["value"] == (
        "GSAP skill repository with multiple animation skills."
    )
    assert result.field_state["repo_summary"]["producer_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 8, "type": "text"}
    ]
    assert "contributions" in result.missing_fields


def test_assembler_retains_manifest_candidate_and_open_finding_patches() -> None:
    result = CapabilityDraftAssembler().assemble(
        source_bundle=_source_bundle(),
        patches=[
            CapabilityDraftFieldPatch(
                field_path="manifest_candidate",
                value={"components": [{"id": "skill:waza/read"}]},
            ),
            CapabilityDraftFieldPatch(
                field_path="open_findings",
                value={
                    "unclassified_requirements": [
                        {"observed": "pip install html2text"}
                    ]
                },
            ),
            CapabilityDraftFieldPatch(
                field_path="target_placement_proposals",
                value=[{"component_id": "skill:waza/read", "target": "server"}],
            ),
            CapabilityDraftFieldPatch(
                field_path="exposed_path_candidates",
                value=[{"component_id": "skill:waza/read", "path": "references/a.md"}],
            ),
        ],
    )

    assert result.draft is None
    assert result.field_state["manifest_candidate"]["value"] == {
        "components": [{"id": "skill:waza/read"}]
    }
    assert result.field_state["open_findings"]["value"] == {
        "unclassified_requirements": [{"observed": "pip install html2text"}]
    }
    assert result.field_state["target_placement_proposals"]["value"] == [
        {"component_id": "skill:waza/read", "target": "server"}
    ]
    assert result.field_state["exposed_path_candidates"]["value"] == [
        {"component_id": "skill:waza/read", "path": "references/a.md"}
    ]


def test_assembler_retains_optional_features_patch() -> None:
    optional_features = [
        {
            "id": "optional:readability",
            "title": "Readability extraction",
            "placement": "server",
            "default_selected": False,
            "selection_scope": "user",
            "requirement_refs": [],
        }
    ]

    result = CapabilityDraftAssembler().assemble(
        source_bundle=_source_bundle(),
        patches=[
            CapabilityDraftFieldPatch(field_path="id", value="gsap-skills"),
            CapabilityDraftFieldPatch(field_path="name", value="GSAP Skills"),
            CapabilityDraftFieldPatch(
                field_path="contributions.skills",
                value=[
                    {
                        "id": "skill:gsap-core",
                        "kind": "skill",
                        "name": "gsap-core",
                        "source_document_id": "cap-src-doc-gsap-core",
                    }
                ],
            ),
            CapabilityDraftFieldPatch(field_path="optional_features", value=optional_features),
            CapabilityDraftFieldPatch(
                field_path="install_plan",
                value=["Install the GSAP skill files."],
            ),
            CapabilityDraftFieldPatch(
                field_path="usage",
                value=["Use gsap-core when authoring GSAP animations."],
            ),
            CapabilityDraftFieldPatch(
                field_path="evidence",
                value=[{"title": "GSAP core", "excerpt": "Use GSAP core."}],
            ),
            CapabilityDraftFieldPatch(field_path="risk_level", value="low"),
        ],
    )

    assert result.failure_code == ""
    assert result.draft is not None
    assert result.draft["optional_features"] == optional_features


def test_assembler_builds_compatible_draft_from_field_patches() -> None:
    result = CapabilityDraftAssembler().assemble(
        source_bundle=_source_bundle(),
        patches=[
            CapabilityDraftFieldPatch(field_path="id", value="gsap-skills"),
            CapabilityDraftFieldPatch(field_path="name", value="GSAP Skills"),
            CapabilityDraftFieldPatch(
                field_path="contributions.skills",
                value=[
                    {
                        "id": "skill:gsap-core",
                        "kind": "skill",
                        "name": "gsap-core",
                        "source_document_id": "cap-src-doc-gsap-core",
                    }
                ],
            ),
            CapabilityDraftFieldPatch(
                field_path="install_plan",
                value=["Install the GSAP skill files."],
            ),
            CapabilityDraftFieldPatch(
                field_path="usage",
                value=["Use gsap-core when authoring GSAP animations."],
            ),
            CapabilityDraftFieldPatch(
                field_path="evidence",
                value=[{"title": "GSAP core", "excerpt": "Use GSAP core."}],
            ),
            CapabilityDraftFieldPatch(field_path="risk_level", value="low"),
        ],
    )

    assert result.failure_code == ""
    assert result.missing_fields == []
    assert result.draft == {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {
            "type": "github_repo",
            "url": "https://github.com/greensock/gsap-skills",
        },
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "source_document_id": "cap-src-doc-gsap-core",
                }
            ]
        },
        "install_plan": ["Install the GSAP skill files."],
        "usage": ["Use gsap-core when authoring GSAP animations."],
        "evidence": [{"title": "GSAP core", "excerpt": "Use GSAP core."}],
        "risk_level": "low",
    }


def test_assembler_accepts_empty_install_plan_and_usage_as_filled() -> None:
    result = CapabilityDraftAssembler().assemble(
        source_bundle=_source_bundle(),
        patches=[
            CapabilityDraftFieldPatch(field_path="id", value="gsap-skills"),
            CapabilityDraftFieldPatch(field_path="name", value="GSAP Skills"),
            CapabilityDraftFieldPatch(
                field_path="contributions.skills",
                value=[
                    {
                        "id": "skill:gsap-core",
                        "kind": "skill",
                        "name": "gsap-core",
                        "source_document_id": "cap-src-doc-gsap-core",
                    }
                ],
            ),
            CapabilityDraftFieldPatch(field_path="install_plan", value=[]),
            CapabilityDraftFieldPatch(field_path="usage", value=[]),
            CapabilityDraftFieldPatch(
                field_path="evidence",
                value=[{"title": "GSAP core", "excerpt": "Use GSAP core."}],
            ),
            CapabilityDraftFieldPatch(field_path="risk_level", value="low"),
        ],
    )

    assert result.failure_code == ""
    assert result.missing_fields == []
    assert result.draft is not None
    assert result.draft["install_plan"] == []
    assert result.draft["usage"] == []


def test_assembler_reports_missing_required_draft_fields() -> None:
    result = CapabilityDraftAssembler().assemble(
        source_bundle=_source_bundle(),
        patches=[
            CapabilityDraftFieldPatch(field_path="id", value="gsap-skills"),
            CapabilityDraftFieldPatch(field_path="name", value="GSAP Skills"),
            CapabilityDraftFieldPatch(field_path="risk_level", value="low"),
        ],
    )

    assert result.draft is None
    assert result.failure_code == CapabilityFailureCode.DRAFT_FIELD_MISSING
    assert result.missing_fields == [
        "contributions",
        "install_plan",
        "usage",
        "evidence",
    ]
    assert result.field_state["id"]["status"] == "filled"
    assert result.field_state["risk_level"]["status"] == "filled"


def test_assembler_converts_legacy_full_draft_to_full_draft_patch() -> None:
    draft = {
        "id": "legacy",
        "name": "Legacy",
        "source": {"type": "project_notes"},
        "contributions": {},
        "evidence": [{"title": "Project notes", "excerpt": "Use legacy draft."}],
        "risk_level": "low",
    }

    patch = CapabilityDraftFieldPatch.from_legacy_full_draft(
        draft,
        producer_event_refs=[{"agent_run_id": "run-1", "seq": 12, "type": "text"}],
    )
    result = CapabilityDraftAssembler().assemble(
        source_bundle={"source": {"type": "project_notes"}},
        patches=[patch],
    )

    assert patch.field_path == "full_draft"
    assert result.draft == draft
    assert result.field_state["full_draft"]["status"] == "filled"
    assert result.field_state["full_draft"]["producer_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 12, "type": "text"}
    ]
