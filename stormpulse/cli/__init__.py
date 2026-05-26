"""CLI argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from stormpulse import __version__

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = "/etc/stormpulse/stormpulse.toml"
_DEFAULT_CREDS_DIR = "/etc/stormpulse"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="stormpulse",
        description="Storm Pulse agent - secure server management over WebSocket",
    )
    parser.add_argument(
        "--version", action="version", version=f"storm-pulse-agent v{__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- run subcommand ---
    run_parser = subparsers.add_parser("run", help="start the agent")
    run_parser.add_argument(
        "config",
        nargs="?",
        default=_DEFAULT_CONFIG,
        help=f"path to config file (default: {_DEFAULT_CONFIG})",
    )

    # --- enroll subcommand ---
    enroll_parser = subparsers.add_parser(
        "enroll", help="enroll this agent with the dashboard",
    )
    enroll_parser.add_argument("endpoint", help="enrollment URL")
    enroll_parser.add_argument("agent_id", help="unique agent identifier")
    enroll_parser.add_argument("token", help="one-time enrollment token from dashboard")
    enroll_parser.add_argument(
        "--creds-dir",
        default=_DEFAULT_CREDS_DIR,
        help=f"directory for credential files (default: {_DEFAULT_CREDS_DIR})",
    )
    enroll_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing credential files",
    )

    # --- init subcommand ---
    init_parser = subparsers.add_parser(
        "init", help="generate config and systemd unit after enrollment",
    )
    init_parser.add_argument(
        "--creds-dir",
        default=_DEFAULT_CREDS_DIR,
        help=f"directory containing credential files (default: {_DEFAULT_CREDS_DIR})",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing config and systemd unit",
    )
    mode_group = init_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--system",
        dest="mode",
        action="store_const",
        const="system",
        help="force system install (legacy rootful path)",
    )
    mode_group.add_argument(
        "--user",
        dest="mode",
        action="store_const",
        const="user",
        help="force user install (rootless / user systemd unit)",
    )

    # --- migrate-to-rootless subcommand ---
    migrate_parser = subparsers.add_parser(
        "migrate-to-rootless",
        help="convert an existing system install to user (rootless) mode",
    )
    migrate_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing user-mode files if a previous migration left them",
    )

    # --- status subcommand ---
    status_parser = subparsers.add_parser("status", help="show agent status")
    status_parser.add_argument(
        "config",
        nargs="?",
        default=_DEFAULT_CONFIG,
        help=f"path to config file (default: {_DEFAULT_CONFIG})",
    )

    # --- garage subcommand group ---
    from stormpulse.cli.garage import add_garage_subparser
    add_garage_subparser(subparsers)

    # --- caddy subcommand group ---
    from stormpulse.cli.caddy import add_caddy_subparser
    add_caddy_subparser(subparsers)

    # --- logging subcommand group ---
    from stormpulse.cli.log import add_logging_subparser
    add_logging_subparser(subparsers)

    # --- signoff subcommand group ---
    from stormpulse.cli.signoff import add_signoff_subparser
    add_signoff_subparser(subparsers)

    args = parser.parse_args()

    log_level = os.environ.get("STORMPULSE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.command == "enroll":
        from stormpulse.cli.enroll import cmd_enroll
        cmd_enroll(args)
    elif args.command == "init":
        from stormpulse.cli.init import cmd_init
        cmd_init(args)
    elif args.command == "migrate-to-rootless":
        from stormpulse.cli.migrate import cmd_migrate_to_rootless
        cmd_migrate_to_rootless(args)
    elif args.command == "run":
        from stormpulse.cli.run import cmd_run
        cmd_run(args)
    elif args.command == "status":
        from stormpulse.cli.status import cmd_status
        cmd_status(args)
    elif args.command == "garage":
        if getattr(args, "garage_command", None) == "init":
            from stormpulse.cli.garage import cmd_garage_init
            cmd_garage_init(args)
        else:
            print("Usage: stormpulse garage <subcommand>\n", file=sys.stderr)
            print("Subcommands:", file=sys.stderr)
            print("  init     Detect and configure Garage integration", file=sys.stderr)
            sys.exit(1)
    elif args.command == "caddy":
        if getattr(args, "caddy_command", None) == "init":
            from stormpulse.cli.caddy import cmd_caddy_init
            cmd_caddy_init(args)
        else:
            print("Usage: stormpulse caddy <subcommand>\n", file=sys.stderr)
            print("Subcommands:", file=sys.stderr)
            print("  init     Detect and configure Caddy integration", file=sys.stderr)
            sys.exit(1)
    elif args.command == "logging":
        if getattr(args, "logging_command", None) == "init":
            from stormpulse.cli.log import cmd_logging_init
            cmd_logging_init(args)
        else:
            print("Usage: stormpulse logging <subcommand>\n", file=sys.stderr)
            print("Subcommands:", file=sys.stderr)
            print("  init     Detect containers and configure log shipping", file=sys.stderr)
            sys.exit(1)
    elif args.command == "signoff":
        signoff_cmd = getattr(args, "signoff_command", None)
        if signoff_cmd == "status":
            from stormpulse.cli.signoff import cmd_signoff_status
            cmd_signoff_status(args)
        elif signoff_cmd == "seal":
            from stormpulse.cli.signoff import cmd_signoff_seal
            cmd_signoff_seal(args)
        elif signoff_cmd == "unseal":
            from stormpulse.cli.signoff import cmd_signoff_unseal
            cmd_signoff_unseal(args)
        else:
            print("Usage: stormpulse signoff <subcommand>\n", file=sys.stderr)
            print("Subcommands:", file=sys.stderr)
            print("  status   Show whether verify-block dispatch is sealed", file=sys.stderr)
            print("  seal     Disable verify-block dispatch on this agent", file=sys.stderr)
            print("  unseal   Re-enable verify-block dispatch on this agent", file=sys.stderr)
            sys.exit(1)
    elif args.command is None:
        # Detect old syntax: stormpulse /path/to/config
        if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
            logger.error(
                "Unknown command: %s. Did you mean: stormpulse run %s",
                sys.argv[1], sys.argv[1],
            )
        else:
            print("Usage: stormpulse <command> [options]\n", file=sys.stderr)
            print("Commands:", file=sys.stderr)
            print("  run                  Start the agent", file=sys.stderr)
            print("  enroll               Enroll this agent with the dashboard", file=sys.stderr)
            print("  init                 Generate config and systemd unit", file=sys.stderr)
            print("  migrate-to-rootless  Convert system install to user mode", file=sys.stderr)
            print("  status               Show agent status", file=sys.stderr)
            print("  garage               Garage S3 node management", file=sys.stderr)
            print("  caddy                Caddy reverse-proxy integration", file=sys.stderr)
            print("  logging              Log shipping configuration", file=sys.stderr)
            print("  signoff              Manage the verify-block seal (ADR CORE-004)", file=sys.stderr)
            print(
                "\nRun 'stormpulse <command> --help' for details.",
                file=sys.stderr,
            )
        sys.exit(1)
