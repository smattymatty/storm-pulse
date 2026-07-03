"""Garage as the reference Integration (CORE-005, GARAGE-001).

Wires garage's existing capability functions into one ``Integration`` contract
and registers it. Importing this module is what puts Garage on the registry;
the Entry-layer manifest does that import, the sibling of how ``cli/init.py``
imports ``garage.init`` to fire its init-step registration.
"""

from __future__ import annotations

from collections.abc import Mapping

from stormpulse.garage import discover as garage_discover
from stormpulse.garage import state as garage_state
from stormpulse.garage.bucket_resolver import BucketIdResolver
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig, parse_garage_config
from stormpulse.garage.preconditions import run_preconditions
from stormpulse.garage.state import GarageBucket, GarageState
from stormpulse.integrations import Detector, Integration, register_integration


def _enabled(config: GarageConfig) -> bool:
    return config.enabled


def _preconditions(config: GarageConfig) -> str | None:
    # Resolve via this module's own ``run_preconditions`` global at call time, so
    # tests patch the bootstrap seam (stormpulse.garage.integration.run_preconditions)
    # without clobbering the real orchestrator that preconditions' own tests call.
    return run_preconditions(config)


# One stateful reader per process: the periodic loop and on-demand refresh share
# it, so topology's slow-multiple cadence and cache persist across reconnects
# (topology does not change on reconnect). Discovery uses the full
# ``collect_garage_state`` directly (see ``_discover``).
_state_reader = garage_state.GarageStateReader()


def _collect_state(config: GarageConfig) -> GarageState | None:
    return _state_reader.collect(config)


def _discover(config: GarageConfig) -> GarageState | None:
    return garage_discover.discover_garage(config)


def _detect(config: GarageConfig, current_state: GarageState | None) -> list[GarageBucket]:
    return garage_state.detect_new_buckets(config, current_state)


def _detect_interval(config: GarageConfig) -> float:
    return config.detector_interval_seconds


def _read_affected(
    config: GarageConfig, state: GarageState, params: Mapping[str, str]
) -> list[GarageBucket]:
    """Post-mutation targeted re-read: plan the affected ids, cap the fan-out, read only those."""
    ids = garage_state.affected_bucket_ids(params, state)
    if not ids:
        return []
    capped = garage_state.cap_targeted_reads(ids, context="Post-mutation")
    return garage_state.read_buckets_by_id(config, capped)


def _log_enricher(state: object) -> BucketIdResolver:
    """Tick-fresh ``(key_id, name) -> bucket_id`` map for ``garage_s3`` lines (BUCKETS-015).

    A ``None`` / foreign state builds the empty resolver: every lookup returns
    ``""``, the honest "no enrichment available" the wire shape already carries.
    """
    return BucketIdResolver.from_state(state if isinstance(state, GarageState) else None)


GARAGE_INTEGRATION = Integration(
    id="garage",
    parse_config=parse_garage_config,
    enabled=_enabled,
    preconditions=_preconditions,
    specs=build_garage_specs,
    discover=_discover,
    collect_state=_collect_state,
    detect=Detector(run=_detect, interval=_detect_interval),
    read_affected=_read_affected,
    log_enrichers={"garage_s3": _log_enricher},
)

register_integration(GARAGE_INTEGRATION)
