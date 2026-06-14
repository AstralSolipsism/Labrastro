from __future__ import annotations

from reuleauxcoder.extensions.tools.registry import build_tool_specs, build_tools
from reuleauxcoder.extensions.tools.spec import (
    ProviderSurface,
    ToolExposure,
    ToolRisk,
)


def _specs_by_name():
    return {spec.name: spec for spec in build_tool_specs()}


def test_registered_tools_publish_structured_specs_and_schema_derives_from_them() -> None:
    tools = build_tools()

    assert tools
    for tool in tools:
        spec = tool.tool_spec()

        assert spec.name == tool.name
        assert spec.description == tool.description
        assert spec.input_schema == tool.parameters
        assert spec.provider_surface == ProviderSurface.FUNCTION
        assert spec.exposure in set(ToolExposure)
        assert spec.risk in set(ToolRisk)
        assert spec.execution.executor_ref.endswith(tool.__class__.__qualname__)
        assert spec.search_text.strip()
        assert tool.schema() == spec.to_openai_chat_tool()


def test_build_tool_specs_is_a_stable_sorted_catalog() -> None:
    specs = build_tool_specs()
    names = [spec.name for spec in specs]

    assert names == sorted(names)
    assert names == sorted(tool.name for tool in build_tools())


def test_builtin_tools_have_explicit_architecture_metadata_matrix() -> None:
    specs = _specs_by_name()
    expected_risk = {
        "apply_patch": ToolRisk.FILE_MUTATION,
        "delegate_agent": ToolRisk.CAPABILITY,
        "draft_document_begin": ToolRisk.DOCUMENT_DRAFT,
        "fetch_capabilities": ToolRisk.CAPABILITY,
        "glob": ToolRisk.READ_ONLY,
        "grep": ToolRisk.READ_ONLY,
        "list_file": ToolRisk.READ_ONLY,
        "lsp": ToolRisk.READ_ONLY,
        "read_file": ToolRisk.READ_ONLY,
        "shell": ToolRisk.COMMAND_EXECUTION,
    }

    assert set(expected_risk).issubset(specs.keys())
    for name, risk in expected_risk.items():
        assert specs[name].risk == risk
        assert specs[name].exposure == ToolExposure.DIRECT

    assert specs["apply_patch"].mutation.modifies_files is True
    assert specs["apply_patch"].mutation.preview_required is True
    assert specs["apply_patch"].mutation.approved_save_candidate_required is True
    assert specs["apply_patch"].permission.policy == "file_mutation"

    assert specs["shell"].permission.policy == "command_execution"
    assert specs["draft_document_begin"].permission.policy == "document_draft"
    assert specs["delegate_agent"].permission.policy == "capability"
    assert specs["fetch_capabilities"].permission.policy == "capability"

    read_only_tools = {"glob", "grep", "list_file", "lsp", "read_file"}
    assert {specs[name].permission.policy for name in read_only_tools} == {"read_only"}
