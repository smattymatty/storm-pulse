"""rclone as a stateless Integration (CORE-005): config, preconditions, and
commands only. No state, no discovery, no detection, no enrichers - it
drives a binary, not a resident system; the manifest import fires it."""

from __future__ import annotations

from stormpulse.integrations import Integration, register_integration
from stormpulse.rclone.commands import build_rclone_specs
from stormpulse.rclone.config import RcloneConfig, parse_rclone_config
from stormpulse.rclone.preconditions import run_preconditions


def _enabled(config: RcloneConfig) -> bool:
    return config.enabled


def _preconditions(config: RcloneConfig) -> str | None:
    return run_preconditions(config)


RCLONE_INTEGRATION = Integration(
    id="rclone",
    parse_config=parse_rclone_config,
    enabled=_enabled,
    preconditions=_preconditions,
    specs=build_rclone_specs,
)

register_integration(RCLONE_INTEGRATION)
