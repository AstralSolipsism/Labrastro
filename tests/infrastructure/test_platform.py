from unittest import mock

import reuleauxcoder.infrastructure.platform as platform_module
from reuleauxcoder.infrastructure.platform import (
    PlatformInfo,
    ShellType,
    get_platform_info,
)


def _unix_info() -> PlatformInfo:
    info = PlatformInfo()
    info._system = "linux"
    info._is_windows = False
    info._is_linux = True
    info._is_darwin = False
    return info


def _windows_info() -> PlatformInfo:
    info = PlatformInfo()
    info._system = "windows"
    info._is_windows = True
    info._is_linux = False
    info._is_darwin = False
    return info


def test_unix_falls_back_to_sh_when_bash_missing() -> None:
    info = _unix_info()

    with mock.patch(
        "reuleauxcoder.infrastructure.platform.shutil.which",
        side_effect=lambda name: {"sh": "/bin/sh"}.get(name),
    ):
        assert info.get_preferred_shell() == ShellType.BASH

    assert info.get_shell_path() == "/bin/sh"
    assert info.get_shell_executable() == ["/bin/sh", "-c"]


def test_unix_unknown_when_no_shell_found() -> None:
    info = _unix_info()

    with mock.patch(
        "reuleauxcoder.infrastructure.platform.shutil.which",
        return_value=None,
    ):
        assert info.get_preferred_shell() == ShellType.UNKNOWN

    assert info.get_shell_path() is None
    assert info.get_shell_executable() == []


def test_windows_still_prefers_git_bash_over_sh() -> None:
    info = _windows_info()

    with (
        mock.patch(
            "reuleauxcoder.infrastructure.platform._find_git_bash",
            return_value="C:/Program Files/Git/bin/bash.exe",
        ),
        mock.patch(
            "reuleauxcoder.infrastructure.platform.shutil.which",
            side_effect=lambda name: {
                "pwsh": "C:/PowerShell/pwsh.exe",
                "sh": "C:/msys/sh.exe",
            }.get(name),
        ),
    ):
        assert info.get_preferred_shell() == ShellType.BASH

    assert info.get_shell_path() == "C:/Program Files/Git/bin/bash.exe"
    assert info.get_shell_executable() == [
        "C:/Program Files/Git/bin/bash.exe",
        "-c",
    ]


def test_platform_info_is_singleton(monkeypatch) -> None:
    monkeypatch.setattr(platform_module, "_platform_info", None)

    assert get_platform_info() is get_platform_info()
