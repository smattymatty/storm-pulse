"""CLI handler for ``stormpulse restart``: restarts the agent's user unit.

Also the workaround for the stuck seal-state banner: the dashboard learns
signoff state only at register (ADR core/004). The helper lives in
``stormpulse.init.system`` so ``update`` and the feature inits share it
without crossing the import-linter layers.
"""

from __future__ import annotations

import argparse
import sys

from stormpulse.init.system import restart_user_unit


def cmd_restart(args: argparse.Namespace) -> None:
    del args  # the subcommand takes no flags today
    code = restart_user_unit()
    if code != 0:
        sys.exit(code)
