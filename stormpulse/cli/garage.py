"""CLI handler for ``stormpulse garage`` subcommand group."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stormpulse.init.files import default_config_path

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = default_config_path()


def cmd_garage_init(args: argparse.Namespace) -> None:
    from stormpulse.init import InitError
    from stormpulse.garage.init import run_garage_init

    try:
        run_garage_init(
            Path(args.config),
            garage_config_override=args.garage_config,
            force=args.force,
        )
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def add_garage_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``garage`` subcommand group with nested subcommands."""
    garage_parser = subparsers.add_parser(
        "garage", help="Garage S3 node management",
    )
    garage_sub = garage_parser.add_subparsers(dest="garage_command")

    # --- garage init ---
    init_parser = garage_sub.add_parser(
        "init", help="detect and configure Garage integration",
    )
    init_parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"path to stormpulse.toml (default: {_DEFAULT_CONFIG})",
    )
    init_parser.add_argument(
        "--garage-config",
        default=None,
        help="path to Garage config file (overrides auto-detection)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing [garage] section",
    )
