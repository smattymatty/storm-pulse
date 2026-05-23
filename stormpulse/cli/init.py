"""CLI handler for ``stormpulse init``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("stormpulse")


def cmd_init(args: argparse.Namespace) -> None:
    # Import feature init modules for their registration side effects: each
    # registers an install step the orchestrator runs without importing it.
    # See CORE-000 and stormpulse/init/registry.py.
    import stormpulse.garage.init  # noqa: F401
    import stormpulse.logging.init  # noqa: F401
    from stormpulse.init import InitError, run_init

    try:
        run_init(Path(args.creds_dir), force=args.force)
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)
