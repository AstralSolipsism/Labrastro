#!/usr/bin/env python3
"""End-to-end smoke orchestration for Labrastro Agent Runtime persistence.

The script has two halves:
- local `remote` mode packages the current source tree, uploads it over SSH,
  and starts the server-side runner.
- server-side `server-steps` mode performs backup, deploy, Postgres wiring,
  runtime worker lifecycle checks, and persistence checks.

Secrets are read from environment variables or stdin JSON and are masked from
logs/reports.
"""

from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass, field
import datetime as dt
import fnmatch
import io
import json
import os
from pathlib import Path
import re
import secrets as secrets_lib
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


SOURCE_EXCLUDES = [
    ".git",
    ".git/**",
    ".venv",
    ".venv/**",
    ".uv-cache",
    ".uv-cache/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".pytest_tmp",
    ".pytest_tmp/**",
    ".codex_tmp_session_tests",
    ".codex_tmp_session_tests/**",
    ".agent_runtime_test_tmp",
    ".agent_runtime_test_tmp/**",
    ".rcoder",
    ".rcoder/**",
    "__pycache__",
    "**/__pycache__/**",
    "*.pyc",
    "artifacts/remote/**",
    "dist/**",
    "build/**",
    "node_modules/**",
]

REQUIRED_TABLES = [
    "labrastro_runtime_tasks",
    "labrastro_runtime_events",
    "labrastro_runtime_claims",
    "labrastro_runtime_sessions",
    "labrastro_runtime_artifacts",
    "labrastro_sessions",
    "labrastro_session_snapshots",
    "labrastro_taskflow_goals",
    "labrastro_taskflow_briefs",
    "labrastro_taskflow_issue_drafts",
    "labrastro_taskflow_task_drafts",
    "labrastro_taskflow_dispatch_decisions",
    "labrastro_taskflow_events",
    "labrastro_issues",
    "labrastro_assignments",
    "labrastro_mentions",
    "labrastro_assignment_events",
    "labrastro_github_pull_requests",
    "labrastro_github_review_comments",
    "labrastro_github_webhook_deliveries",
]

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled", "blocked", "timeout"}
SMOKE_PERSISTENCE_RE = re.compile(
    r"\n# runtime_e2e_smoke [^\n]+\npersistence:\n(?:  .*(?:\n|$))*"
)
DEFAULT_SMOKE_DATABASE = "ezcode_smoke"


def utc_timestamp() -> str:
    return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def q(value: str | Path) -> str:
    return shlex.quote(str(value))


def is_safe_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""))


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Masker:
    values: list[str] = field(default_factory=list)

    def add(self, value: str | None) -> None:
        if value and value not in self.values:
            self.values.append(value)

    def mask(self, text: str) -> str:
        masked = str(text)
        for value in sorted(self.values, key=len, reverse=True):
            if len(value) >= 4:
                masked = masked.replace(value, "***")
        masked = re.sub(
            r"(postgresql(?:\+\w+)?://[^:\s/@]+:)[^@\s]+(@)",
            r"\1***\2",
            masked,
        )
        masked = re.sub(r"(bt_[A-Za-z0-9_\-]+)", "***", masked)
        masked = re.sub(r"(pt_[A-Za-z0-9_\-]+)", "***", masked)
        return masked


def print_masked(masker: Masker, message: str) -> None:
    print(masker.mask(message), flush=True)


