"""Garage node discovery - checks if this agent manages a Garage node."""

from __future__ import annotations

import logging

from stormpulse.config import GarageConfig
from stormpulse.garage.state import GarageState, collect_garage_state

logger = logging.getLogger(__name__)


def discover_garage(config: GarageConfig | None) -> GarageState | None:
    """Detect if this is a Garage node and return initial state.

    Returns None if:
    - config.garage is None (no [garage] section)
    - config.garage.enabled is False
    - config_path doesn't exist on disk
    - garage status fails or returns no nodes

    Called once during registration to include initial state in the
    register payload.
    """
    if config is None:
        return None

    if not config.enabled:
        logger.debug("Garage integration disabled in config")
        return None

    if not config.config_path.is_file():
        logger.warning(
            "Garage config_path does not exist: %s", config.config_path,
        )
        return None

    logger.info("Garage node detected, collecting initial state")
    return collect_garage_state(config)
