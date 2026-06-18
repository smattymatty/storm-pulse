"""CLI handler for ``stormpulse status``."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path


def cmd_status(args: argparse.Namespace) -> None:
    from stormpulse.signoff import (
        SignoffState,
        format_unsealed_duration,
        state_dir_from_db_path,
    )
    from stormpulse.status import collect_status, print_status

    info = collect_status(Path(args.config))
    # CLI (Entry) enriches the StatusInfo with seal state - the signoff
    # Feature can't be imported from the sibling-Feature ``status`` module
    # under CORE-000's no-sibling-Features rule.
    if info.db_path != Path("unknown"):
        state = SignoffState(state_dir_from_db_path(info.db_path))
        info = replace(
            info,
            signoff_sealed=state.is_sealed(),
            unsealed_duration=(
                None
                if state.is_sealed()
                else format_unsealed_duration(state.unsealed_since())
            ),
        )
    print_status(info)