def should_exclude(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    for pattern in SOURCE_EXCLUDES:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def create_source_archive(repo_root: Path, timestamp: str, out_dir: Path) -> Path:
    archive_path = out_dir / f"labrastro-src-{timestamp}.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for path in sorted(repo_root.rglob("*")):
            rel = path.relative_to(repo_root).as_posix()
            if should_exclude(rel):
                continue
            if path.is_dir():
                continue
            tar.add(path, arcname=rel, recursive=False)
    return archive_path


def load_secret_from_env(env_name: str, label: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(f"{label} is required in environment variable {env_name}")
    return value


class LocalSSH:
    def __init__(self, host: str, user: str, password: str, masker: Masker):
        try:
            import paramiko  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised by CLI usage
            raise SystemExit(
                "paramiko is required for remote mode. Run with `uv run --with paramiko ...`."
            ) from exc

        self._paramiko = paramiko
        self.masker = masker
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=host,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
        )

    def close(self) -> None:
        self.client.close()

    def run(
        self,
        command: str,
        *,
        input_text: str | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        stdin, stdout, stderr = self.client.exec_command(
            command,
            get_pty=False,
            timeout=timeout,
        )
        if input_text is not None:
            stdin.write(input_text)
            stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def run_checked(
        self,
        command: str,
        *,
        input_text: str | None = None,
        timeout: int | None = None,
    ) -> str:
        code, out, err = self.run(command, input_text=input_text, timeout=timeout)
        if out:
            print_masked(self.masker, out.rstrip())
        if err:
            print_masked(self.masker, err.rstrip())
        if code != 0:
            raise RuntimeError(
                self.masker.mask(
                    f"remote command failed exit={code}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                )
            )
        return out

    def put(self, local: Path, remote: str) -> None:
        with self.client.open_sftp() as sftp:
            sftp.put(str(local), remote)

    def put_bytes(self, data: bytes, remote: str, mode: int = 0o600) -> None:
        with self.client.open_sftp() as sftp:
            with sftp.file(remote, "wb") as handle:
                handle.write(data)
            sftp.chmod(remote, mode)


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str


class ServerRunner:
    def __init__(self, args: argparse.Namespace, secrets_payload: dict[str, str]):
        self.args = args
        self.timestamp = args.timestamp or utc_timestamp()
        self.root = Path(args.root)
        self.host_container = args.host_container
        self.pg_container = args.pg_container
        self.pg_user = args.pg_user
        self.pg_password = secrets_payload.get("pg_password") or os.environ.get(
            args.pg_password_env or ""
        )
        if not self.pg_password:
            raise SystemExit("Postgres password is required")
        self.masker = Masker([self.pg_password])
        self.db_name = args.database_name or DEFAULT_SMOKE_DATABASE
        if not is_safe_identifier(self.db_name):
            raise SystemExit(f"Unsafe database name: {self.db_name}")
        if not is_safe_identifier(self.pg_user):
            raise SystemExit(f"Unsafe Postgres user: {self.pg_user}")
        self.incoming = self.root / "incoming"
        self.backup_dir = self.root / "backups" / self.timestamp
        self.smoke_dir = self.root / "runtime-smoke" / self.timestamp
        self.report: dict[str, Any] = {
            "timestamp": self.timestamp,
            "root": str(self.root),
            "host_container": self.host_container,
            "pg_container": self.pg_container,
            "database": self.db_name,
            "steps": [],
            "checks": {},
            "tasks": {},
            "flows": {},
            "paths": {
                "backup": str(self.backup_dir),
                "smoke": str(self.smoke_dir),
            },
        }
        self.access_token = ""
        self.refresh_token = ""
        self.auth_username = secrets_payload.get("auth_username") or os.environ.get(
            "LABRASTRO_SUPERADMIN_USERNAME", "admin"
        )
        self.auth_password = secrets_payload.get("auth_password") or os.environ.get(
            "LABRASTRO_SUPERADMIN_PASSWORD", ""
        )
        self.masker.add(self.auth_password)
        self.config_path: Path | None = None
        self.compose_path: Path | None = None
        self.compose_service = ""
        self.host_url = "http://127.0.0.1:8765"
        self.original_agent_runtime: dict[str, Any] = {}
        self.worker_proc: subprocess.Popen[str] | None = None

    def log(self, message: str) -> None:
        print_masked(self.masker, f"[runtime-smoke] {message}")

    def record_step(self, name: str, status: str = "ok", **extra: Any) -> None:
        safe_extra = json.loads(self.masker.mask(json.dumps(extra, ensure_ascii=False)))
        self.report["steps"].append(
            {
                "name": name,
                "status": status,
                "at": dt.datetime.utcnow().isoformat() + "Z",
                **safe_extra,
            }
        )

    def run_cmd(
        self,
        argv: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=proc_env,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            raise RuntimeError(
                self.masker.mask(
                    "command failed "
                    + json.dumps(argv)
                    + f"\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )
            )
        return result

    def bash(self, script: str, *, check: bool = True, timeout: int | None = None) -> CommandResult:
        return self.run_cmd(["bash", "-lc", script], check=check, timeout=timeout)

    def http_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 20,
    ) -> dict[str, Any]:
        data = None
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            self.host_url + path,
            data=data,
            headers=req_headers,
            method=method,
        )
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body or "{}")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                self.masker.mask(
                    f"HTTP {method} {path} failed status={exc.code}: {body}"
                )
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"HTTP {method} {path} failed: {exc}") from exc

    def admin_json(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        retry_on_unauthorized: bool = True,
    ) -> dict[str, Any]:
        try:
            return self.http_json(
                "POST",
                path,
                payload or {},
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=60,
            )
        except RuntimeError as exc:
            if retry_on_unauthorized and "status=401" in str(exc):
                self.record_step("admin_relogin", path=path)
                self.login()
                return self.admin_json(
                    path,
                    payload,
                    retry_on_unauthorized=False,
                )
            raise

    def login(self) -> None:
        if not self.auth_password:
            raise RuntimeError("LABRASTRO_SUPERADMIN_PASSWORD is required for auth smoke login")
        body = self.http_json(
            "POST",
            "/remote/auth/login",
            {
                "username": self.auth_username,
                "password": self.auth_password,
                "device_label": "runtime_e2e_smoke",
            },
            timeout=60,
        )
        self.access_token = str(body.get("access_token") or "")
        self.refresh_token = str(body.get("refresh_token") or "")
        self.masker.add(self.access_token)
        self.masker.add(self.refresh_token)
        if not self.access_token:
            raise RuntimeError("auth login did not return access token")

    def peer_post(
        self, path: str, peer_token: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = dict(payload or {})
        body["peer_token"] = peer_token
        return self.http_json("POST", path, body, timeout=60)

    def peer_get(
        self,
        path: str,
        peer_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = {"peer_token": peer_token, **dict(params or {})}
        separator = "&" if "?" in path else "?"
        return self.http_json("GET", path + separator + urlencode(query), timeout=60)

    def expect_peer_failure(
        self,
        method: str,
        path: str,
        peer_token: str,
        *,
        status: int,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        try:
            if method.upper() == "GET":
                self.peer_get(path, peer_token, params)
            else:
                self.peer_post(path, peer_token, payload)
        except RuntimeError as exc:
            message = str(exc)
            if f"status={status}" not in message:
                raise RuntimeError(
                    f"expected HTTP {status} for {method} {path}, got: {message}"
                ) from exc
            return self.masker.mask(message)
        raise RuntimeError(f"expected HTTP {status} for {method} {path}, got success")

    def psql(self, sql: str, *, database: str = "postgres", check: bool = True) -> CommandResult:
        return self.run_cmd(
            [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={self.pg_password}",
                self.pg_container,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                self.pg_user,
                "-d",
                database,
                "-tAc",
                sql,
            ],
            check=check,
            timeout=60,
        )

    def docker_inspect(self, name: str) -> dict[str, Any]:
        result = self.run_cmd(["docker", "inspect", name], timeout=30)
        data = json.loads(result.stdout)
        if not data:
            raise RuntimeError(f"docker inspect returned no data for {name}")
        return data[0]

    def container_env(self, name: str) -> dict[str, str]:
        info = self.docker_inspect(name)
        env: dict[str, str] = {}
        for entry in info.get("Config", {}).get("Env", []) or []:
            if "=" in entry:
                key, value = entry.split("=", 1)
                env[key] = value
        return env

    def container_config_path(self) -> str:
        env = self.container_env(self.host_container)
        configured = env.get("RCODER_CONFIG_PATH")
        if configured:
            return configured
        if self.config_path is not None:
            host_config = str(self.config_path)
            info = self.docker_inspect(self.host_container)
            for mount in info.get("Mounts", []) or []:
                source = str(mount.get("Source") or "")
                destination = str(mount.get("Destination") or "")
                if source and destination and (
                    host_config == source or host_config.startswith(source.rstrip("/") + "/")
                ):
                    rel = host_config[len(source) :].lstrip("/")
                    return destination.rstrip("/") + ("/" + rel if rel else "")
        return "/app/.rcoder/config.host.yaml"

    def preflight(self) -> None:
        self.log("preflight")
        self.bash("command -v docker >/dev/null")
        self.run_cmd(["docker", "inspect", self.host_container], timeout=30)
        self.run_cmd(["docker", "inspect", self.pg_container], timeout=30)
        for path in (self.root, self.incoming):
            path.mkdir(parents=True, exist_ok=True)
        self.config_path = self.discover_config_path()
        self.compose_path = self.discover_compose_path()
        self.compose_service = self.discover_compose_service()
        self.report["paths"].update(
            {
                "config": str(self.config_path),
                "compose": str(self.compose_path),
                "compose_service": self.compose_service,
            }
        )
        self.record_step("preflight", config=str(self.config_path), compose=str(self.compose_path))

    def discover_config_path(self) -> Path:
        candidates = [
            self.root / "config" / "config.host.yaml",
            self.root / "config.host.yaml",
            self.root / ".rcoder" / "config.host.yaml",
            self.root / "src" / ".rcoder" / "config.host.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        info = self.docker_inspect(self.host_container)
        env = self.container_env(self.host_container)
        container_config = env.get("RCODER_CONFIG_PATH", "/app/.rcoder/config.host.yaml")
        mounts = info.get("Mounts", []) or []
        for mount in mounts:
            destination = mount.get("Destination") or ""
            source = mount.get("Source") or ""
            if container_config == destination or container_config.startswith(destination.rstrip("/") + "/"):
                rel = container_config[len(destination) :].lstrip("/")
                host_path = Path(source) / rel
                if host_path.exists():
                    return host_path
        raise RuntimeError("Unable to discover active config.host.yaml path")

    def discover_compose_path(self) -> Path:
        candidates = [
            self.root / "docker-compose.yml",
            self.root / "compose.yml",
            self.root / "src" / "docker" / "docker-compose.yml",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("Unable to discover docker compose file")

    def discover_compose_service(self) -> str:
        info = self.docker_inspect(self.host_container)
        labels = info.get("Config", {}).get("Labels", {}) or {}
        service = labels.get("com.docker.compose.service")
        services = self.run_cmd(
            ["docker", "compose", "-f", str(self.compose_path), "config", "--services"],
            timeout=30,
        ).stdout.splitlines()
        services = [item.strip() for item in services if item.strip()]
        if service and service in services:
            return service
        if self.host_container in services:
            return self.host_container
        for candidate in ("labrastro-host", "rcoder-host", "reuleauxcoder-host"):
            if candidate in services:
                return candidate
        if len(services) == 1:
            return services[0]
        raise RuntimeError(f"Unable to choose compose service from: {services}")

    def backup(self) -> None:
        self.log("backup")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        backup_targets = [
            "src",
            "config",
            "config-home",
            "sessions",
            "docker-compose.yml",
            "Dockerfile.runtime",
        ]
        for target in backup_targets:
            path = self.root / target
            if path.exists():
                self.bash(f"cp -a {q(path)} {q(self.backup_dir / target)}")
        self.run_cmd(
            ["docker", "inspect", self.host_container],
            timeout=30,
        )
        (self.backup_dir / f"{self.host_container}.inspect.json").write_text(
            self.run_cmd(["docker", "inspect", self.host_container], timeout=30).stdout,
            encoding="utf-8",
        )
        (self.backup_dir / f"{self.pg_container}.inspect.json").write_text(
            self.run_cmd(["docker", "inspect", self.pg_container], timeout=30).stdout,
            encoding="utf-8",
        )
        image = self.docker_inspect(self.host_container).get("Image")
        if image:
            for tag in (
                f"labrastro-host:runtime.backup-{self.timestamp}",
                f"labrastro-host:test.backup-{self.timestamp}",
            ):
                self.run_cmd(["docker", "tag", image, tag], check=False, timeout=60)
        self.record_step("backup", backup_dir=str(self.backup_dir))

    def create_database(self) -> str:
        self.log("create postgres smoke database")
        self.psql(
            f"DROP DATABASE IF EXISTS {self.db_name} WITH (FORCE)",
            database="postgres",
        )
        self.psql(f"CREATE DATABASE {self.db_name}", database="postgres")
        dsn = f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_container}:5432/{self.db_name}"
        self.masker.add(dsn)
        self.record_step("create_database", database=self.db_name)
        return dsn

    def ensure_network(self) -> None:
        host_info = self.docker_inspect(self.host_container)
        pg_info = self.docker_inspect(self.pg_container)
        host_networks = set((host_info.get("NetworkSettings", {}).get("Networks") or {}).keys())
        pg_networks = set((pg_info.get("NetworkSettings", {}).get("Networks") or {}).keys())
        shared = host_networks & pg_networks
        if shared:
            self.record_step("network", shared=sorted(shared))
            return
        if not pg_networks:
            raise RuntimeError("Postgresql container is not attached to any Docker network")
        network = sorted(pg_networks)[0]
        self.run_cmd(
            ["docker", "network", "connect", network, self.host_container],
            check=False,
            timeout=30,
        )
        self.record_step("network", connected=network)

    def append_persistence_config(self, database_url: str) -> None:
        assert self.config_path is not None
        self.log("patch persistence config")
        original = self.config_path.read_text(encoding="utf-8")
        cleaned = SMOKE_PERSISTENCE_RE.sub("\n", original).rstrip()
        (self.backup_dir / "active-config-before-persistence.yaml").write_text(
            original,
            encoding="utf-8",
        )
        block = textwrap.dedent(
            f"""

            # runtime_e2e_smoke {self.timestamp}
            persistence:
              backend: postgres
              database_url: {database_url}
              auto_migrate: true
              runtime_enabled: true
              sessions_enabled: true
              retention_days: 0
            """
        )
        self.config_path.write_text(cleaned + block + "\n", encoding="utf-8")
        self.record_step("patch_config", config=str(self.config_path), database_url="***")

    def cleanup_generated_global_example_config(self) -> None:
        home_config = self.root / "home" / ".rcoder" / "config.yaml"
        if not home_config.exists():
            return
        text = home_config.read_text(encoding="utf-8", errors="replace")
        looks_generated = (
            "YOUR_API_KEY_HERE" in text
            or "your-api-key-here" in text
            or "your-deepseek-api-key-here" in text
            or "api_key: sk-" not in text
            and "models:" in text
            and "profiles:" in text
        )
        if not looks_generated:
            self.record_step("global_example_config", "kept", path=str(home_config))
            return
        target = self.backup_dir / "generated-global-example-config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(home_config), str(target))
        self.record_step(
            "global_example_config",
            "moved",
            path=str(home_config),
            backup=str(target),
        )

    def resolve_config_secret(self, key: str) -> str:
        assert self.config_path is not None
        text = self.config_path.read_text(encoding="utf-8")
        matches = re.findall(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text, flags=re.M)
        if not matches:
            return ""
        raw = matches[-1].strip().strip('"').strip("'")
        env_match = re.fullmatch(r"\$\{([^}:]+)(?::-[^}]*)?\}", raw)
        if not env_match:
            return raw
        env_name = env_match.group(1)
        env = self.container_env(self.host_container)
        return env.get(env_name, "")

    def deploy_source(self) -> None:
        archive = Path(self.args.source_archive)
        if not archive.exists():
            raise RuntimeError(f"source archive does not exist: {archive}")
        self.log("deploy source and rebuild container")
        stage = self.root / f"src.stage-{self.timestamp}"
        previous = self.backup_dir / "src.previous"
        if stage.exists():
            self.bash(f"rm -rf {q(stage)}")
        stage.mkdir(parents=True)
        self.bash(f"tar -xzf {q(archive)} -C {q(stage)}", timeout=120)
        src = self.root / "src"
        if src.exists():
            self.bash(f"mv {q(src)} {q(previous)}", timeout=120)
        self.bash(f"mv {q(stage)} {q(src)}", timeout=120)
        base_dockerfile = src / "docker" / "Dockerfile"
        if base_dockerfile.exists():
            base_image = "labrastro-host:test"
            runtime_dockerfile = self.root / "Dockerfile.runtime"
            if runtime_dockerfile.exists():
                match = re.search(
                    r"(?im)^\s*FROM\s+([^\s]+)",
                    runtime_dockerfile.read_text(encoding="utf-8", errors="replace"),
                )
                if match:
                    base_image = match.group(1)
            self.run_cmd(
                [
                    "docker",
                    "build",
                    "-t",
                    base_image,
                    "-f",
                    str(base_dockerfile),
                    str(src),
                ],
                timeout=1800,
            )
            self.record_step("build_base_image", image=base_image)
        self.run_cmd(
            [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "build",
                self.compose_service,
            ],
            timeout=1800,
        )
        self.record_step("deploy_source", compose_service=self.compose_service)

    def start_host(self) -> None:
        self.run_cmd(
            [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "up",
                "-d",
                self.compose_service,
            ],
            timeout=300,
        )
        self.ensure_network()
        self.run_cmd(["docker", "restart", self.host_container], timeout=120)
        self.wait_http_ready()
        self.record_step("start_host", compose_service=self.compose_service)

    def wait_http_ready(self, timeout_sec: int = 120) -> None:
        deadline = time.time() + timeout_sec
        last_error = ""
        while time.time() < deadline:
            try:
                body = self.http_json("GET", "/remote/capabilities", timeout=5)
                if body.get("ok"):
                    return
            except Exception as exc:  # noqa: BLE001 - logged after timeout
                last_error = str(exc)
            time.sleep(2)
        logs = self.run_cmd(
            ["docker", "logs", "--tail", "120", self.host_container],
            check=False,
            timeout=30,
        )
        raise RuntimeError(
            self.masker.mask(
                f"host did not become ready: {last_error}\nLOGS:\n{logs.stdout}\n{logs.stderr}"
            )
        )

    def verify_service(self) -> None:
        self.log("verify service and database")
        capabilities = self.http_json("GET", "/remote/capabilities")
        db_status = self.run_cmd(
            [
                "docker",
                "exec",
                self.host_container,
                "rcoder",
                "--config",
                self.container_config_path(),
                "db",
                "status",
            ],
            timeout=60,
        ).stdout.strip()
        existing_tables = self.psql(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'labrastro_%' ORDER BY tablename",
            database=self.db_name,
        ).stdout.splitlines()
        existing = {item.strip() for item in existing_tables if item.strip()}
        missing = [table for table in REQUIRED_TABLES if table not in existing]
        if missing:
            raise RuntimeError(f"missing Postgres tables: {missing}")
        self.report["checks"]["capabilities"] = capabilities
        self.report["checks"]["db_status"] = db_status
        self.report["checks"]["tables"] = sorted(existing)
        self.record_step("verify_service", db_status=db_status)

    def setup_smoke_agent(self) -> tuple[str, str]:
        self.log("configure smoke runtime profile and agent")
        read = self.admin_json("/remote/admin/server-settings/read")
        settings = read.get("settings") or read
        runtime = copy.deepcopy(settings.get("agent_runtime") or {})
        removed_stale = self.strip_smoke_agent_runtime_entries(runtime)
        self.original_agent_runtime = copy.deepcopy(runtime)
        profile_id = f"zz_smoke_runtime_{self.timestamp.lower()}"
        agent_id = f"zz_smoke_agent_{self.timestamp.lower()}"
        shadow_agent_id = f"zz_smoke_agent_shadow_{self.timestamp.lower()}"
        runtime.setdefault("max_running_agents", 4)
        runtime.setdefault("max_shells_per_agent", 1)
        runtime.setdefault("runtime_profiles", {})
        runtime.setdefault("agents", {})
        runtime["runtime_profiles"][profile_id] = {
            "executor": "fake",
            "execution_location": "daemon_worktree",
            "runtime_home_policy": "per_task",
            "credential_refs": {"model": "smoke_model_ref"},
        }
        runtime["agents"][agent_id] = {
            "name": "Runtime Smoke Agent",
            "runtime_profile": profile_id,
            "capabilities": ["read_repo", "code_review"],
            "prompt": {"system_append": "Runtime smoke test agent."},
            "max_concurrent_tasks": 1,
        }
        runtime["agents"][shadow_agent_id] = {
            "name": "Runtime Smoke Agent",
            "runtime_profile": profile_id,
            "capabilities": ["shadow_only"],
            "prompt": {"system_append": "Runtime smoke ambiguous mention shadow."},
            "max_concurrent_tasks": 1,
        }
        update = self.admin_json(
            "/remote/admin/server-settings/update",
            {
                "agent_runtime_update_mode": "replace",
                "agent_runtime": runtime,
            },
        )
        if update.get("ok") is not True:
            raise RuntimeError(f"server-settings/update failed: {update}")
        self.report["checks"]["smoke_agent"] = {
            "profile_id": profile_id,
            "agent_id": agent_id,
            "shadow_agent_id": shadow_agent_id,
            "removed_stale": removed_stale,
        }
        self.record_step(
            "setup_smoke_agent",
            profile_id=profile_id,
            agent_id=agent_id,
            removed_stale=removed_stale,
        )
        return profile_id, agent_id

    def strip_smoke_agent_runtime_entries(self, runtime: dict[str, Any]) -> dict[str, int]:
        profiles = runtime.setdefault("runtime_profiles", {})
        agents = runtime.setdefault("agents", {})
        removed_profiles = 0
        removed_agents = 0
        if isinstance(profiles, dict):
            for key in list(profiles):
                if str(key).startswith("zz_smoke_runtime_"):
                    del profiles[key]
                    removed_profiles += 1
        if isinstance(agents, dict):
            for key in list(agents):
                if str(key).startswith("zz_smoke_agent_"):
                    del agents[key]
                    removed_agents += 1
        return {"runtime_profiles": removed_profiles, "agents": removed_agents}

    def restore_agent_runtime(self) -> None:
        if not self.original_agent_runtime:
            return
        self.log("restore original agent_runtime config")
        try:
            result = self.admin_json(
                "/remote/admin/server-settings/update",
                {
                    "agent_runtime_update_mode": "replace",
                    "agent_runtime": self.original_agent_runtime,
                },
            )
            if result.get("ok") is not True:
                self.record_step("restore_agent_runtime", "failed", response=result)
                raise RuntimeError(f"restore_agent_runtime failed: {result}")
            else:
                self.record_step("restore_agent_runtime")
        except Exception as exc:  # noqa: BLE001
            self.record_step("restore_agent_runtime", "failed", error=str(exc))
            raise

    def create_git_fixture(self) -> dict[str, str]:
        self.log("create git fixture")
        root = self.smoke_dir
        origin = root / "origin.git"
        repo = root / "repo"
        seed = root / "seed"
        root.mkdir(parents=True, exist_ok=True)
        for path in (origin, repo, seed):
            if path.exists():
                self.bash(f"rm -rf {q(path)}")
        self.run_cmd(["git", "init", "--bare", str(origin)], timeout=60)
        self.run_cmd(["git", "init", str(seed)], timeout=60)
        self.run_cmd(["git", "config", "user.email", "smoke@example.invalid"], cwd=seed)
        self.run_cmd(["git", "config", "user.name", "Labrastro Smoke"], cwd=seed)
        (seed / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.run_cmd(["git", "add", "tracked.txt"], cwd=seed)
        self.run_cmd(["git", "commit", "-m", "initial"], cwd=seed, timeout=60)
        self.run_cmd(["git", "branch", "-M", "main"], cwd=seed)
        self.run_cmd(["git", "remote", "add", "origin", str(origin)], cwd=seed)
        self.run_cmd(["git", "push", "-u", "origin", "main"], cwd=seed, timeout=60)
        self.run_cmd(["git", "clone", str(origin), str(repo)], timeout=60)
        self.run_cmd(["git", "config", "user.email", "smoke@example.invalid"], cwd=repo)
        self.run_cmd(["git", "config", "user.name", "Labrastro Smoke"], cwd=repo)
        self.record_step("create_git_fixture", repo=str(repo), origin=str(origin))
        return {
            "root": str(root),
            "origin": str(origin),
            "repo": str(repo),
            "repo_url": "file://" + str(origin),
        }

    def install_fake_gh(self) -> dict[str, str]:
        bin_dir = self.smoke_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        gh = bin_dir / "gh"
        log_path = self.smoke_dir / "gh.log"
        script = textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$@" >> {q(log_path)}
            if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
              exit 1
            fi
            if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
              if [ -f {q(self.smoke_dir / "gh_sleep")} ]; then
                sleep "$(cat {q(self.smoke_dir / "gh_sleep")})"
              fi
              echo "https://example.test/pr/labrastro-runtime-smoke"
              exit 0
            fi
            echo "unsupported fake gh command: $@" >&2
            exit 1
            """
        )
        gh.write_text(script, encoding="utf-8")
        gh.chmod(0o755)
        self.record_step("install_fake_gh", path=str(gh))
        return {"bin": str(bin_dir), "log": str(log_path)}

    def bootstrap_token(self) -> str:
        body = self.http_json(
            "POST",
            "/remote/auth/bootstrap-token",
            {},
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=20,
        )
        token = str(body.get("bootstrap_token") or "")
        if not token:
            raise RuntimeError("bootstrap token not returned by auth API")
        self.masker.add(token)
        return token

    def download_peer(self) -> Path:
        target = self.smoke_dir / "rcoder-peer"
        if target.exists():
            return target
        with urlrequest.urlopen(
            self.host_url + "/remote/artifacts/linux/amd64/rcoder-peer",
            timeout=120,
        ) as resp:
            target.write_bytes(resp.read())
        target.chmod(0o755)
        self.record_step("download_peer", path=str(target))
        return target

    def start_worker(
        self,
        fixture: dict[str, str],
        fake_gh: dict[str, str],
        *,
        worker_id: str,
    ) -> Path:
        peer = self.download_peer()
        token = self.bootstrap_token()
        peer_info = self.smoke_dir / f"{worker_id}.peer.json"
        stdout_path = self.smoke_dir / f"{worker_id}.stdout.log"
        stderr_path = self.smoke_dir / f"{worker_id}.stderr.log"
        env = os.environ.copy()
        env["PATH"] = fake_gh["bin"] + os.pathsep + env.get("PATH", "")
        proc = subprocess.Popen(
            [
                str(peer),
                "--host",
                self.host_url,
                "--bootstrap-token",
                token,
                "--cwd",
                fixture["repo"],
                "--workspace-root",
                fixture["repo"],
                "--peer-info-file",
                str(peer_info),
                "--poll-interval",
                "200ms",
                "--agent-runtime",
                "--runtime-worker-id",
                worker_id,
            ],
            stdout=stdout_path.open("w", encoding="utf-8"),
            stderr=stderr_path.open("w", encoding="utf-8"),
            text=True,
            env=env,
        )
        self.worker_proc = proc
        deadline = time.time() + 30
        while time.time() < deadline:
            if peer_info.exists():
                try:
                    data = json.loads(peer_info.read_text(encoding="utf-8"))
                    self.masker.add(data.get("peer_token"))
                    self.masker.add(data.get("peer_id"))
                    break
                except Exception:
                    pass
            if proc.poll() is not None:
                raise RuntimeError(
                    f"worker exited early code={proc.returncode}; stderr={stderr_path.read_text(encoding='utf-8', errors='replace')}"
                )
            time.sleep(0.2)
        else:
            raise RuntimeError("worker did not register within timeout")
        self.record_step("start_worker", worker_id=worker_id)
        return peer_info

    def read_peer_info(self, peer_info: Path) -> dict[str, Any]:
        data = json.loads(peer_info.read_text(encoding="utf-8"))
        self.masker.add(data.get("peer_token"))
        self.masker.add(data.get("peer_id"))
        return data

    def register_manual_peer(
        self,
        fixture: dict[str, str],
        *,
        suffix: str,
        capabilities: list[str] | None = None,
    ) -> str:
        token = self.bootstrap_token()
        body = self.http_json(
            "POST",
            "/remote/register",
            {
                "bootstrap_token": token,
                "cwd": fixture["repo"],
                "workspace_root": fixture["repo"],
                "capabilities": capabilities or ["agent_runtime"],
                "metadata": {"runtime_smoke_peer": suffix},
            },
        )
        peer_token = str((body.get("payload") or {}).get("peer_token") or "")
        if not peer_token:
            raise RuntimeError(f"manual peer registration failed: {body}")
        self.masker.add(peer_token)
        self.record_step("register_manual_peer", suffix=suffix)
        return peer_token

    def stop_worker(self) -> None:
        proc = self.worker_proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
        self.record_step("stop_worker", returncode=proc.returncode)
        self.worker_proc = None

    def submit_task(
        self,
        task_id: str,
        agent_id: str,
        fixture: dict[str, str],
        *,
        suffix: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = self.runtime_task_metadata(fixture, suffix=suffix)
        if extra_metadata:
            metadata.update(extra_metadata)
        body = self.admin_json(
            "/remote/admin/runtime/submit",
            {
                "task_id": task_id,
                "issue_id": f"runtime-smoke-{self.timestamp}",
                "agent_id": agent_id,
                "prompt": f"runtime smoke {suffix}",
                "metadata": metadata,
            },
        )
        if body.get("ok") is not True:
            raise RuntimeError(f"runtime submit failed: {body}")
        self.record_step("submit_task", task_id=task_id)
        return body

    def runtime_task_metadata(
        self,
        fixture: dict[str, str],
        *,
        suffix: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "repo_url": fixture["repo_url"],
            "workspace_id": f"runtime-smoke-{self.timestamp}",
            "prompt_files": {"AGENTS.md": "Runtime smoke test only.\n"},
            "fake_files": {f"agent-output-{suffix}.txt": f"created by {suffix}\n"},
            "pr_body": f"Runtime smoke {suffix}",
            "pr_title": f"Runtime smoke {suffix}",
            "commit_message": f"agent: runtime smoke {suffix}",
            "pr_enabled": False,
        }
        if extra:
            metadata.update(extra)
        return metadata

    def load_task(self, task_id: str) -> dict[str, Any]:
        return self.admin_json(
            "/remote/admin/runtime/tasks/load",
            {"task_id": task_id, "event_limit": 200},
        )

    def poll_task(self, task_id: str, *, timeout_sec: int = 90) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        detail: dict[str, Any] = {}
        while time.time() < deadline:
            detail = self.load_task(task_id)
            status = str((detail.get("task") or {}).get("status") or "")
            if status in TERMINAL_STATUSES:
                return detail
            time.sleep(1)
        raise RuntimeError(f"task {task_id} did not finish, last detail={detail}")

    def claim_task_until(
        self,
        *,
        peer_token: str,
        task_id: str,
        worker_id: str,
        executors: list[str],
        timeout_sec: int = 60,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        last_detail: dict[str, Any] = {}
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            claim = self.http_json(
                "POST",
                "/remote/runtime/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": worker_id,
                    "executors": executors,
                    "wait_sec": 1,
                },
            ).get("claim")
            if claim:
                claimed_task = (claim.get("task") or {}).get("id")
                if claimed_task != task_id:
                    raise RuntimeError(
                        f"manual worker claimed unexpected task {claimed_task}; expected {task_id}"
                    )
                self.record_step(
                    "claim_task_until",
                    task_id=task_id,
                    worker_id=worker_id,
                    attempts=attempts,
                )
                return claim
            last_detail = self.load_task(task_id)
            status = str((last_detail.get("task") or {}).get("status") or "")
            if status in TERMINAL_STATUSES:
                raise RuntimeError(
                    f"task {task_id} reached terminal status before manual claim: {status}"
                )
            time.sleep(1)
        raise RuntimeError(
            f"manual recovery claim returned no claim for {task_id}; last detail={last_detail}"
        )

    def event_labels(self, detail: dict[str, Any]) -> set[str]:
        labels: set[str] = set()
        for event in detail.get("events") or []:
            event_type = str(event.get("type") or "")
            if event_type:
                labels.add(event_type)
            payload = event.get("payload") or {}
            data = payload.get("data") if isinstance(payload, dict) else {}
            if isinstance(data, dict) and data.get("status"):
                labels.add(str(data["status"]))
            if event_type == "text":
                labels.add("text")
        return labels

    def assert_task_labels(self, task_id: str, detail: dict[str, Any], required: set[str]) -> None:
        labels = self.event_labels(detail)
        missing = sorted(required - labels)
        if missing:
            raise RuntimeError(f"task {task_id} missing event labels: {missing}; got={sorted(labels)}")

    def run_happy_cancel_retry(self, fixture: dict[str, str], fake_gh: dict[str, str], agent_id: str) -> None:
        worker_id = f"smoke-{self.timestamp.lower()}"
        self.start_worker(fixture, fake_gh, worker_id=worker_id)
        try:
            happy_id = f"task-happy-{self.timestamp.lower()}"
            self.submit_task(happy_id, agent_id, fixture, suffix="happy")
            happy = self.poll_task(happy_id, timeout_sec=120)
            if happy["task"]["status"] != "completed":
                raise RuntimeError(f"happy task did not complete: {happy['task']}")
            self.assert_task_labels(
                happy_id,
                happy,
                {"queued", "claimed", "worktree_ready", "text", "branch_pushed"},
            )
            artifacts = {item.get("type"): item for item in happy.get("artifacts") or []}
            if "branch" not in artifacts:
                raise RuntimeError(f"happy task missing artifacts: {artifacts}")
            workdir = Path((happy.get("session") or {}).get("workdir") or "")
            if not (workdir / "agent-output-happy.txt").exists():
                raise RuntimeError(f"happy task workdir output missing: {workdir}")
            self.report["tasks"]["happy"] = self.summarize_task(happy)

            cancel_id = f"task-cancel-{self.timestamp.lower()}"
            self.submit_task(
                cancel_id,
                agent_id,
                fixture,
                suffix="cancel",
                extra_metadata={"fake_sleep_sec": 5},
            )
            self.wait_for_label(cancel_id, "running", timeout_sec=90)
            cancel_body = self.admin_json(
                "/remote/admin/runtime/cancel",
                {"task_id": cancel_id, "reason": "runtime_smoke_cancel"},
            )
            if cancel_body.get("ok") is not True:
                raise RuntimeError(f"cancel failed: {cancel_body}")
            cancel_detail = self.poll_task(cancel_id, timeout_sec=90)
            status = str(cancel_detail["task"]["status"])
            if status not in {"cancelled", "canceled"}:
                raise RuntimeError(f"cancel task status mismatch: {cancel_detail['task']}")
            self.report["tasks"]["cancel"] = self.summarize_task(cancel_detail)

            retry_id = f"task-retry-{self.timestamp.lower()}"
            retry = self.admin_json(
                "/remote/admin/runtime/retry",
                {"task_id": cancel_id, "new_task_id": retry_id},
            )
            if retry.get("ok") is not True:
                raise RuntimeError(f"retry failed: {retry}")
            retry_detail = self.poll_task(retry_id, timeout_sec=120)
            if retry_detail["task"]["status"] != "completed":
                raise RuntimeError(f"retry task did not complete: {retry_detail['task']}")
            self.report["tasks"]["retry"] = self.summarize_task(retry_detail)
            self.record_step("worker_lifecycle")
        finally:
            (self.smoke_dir / "gh_sleep").unlink(missing_ok=True)
            self.stop_worker()

    def run_taskflow_issue_mention_e2e(
        self,
        fixture: dict[str, str],
        fake_gh: dict[str, str],
        agent_id: str,
    ) -> None:
        self.log("run taskflow / issue assignment / mention smoke")
        worker_id = f"flow-{self.timestamp.lower()}"
        peer_info = self.start_worker(fixture, fake_gh, worker_id=worker_id)
        peer_token = str(self.read_peer_info(peer_info).get("peer_token") or "")
        if not peer_token:
            raise RuntimeError("worker peer token missing")
        try:
            self.run_taskflow_e2e(peer_token, fixture, agent_id)
            self.run_issue_assignment_mention_e2e(peer_token, fixture, agent_id)
            self.record_step("taskflow_issue_mention_e2e")
        finally:
            self.stop_worker()

    def run_taskflow_e2e(
        self, peer_token: str, fixture: dict[str, str], agent_id: str
    ) -> None:
        suffix = self.timestamp.lower()
        goal_id = f"goal-smoke-{suffix}"
        issue_draft_id = f"issue-draft-smoke-{suffix}"
        draft_id = f"task-draft-smoke-{suffix}"
        goal = self.peer_post(
            "/remote/taskflow/goals",
            peer_token,
            {
                "goal_id": goal_id,
                "title": "Runtime smoke Taskflow goal",
                "prompt": "Turn this smoke request into a confirmed runtime task.",
                "metadata": {"runtime_smoke": self.timestamp},
            },
        )["goal"]
        if goal["id"] != goal_id:
            raise RuntimeError(f"taskflow goal id mismatch: {goal}")

        brief = self.peer_post(
            f"/remote/taskflow/goals/{goal_id}/brief",
            peer_token,
            {
                "summary": "Smoke brief for Taskflow dispatch.",
                "decision_points": [
                    {
                        "id": f"decision-smoke-{suffix}",
                        "question": "Run fake executor smoke?",
                        "status": "resolved",
                    }
                ],
                "issue_drafts": [
                    {
                        "id": issue_draft_id,
                        "title": "Runtime smoke issue draft",
                        "description": "Dispatch through Taskflow into Runtime.",
                        "task_drafts": [
                            {
                                "id": draft_id,
                                "title": "Runtime smoke Taskflow task",
                                "prompt": "runtime smoke taskflow",
                                "required_capabilities": ["code_review"],
                                "preferred_capabilities": ["read_repo"],
                                "task_type": "code_review",
                                "execution_location": "daemon_worktree",
                                "repo_url": fixture["repo_url"],
                                "metadata": self.runtime_task_metadata(
                                    fixture, suffix="taskflow"
                                ),
                            }
                        ],
                    }
                ],
                "ready": True,
            },
        )
        if brief["goal"]["status"] != "ready":
            raise RuntimeError(f"taskflow brief did not mark goal ready: {brief}")
        pre_dispatch = self.peer_get(f"/remote/taskflow/goals/{goal_id}", peer_token)
        draft = self._single_by_id(pre_dispatch.get("task_drafts") or [], draft_id)
        if draft.get("runtime_task_id"):
            raise RuntimeError(f"taskflow created runtime task before dispatch: {draft}")

        confirmed = self.peer_post(
            f"/remote/taskflow/goals/{goal_id}/confirm", peer_token
        )
        if confirmed["goal"]["status"] != "confirmed":
            raise RuntimeError(f"taskflow goal was not confirmed: {confirmed}")

        dispatch = self.peer_post(
            f"/remote/taskflow/task-drafts/{draft_id}/dispatch", peer_token
        )
        decision = dispatch["decision"]
        runtime_task_id = decision.get("runtime_task_id")
        if decision.get("selected_agent_id") != agent_id or not runtime_task_id:
            raise RuntimeError(f"taskflow dispatch did not select smoke agent: {decision}")
        detail = self.poll_task(str(runtime_task_id), timeout_sec=120)
        if detail["task"]["status"] != "completed":
            raise RuntimeError(f"taskflow runtime task did not complete: {detail['task']}")
        task_metadata = detail["task"].get("metadata") or {}
        if task_metadata.get("dispatch_source") != "taskflow":
            raise RuntimeError(f"taskflow runtime metadata missing source: {task_metadata}")
        if task_metadata.get("taskflow_goal_id") != goal_id:
            raise RuntimeError(f"taskflow runtime metadata missing goal id: {task_metadata}")
        runtime_events = self.peer_get(
            f"/remote/agent-runtime/tasks/{runtime_task_id}/events",
            peer_token,
            {"after_seq": 0},
        )
        if "queued" not in {event.get("type") for event in runtime_events.get("events") or []}:
            raise RuntimeError(f"taskflow runtime events missing queued: {runtime_events}")
        taskflow_events = self.peer_get(
            f"/remote/taskflow/goals/{goal_id}/events",
            peer_token,
            {"after_seq": 0},
        )
        if not {
            "goal_created",
            "brief_recorded",
            "goal_confirmed",
            "task_draft_dispatched",
        }.issubset({event.get("type") for event in taskflow_events.get("events") or []}):
            raise RuntimeError(f"taskflow events incomplete: {taskflow_events}")

        self.run_taskflow_negative_paths(peer_token, fixture)
        self.report["tasks"]["taskflow"] = self.summarize_task(detail)
        self.report["flows"]["taskflow"] = {
            "goal_id": goal_id,
            "issue_draft_id": issue_draft_id,
            "task_draft_id": draft_id,
            "dispatch_decision_id": decision.get("id"),
            "runtime_task_id": runtime_task_id,
        }
        self.record_step("taskflow_e2e", runtime_task_id=runtime_task_id)

    def run_taskflow_negative_paths(
        self, peer_token: str, fixture: dict[str, str]
    ) -> None:
        suffix = self.timestamp.lower()
        blocked_goal_id = f"goal-blocked-{suffix}"
        blocked_draft_id = f"task-draft-blocked-{suffix}"
        self.peer_post(
            "/remote/taskflow/goals",
            peer_token,
            {
                "goal_id": blocked_goal_id,
                "title": "Blocked Taskflow goal",
                "prompt": "This should need assignment.",
            },
        )
        self.peer_post(
            f"/remote/taskflow/goals/{blocked_goal_id}/brief",
            peer_token,
            {
                "summary": "No Agent has this required capability.",
                "task_drafts": [
                    {
                        "id": blocked_draft_id,
                        "title": "Impossible task",
                        "prompt": "cannot dispatch",
                        "required_capabilities": ["capability_that_does_not_exist"],
                        "repo_url": fixture["repo_url"],
                    }
                ],
                "ready": True,
            },
        )
        self.peer_post(f"/remote/taskflow/goals/{blocked_goal_id}/confirm", peer_token)
        blocked = self.peer_post(
            f"/remote/taskflow/task-drafts/{blocked_draft_id}/dispatch", peer_token
        )["decision"]
        if blocked.get("runtime_task_id") or blocked.get("status") != "needs_assignment":
            raise RuntimeError(f"blocked taskflow dispatch mismatch: {blocked}")

        cancelled_goal_id = f"goal-cancelled-{suffix}"
        self.peer_post(
            "/remote/taskflow/goals",
            peer_token,
            {
                "goal_id": cancelled_goal_id,
                "title": "Cancelled Taskflow goal",
                "prompt": "This should not confirm after cancellation.",
            },
        )
        self.peer_post(
            f"/remote/taskflow/goals/{cancelled_goal_id}/cancel",
            peer_token,
            {"reason": "runtime_smoke_cancel_goal"},
        )
        self.expect_peer_failure(
            "POST",
            f"/remote/taskflow/goals/{cancelled_goal_id}/confirm",
            peer_token,
            status=400,
        )

        peer_b = self.register_manual_peer(fixture, suffix="taskflow-forbidden")
        self.expect_peer_failure(
            "GET",
            f"/remote/taskflow/goals/{blocked_goal_id}",
            peer_b,
            status=403,
        )
        self.report["flows"]["taskflow_negative"] = {
            "needs_assignment_goal_id": blocked_goal_id,
            "needs_assignment_draft_id": blocked_draft_id,
            "cancelled_goal_id": cancelled_goal_id,
            "cross_peer_forbidden": True,
        }

    def run_issue_assignment_mention_e2e(
        self, peer_token: str, fixture: dict[str, str], agent_id: str
    ) -> None:
        suffix = self.timestamp.lower()
        issue_id = f"issue-smoke-{suffix}"
        assignment_id = f"assignment-smoke-{suffix}"
        issue = self.peer_post(
            "/remote/issues",
            peer_token,
            {
                "issue_id": issue_id,
                "title": "Runtime smoke issue",
                "description": "Create assignment and dispatch through Taskflow.",
                "metadata": {"runtime_smoke": self.timestamp},
            },
        )["issue"]
        if issue["id"] != issue_id:
            raise RuntimeError(f"issue id mismatch: {issue}")

        assignment = self.peer_post(
            f"/remote/issues/{issue_id}/assignments",
            peer_token,
            {
                "assignment_id": assignment_id,
                "target_agent_id": agent_id,
                "title": "Runtime smoke assignment",
                "prompt": "runtime smoke assignment",
                "required_capabilities": ["code_review"],
                "preferred_capabilities": ["read_repo"],
                "task_type": "code_review",
                "execution_location": "daemon_worktree",
                "repo_url": fixture["repo_url"],
                "reason": "runtime smoke manual assignment",
                "metadata": self.runtime_task_metadata(
                    fixture, suffix="assignment"
                ),
            },
        )["assignment"]
        if assignment["status"] != "ready" or assignment.get("runtime_task_id"):
            raise RuntimeError(f"assignment should be ready without runtime task: {assignment}")

        reassigned = self.peer_post(
            f"/remote/assignments/{assignment_id}/assign",
            peer_token,
            {"agent_id": agent_id, "reason": "runtime smoke confirmation"},
        )["assignment"]
        if reassigned.get("target_agent_id") != agent_id:
            raise RuntimeError(f"assignment reassign failed: {reassigned}")

        dispatch = self.peer_post(
            f"/remote/assignments/{assignment_id}/dispatch", peer_token
        )["assignment"]
        assignment_task_id = dispatch.get("runtime_task_id")
        if dispatch.get("status") != "dispatched" or not assignment_task_id:
            raise RuntimeError(f"assignment dispatch failed: {dispatch}")
        assignment_detail = self.poll_task(str(assignment_task_id), timeout_sec=120)
        assignment_metadata = assignment_detail["task"].get("metadata") or {}
        for key, expected in {
            "dispatch_source": "assignment",
            "issue_id": issue_id,
            "assignment_id": assignment_id,
        }.items():
            if assignment_metadata.get(key) != expected:
                raise RuntimeError(
                    f"assignment runtime metadata mismatch for {key}: {assignment_metadata}"
                )

        parse = self.peer_post(
            "/remote/mentions/parse",
            peer_token,
            {"raw_text": f"@{agent_id} please help this issue"},
        )["mention"]
        if parse.get("resolved_agent_id") != agent_id:
            raise RuntimeError(f"mention parse did not resolve agent id: {parse}")

        missing = self.peer_post(
            "/remote/mentions/parse",
            peer_token,
            {"raw_text": "@missing_agent please help"},
        )["mention"]
        if missing.get("status") != "needs_assignment":
            raise RuntimeError(f"missing mention did not need assignment: {missing}")

        ambiguous = self.peer_post(
            "/remote/mentions/parse",
            peer_token,
            {"raw_text": "please help", "agent_ref": "Runtime Smoke Agent"},
        )["mention"]
        if ambiguous.get("reason") != "alias_ambiguous":
            raise RuntimeError(f"ambiguous mention did not report conflict: {ambiguous}")

        mention = self.peer_post(
            "/remote/mentions",
            peer_token,
            {
                "issue_id": issue_id,
                "raw_text": f"@{agent_id} please draft this.",
                "prompt": "runtime smoke mention",
                "metadata": self.runtime_task_metadata(fixture, suffix="mention"),
            },
        )["mention"]
        mention_assignment_id = mention.get("assignment_id")
        if mention.get("status") != "ready" or not mention_assignment_id:
            raise RuntimeError(f"mention did not create ready assignment: {mention}")

        mention_dispatch = self.peer_post(
            f"/remote/assignments/{mention_assignment_id}/dispatch", peer_token
        )["assignment"]
        mention_task_id = mention_dispatch.get("runtime_task_id")
        if mention_dispatch.get("status") != "dispatched" or not mention_task_id:
            raise RuntimeError(f"mention assignment dispatch failed: {mention_dispatch}")
        mention_detail = self.poll_task(str(mention_task_id), timeout_sec=120)
        mention_metadata = mention_detail["task"].get("metadata") or {}
        for key, expected in {
            "dispatch_source": "mention",
            "issue_id": issue_id,
            "assignment_id": mention_assignment_id,
            "mention_id": mention["id"],
        }.items():
            if mention_metadata.get(key) != expected:
                raise RuntimeError(
                    f"mention runtime metadata mismatch for {key}: {mention_metadata}"
                )

        issue_detail = self.peer_get(f"/remote/issues/{issue_id}", peer_token)
        if not issue_detail.get("taskflow"):
            raise RuntimeError(f"issue detail missing taskflow chain: {issue_detail}")
        issue_events = self.peer_get(
            f"/remote/issues/{issue_id}/events", peer_token, {"after_seq": 0}
        )
        expected_events = {"issue_created", "assignment_dispatched", "mention_created"}
        if not expected_events.issubset(
            {event.get("type") for event in issue_events.get("events") or []}
        ):
            raise RuntimeError(f"issue events incomplete: {issue_events}")

        blocked_issue_id = f"issue-blocked-{suffix}"
        blocked_assignment_id = f"assignment-blocked-{suffix}"
        self.peer_post(
            "/remote/issues",
            peer_token,
            {
                "issue_id": blocked_issue_id,
                "title": "Blocked assignment issue",
                "description": "No Agent should match this capability.",
            },
        )
        self.peer_post(
            f"/remote/issues/{blocked_issue_id}/assignments",
            peer_token,
            {
                "assignment_id": blocked_assignment_id,
                "required_capabilities": ["capability_that_does_not_exist"],
            },
        )
        blocked_dispatch = self.peer_post(
            f"/remote/assignments/{blocked_assignment_id}/dispatch", peer_token
        )["assignment"]
        if blocked_dispatch.get("status") != "needs_assignment":
            raise RuntimeError(f"blocked assignment did not need assignment: {blocked_dispatch}")

        peer_b = self.register_manual_peer(fixture, suffix="issue-forbidden")
        self.expect_peer_failure(
            "GET", f"/remote/issues/{issue_id}", peer_b, status=403
        )

        self.report["tasks"]["assignment"] = self.summarize_task(assignment_detail)
        self.report["tasks"]["mention"] = self.summarize_task(mention_detail)
        self.report["flows"]["issue_assignment_mention"] = {
            "issue_id": issue_id,
            "assignment_id": assignment_id,
            "assignment_runtime_task_id": assignment_task_id,
            "mention_id": mention["id"],
            "mention_assignment_id": mention_assignment_id,
            "mention_runtime_task_id": mention_task_id,
            "blocked_issue_id": blocked_issue_id,
            "blocked_assignment_id": blocked_assignment_id,
            "mention_not_found": missing.get("reason"),
            "mention_ambiguous": ambiguous.get("reason"),
            "cross_peer_forbidden": True,
        }
        self.record_step(
            "issue_assignment_mention_e2e",
            assignment_runtime_task_id=assignment_task_id,
            mention_runtime_task_id=mention_task_id,
        )

    def _single_by_id(self, items: list[Any], item_id: str) -> dict[str, Any]:
        for item in items:
            if isinstance(item, dict) and item.get("id") == item_id:
                return item
        raise RuntimeError(f"item {item_id} not found in {items}")

    def wait_for_label(self, task_id: str, label: str, *, timeout_sec: int) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        detail: dict[str, Any] = {}
        while time.time() < deadline:
            detail = self.load_task(task_id)
            if label in self.event_labels(detail):
                return detail
            status = str((detail.get("task") or {}).get("status") or "")
            if status in TERMINAL_STATUSES:
                raise RuntimeError(f"task {task_id} reached {status} before label {label}")
            time.sleep(1)
        raise RuntimeError(f"task {task_id} did not reach label {label}; last={detail}")

    def summarize_task(self, detail: dict[str, Any]) -> dict[str, Any]:
        task = detail.get("task") or {}
        return {
            "id": task.get("id"),
            "status": task.get("status"),
            "agent_id": task.get("agent_id"),
            "runtime_profile_id": task.get("runtime_profile_id"),
            "executor": task.get("executor"),
            "execution_location": task.get("execution_location"),
            "branch_name": task.get("branch_name"),
            "pr_url": task.get("pr_url"),
            "event_labels": sorted(self.event_labels(detail)),
            "artifact_types": sorted(
                str(item.get("type")) for item in (detail.get("artifacts") or [])
            ),
            "session": detail.get("session"),
            "claim": detail.get("claim"),
        }

    def run_restart_recovery(self, fixture: dict[str, str], agent_id: str) -> None:
        self.log("run restart recovery smoke")
        token = self.bootstrap_token()
        register = self.http_json(
            "POST",
            "/remote/register",
            {
                "bootstrap_token": token,
                "cwd": fixture["repo"],
                "workspace_root": fixture["repo"],
                "capabilities": [
                    "agent_runtime",
                    "agent_runtime.daemon_worktree",
                ],
            },
        )
        peer_token = register.get("payload", {}).get("peer_token")
        if not peer_token:
            raise RuntimeError(f"manual peer registration failed: {register}")
        self.masker.add(peer_token)
        task_id = f"task-recovery-{self.timestamp.lower()}"
        self.submit_task(task_id, agent_id, fixture, suffix="recovery")
        claim = self.claim_task_until(
            peer_token=peer_token,
            task_id=task_id,
            worker_id="manual-recovery-worker",
            executors=["fake"],
            timeout_sec=75,
        )
        request_id = claim["request_id"]
        self.masker.add(request_id)
        hb = self.http_json(
            "POST",
            "/remote/runtime/heartbeat",
            {
                "peer_token": peer_token,
                "request_id": request_id,
                "task_id": task_id,
                "worker_id": "manual-recovery-worker",
                "lease_sec": 30,
            },
        )
        if hb.get("ok") is not True:
            raise RuntimeError(f"heartbeat failed before recovery restart: {hb}")
        self.restart_host()
        self.login()
        detail = self.load_task(task_id)
        if detail["task"]["status"] != "failed":
            raise RuntimeError(f"recovery task was not marked failed: {detail['task']}")
        if "host_recovered_task_failed" not in self.event_labels(detail):
            raise RuntimeError(f"recovery event missing: {detail.get('events')}")
        self.report["tasks"]["restart_recovery"] = self.summarize_task(detail)
        self.record_step("restart_recovery")

    def restart_host(self) -> None:
        self.run_cmd(
            [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "restart",
                self.compose_service,
            ],
            timeout=300,
        )
        self.ensure_network()
        self.run_cmd(["docker", "restart", self.host_container], timeout=120)
        self.wait_http_ready(timeout_sec=120)

    def run_session_persistence(self) -> None:
        self.log("run session persistence smoke")
        script = textwrap.dedent(
            f"""
            import json, os
            from pathlib import Path
            from reuleauxcoder.services.config.loader import ConfigLoader
            from labrastro_server.infrastructure.persistence.db import create_postgres_engine
            from labrastro_server.infrastructure.persistence.postgres_session_store import PostgresSessionStore
            config_path = Path({self.container_config_path()!r})
            cfg = ConfigLoader.from_path(config_path)
            store = PostgresSessionStore(create_postgres_engine(cfg.persistence.database_url))
            sid = store.save(
                messages=[{{"role": "user", "content": "runtime session smoke {self.timestamp}"}}],
                model="smoke-model",
                fingerprint="runtime-smoke:{self.timestamp}",
            )
            store.save_snapshot(sid, {{"turns": [{{"id": "turn-1"}}], "traceNodes": [{{"id": "node-1"}}], "traceEdges": []}})
            snapshot, error = store.load_snapshot(sid)
            print(json.dumps({{"session_id": sid, "snapshot_ok": snapshot is not None, "error": error}}))
            """
        )
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        result = self.run_cmd(
            [
                "docker",
                "exec",
                self.host_container,
                "python",
                "-c",
                f"import base64; exec(base64.b64decode('{encoded}').decode())",
            ],
            timeout=60,
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        if not data.get("session_id") or data.get("snapshot_ok") is not True:
            raise RuntimeError(f"session persistence smoke failed: {data}")
        self.restart_host()
        session_count = self.psql(
            "SELECT count(*) FROM labrastro_sessions WHERE fingerprint='runtime-smoke:" + self.timestamp + "'",
            database=self.db_name,
        ).stdout.strip()
        snapshot_count = self.psql(
            "SELECT count(*) FROM labrastro_session_snapshots WHERE session_id='" + data["session_id"] + "'",
            database=self.db_name,
        ).stdout.strip()
        if session_count == "0" or snapshot_count == "0":
            raise RuntimeError(
                f"session/snapshot rows missing: sessions={session_count} snapshots={snapshot_count}"
            )
        self.report["checks"]["session_persistence"] = {
            "session_id": data["session_id"],
            "sessions": session_count,
            "snapshots": snapshot_count,
        }
        self.record_step("session_persistence", session_id=data["session_id"])

    def verify_db_counts(self) -> None:
        counts: dict[str, int] = {}
        for table in REQUIRED_TABLES:
            out = self.psql(f"SELECT count(*) FROM {table}", database=self.db_name).stdout.strip()
            counts[table] = int(out or 0)
        self.report["checks"]["db_counts"] = counts
        self.record_step("db_counts", counts=counts)

    def write_report(self) -> None:
        self.smoke_dir.mkdir(parents=True, exist_ok=True)
        report_json = self.smoke_dir / "report.json"
        report_md = self.smoke_dir / "report.md"
        safe = json.loads(self.masker.mask(json.dumps(self.report, ensure_ascii=False)))
        report_json.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# Labrastro Runtime E2E Smoke Report",
            "",
            f"- Timestamp: `{self.timestamp}`",
            f"- Host container: `{self.host_container}`",
            f"- Postgres container: `{self.pg_container}`",
            f"- Database: `{self.db_name}`",
            f"- Backup: `{self.backup_dir}`",
            f"- Smoke dir: `{self.smoke_dir}`",
            "",
            "## Steps",
        ]
        for step in safe.get("steps", []):
            lines.append(f"- `{step.get('status')}` {step.get('name')}")
        lines.extend(["", "## Tasks"])
        for name, task in safe.get("tasks", {}).items():
            lines.append(
                f"- `{name}` `{task.get('id')}` status=`{task.get('status')}` "
                f"events=`{','.join(task.get('event_labels') or [])}`"
            )
        lines.extend(["", "## Flows"])
        for name, flow in safe.get("flows", {}).items():
            lines.append(f"- `{name}`: `{compact_json(flow)}`")
        lines.extend(["", "## DB Counts"])
        for table, count in (safe.get("checks", {}).get("db_counts") or {}).items():
            lines.append(f"- `{table}`: {count}")
        report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log(f"report written: {report_md}")

    def rollback(self) -> None:
        self.log("rollback requested after failure")
        try:
            if (self.backup_dir / "src.previous").exists():
                self.bash(f"rm -rf {q(self.root / 'src')}")
                self.bash(f"mv {q(self.backup_dir / 'src.previous')} {q(self.root / 'src')}")
            elif (self.backup_dir / "src").exists():
                self.bash(f"rm -rf {q(self.root / 'src')}")
                self.bash(f"cp -a {q(self.backup_dir / 'src')} {q(self.root / 'src')}")
            if self.config_path and (self.backup_dir / "active-config-before-persistence.yaml").exists():
                shutil.copy2(self.backup_dir / "active-config-before-persistence.yaml", self.config_path)
            self.run_cmd(
                ["docker", "compose", "-f", str(self.compose_path), "up", "-d", self.compose_service],
                check=False,
                timeout=300,
            )
            self.record_step("rollback")
        except Exception as exc:  # noqa: BLE001
            self.record_step("rollback", "failed", error=str(exc))

    def run(self) -> int:
        success = False
        exit_code = 1
        try:
            self.preflight()
            self.backup()
            self.ensure_network()
            dsn = self.create_database()
            self.deploy_source()
            self.config_path = self.discover_config_path()
            self.append_persistence_config(dsn)
            self.cleanup_generated_global_example_config()
            self.start_host()
            self.verify_service()
            self.login()
            _, agent_id = self.setup_smoke_agent()
            fixture = self.create_git_fixture()
            fake_gh = self.install_fake_gh()
            self.run_happy_cancel_retry(fixture, fake_gh, agent_id)
            self.run_taskflow_issue_mention_e2e(fixture, fake_gh, agent_id)
            self.run_restart_recovery(fixture, agent_id)
            self.run_session_persistence()
            self.verify_db_counts()
            success = True
            self.record_step("success")
            exit_code = 0
        except Exception as exc:  # noqa: BLE001
            self.report["error"] = self.masker.mask(str(exc))
            self.record_step("failed", "failed", error=str(exc))
            logs = self.run_cmd(
                ["docker", "logs", "--tail", "120", self.host_container],
                check=False,
                timeout=30,
            )
            self.report["container_logs_tail"] = self.masker.mask(logs.stdout + logs.stderr)
            if not self.args.retain_success:
                self.rollback()
            self.log(f"FAILED: {exc}")
            exit_code = 1
        finally:
            self.stop_worker()
            if success:
                try:
                    self.restore_agent_runtime()
                except Exception as exc:  # noqa: BLE001
                    self.report["error"] = self.masker.mask(str(exc))
                    self.record_step("failed", "failed", error=str(exc))
                    if not self.args.retain_success:
                        self.rollback()
                    self.log(f"FAILED: {exc}")
                    exit_code = 1
            self.write_report()
        return exit_code


def dry_run(args: argparse.Namespace) -> int:
    masker = Masker()
    for env_name in (args.ssh_password_env, args.pg_password_env):
        value = os.environ.get(env_name or "")
        if value:
            masker.add(value)
    plan = {
        "mode": args.command,
        "server": args.server,
        "ssh_user": args.ssh_user,
        "root": args.root,
        "host_container": args.host_container,
        "pg_container": args.pg_container,
        "pg_user": args.pg_user,
        "ssh_password_env": args.ssh_password_env,
        "pg_password_env": args.pg_password_env,
        "steps": [
            "package current labrastro source",
            "upload archive and script",
            "backup server files and container inspect",
            "create smoke Postgres database",
            "patch persistence config",
            "rebuild/restart host container",
            "run runtime worker lifecycle smoke",
            "run Taskflow / Issue Assignment / Mention API smoke",
            "run restart recovery smoke",
            "run session persistence smoke",
            "write report",
        ],
    }
    print_masked(masker, json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def remote(args: argparse.Namespace) -> int:
    ssh_password = load_secret_from_env(args.ssh_password_env, "SSH password")
    pg_password = load_secret_from_env(args.pg_password_env, "Postgres password")
    auth_username = os.environ.get("LABRASTRO_SUPERADMIN_USERNAME", "admin")
    auth_password = os.environ.get("LABRASTRO_SUPERADMIN_PASSWORD", "")
    timestamp = args.timestamp or utc_timestamp()
    masker = Masker([ssh_password, pg_password, auth_password])
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="labrastro-runtime-smoke-") as tmp:
        tmp_path = Path(tmp)
        archive = create_source_archive(repo_root, timestamp, tmp_path)
        print_masked(masker, f"archive={archive} size={archive.stat().st_size}")
        ssh = LocalSSH(args.server, args.ssh_user, ssh_password, masker)
        try:
            incoming = f"{args.root.rstrip('/')}/incoming"
            ssh.run_checked(f"mkdir -p {q(incoming)}")
            remote_archive = f"{incoming}/labrastro-src-{timestamp}.tgz"
            remote_script = f"{incoming}/runtime_e2e_smoke-{timestamp}.py"
            ssh.put(archive, remote_archive)
            ssh.put(Path(__file__).resolve(), remote_script)
            ssh.run_checked(f"chmod 700 {q(remote_script)}")
            server_args = [
                "python3",
                remote_script,
                "server-steps",
                "--root",
                args.root,
                "--host-container",
                args.host_container,
                "--pg-container",
                args.pg_container,
                "--pg-user",
                args.pg_user,
                "--source-archive",
                remote_archive,
                "--timestamp",
                timestamp,
                "--pg-password-env",
                args.pg_password_env,
            ]
            if args.retain_success:
                server_args.append("--retain-success")
            if args.database_name:
                server_args.extend(["--database-name", args.database_name])
            command = " ".join(q(item) for item in server_args) + " --secrets-stdin"
            secrets_payload = json.dumps(
                {
                    "pg_password": pg_password,
                    "auth_username": auth_username,
                    "auth_password": auth_password,
                }
            )
            code, out, err = ssh.run(command, input_text=secrets_payload, timeout=None)
            if out:
                print_masked(masker, out.rstrip())
            if err:
                print_masked(masker, err.rstrip())
            if code != 0:
                raise RuntimeError(f"server-steps failed with exit={code}")
            return 0
        finally:
            ssh.close()


def server_steps(args: argparse.Namespace) -> int:
    secrets_payload: dict[str, str] = {}
    if args.secrets_stdin:
        raw = sys.stdin.read()
        if raw.strip():
            secrets_payload = json.loads(raw)
    runner = ServerRunner(args, secrets_payload)
    return runner.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--server", default="192.168.50.149")
        p.add_argument("--ssh-user", default="root")
        p.add_argument("--ssh-password-env", default="LABRASTRO_SSH_PASSWORD")
        p.add_argument("--root", default="/data/labrastro")
        p.add_argument("--host-container", default="labrastro-host")
        p.add_argument("--pg-container", default="Postgresql")
        p.add_argument("--pg-user", default="user_rBrNr5")
        p.add_argument("--pg-password-env", default="LABRASTRO_PG_PASSWORD")
        p.add_argument("--timestamp")
        p.add_argument("--retain-success", action="store_true")

    dry = sub.add_parser("dry-run", help="Print a masked execution plan")
    add_common(dry)
    dry.set_defaults(func=dry_run)

    remote_parser = sub.add_parser("remote", help="Upload and execute server smoke")
    add_common(remote_parser)
    remote_parser.add_argument("--database-name")
    remote_parser.set_defaults(func=remote)

    server = sub.add_parser("server-steps", help="Run the smoke on the server")
    server.add_argument("--root", required=True)
    server.add_argument("--host-container", required=True)
    server.add_argument("--pg-container", required=True)
    server.add_argument("--pg-user", required=True)
    server.add_argument("--pg-password-env", default="LABRASTRO_PG_PASSWORD")
    server.add_argument("--source-archive", required=True)
    server.add_argument("--timestamp", required=True)
    server.add_argument("--database-name")
    server.add_argument("--retain-success", action="store_true")
    server.add_argument("--secrets-stdin", action="store_true")
    server.set_defaults(func=server_steps)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
