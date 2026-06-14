"""Shell command execution with safety checks."""

from __future__ import annotations

import os
import re
import subprocess

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import ToolRisk
from reuleauxcoder.infrastructure.platform import ShellType, get_platform_info


MAX_INTENT_CHARS = 88
GENERIC_INTENTS = {
    "执行命令",
    "执行一下",
    "运行命令",
    "运行一下",
    "跑一下",
    "run command",
    "execute command",
}


@register_tool
class ShellTool(Tool):
    name = "shell"
    risk = ToolRisk.COMMAND_EXECUTION
    permission_policy = "command_execution"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "intent": {
                "type": "string",
                "description": (
                    "A concise, user-facing sentence explaining what this command "
                    "is intended to accomplish. Do not repeat the command."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
        },
        "required": ["command", "intent"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._cwd: str | None = None

    def preflight_validate(self, **kwargs) -> str | None:
        return validate_shell_intent(kwargs.get("command"), kwargs.get("intent"))

    def execute(self, command: str, intent: str, timeout: int = 120) -> str:
        validation_error = validate_shell_intent(command, intent)
        if validation_error:
            return validation_error
        return self.run_backend(command=command, intent=intent, timeout=timeout)

    @backend_handler("remote_relay")
    def _execute_remote(self, command: str, intent: str, timeout: int = 120) -> str:
        if not isinstance(command, str) or not command:
            return "Error: shell command must be a non-empty string"
        validation_error = validate_shell_intent(command, intent)
        if validation_error:
            return validation_error
        if not isinstance(timeout, int) or timeout < 1:
            return "Error: timeout must be a positive integer"
        return self.backend.exec_tool("shell", {"command": command, "intent": intent, "timeout": timeout})

    @backend_handler("local")
    def _execute_local(self, command: str, intent: str, timeout: int = 120) -> str:
        validation_error = validate_shell_intent(command, intent)
        if validation_error:
            return validation_error
        cwd = self._cwd or os.getcwd()

        # Detect stale CWD (e.g. deleted temp dir) and reset to workspace root
        if self._cwd is not None and not os.path.isdir(self._cwd):
            self._cwd = None
            return (
                f"Error: working directory no longer exists ({cwd}). "
                "Directory has been reset to the project root."
            )

        platform_info = get_platform_info()
        shell = platform_info.get_preferred_shell()

        try:
            if platform_info.is_windows and shell in (
                ShellType.POWERSHELL,
                ShellType.POWERSHELL_CORE,
            ):
                proc = self._run_powershell(command, cwd, timeout)
            else:
                shell_cmd = platform_info.get_shell_executable()
                if shell_cmd:
                    proc = subprocess.run(
                        shell_cmd + [command],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=cwd,
                    )
                else:
                    proc = subprocess.run(
                        command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=cwd,
                    )

            if proc.returncode == 0:
                self._update_cwd(command, cwd, platform_info.is_windows)

            out = proc.stdout
            if proc.stderr:
                out += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                out += f"\n[exit code: {proc.returncode}]"
            if len(out) > 15_000:
                out = (
                    out[:6000]
                    + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
                    + out[-3000:]
                )
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {timeout}s"
        except Exception as e:
            return f"Error running command: {e}"

    def _run_powershell(
        self, command: str, cwd: str, timeout: int
    ) -> subprocess.CompletedProcess:
        """Run a command through PowerShell on Windows."""
        platform_info = get_platform_info()
        shell_cmd = platform_info.get_shell_executable()
        shell = platform_info.get_preferred_shell()

        normalized = (
            command
            if shell == ShellType.POWERSHELL_CORE
            else command.replace("&&", ";")
        )

        proc = subprocess.run(
            shell_cmd + [normalized],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc

    def _update_cwd(
        self, command: str, current_cwd: str, is_windows: bool = False
    ) -> None:
        if is_windows:
            shell = get_platform_info().get_preferred_shell()
            if shell in (ShellType.BASH, ShellType.POWERSHELL_CORE):
                parts = re.split(r"&&|\|\||[;]|\n", command)
            else:
                parts = re.split(r"[;]|\n", command)
        else:
            parts = re.split(r"&&|\|\||[;]|\n", command)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            target: str | None = None
            if part.startswith("cd "):
                target = part[3:].strip().strip("'\"")
            elif part.lower().startswith("set-location "):
                target = part[13:].strip().strip("'\"")
            elif part.lower().startswith("chdir "):
                target = part[6:].strip().strip("'\"")
            elif len(part) > 3 and part.lower().startswith("sl "):
                target = part[3:].strip().strip("'\"")

            if target:
                new_dir = os.path.normpath(
                    os.path.join(current_cwd, os.path.expanduser(target))
                )
                if os.path.isdir(new_dir):
                    self._cwd = new_dir


def validate_shell_intent(command: object, intent: object) -> str | None:
    if isinstance(command, str):
        manual_write = classify_manual_file_write(command)
        if manual_write:
            return manual_write
    if not isinstance(intent, str) or not intent.strip():
        return "Error: shell tool requires a user-facing 'intent' before execution."
    value = intent.strip()
    if len(value) < 8:
        return "Error: shell tool intent is too short; describe the action in one clear sentence."
    if len(value) > MAX_INTENT_CHARS:
        return "Error: shell tool intent is too long; keep it to one concise user-facing sentence."
    normalized_intent = _normalize_intent_for_comparison(value)
    if normalized_intent in GENERIC_INTENTS:
        return "Error: shell tool intent is too generic; describe what the command should accomplish."
    if isinstance(command, str) and normalized_intent == _normalize_intent_for_comparison(command):
        return "Error: shell tool intent must not just repeat the command."
    return None


def _normalize_intent_for_comparison(value: str) -> str:
    return re.sub(r"[\s`'\"“”‘’。.!！?？:：;；,，]+", "", value.strip().lower())


def classify_manual_file_write(command: str) -> str | None:
    normalized = str(command or "")
    patterns = (
        (r"(?im)^\s*(echo|printf)\b.*>\s*(?![&0-9])", "shell redirection"),
        (r"(?im)<<\s*['\"]?\w+['\"]?\s*>\s*", "heredoc redirection"),
        (r"(?im)\|\s*tee\s+(-a\s+)?[^\s|;]+", "tee file write"),
        (r"(?i)\bSet-Content\b", "Set-Content"),
        (r"(?i)\bOut-File\b", "Out-File"),
        (r"(?i)\bAdd-Content\b", "Add-Content"),
        (r"(?i)\bpython(?:3)?\b.*\bopen\s*\([^)]*['\"]w", "python file write"),
        (r"(?i)\bnode\b.*\bwriteFileSync\s*\(", "node file write"),
        (r"(?i)\bfs\.writeFileSync\s*\(", "node file write"),
    )
    for pattern, reason in patterns:
        if re.search(pattern, normalized):
            return (
                f"Error: shell file editing is not allowed ({reason}). "
                "Use apply_patch for file changes or draft_document_begin for long markdown documents."
            )
    return None
