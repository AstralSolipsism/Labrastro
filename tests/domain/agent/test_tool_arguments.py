from reuleauxcoder.domain.agent.tool_arguments import (
    ToolArgumentRepairPolicy,
    format_tool_argument_retry_message,
    validate_and_repair_tool_arguments,
)


def test_optional_null_is_omitted_before_final_validation() -> None:
    schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "offset": {"type": "integer"},
        },
        "required": ["file_path"],
    }

    result = validate_and_repair_tool_arguments(
        tool_name="read_file",
        arguments={"file_path": "demo.md", "offset": None},
        schema=schema,
    )

    assert result.final_valid is True
    assert result.arguments == {"file_path": "demo.md"}
    assert result.initial_issues[0].code == "optional_null"
    assert result.repairs[0].action == "omit_optional_null"


def test_required_null_remains_invalid() -> None:
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }

    result = validate_and_repair_tool_arguments(
        tool_name="apply_patch",
        arguments={"content": None},
        schema=schema,
    )

    assert result.final_valid is False
    assert result.final_issues[0].path == "$.content"
    assert result.final_issues[0].code == "null_required"


def test_scalar_strings_are_safely_coerced() -> None:
    schema = {
        "type": "object",
        "properties": {
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
            "override": {"type": "boolean"},
        },
    }

    result = validate_and_repair_tool_arguments(
        tool_name="read_file",
        arguments={"offset": "12", "limit": "+30", "override": "false"},
        schema=schema,
    )

    assert result.final_valid is True
    assert result.arguments == {"offset": 12, "limit": 30, "override": False}
    assert [repair.action for repair in result.repairs] == [
        "coerce_scalar_string",
        "coerce_scalar_string",
        "coerce_scalar_string",
    ]


def test_json_string_array_is_parsed_before_bare_string_wrapping() -> None:
    schema = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    result = validate_and_repair_tool_arguments(
        tool_name="mcp_batch",
        arguments={"paths": '["a"]'},
        schema=schema,
        policy=ToolArgumentRepairPolicy(name="deepseek", wrap_bare_string_arrays=True),
    )

    assert result.final_valid is True
    assert result.arguments == {"paths": ["a"]}
    assert [repair.action for repair in result.repairs] == ["parse_json_string"]


def test_deepseek_policy_wraps_bare_string_array_after_json_parse_step() -> None:
    schema = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    result = validate_and_repair_tool_arguments(
        tool_name="mcp_batch",
        arguments={"paths": "a"},
        schema=schema,
        policy=ToolArgumentRepairPolicy(name="deepseek", wrap_bare_string_arrays=True),
    )

    assert result.final_valid is True
    assert result.arguments == {"paths": ["a"]}
    assert result.repairs[0].action == "wrap_bare_string_array"


def test_generic_policy_does_not_wrap_bare_string_array() -> None:
    schema = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    result = validate_and_repair_tool_arguments(
        tool_name="mcp_batch",
        arguments={"paths": "a"},
        schema=schema,
    )

    assert result.final_valid is False
    assert result.final_issues[0].path == "$.paths"
    assert result.final_issues[0].expected == "array"
    assert result.final_issues[0].actual == "string"


def test_empty_object_placeholder_is_not_wrapped_as_array() -> None:
    schema = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    result = validate_and_repair_tool_arguments(
        tool_name="mcp_batch",
        arguments={"paths": "{}"},
        schema=schema,
        policy=ToolArgumentRepairPolicy(name="deepseek", wrap_bare_string_arrays=True),
    )

    assert result.final_valid is False
    assert result.arguments == {"paths": "{}"}
    assert result.repairs == []


def test_retry_message_lists_precise_paths_and_types() -> None:
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    result = validate_and_repair_tool_arguments(
        tool_name="apply_patch",
        arguments={},
        schema=schema,
    )

    message = format_tool_argument_retry_message(
        "apply_patch", result.final_issues
    )

    assert "$.content: expected string, got missing" in message
    assert "Re-call apply_patch" in message
