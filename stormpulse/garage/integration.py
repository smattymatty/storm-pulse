"""Garage as the reference Integration (CORE-005, GARAGE-001).

Wires garage's existing capability functions into one ``Integration`` contract
and registers it. Importing this module is what puts Garage on the registry;
the Entry-layer manifest does that import, the sibling of how ``cli/init.py``
imports ``garage.init`` to fire its init-step registration.
"""

from __future__ import annotations

from stormpulse.garage import discover as garage_discover
from stormpulse.garage import state as garage_state
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig, parse_garage_config
from stormpulse.garage.preconditions import run_preconditions
from stormpulse.garage.state import GarageState
from stormpulse.integrations import Integration, register_integration


def _enabled(config: GarageConfig) -> bool:
    return config.enabled


def _preconditions(config: GarageConfig) -> str | None:
    # Resolve via this module's own ``run_preconditions`` global at call time, so
    # tests patch the bootstrap seam (stormpulse.garage.integration.run_preconditions)
    # without clobbering the real orchestrator that preconditions' own tests call.
    return run_preconditions(config)


def _collect_state(config: GarageConfig) -> GarageState | None:
    return garage_state.collect_garage_state(config)


def _discover(config: GarageConfig) -> GarageState | None:
    return garage_discover.discover_garage(config)


def _state_push_interval(config: GarageConfig) -> float:
    return config.state_push_interval_seconds


GARAGE_INTEGRATION = Integration(
    id="garage",
    parse_config=parse_garage_config,
    enabled=_enabled,
    preconditions=_preconditions,
    specs=build_garage_specs,
    discover=_discover,
    collect_state=_collect_state,
    state_push_interval=_state_push_interval,
)

register_integration(GARAGE_INTEGRATION)
