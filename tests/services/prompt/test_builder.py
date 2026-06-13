from reuleauxcoder.services.prompt.builder import system_prompt

from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


def test_system_prompt_includes_user_system_append() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        user_system_append="Always answer in Chinese.",
    )

    assert "# User Instructions" in prompt
    assert "Always answer in Chinese." in prompt


def test_system_prompt_includes_user_append_before_mode_instructions() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        mode_name="coder",
        mode_prompt_append="Focus on concrete code changes.",
        user_system_append="Always answer in Chinese.",
    )

    assert prompt.index("# User Instructions") < prompt.index("# Active Mode")
    assert "Focus on concrete code changes." in prompt


def test_system_prompt_contains_only_static_and_semi_static_blocks() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        mode_name="coder",
        mode_prompt_append="Focus on concrete code changes.",
        user_system_append="Always answer in Chinese.",
        skills_catalog="# Skills\n- skill-a",
    )

    assert prompt.index("# Tools") < prompt.index("# User Instructions")
    assert prompt.index("# User Instructions") < prompt.index("# Active Mode")
    assert "# Environment" not in prompt
    assert "- Working directory: " not in prompt


def test_system_prompt_includes_capability_catalog_before_user_instructions() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        user_system_append="Always answer in Chinese.",
        capability_catalog=(
            "- `review`: Review\n"
            "  - `envreq:executable:gh` [environment_requirement] gh"
        ),
    )

    assert "# Capability Packages" in prompt
    assert "`envreq:executable:gh`" in prompt
    assert prompt.index("# Capability Packages") < prompt.index("# User Instructions")


def test_system_prompt_includes_clickable_markdown_link_guidance() -> None:
    prompt = system_prompt([_Tool("read_file", "Read file")])

    assert "# Markdown Formatting" in prompt
    assert "`[`label`](relative/path.ext:line)`" in prompt
    assert "Do not invent links" in prompt


def test_system_prompt_exposes_apply_patch_contract() -> None:
    prompt = system_prompt([ApplyPatchTool()])

    assert "# Tools" in prompt
    assert "JSON function wrapper" in prompt
    assert "*** Add File:" in prompt
    assert "*** Update File:" in prompt
    assert "*** Delete File:" in prompt
    assert "Add File content lines must start with +" in prompt
    assert "Do not use *** File:" in prompt
    assert "Do not use" in prompt and "*** Action:" in prompt
    assert "unified diff" in prompt
    assert "C:foo.txt" in prompt
    assert "draft_document_begin" in prompt
