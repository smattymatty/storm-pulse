"""CLI handler for ``stormpulse rclone`` subcommand group."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stormpulse.init.files import default_config_path

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = default_config_path()


def cmd_rclone_init(args: argparse.Namespace) -> None:
    from stormpulse.init import InitError
    from stormpulse.rclone.init import run_rclone_init

    try:
        run_rclone_init(
            Path(args.config),
            binary_path_override=args.binary_path,
            force=args.force,
        )
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def add_rclone_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``rclone`` subcommand group with nested subcommands."""
    rclone_parser = subparsers.add_parser(
        "rclone",
        help="rclone migration/backup Runner integration",
    )
    rclone_sub = rclone_parser.add_subparsers(dest="rclone_command")

    # --- rclone init ---
    init_parser = rclone_sub.add_parser(
        "init",
        help="detect rclone and configure this box as a backup Runner",
    )
    init_parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"path to stormpulse.toml (default: {_DEFAULT_CONFIG})",
    )
    init_parser.add_argument(
        "--binary-path",
        default=None,
        help="path to the rclone binary (overrides auto-detection)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing [rclone] section",
    )
