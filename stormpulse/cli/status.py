"""CLI handler for ``stormpulse status``."""

from __future__ import annotations

import argparse
from pathlib import Path


def cmd_status(args: argparse.Namespace) -> None:
    from stormpulse.status import collect_status, print_status

    info = collect_status(Path(args.config))
    print_status(info)
