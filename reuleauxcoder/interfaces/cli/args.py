"""CLI argument parsing."""

import argparse

from reuleauxcoder import __version__


def parse_args():
    parser = argparse.ArgumentParser(
        prog="rcoder",
        description="ReuleauxCoder terminal-native coding agent.",
    )
    parser.add_argument("-c", "--config", help="Path to config.yaml")
    parser.add_argument("-m", "--model", help="Select configured model profile")
    parser.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    parser.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run as a dedicated remote relay host",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    db_parser = subparsers.add_parser("db", help="Manage optional Postgres persistence")
    db_subparsers = db_parser.add_subparsers(dest="db_command")
    db_subparsers.add_parser("migrate", help="Run database migrations")
    db_subparsers.add_parser("status", help="Print current database migration revision")
    cleanup = db_subparsers.add_parser(
        "cleanup", help="Delete old persisted snapshots and terminal task events"
    )
    cleanup.add_argument("--retention-days", type=int, required=True)

    auth_parser = subparsers.add_parser("auth", help="Authentication helpers")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command")
    hash_password = auth_subparsers.add_parser(
        "hash-password", help="Generate a password hash for a configured superadmin"
    )
    hash_password.add_argument("--password")
    hash_password.add_argument("--iterations", type=int, default=260_000)
    auth_subparsers.add_parser(
        "verify-config", help="Validate remote auth configuration readiness"
    )

    env_parser = subparsers.add_parser(
        "env", help="Record lightweight environment requirement manifest entries"
    )
    env_subparsers = env_parser.add_subparsers(dest="env_command")
    env_record = env_subparsers.add_parser(
        "record", help="Record a server-authoritative executable requirement"
    )
    env_record.add_argument("tool_name")
    env_record.add_argument("--command", required=True, dest="tool_command")
    env_record.add_argument("--check", required=True)
    env_record.add_argument("--install")
    env_record.add_argument("--tag", action="append", default=[])
    env_record.add_argument("--version")
    env_record.add_argument("--source")
    env_record.add_argument("--description")

    provider_parser = subparsers.add_parser(
        "provider", help="Manage server-side LLM providers"
    )
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command")

    provider_list = provider_subparsers.add_parser(
        "list", help="List configured LLM providers"
    )

    provider_record = provider_subparsers.add_parser(
        "record", help="Record or update an LLM provider entry"
    )
    provider_record.add_argument("provider_id")
    provider_record.add_argument(
        "--type",
        required=True,
        dest="provider_type",
        choices=["openai_chat", "anthropic_messages", "openai_responses"],
    )
    provider_record.add_argument(
        "--compat",
        choices=["generic", "deepseek", "kimi", "glm", "qwen", "zenmux"],
    )
    provider_record.add_argument("--api-key")
    provider_record.add_argument("--api-key-env")
    provider_record.add_argument("--base-url")
    provider_record.add_argument("--base-url-env")
    provider_record.add_argument("--header", action="append", default=[])
    provider_record.add_argument("--timeout-sec", type=int, default=120)
    provider_record.add_argument("--max-retries", type=int, default=3)
    provider_record.add_argument("--api-feature", action="append", default=[])
    provider_record.add_argument("--extra", action="append", default=[])

    provider_test = provider_subparsers.add_parser(
        "test", help="Run an explicit provider smoke test"
    )
    provider_test.add_argument("provider_id")
    provider_test.add_argument("--model", required=True)
    provider_test.add_argument("--prompt", default="ping")

    mcp_parser = subparsers.add_parser("mcp", help="Manage MCP configuration")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command")

    mcp_record = mcp_subparsers.add_parser(
        "record", help="Record a server-authoritative MCP manifest entry"
    )
    mcp_record.add_argument("server_name")
    mcp_record.add_argument("--command", required=True, dest="mcp_tool_command")
    mcp_record.add_argument("--arg", action="append", dest="mcp_arg", default=[])
    mcp_record.add_argument("--env", action="append", default=[])
    mcp_record.add_argument(
        "--placement",
        choices=["server", "peer", "both"],
        default="peer",
    )
    mcp_record.add_argument(
        "--distribution",
        choices=["command", "artifact"],
        default="command",
    )
    mcp_record.add_argument("--version")
    mcp_record.add_argument("--check")
    mcp_record.add_argument("--install")
    mcp_record.add_argument("--environment-requirement-ref", action="append", default=[])
    mcp_record.add_argument("--source")
    mcp_record.add_argument("--description")

    install_node = mcp_subparsers.add_parser(
        "install-node", help="Install a Node/npx MCP server"
    )
    install_node.add_argument("server_name")
    install_node.add_argument("--package", required=True, dest="package")
    install_node.add_argument("--bin", required=True, dest="bin")
    install_node.add_argument(
        "--placement",
        choices=["server", "peer", "both"],
        default="server",
    )
    install_node.add_argument("--platform", nargs="+", action="append")
    install_node.add_argument("--arg", action="append", dest="node_arg", default=[])
    install_node.add_argument("--env", action="append", default=[])

    artifact_parser = mcp_subparsers.add_parser(
        "artifact", help="Manage server-hosted MCP artifacts"
    )
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_command")

    build_node = artifact_subparsers.add_parser(
        "build-node", help="Build a lightweight Node/npx MCP artifact"
    )
    build_node.add_argument("server_name")
    build_node.add_argument("--package", required=True, dest="package")
    build_node.add_argument("--bin", required=True, dest="bin")
    build_node.add_argument("--platform", required=True, nargs="+")

    import_artifact = artifact_subparsers.add_parser(
        "import", help="Import an existing peer MCP artifact archive"
    )
    import_artifact.add_argument("server_name")
    import_artifact.add_argument("version")
    import_artifact.add_argument("platform")
    import_artifact.add_argument("archive")

    list_artifacts = artifact_subparsers.add_parser(
        "list", help="List configured MCP artifacts"
    )
    list_artifacts.add_argument("server_name", nargs="?")

    verify_artifacts = artifact_subparsers.add_parser(
        "verify", help="Verify configured MCP artifact checksums"
    )
    verify_artifacts.add_argument("server_name", nargs="?")
    return parser.parse_args()
