"""Caddy Integration config: its own typed dataclass and parser.

CORE-005 decision 4: each Integration owns its typed config, so ``CaddyConfig``
lives here, not in Foundation. ``parse_caddy_config`` is called by the bootstrap
loop with the raw ``[caddy]`` table; a ConfigError it raises soft-disables the
Caddy Integration (CORE-005 decision 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stormpulse.config import ConfigError, require_key


@dataclass(frozen=True, slots=True)
class CaddyConfig:
    """Typed [caddy] section - Caddy admin API + drop-in management.

    Present only on regional-VPS agents that host customer custom-domain
    serving. The agent uses ``admin_url`` to POST Caddyfile fragments,
    ``main_caddyfile`` for the boot-time import check, and ``drop_in_path``
    as the persisted location of the per-region fragment.
    """

    enabled: bool
    admin_url: str
    main_caddyfile: Path
    drop_in_path: Path


def parse_caddy_config(section: dict[str, Any]) -> CaddyConfig:
    """Parse the raw [caddy] table into a typed CaddyConfig.

    Raises ConfigError on any invalid value; the bootstrap loop turns that into
    a soft-disable for Caddy alone (CORE-005 decision 5).
    """
    enabled = require_key(section, "enabled", bool, "caddy")
    admin_url = require_key(section, "admin_url", str, "caddy")
    if not admin_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"'admin_url' in [caddy] must start with http:// or https://, "
            f"got {admin_url!r}"
        )
    main_raw = require_key(section, "main_caddyfile", str, "caddy")
    if not main_raw.startswith("/"):
        raise ConfigError(
            f"'main_caddyfile' in [caddy] must be an absolute path, got {main_raw!r}"
        )
    drop_in_raw = require_key(section, "drop_in_path", str, "caddy")
    if not drop_in_raw.startswith("/"):
        raise ConfigError(
            f"'drop_in_path' in [caddy] must be an absolute path, got {drop_in_raw!r}"
        )

    return CaddyConfig(
        enabled=enabled,
        admin_url=admin_url,
        main_caddyfile=Path(main_raw),
        drop_in_path=Path(drop_in_raw),
    )
