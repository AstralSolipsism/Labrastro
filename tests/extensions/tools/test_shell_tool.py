"""Tests for ShellTool CWD tracking and stale-directory recovery."""

import os
import subprocess
from unittest import mock

from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.infrastructure.platform import ShellType


class _PlatformStub:
    def __init__(
        self,
        *,
        is_windows: bool,
        shell: ShellType,
        shell_cmd: list[str] | None,
    ) -> None:
        self.is_windows = is_windows
        self._shell = shell
        self._shell_cmd = shell_cmd or []

    def get_preferred_shell(self) -> ShellType:
        return self._shell

    def get_shell_executable(self) -> list[str]:
        return list(self._shell_cmd)


def _completed(stdout: str = "ok\n") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def test_execute_local_resets_cwd_when_directory_deleted():
    """When tracked CWD is deleted externally, next command resets to project root."""
    tool = ShellTool()
    tmpdir = os.path.join(os.getcwd(), "missing-shell-cwd")
    tool._cwd = tmpdir

    # Next command should detect stale CWD and reset
    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.os.path.isdir",
        return_value=False,
    ):
        result = tool._execute_local("echo hello")

    assert "working directory no longer exists" in result
    assert "reset to the project root" in result
    assert tmpdir in result
    assert tool._cwd is None


def test_execute_local_succeeds_when_cwd_valid():
    """Normal execution works fine when CWD is valid."""
    tool = ShellTool()

    result = tool._execute_local("echo hello")

    assert "hello" in result
    assert "working directory no longer exists" not in result


def test_execute_local_uses_os_getcwd_when_cwd_is_none():
    """When _cwd is None, fallback to os.getcwd() works normally."""
    tool = ShellTool()
    tool._cwd = None

    result = tool._execute_local("echo hello")

    assert "hello" in result


def test_run_powershell_core_preserves_and_operator():
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=True,
        shell=ShellType.POWERSHELL_CORE,
        shell_cmd=["pwsh", "-NoProfile", "-Command"],
    )

    with (
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
            return_value=platform,
        ),
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.subprocess.run",
            return_value=_completed(),
        ) as run,
    ):
        tool._run_powershell("echo a && echo b", "C:\\work", 5)

    assert run.call_args.args[0] == [
        "pwsh",
        "-NoProfile",
        "-Command",
        "echo a && echo b",
    ]


def test_run_powershell_legacy_replaces_and_operator():
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=True,
        shell=ShellType.POWERSHELL,
        shell_cmd=["powershell", "-NoProfile", "-Command"],
    )

    with (
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
            return_value=platform,
        ),
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.subprocess.run",
            return_value=_completed(),
        ) as run,
    ):
        tool._run_powershell("echo a && echo b", "C:\\work", 5)

    assert run.call_args.args[0] == [
        "powershell",
        "-NoProfile",
        "-Command",
        "echo a ; echo b",
    ]


def test_update_cwd_splits_on_and_for_git_bash_on_windows():
    root = os.path.normpath(os.getcwd())
    first = os.path.join(root, "first")
    second = os.path.join(root, "second")
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=True,
        shell=ShellType.BASH,
        shell_cmd=["bash", "-c"],
    )

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
        return_value=platform,
    ):
        with mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.os.path.isdir",
            side_effect=lambda path: os.path.normpath(path) in {first, second},
        ):
            tool._update_cwd(
                f"cd {first} && cd {second}",
                root,
                is_windows=True,
            )

    assert tool._cwd == second


def test_update_cwd_splits_on_and_for_pwsh_core_on_windows():
    root = os.path.normpath(os.getcwd())
    first = os.path.join(root, "first")
    second = os.path.join(root, "second")
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=True,
        shell=ShellType.POWERSHELL_CORE,
        shell_cmd=["pwsh", "-NoProfile", "-Command"],
    )

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
        return_value=platform,
    ):
        with mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.os.path.isdir",
            side_effect=lambda path: os.path.normpath(path) in {first, second},
        ):
            tool._update_cwd(
                f"cd {first} || cd {second}",
                root,
                is_windows=True,
            )

    assert tool._cwd == second


def test_update_cwd_splits_on_semicolon_for_legacy_powershell():
    root = os.path.normpath(os.getcwd())
    first = os.path.join(root, "first")
    second = os.path.join(root, "second")
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=True,
        shell=ShellType.POWERSHELL,
        shell_cmd=["powershell", "-NoProfile", "-Command"],
    )

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
        return_value=platform,
    ):
        with mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.os.path.isdir",
            side_effect=lambda path: os.path.normpath(path) in {first, second},
        ):
            tool._update_cwd(
                f"cd {first}; cd {second}",
                root,
                is_windows=True,
            )

    assert tool._cwd == second


def test_update_cwd_unix_splits_on_and_and_or_and_semicolon():
    root = os.path.normpath(os.getcwd())
    first = os.path.join(root, "first")
    second = os.path.join(root, "second")
    third = os.path.join(root, "third")
    tool = ShellTool()

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.os.path.isdir",
        side_effect=lambda path: os.path.normpath(path) in {first, second, third},
    ):
        tool._update_cwd(
            f"cd {first} && cd {second} || cd {third}",
            root,
            is_windows=False,
        )

    assert tool._cwd == third


def test_execute_local_uses_explicit_shell_executable_on_unix():
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=False,
        shell=ShellType.BASH,
        shell_cmd=["/bin/sh", "-c"],
    )

    with (
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
            return_value=platform,
        ),
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.subprocess.run",
            return_value=_completed("ok\n"),
        ) as run,
    ):
        result = tool._execute_local("echo ok", timeout=5)

    assert result == "ok"
    assert run.call_args.args[0] == ["/bin/sh", "-c", "echo ok"]
    assert "shell" not in run.call_args.kwargs


def test_execute_local_falls_back_to_shell_true_when_no_shell():
    tool = ShellTool()
    platform = _PlatformStub(
        is_windows=False,
        shell=ShellType.UNKNOWN,
        shell_cmd=[],
    )

    with (
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info",
            return_value=platform,
        ),
        mock.patch(
            "reuleauxcoder.extensions.tools.builtin.shell.subprocess.run",
            return_value=_completed("ok\n"),
        ) as run,
    ):
        result = tool._execute_local("echo ok", timeout=5)

    assert result == "ok"
    assert run.call_args.args[0] == "echo ok"
    assert run.call_args.kwargs["shell"] is True
