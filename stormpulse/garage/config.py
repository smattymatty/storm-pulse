"""Garage Integration config: its own typed dataclass and parser.

CORE-005 decision 4: each Integration owns its typed config, so ``GarageConfig``
lives here, not in Foundation. ``parse_garage_config`` is called by the bootstrap
loop with the raw ``[garage]`` table; a ConfigError it raises soft-disables the
Garage Integration (CORE-005 decision 5), it does not abort the agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stormpulse.config import ConfigError, optional_key, require_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GarageConfig:
    """Typed [garage] section - Garage S3 node management."""

    enabled: bool
    container_name: str
    garage_binary: str
    docker_binary: str
    config_path: Path
    state_push_interval_seconds: float
    # Garage admin HTTP API (port 3903). Optional: empty when not configured,
    # in which case admin-API-backed commands (set-quota) fail loudly rather
    # than silently. admin_token is the resolved Bearer token (a node secret,
    # never sent over the wire), read inline or from admin_token_file.
    admin_url: str = ""
    admin_token: str = ""


def parse_garage_config(section: dict[str, Any]) -> GarageConfig:
    """Parse the raw [garage] table into a typed GarageConfig.

    Raises ConfigError on any invalid value; the bootstrap loop turns that into
    a soft-disable for Garage alone (CORE-005 decision 5).
    """
    enabled = require_key(section, "enabled", bool, "garage")
    container_name = require_key(section, "container_name", str, "garage")
    if not container_name:
        raise ConfigError("'container_name' in [garage] must not be empty")
    garage_binary = require_key(section, "garage_binary", str, "garage")
    if not garage_binary:
        raise ConfigError("'garage_binary' in [garage] must not be empty")
    docker_binary = require_key(section, "docker_binary", str, "garage")
    if not docker_binary.startswith("/"):
        raise ConfigError(
            f"'docker_binary' in [garage] must be an absolute path, got {docker_binary!r}"
        )
    config_path = Path(require_key(section, "config_path", str, "garage"))
    interval = float(
        require_key(section, "state_push_interval_seconds", (int, float), "garage")
    )
    if interval <= 0:
        raise ConfigError("'state_push_interval_seconds' in [garage] must be positive")

    # Optional admin HTTP API wiring. admin_token may be given inline or, like
    # Garage's own garage.toml, via a file path; the file is read once at
    # startup. Both absent means the admin API is simply not configured.
    admin_url = optional_key(section, "admin_url", str, "", "garage")
    if admin_url and not admin_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"'admin_url' in [garage] must start with http:// or https://, got {admin_url!r}"
        )
    admin_token = optional_key(section, "admin_token", str, "", "garage")
    admin_token_file = optional_key(section, "admin_token_file", str, "", "garage")
    if admin_token_file and not admin_token:
        try:
            admin_token = Path(admin_token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            # The admin API is optional; a bad token path must NOT crash the whole
            # agent into a restart loop. Degrade: disable the admin API (quota
            # writes then fail loudly per-command) and keep everything else running.
            logger.warning(
                "[garage] admin_token_file %r could not be read (%s); disabling the "
                "Garage admin API. Quota writes will fail until this is fixed; the "
                "rest of the agent runs normally.",
                admin_token_file, exc,
            )
            admin_token = ""

    return GarageConfig(
        enabled=enabled,
        container_name=container_name,
        garage_binary=garage_binary,
        docker_binary=docker_binary,
        config_path=config_path,
        state_push_interval_seconds=interval,
        admin_url=admin_url,
        admin_token=admin_token,
    )
