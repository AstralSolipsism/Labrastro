"""Server-side MCP artifact management."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


DEFAULT_PEER_PLATFORMS = ["windows-amd64", "linux-amd64"]
MCP_PLACEMENTS = {"server", "peer", "both"}


@dataclass(slots=True)
class ArtifactRecord:
    server: str
    version: str
    platform: str
    path: str
    sha256: str
    exists: bool
    distribution: str = "artifact"
    verified: bool | None = None


@dataclass(slots=True)
class NodeInstallResult:
    server: str
    package: str
    version: str
    placement: str
    artifacts: list[ArtifactRecord]


class MCPArtifactError(RuntimeError):
    """Raised when MCP artifact management fails."""


class MCPArtifactManager:
    """Manage server-hosted artifacts for peer MCP servers."""

    def __init__(self, config_path: Path | None = None, *, npm_cmd: str = "npm"):
        self.config_path = config_path or ConfigLoader.WORKSPACE_CONFIG_PATH
        self.npm_cmd = npm_cmd

    def import_artifact(
        self, server: str, version: str, platform: str, archive: Path
    ) -> ArtifactRecord:
        if not archive.exists() or not archive.is_file():
            raise MCPArtifactError(f"artifact file does not exist: {archive}")
        data = self._load_data()
        artifact_root = self._artifact_root(data)
        suffix = self._archive_suffix(archive)
        rel_path = Path(server) / version / f"{platform}{suffix}"
        dest = artifact_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if archive.resolve() != dest.resolve():
            shutil.copy2(archive, dest)
        sha256 = _sha256_file(dest)
        self._update_server_artifact(
            data,
            server,
            version,
            platform,
            rel_path.as_posix(),
            sha256,
            launch=None,
            environment_requirement_refs=None,
            build_update=None,
            placement="peer",
        )
        self._save_data(data)
        return ArtifactRecord(
            server=server,
            version=version,
            platform=platform,
            path=rel_path.as_posix(),
            sha256=sha256,
            exists=True,
            distribution="artifact",
            verified=True,
        )

    def build_node(
        self,
        server: str,
        package_spec: str,
        bin_name: str,
        platforms: list[str],
        *,
        placement: str = "peer",
        launch_args: list[str] | None = None,
        launch_env: dict[str, str] | None = None,
    ) -> list[ArtifactRecord]:
        if not platforms:
            raise MCPArtifactError("at least one --platform is required")
        if not shutil.which(self.npm_cmd):
            raise MCPArtifactError(f"cannot find npm command: {self.npm_cmd}")
        package_name, requested = _split_npm_package_spec(package_spec)
        version = self._resolve_npm_version(package_name, requested)

        data = self._load_data()
        records = self._build_node_artifacts(
            data=data,
            server=server,
            package_name=package_name,
            version=version,
            bin_name=bin_name,
            platforms=_flatten_platforms(platforms),
            placement=placement,
            launch_args=launch_args or [],
            launch_env=launch_env or {},
        )
        self._save_data(data)
        return records

    def install_node(
        self,
        server: str,
        package_spec: str,
        bin_name: str,
        *,
        placement: str = "server",
        platforms: list[str] | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> NodeInstallResult:
        if placement not in MCP_PLACEMENTS:
            raise MCPArtifactError(
                "placement must be one of server, peer, or both"
            )
        if not shutil.which(self.npm_cmd):
            raise MCPArtifactError(f"cannot find npm command: {self.npm_cmd}")
        package_name, requested = _split_npm_package_spec(package_spec)
        version = self._resolve_npm_version(package_name, requested)
        install_args = list(args or [])
        install_env = dict(env or {})
        data = self._load_data()
        records: list[ArtifactRecord] = []

        if placement in {"server", "both"}:
            self._update_node_server_config(
                data,
                server,
                package_name,
                version,
                bin_name,
                placement,
                install_args,
                install_env,
            )

        if placement in {"peer", "both"}:
            records = self._build_node_artifacts(
                data=data,
                server=server,
                package_name=package_name,
                version=version,
                bin_name=bin_name,
                platforms=_flatten_platforms(platforms or DEFAULT_PEER_PLATFORMS),
                placement=placement,
                launch_args=install_args,
                launch_env=install_env,
            )

        self._save_data(data)
        return NodeInstallResult(
            server=server,
            package=package_name,
            version=version,
            placement=placement,
            artifacts=records,
        )

    def _build_node_artifacts(
        self,
        *,
        data: dict[str, Any],
        server: str,
        package_name: str,
        version: str,
        bin_name: str,
        platforms: list[str],
        placement: str,
        launch_args: list[str],
        launch_env: dict[str, str],
    ) -> list[ArtifactRecord]:
        if placement not in MCP_PLACEMENTS:
            raise MCPArtifactError(
                "placement must be one of server, peer, or both"
            )
        artifact_root = self._artifact_root(data)
        records: list[ArtifactRecord] = []
        with tempfile.TemporaryDirectory(prefix="rcoder-mcp-node-") as tmp:
            tmp_path = Path(tmp)
            package_dir = tmp_path / "package"
            cache_dir = tmp_path / "npm-cache"
            package_dir.mkdir(parents=True)
            cache_dir.mkdir(parents=True)
            self._write_node_package(package_dir, server, package_name, version)
            self._run_npm(
                [
                    "install",
                    "--cache",
                    str(cache_dir),
                    "--ignore-scripts",
                    "--omit=dev",
                    "--no-audit",
                    "--no-fund",
                ],
                cwd=package_dir,
            )
            node_modules = package_dir / "node_modules"
            if node_modules.exists():
                shutil.rmtree(node_modules)
            if not (package_dir / "package-lock.json").exists():
                raise MCPArtifactError("npm install did not produce package-lock.json")

            for platform in platforms:
                record = self._build_node_platform_artifact(
                    data=data,
                    artifact_root=artifact_root,
                    tmp_path=tmp_path,
                    package_dir=package_dir,
                    cache_dir=cache_dir,
                    server=server,
                    package_name=package_name,
                    version=version,
                    bin_name=bin_name,
                    platform=platform,
                    placement=placement,
                    launch_args=launch_args,
                    launch_env=launch_env,
                )
                records.append(record)
        return records

    def list_artifacts(self, server: str | None = None) -> list[ArtifactRecord]:
        data = self._load_data()
        artifact_root = self._artifact_root(data)
        records: list[ArtifactRecord] = []
        servers = (data.get("mcp", {}) or {}).get("servers", {}) or {}
        if not isinstance(servers, dict):
            return records
        for server_name, server_data in sorted(servers.items()):
            if server and server_name != server:
                continue
            if not isinstance(server_data, dict):
                continue
            version = str(server_data.get("version") or "")
            artifacts = server_data.get("artifacts", {}) or {}
            if not isinstance(artifacts, dict):
                continue
            distribution = str(
                server_data.get("distribution")
                or ("artifact" if artifacts else "command")
            ).lower()
            if distribution not in {"command", "artifact"}:
                distribution = "artifact" if artifacts else "command"
            for platform, artifact_data in sorted(artifacts.items()):
                if not isinstance(artifact_data, dict):
                    continue
                rel_path = str(artifact_data.get("path", ""))
                sha256 = str(artifact_data.get("sha256", ""))
                abs_path = artifact_root / rel_path
                records.append(
                    ArtifactRecord(
                        server=server_name,
                        version=version,
                        platform=str(platform),
                        path=rel_path,
                        sha256=sha256,
                        exists=abs_path.exists(),
                        distribution=distribution,
                    )
                )
        return records

    def verify_artifacts(self, server: str | None = None) -> list[ArtifactRecord]:
        data = self._load_data()
        artifact_root = self._artifact_root(data)
        verified: list[ArtifactRecord] = []
        for record in self.list_artifacts(server):
            abs_path = artifact_root / record.path
            ok = abs_path.exists() and _sha256_file(abs_path) == record.sha256
            verified.append(
                ArtifactRecord(
                    server=record.server,
                    version=record.version,
                    platform=record.platform,
                    path=record.path,
                    sha256=record.sha256,
                    exists=record.exists,
                    distribution=record.distribution,
                    verified=ok,
                )
            )
        return verified

    def _build_node_platform_artifact(
        self,
        *,
        data: dict[str, Any],
        artifact_root: Path,
        tmp_path: Path,
        package_dir: Path,
        cache_dir: Path,
        server: str,
        package_name: str,
        version: str,
        bin_name: str,
        platform: str,
        placement: str,
        launch_args: list[str],
        launch_env: dict[str, str],
    ) -> ArtifactRecord:
        stage = tmp_path / "stage" / platform
        if stage.exists():
            shutil.rmtree(stage)
        (stage / "package").mkdir(parents=True)
        shutil.copy2(package_dir / "package.json", stage / "package" / "package.json")
        shutil.copy2(
            package_dir / "package-lock.json",
            stage / "package" / "package-lock.json",
        )
        shutil.copytree(cache_dir, stage / "npm-cache", dirs_exist_ok=True)
        launch = self._write_node_wrapper(stage, platform, bin_name, launch_args, launch_env)

        suffix = ".zip" if platform.startswith("windows-") else ".tar.gz"
        rel_path = Path(server) / version / f"{platform}{suffix}"
        archive_path = artifact_root / rel_path
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".zip":
            _write_zip(stage, archive_path)
        else:
            _write_tar_gz(stage, archive_path)
        sha256 = _sha256_file(archive_path)
        self._update_server_artifact(
            data,
            server,
            version,
            platform,
            rel_path.as_posix(),
            sha256,
            launch=launch,
            environment_requirement_refs=[
                "envreq:runtime:node",
                "envreq:executable:npm",
            ],
            build_update={
                "type": "node",
                "package": package_name,
                "package_version": version,
                "bin": bin_name,
            },
            placement=placement,
        )
        return ArtifactRecord(
            server=server,
            version=version,
            platform=platform,
            path=rel_path.as_posix(),
            sha256=sha256,
            exists=True,
            distribution="artifact",
            verified=True,
        )

    def _write_node_wrapper(
        self,
        stage: Path,
        platform: str,
        bin_name: str,
        launch_args: list[str],
        launch_env: dict[str, str],
    ) -> dict[str, Any]:
        if platform.startswith("windows-"):
            wrapper = stage / "run.cmd"
            wrapper.write_text(
                "\r\n".join(
                    [
                        "@echo off",
                        "setlocal",
                        'set "DIR=%~dp0"',
                        'cd /d "%DIR%package"',
                        'if not exist "node_modules\\.install-complete" (',
                        '  if exist "node_modules" rmdir /s /q "node_modules"',
                        '  if exist "%DIR%package.tmp" rmdir /s /q "%DIR%package.tmp"',
                        '  mkdir "%DIR%package.tmp"',
                        '  copy /y "package.json" "%DIR%package.tmp\\package.json" >nul',
                        '  copy /y "package-lock.json" "%DIR%package.tmp\\package-lock.json" >nul',
                        '  pushd "%DIR%package.tmp"',
                        '  npm ci --offline --cache "%DIR%npm-cache" --omit=dev --no-audit --no-fund',
                        "  if errorlevel 1 exit /b %errorlevel%",
                        "  popd",
                        '  move "%DIR%package.tmp\\node_modules" "node_modules" >nul',
                        "  if errorlevel 1 exit /b %errorlevel%",
                        '  rmdir /s /q "%DIR%package.tmp"',
                        '  echo ok>"node_modules\\.install-complete"',
                        ")",
                        f'if exist "node_modules\\.bin\\{bin_name}.cmd" (',
                        f'  call "node_modules\\.bin\\{bin_name}.cmd" %*',
                        ") else (",
                        f'  call "node_modules\\.bin\\{bin_name}" %*',
                        ")",
                    ]
                )
                + "\r\n",
                encoding="utf-8",
            )
            return {
                "command": "{{bundle}}/run.cmd",
                "args": list(launch_args),
                "env": dict(launch_env),
            }

        wrapper = stage / "run.sh"
        wrapper.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env sh",
                    "set -eu",
                    'DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"',
                    'cd "$DIR/package"',
                    'if [ ! -f node_modules/.install-complete ]; then',
                    '  rm -rf node_modules "$DIR/package.tmp"',
                    '  mkdir -p "$DIR/package.tmp"',
                    '  cp package.json package-lock.json "$DIR/package.tmp/"',
                    '  (cd "$DIR/package.tmp" && npm ci --offline --cache "$DIR/npm-cache" --omit=dev --no-audit --no-fund)',
                    '  mv "$DIR/package.tmp/node_modules" node_modules',
                    '  rm -rf "$DIR/package.tmp"',
                    '  printf "ok\\n" > node_modules/.install-complete',
                    "fi",
                    f'exec "$DIR/package/node_modules/.bin/{bin_name}" "$@"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return {
            "command": "{{bundle}}/run.sh",
            "args": list(launch_args),
            "env": dict(launch_env),
        }

    def _resolve_npm_version(self, package_name: str, requested: str | None) -> str:
        spec = package_name if not requested else f"{package_name}@{requested}"
        result = self._run_npm(["view", spec, "version", "--json"], cwd=None)
        raw = result.stdout.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw.strip('"')
        if isinstance(parsed, list):
            if not parsed:
                raise MCPArtifactError(f"npm returned no versions for {spec}")
            parsed = parsed[-1]
        version = str(parsed).strip()
        if not version or version == "latest":
            raise MCPArtifactError(f"npm did not resolve {spec} to an exact version")
        return version

    def _update_node_server_config(
        self,
        data: dict[str, Any],
        server: str,
        package_name: str,
        version: str,
        bin_name: str,
        placement: str,
        args: list[str],
        env: dict[str, str],
    ) -> None:
        mcp_data = data.setdefault("mcp", {})
        servers = mcp_data.setdefault("servers", {})
        server_data = servers.setdefault(server, {})
        server_data["command"] = "npx"
        server_data["args"] = ["-y", f"{package_name}@{version}", *args]
        server_data["env"] = dict(env)
        server_data.setdefault("cwd", None)
        server_data["enabled"] = True
        server_data["placement"] = placement
        server_data["distribution"] = (
            "artifact" if placement in {"peer", "both"} else "command"
        )
        server_data["version"] = version
        existing_build = server_data.setdefault("build", {})
        existing_build.update(
            {
                "type": "node",
                "package": package_name,
                "package_version": version,
                "bin": bin_name,
            }
        )

    def _run_npm(
        self, args: list[str], *, cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.npm_cmd, *args],
                cwd=str(cwd) if cwd is not None else None,
                check=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise MCPArtifactError(
                f"npm {' '.join(args)} failed"
                + (f": {detail}" if detail else "")
            ) from exc

    @staticmethod
    def _write_node_package(
        package_dir: Path, server: str, package_name: str, version: str
    ) -> None:
        package_dir.joinpath("package.json").write_text(
            json.dumps(
                {
                    "name": f"rcoder-mcp-{_safe_package_name(server)}",
                    "version": "0.0.0",
                    "private": True,
                    "dependencies": {package_name: version},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _load_data(self) -> dict[str, Any]:
        try:
            return load_yaml_config(self.config_path)
        except FileNotFoundError:
            return {}

    def _save_data(self, data: dict[str, Any]) -> None:
        save_yaml_config(self.config_path, data)

    def _artifact_root(self, data: dict[str, Any]) -> Path:
        mcp_data = data.setdefault("mcp", {})
        artifact_root = Path(str(mcp_data.get("artifact_root", ".rcoder/mcp-artifacts")))
        if not artifact_root.is_absolute():
            artifact_root = Path.cwd() / artifact_root
        return artifact_root.resolve()

    def _update_server_artifact(
        self,
        data: dict[str, Any],
        server: str,
        version: str,
        platform: str,
        artifact_path: str,
        sha256: str,
        *,
        launch: dict[str, Any] | None,
        environment_requirement_refs: list[str] | None,
        build_update: dict[str, Any] | None,
        placement: str = "peer",
    ) -> None:
        mcp_data = data.setdefault("mcp", {})
        servers = mcp_data.setdefault("servers", {})
        server_data = servers.setdefault(server, {})
        server_data.setdefault("command", "")
        server_data.setdefault("args", [])
        server_data.setdefault("env", {})
        server_data.setdefault("enabled", True)
        server_data["placement"] = placement
        server_data["distribution"] = "artifact"
        server_data["version"] = version
        artifact_data: dict[str, Any] = {"path": artifact_path, "sha256": sha256}
        if launch is not None:
            artifact_data["launch"] = launch
        server_data.setdefault("artifacts", {})[platform] = artifact_data
        if environment_requirement_refs:
            refs = [
                str(ref)
                for ref in server_data.get("environment_requirement_refs", [])
                if str(ref).strip()
            ]
            refs.extend(str(ref) for ref in environment_requirement_refs if str(ref).strip())
            server_data["environment_requirement_refs"] = sorted(dict.fromkeys(refs))
        if build_update:
            existing_build = server_data.setdefault("build", {})
            existing_build.update(build_update)

    @staticmethod
    def _archive_suffix(path: Path) -> str:
        name = path.name.lower()
        if name.endswith(".tar.gz"):
            return ".tar.gz"
        if name.endswith(".tgz"):
            return ".tgz"
        if name.endswith(".zip"):
            return ".zip"
        return path.suffix or ".artifact"


def run_mcp_artifact_cli(args: argparse.Namespace) -> int:
    try:
        write_command = args.artifact_command in {"import", "build-node"}
        manager = MCPArtifactManager(
            resolve_cli_config_path(
                args,
                require=write_command,
                purpose=f"mcp artifact {args.artifact_command or ''}".strip(),
            )
        )
        if args.artifact_command == "import":
            record = manager.import_artifact(
                args.server_name, args.version, args.platform, Path(args.archive)
            )
            _print_record("imported", record)
            return 0
        if args.artifact_command == "build-node":
            records = manager.build_node(
                args.server_name, args.package, args.bin, list(args.platform or [])
            )
            for record in records:
                _print_record("built", record)
            return 0
        if args.artifact_command == "list":
            records = manager.list_artifacts(args.server_name)
            for record in records:
                _print_record("artifact", record)
            return 0
        if args.artifact_command == "verify":
            records = manager.verify_artifacts(args.server_name)
            failed = False
            for record in records:
                _print_record("verified" if record.verified else "failed", record)
                failed = failed or not bool(record.verified)
            return 1 if failed else 0
    except (MCPArtifactError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print("Error: missing artifact command")
    return 1


def run_mcp_install_node_cli(args: argparse.Namespace) -> int:
    try:
        manager = MCPArtifactManager(
            resolve_cli_config_path(args, require=True, purpose="mcp install-node")
        )
        result = manager.install_node(
            args.server_name,
            args.package,
            args.bin,
            placement=args.placement,
            platforms=_flatten_platforms(args.platform or []),
            args=list(args.node_arg or []),
            env=_parse_env_entries(list(args.env or [])),
        )
    except (MCPArtifactError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print(
        f"installed: {result.server} {result.package}@{result.version} "
        f"placement={result.placement}"
    )
    for record in result.artifacts:
        _print_record("built", record)
    return 0


def _print_record(prefix: str, record: ArtifactRecord) -> None:
    status = "ok" if record.verified is not False and record.exists else "missing"
    notes = ""
    if record.distribution == "command":
        notes = " usage=retained-not-default"
    print(
        f"{prefix}: {record.server} {record.version} {record.platform} "
        f"{record.path} sha256={record.sha256} status={status} "
        f"distribution={record.distribution}{notes}"
    )


def _split_npm_package_spec(spec: str) -> tuple[str, str | None]:
    spec = spec.strip()
    if not spec:
        raise MCPArtifactError("npm package spec is required")
    if spec.startswith("@"):
        slash = spec.find("/")
        if slash < 0:
            raise MCPArtifactError(f"invalid scoped npm package: {spec}")
        version_at = spec.find("@", slash + 1)
        if version_at >= 0:
            return spec[:version_at], spec[version_at + 1 :] or None
        return spec, None
    if "@" in spec:
        package, version = spec.rsplit("@", 1)
        return package, version or None
    return spec, None


def _flatten_platforms(values: list[Any]) -> list[str]:
    platforms: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            platforms.extend(str(item) for item in value if str(item).strip())
        elif str(value).strip():
            platforms.append(str(value))
    return platforms


def _parse_env_entries(entries: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise MCPArtifactError(f"invalid --env entry, expected KEY=VALUE: {entry}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise MCPArtifactError(f"invalid --env entry, empty key: {entry}")
        env[key] = value
    return env


def _safe_package_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_zip(source_dir: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in source_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir).as_posix())


def _write_tar_gz(source_dir: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tf:
        for path in source_dir.rglob("*"):
            arcname = path.relative_to(source_dir).as_posix()
            info = tf.gettarinfo(str(path), arcname)
            if path.name == "run.sh":
                info.mode = 0o755
            if path.is_file():
                with path.open("rb") as f:
                    tf.addfile(info, f)
            else:
                tf.addfile(info)


__all__ = [
    "ArtifactRecord",
    "MCPArtifactError",
    "MCPArtifactManager",
    "NodeInstallResult",
    "run_mcp_artifact_cli",
    "run_mcp_install_node_cli",
]
