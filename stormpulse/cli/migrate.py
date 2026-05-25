"""CLI handler for ``stormpulse migrate-to-rootless``."""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("stormpulse")


def cmd_migrate_to_rootless(args: argparse.Namespace) -> None:
    from stormpulse.init import InitError
    from stormpulse.init.migrate import run_migration

    try:
        run_migration(force=args.force)
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)
