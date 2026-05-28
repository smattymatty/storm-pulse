"""CLI handler for ``stormpulse caddy`` subcommand group."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stormpulse.init.files import default_config_path

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = default_config_path()


def cmd_caddy_init(args: argparse.Namespace) -> None:
    from stormpulse.caddy.init import run_caddy_init
    from stormpulse.init import InitError

    try:
        run_caddy_init(
            Path(args.config),
            main_caddyfile_override=args.main_caddyfile,
            force=args.force,
        )
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def add_caddy_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``caddy`` subcommand group with nested subcommands."""
    caddy_parser = subparsers.add_parser(
        "caddy", help="Caddy reverse-proxy integration",
    )
    caddy_sub = caddy_parser.add_subparsers(dest="caddy_command")

    # --- caddy init ---
    init_parser = caddy_sub.add_parser(
        "init", help="detect and configure Caddy integration",
    )
    init_parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"path to stormpulse.toml (default: {_DEFAULT_CONFIG})",
    )
    init_parser.add_argument(
        "--main-caddyfile",
        default=None,
        help="path to main Caddyfile (overrides auto-detection)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing [caddy] section",
    )
