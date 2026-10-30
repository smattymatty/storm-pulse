"""CLI handler for ``stormpulse init``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stormpulse.init.files import user_config_dir

logger = logging.getLogger("stormpulse")


def cmd_init(args: argparse.Namespace) -> None:
    # Import feature init modules for their registration side effects: each
    # registers an install step the orchestrator runs without importing it.
    # See CORE-000 and stormpulse/init/registry.py.
    import stormpulse.garage.init  # noqa: F401
    import stormpulse.logging.init  # noqa: F401
    import stormpulse.signoff.init  # noqa: F401
    from stormpulse.init import InitError, run_init

    try:
        run_init(Path(args.creds_dir), force=args.force)
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def add_init_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    default_creds_dir = str(user_config_dir())
    parser = subparsers.add_parser(
        "init",
        help="generate config and systemd unit after enrollment",
    )
    parser.add_argument(
        "--creds-dir",
        default=default_creds_dir,
        help=f"directory containing credential files (default: {default_creds_dir})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing config and systemd unit",
    )
    parser.set_defaults(func=cmd_init)
