"""Auth-related CLI commands."""

from __future__ import annotations

import getpass

from labrastro_server.services.auth.crypto import hash_password
from labrastro_server.services.auth.service import validate_auth_config
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.services.config.loader import (
    ConfigEnvironmentError,
    ConfigSchemaError,
    ExampleConfigError,
    ConfigLoader,
)


def run_auth_cli(args) -> int:
    if getattr(args, "auth_command", None) == "hash-password":
        password = getattr(args, "password", None)
        if not password:
            password = getpass.getpass("Password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("passwords do not match")
                return 1
        iterations = int(getattr(args, "iterations", 260_000) or 260_000)
        print(hash_password(password, iterations=iterations))
        return 0
    if getattr(args, "auth_command", None) == "verify-config":
        try:
            config = ConfigLoader.from_path(
                resolve_cli_config_path(
                    args, require=True, purpose="auth verify-config"
                )
            )
        except (ConfigEnvironmentError, ConfigSchemaError, ExampleConfigError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 1
        errors = config.validate()
        errors.extend(validate_auth_config(config.auth))
        warnings: list[str] = []
        if config.auth.enabled and config.auth.store_backend == "postgres" and not config.persistence.database_url:
            errors.append("auth.store_backend=postgres requires persistence.database_url")
        if config.auth.enabled and config.auth.store_backend == "auto" and not config.persistence.database_url:
            warnings.append("auth.store_backend=auto will use file store because persistence.database_url is empty")
        remote_exec = getattr(config, "remote_exec", None)
        if (
            config.auth.enabled
            and remote_exec is not None
            and remote_exec.host_mode
            and str(remote_exec.relay_bind).startswith(("0.0.0.0:", "[::]:"))
        ):
            warnings.append("remote host mode should be exposed through HTTPS in production")
        if errors:
            for error in sorted(set(errors)):
                print(f"ERROR: {error}")
            for warning in sorted(set(warnings)):
                print(f"WARNING: {warning}")
            return 1
        print("remote auth config ok")
        for warning in sorted(set(warnings)):
            print(f"WARNING: {warning}")
        return 0
    print("missing auth command")
    return 2
