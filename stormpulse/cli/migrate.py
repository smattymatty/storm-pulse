"""CLI handler for ``stormpulse migrate-to-rootless``."""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("stormpulse")


def cmd_migrate_to_rootless(args: argparse.Namespace) -> None:
    # Same side-effect imports as `init`, so every feature's step is registered.
    import stormpulse.garage.init  # noqa: F401
    import stormpulse.logging.init  # noqa: F401
    import stormpulse.signoff.init  # noqa: F401
    from stormpulse.init import InitError
    from stormpulse.init.migrate import run_migration

    try:
        run_migration(force=args.force)
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)
