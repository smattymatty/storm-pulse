"""CLI handler for ``stormpulse config check`` (CORE-005 decisions 5, 7).

Validates the TOML without booting: core config is fatal (exit 1, the line
``stormpulse update`` gates on), each Integration section is reported as it
would resolve at boot. Live preconditions are NOT run here - they touch the
running system (docker, the Caddy admin API); this is a pre-flight, the loud
restart is the real fail-fast.

Sealed external adapters (CORE-007) are loaded here exactly as boot loads them,
from the same state dir, so a ``[section]`` a sealed adapter claims resolves the
same way in both places. Before this the pre-flight knew only the built-ins and
told the operator a sealed adapter's section "will be ignored at boot" while
boot went on to load it: a warning identical for "fine" and "broken" is a dead
knob (CONTEXT.md, privacy by design). Loading reads the grant tree and imports
the package; it writes nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# The loader reports a granted adapter it could not load through these loggers
# (soft-disable, never an exception). The CLI configures no logging, so without
# a handler the reason would vanish and the section would read as merely
# unknown; the reason is the whole point of running the loader pre-flight.
_LOADER_LOGGERS = (
    "stormpulse.agent.external_adapters",
    "stormpulse.integrations.external.loader",
)


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

    # Same state dir, same call as ``build_agent_dependencies``: the pre-flight
    # resolves external sections the way boot will, not the way it guesses.
    external_ids = _load_external_reporting_failures(config.storage.db_path.parent)

    known = {integ.id for integ in registered_integrations()}
    for integ in registered_integrations():
        raw = config.integrations.get(integ.id)
        if raw is None:
            continue
        origin = " (external adapter, sealed grant)" if integ.id in external_ids else ""
        try:
            ic = integ.parse_config(raw)
        except ConfigError as exc:
            print(f"  [{integ.id}] disabled_error (config): {exc}{origin}")
            continue
        if not integ.enabled(ic):
            print(f"  [{integ.id}] disabled_choice (enabled = false){origin}")
            continue
        print(
            f"  [{integ.id}] config OK, enabled{origin} "
            "(preconditions run at boot, not here)"
        )

    for key in config.integrations:
        if key not in known:
            print(
                f"  [{key}] unknown section: no built-in and no sealed external "
                "adapter claims it; it will be ignored at boot"
            )


def _load_external_reporting_failures(state_dir: Path) -> frozenset[str]:
    """Run the external loader with its soft-disable warnings printed inline,
    indented like every other line of the report, then detach the handler."""
    from stormpulse.agent.external_adapters import load_and_register_external

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("  %(message)s"))
    loggers = [logging.getLogger(name) for name in _LOADER_LOGGERS]
    saved_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.WARNING:
            lg.setLevel(logging.WARNING)
    try:
        return load_and_register_external(state_dir)
    finally:
        for lg, level in zip(loggers, saved_levels, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)
