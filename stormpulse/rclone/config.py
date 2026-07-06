"""rclone Integration config. A ConfigError here soft-disables rclone
alone (CORE-005 decision 5); it never aborts the agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stormpulse.config import ConfigError, optional_key, require_key

_DEFAULT_BINARY = "/usr/bin/rclone"


@dataclass(frozen=True, slots=True)
class RcloneConfig:
    """Typed [rclone] section - S3-to-S3 migration jobs on a Runner."""

    enabled: bool
    binary_path: str = _DEFAULT_BINARY


def parse_rclone_config(section: dict[str, Any]) -> RcloneConfig:
    """Parse the raw [rclone] table. Two keys only; everything else is a
    constant, not a knob."""
    enabled = require_key(section, "enabled", bool, "rclone")
    binary_path = optional_key(section, "binary_path", str, _DEFAULT_BINARY, "rclone")
    if not binary_path.startswith("/"):
        raise ConfigError(
            f"'binary_path' in [rclone] must be an absolute path, got {binary_path!r}"
        )
    return RcloneConfig(enabled=enabled, binary_path=binary_path)
