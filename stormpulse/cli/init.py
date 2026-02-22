"""CLI handler for ``stormpulse init``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("stormpulse")


def cmd_init(args: argparse.Namespace) -> None:
    from stormpulse.init import InitError, run_init

    try:
        run_init(Path(args.creds_dir), force=args.force)
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)
