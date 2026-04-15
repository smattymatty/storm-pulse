"""CLI handler for ``stormpulse logging`` subcommand group."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = "/etc/stormpulse/stormpulse.toml"


def cmd_logging_init(args: argparse.Namespace) -> None:
    from stormpulse.init import InitError
    from stormpulse.logging.init import run_logging_init

    try:
        run_logging_init(Path(args.config))
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def add_logging_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``logging`` subcommand group with nested subcommands."""
    logging_parser = subparsers.add_parser(
        "logging", help="log shipping configuration",
    )
    logging_sub = logging_parser.add_subparsers(dest="logging_command")

    init_parser = logging_sub.add_parser(
        "init", help="detect running containers and configure log shipping",
    )
    init_parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"path to stormpulse.toml (default: {_DEFAULT_CONFIG})",
    )
