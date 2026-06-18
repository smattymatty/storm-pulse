"""CLI handler for ``stormpulse config check`` (CORE-005 decisions 5, 7).

Validates the TOML without booting: core config is fatal (exit 1, the line
``stormpulse update`` gates on), each Integration section is reported as it
would resolve at boot. Live preconditions are NOT run here - they touch the
running system (docker, the Caddy admin API); this is a pre-flight, the loud
restart is the real fail-fast.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_config_check(args: argparse.Namespace) -> None:
    import stormpulse.agent.integrations_manifest  # noqa: F401  (registers Integrations)
    from stormpulse.config import ConfigError, load_config
    from stormpulse.integrations import registered_integrations

    path = Path(args.config)
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"FATAL: core config invalid: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        config.validate_paths()
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Core config OK: {path}")

    known = {integ.id for integ in registered_integrations()}
    for integ in registered_integrations():
        raw = config.integrations.get(integ.id)
        if raw is None:
            continue
        try:
            ic = integ.parse_config(raw)
        except ConfigError as exc:
            print(f"  [{integ.id}] disabled_error (config): {exc}")
            continue
        if not integ.enabled(ic):
            print(f"  [{integ.id}] disabled_choice (enabled = false)")
            continue
        print(
            f"  [{integ.id}] config OK, enabled "
            "(preconditions run at boot, not here)"
        )

    for key in config.integrations:
        if key not in known:
            print(
                f"  [{key}] unknown section: no registered integration claims it; "
                "it will be ignored at boot"
            )
