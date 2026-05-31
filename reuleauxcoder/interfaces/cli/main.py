"""CLI entry point - thin wrapper around the shared entrypoint.

This module handles CLI-specific concerns:
- Argument parsing
- One-shot prompt mode
- REPL loop
"""

import sys
import time

from reuleauxcoder.domain.approval import SharedApprovalProvider
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.interfaces.cli.agent_run import run_agent_run_cli
from reuleauxcoder.interfaces.cli.approval_handler import make_cli_handler
from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.registration import create_cli_registration
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.interfaces.entrypoint import AppRunner, AppOptions
from reuleauxcoder.interfaces.events import AgentEventBridge
from reuleauxcoder.interfaces.ui_registry import UIRegistry
from reuleauxcoder.services.llm.client import llm_is_configured, llm_unavailable_reason
from reuleauxcoder.extensions.environment.manifest import run_env_record_cli
from reuleauxcoder.extensions.auth.cli import run_auth_cli
from reuleauxcoder.extensions.db.cli import run_db_cli
from reuleauxcoder.extensions.mcp.artifacts import (
    run_mcp_artifact_cli,
    run_mcp_install_node_cli,
)
from reuleauxcoder.extensions.mcp.manifest import run_mcp_record_cli
from reuleauxcoder.extensions.provider.manifest import (
    run_provider_list_cli,
    run_provider_record_cli,
    run_provider_test_cli,
)
from reuleauxcoder.services.config.loader import (
    ConfigEnvironmentError,
    ConfigSchemaError,
    ConfigValidationError,
    ExampleConfigError,
)


def _run_once(agent, prompt: str):
    """Run a single prompt and exit."""
    agent.chat(prompt)


def main():
    """CLI main entry point."""
    args = parse_args()
    if getattr(args, "command", None) == "agent-run":
        sys.exit(run_agent_run_cli(args))
    if getattr(args, "command", None) == "db":
        sys.exit(run_db_cli(args))
    if getattr(args, "command", None) == "auth":
        sys.exit(run_auth_cli(args))
    if getattr(args, "command", None) == "env" and getattr(
        args, "env_command", None
    ) == "record":
        sys.exit(run_env_record_cli(args))
    if getattr(args, "command", None) == "provider" and getattr(
        args, "provider_command", None
    ) == "list":
        sys.exit(run_provider_list_cli(args))
    if getattr(args, "command", None) == "provider" and getattr(
        args, "provider_command", None
    ) == "record":
        sys.exit(run_provider_record_cli(args))
    if getattr(args, "command", None) == "provider" and getattr(
        args, "provider_command", None
    ) == "test":
        sys.exit(run_provider_test_cli(args))
    if getattr(args, "command", None) == "mcp" and getattr(
        args, "mcp_command", None
    ) == "record":
        sys.exit(run_mcp_record_cli(args))
    if getattr(args, "command", None) == "mcp" and getattr(
        args, "mcp_command", None
    ) == "artifact":
        sys.exit(run_mcp_artifact_cli(args))
    if getattr(args, "command", None) == "mcp" and getattr(
        args, "mcp_command", None
    ) == "install-node":
        sys.exit(run_mcp_install_node_cli(args))

    try:
        config_path = resolve_cli_config_path(
            args, require=bool(args.server), purpose="server mode"
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Build options from CLI args
    options = AppOptions(
        config_path=config_path,
        model=args.model,
        resume_session_id=args.resume,
        auto_resume_latest=True,
        server_mode=args.server,
    )

    # Initialize application using shared entrypoint
    try:
        runner = AppRunner(options)
        ctx = runner.initialize()
    except (
        ConfigEnvironmentError,
        ConfigSchemaError,
        ConfigValidationError,
        ExampleConfigError,
    ) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    ui_registry = UIRegistry([create_cli_registration(ctx.ui_bus)])
    cli_ui = ui_registry.require("cli")

    renderer = CLIRenderer(view_registry=cli_ui.view_registry)
    ctx.ui_bus.subscribe(renderer.on_ui_event)

    remote_exec = getattr(ctx.config, "remote_exec", None)
    is_host_mode = remote_exec and remote_exec.enabled and remote_exec.host_mode
    if args.server or is_host_mode:
        ctx.ui_bus.info("Remote relay host mode active. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            renderer.close()
            runner.cleanup()
        return

    ctx.ui_interactor = cli_ui.interactor
    setattr(ctx.agent, "ui_interactor", cli_ui.interactor)
    ctx.agent.approval_provider = SharedApprovalProvider(
        handler=make_cli_handler(cli_ui.interactor)
    )

    # Add CLI renderer and bridge agent events onto the UI bus
    bridge = AgentEventBridge(ctx.ui_bus)
    ctx.agent.add_event_handler(bridge.on_agent_event)

    if not llm_is_configured(ctx.agent.llm):
        ctx.ui_bus.error(llm_unavailable_reason(ctx.agent.llm))
        renderer.close()
        sys.exit(1)

    try:
        # One-shot mode
        if args.prompt:
            _run_once(ctx.agent, args.prompt)
            return

        # Interactive REPL mode
        run_repl(
            ctx.agent,
            ctx.config,
            ctx.ui_bus,
            cli_ui.profile,
            ctx.action_registry,
            ctx.current_session_id,
            ctx.sessions_dir,
            ctx.session_exit_time,
            ctx.skills_service,
        )
    finally:
        renderer.close()
        runner.cleanup()
