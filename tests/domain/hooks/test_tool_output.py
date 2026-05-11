from pathlib import Path

from reuleauxcoder.domain.hooks.builtin.tool_output import ToolOutputTruncationHook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.domain.llm.models import ToolCall


def _ctx(
    file_path: str, result: str, *, override: bool = False
) -> AfterToolExecuteContext:
    return AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="1",
            name="read_file",
            arguments={"file_path": file_path, "override": override},
        ),
        result=result,
        round_index=1,
    )


def test_tool_output_truncates_regular_read_file_output() -> None:
    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"

    ctx = _ctx("/tmp/notes.md", long_text)
    out = hook.run(ctx)

    assert "[truncated]" in out.result


def test_tool_output_bypasses_truncation_for_workspace_skills_markdown(
    monkeypatch,
) -> None:
    workspace = (Path.cwd() / "synthetic-workspace").resolve(strict=False)
    monkeypatch.setattr(Path, "cwd", lambda: workspace)

    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"
    skill_md = workspace / ".rcoder" / "skills" / "demo" / "SKILL.md"

    ctx = _ctx(str(skill_md), long_text)
    out = hook.run(ctx)

    assert out.result == long_text


def test_tool_output_bypasses_truncation_for_global_skills_markdown(
    monkeypatch,
) -> None:
    home = (Path.cwd() / "synthetic-home").resolve(strict=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)

    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"
    skill_md = home / ".rcoder" / "skills" / "demo" / "guide.md"

    ctx = _ctx(str(skill_md), long_text)
    out = hook.run(ctx)

    assert out.result == long_text


def test_tool_output_does_not_bypass_non_markdown_under_skills(
    monkeypatch,
) -> None:
    workspace = (Path.cwd() / "synthetic-workspace").resolve(strict=False)
    monkeypatch.setattr(Path, "cwd", lambda: workspace)

    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"
    skill_txt = workspace / ".rcoder" / "skills" / "demo" / "notes.txt"

    ctx = _ctx(str(skill_txt), long_text)
    out = hook.run(ctx)

    assert "[truncated]" in out.result
