from labrastro_server.services.capability_package_normalizer import (
    normalize_capability_manifest_candidate,
)


def test_python_package_requirement_becomes_unmapped_runtime_finding() -> None:
    result = normalize_capability_manifest_candidate(
        {
            "package": {"id": "waza"},
            "components": [
                {
                    "id": "skill:waza/read",
                    "kind": "skill",
                    "name": "read",
                    "environment_requirement_refs": [
                        "envreq:python-pkg:readability-lxml"
                    ],
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
    assert (
        result.unmapped_findings["unclassified_requirements"][0]["observed"]
        == "pip install --user readability-lxml html2text"
    )
    assert (
        result.unmapped_findings["unclassified_requirements"][0]["suggested_kind"]
        == "python_package"
    )


def test_unknown_envreq_id_kind_is_unmapped_instead_of_raised() -> None:
    result = normalize_capability_manifest_candidate(
        {
            "package": {"id": "waza"},
            "components": [
                {
                    "id": "envreq:python-pkg:html2text",
                    "kind": "environment_requirement",
                    "name": "html2text",
                    "install": "pip install html2text",
                },
            ],
        }
    )

    assert result.components == []
    assert result.unmapped_findings["unclassified_requirements"] == [
        {
            "id": "envreq:python-pkg:html2text",
            "observed": "pip install html2text",
            "suggested_kind": "environment_requirement",
            "mapping_state": "mapping_required",
        }
    ]


def test_component_id_prefix_must_match_backend_kind() -> None:
    result = normalize_capability_manifest_candidate(
        {
            "package": {"id": "waza"},
            "components": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "skill",
                    "name": "gh",
                    "check": "gh --version",
                },
                {
                    "id": "skill:waza/read",
                    "kind": "mcp_server",
                    "name": "read",
                },
                {
                    "id": "mcp_server:github",
                    "kind": "skill",
                    "name": "github",
                },
            ],
        }
    )

    assert result.components == []
    findings = [
        item["id"]
        for items in result.unmapped_findings.values()
        for item in items
    ]
    assert findings == [
        "envreq:executable:gh",
        "skill:waza/read",
        "mcp_server:github",
    ]


def test_waza_like_multi_skill_repo_keeps_skills_when_python_packages_are_unmapped() -> None:
    skill_components = [
        {
            "id": f"skill:waza/{name}",
            "kind": "skill",
            "name": name,
            "source_path": f"skills/{name}/SKILL.md",
            "environment_requirement_refs": [
                "envreq:python-pkg:readability-lxml"
            ] if name in {"read", "summarize"} else [],
        }
        for name in [
            "read",
            "summarize",
            "translate",
            "search",
            "extract",
            "outline",
            "rewrite",
            "publish",
        ]
    ]

    result = normalize_capability_manifest_candidate(
        {
            "package": {
                "id": "waza",
                "source_url": "https://github.com/tw93/Waza",
            },
            "components": [
                *skill_components,
                {
                    "id": "envreq:python-pkg:readability-lxml",
                    "kind": "python_package",
                    "name": "readability-lxml",
                    "install": "pip install readability-lxml html2text",
                    "source_path": "requirements.txt",
                },
            ],
        }
    )

    assert [item["id"] for item in result.components] == [
        "skill:waza/read",
        "skill:waza/summarize",
        "skill:waza/translate",
        "skill:waza/search",
        "skill:waza/extract",
        "skill:waza/outline",
        "skill:waza/rewrite",
        "skill:waza/publish",
    ]
    assert len(result.components) == 8
    assert result.package["id"] == "waza"
    assert result.unmapped_findings["unclassified_requirements"] == [
        {
            "id": "envreq:python-pkg:readability-lxml",
            "observed": "pip install readability-lxml html2text",
            "suggested_kind": "python_package",
            "source_path": "requirements.txt",
            "mapping_state": "mapping_required",
        }
    ]
