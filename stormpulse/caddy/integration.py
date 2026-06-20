"""Caddy as the second reference Integration (CORE-005).

caddy declares only the capabilities it has: config, an enabled predicate,
soft-disabling preconditions, commands, and long-running factories. It has no
discovery surface and no periodic state loop, so it declares neither - the
opt-in capability model means no empty stubs. Its live wire ``state`` is the
empty object the envelope emits for a live Integration with nothing collected.
"""

from __future__ import annotations

from stormpulse.caddy.commands import build_caddy_specs
from stormpulse.caddy.config import CaddyConfig, parse_caddy_config
from stormpulse.caddy.preconditions import run_preconditions
from stormpulse.integrations import Integration, register_integration


def _enabled(config: CaddyConfig) -> bool:
    return config.enabled


CADDY_INTEGRATION = Integration(
    id="caddy",
    parse_config=parse_caddy_config,
    enabled=_enabled,
    preconditions=run_preconditions,
    specs=build_caddy_specs,
)

register_integration(CADDY_INTEGRATION)
