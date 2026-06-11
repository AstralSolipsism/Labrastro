from labrastro_server.services.capability_package_dependencies import (
    build_dependency_graph,
    default_shared_capability_registry,
)


def test_shared_gh_requirement_is_single_registry_backed_node() -> None:
    registry = default_shared_capability_registry()

    graph = build_dependency_graph(
        components=[
            {
                "id": "skill:pkg-a/review",
                "environment_requirement_refs": ["envreq:executable:gh"],
            },
            {
                "id": "skill:pkg-b/issues",
                "environment_requirement_refs": ["envreq:executable:gh"],
            },
        ],
        requirements=[
            {"id": "envreq:executable:gh", "kind": "executable", "name": "gh"},
        ],
        registry=registry,
    )

    assert (
        graph.requirements["envreq:executable:gh"]["shared_registry_id"]
        == "shared:executable:gh"
    )
    assert sorted(edge["from_component_id"] for edge in graph.edges) == [
        "skill:pkg-a/review",
        "skill:pkg-b/issues",
    ]
    assert {edge["shared_registry_id"] for edge in graph.edges} == {
        "shared:executable:gh"
    }


def test_invalid_dependency_edge_blocks_only_dependent_component() -> None:
    graph = build_dependency_graph(
        components=[
            {
                "id": "skill:pkg/read",
                "environment_requirement_refs": ["envreq:unknown:thing"],
            },
            {
                "id": "skill:pkg/write",
                "environment_requirement_refs": ["envreq:executable:gh"],
            },
        ],
        requirements=[],
        registry=default_shared_capability_registry(),
    )

    invalid_edges = [edge for edge in graph.edges if edge["status"] == "invalid"]
    assert invalid_edges == [
        {
            "from_component_id": "skill:pkg/read",
            "to_requirement_id": "envreq:unknown:thing",
            "status": "invalid",
            "reason": "missing_requirement",
        }
    ]
    assert graph.blocked_component_ids == ["skill:pkg/read"]


def test_shared_capability_registry_entries_have_required_policy_fields() -> None:
    registry = default_shared_capability_registry()

    for shared_id in [
        "shared:executable:git",
        "shared:executable:gh",
        "shared:executable:bash",
        "shared:executable:sh",
        "shared:executable:python3",
        "shared:executable:node",
        "shared:executable:npm",
        "shared:executable:pnpm",
        "shared:executable:yarn",
        "shared:executable:docker",
        "shared:executable:jq",
        "shared:executable:curl",
        "shared:executable:wget",
        "shared:executable:rg",
    ]:
        entry = registry[shared_id]
        assert entry["version_check_action"]
        assert entry["install_action_policy"]
        assert entry["platforms"]
        assert entry["credential_interaction"]
        assert entry["conflict_policy"]
        assert entry["evidence_required"]


def test_waza_unresolved_python_packages_block_only_dependent_skills() -> None:
    components = [
        {
            "id": "skill:waza/read",
            "environment_requirement_refs": [
                "envreq:python-pkg:readability-lxml",
                "envreq:python-pkg:html2text",
            ],
        },
        {
            "id": "skill:waza/summarize",
            "environment_requirement_refs": ["envreq:python-pkg:html2text"],
        },
        {"id": "skill:waza/translate", "environment_requirement_refs": []},
        {"id": "skill:waza/search", "environment_requirement_refs": []},
        {"id": "skill:waza/extract", "environment_requirement_refs": []},
        {"id": "skill:waza/outline", "environment_requirement_refs": []},
        {"id": "skill:waza/rewrite", "environment_requirement_refs": []},
        {"id": "skill:waza/publish", "environment_requirement_refs": []},
    ]

    graph = build_dependency_graph(
        components=components,
        requirements=[],
        registry=default_shared_capability_registry(),
    )

    assert graph.blocked_component_ids == [
        "skill:waza/read",
        "skill:waza/summarize",
    ]
    assert {
        edge["from_component_id"] for edge in graph.edges if edge["status"] == "invalid"
    } == {
        "skill:waza/read",
        "skill:waza/summarize",
    }
    assert "skill:waza/translate" not in graph.blocked_component_ids
