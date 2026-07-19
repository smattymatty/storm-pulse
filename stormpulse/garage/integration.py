"""Garage as the reference Integration (CORE-005): wires garage's
capability functions into one registered contract; the manifest import fires it."""

from __future__ import annotations

from collections.abc import Mapping

from stormpulse.garage import discover as garage_discover
from stormpulse.garage import state as garage_state
from stormpulse.garage.bucket_resolver import BucketIdResolver
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig, parse_garage_config
from stormpulse.garage.preconditions import run_preconditions
from stormpulse.garage.state import GarageBucket, GarageState
from stormpulse.garage.investigate import run_health
from stormpulse.integrations import (
    Detector,
    Integration,
    InvestigationSpec,
    register_integration,
)
from stormpulse.sdk import Capability


def _enabled(config: GarageConfig) -> bool:
    return config.enabled


def _preconditions(config: GarageConfig) -> str | None:
    # Resolved via this module's global at call time, so tests patch the bootstrap
    # seam without clobbering the real orchestrator.
    return run_preconditions(config)


# One stateful reader per process: the periodic loop and on-demand refresh share
# it, so topology's slow-multiple cadence and cache persist across reconnects
# (topology does not change on reconnect). Discovery uses the full
# ``collect_garage_state`` directly (see ``_discover``).
_state_reader = garage_state.GarageStateReader()


def _collect_state(config: GarageConfig) -> GarageState | None:
    return _state_reader.collect(config)


def _collect_state_fresh(config: GarageConfig) -> GarageState | None:
    """On-demand ``garage_refresh`` path: bypass the topology cache so an
    operator's layout change (capacity, zones) is visible immediately."""
    return _state_reader.collect(config, force_topology=True)


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
    """Tick-fresh ``(key_id, name) -> bucket_id`` map for ``garage_s3`` lines
   ; a None/foreign state builds the honest empty resolver."""
    return BucketIdResolver.from_state(state if isinstance(state, GarageState) else None)


GARAGE_INTEGRATION = Integration(
    id="garage",
    parse_config=parse_garage_config,
    enabled=_enabled,
    preconditions=_preconditions,
    specs=build_garage_specs,
    discover=_discover,
    collect_state=_collect_state,
    collect_state_fresh=_collect_state_fresh,
    detect=Detector(run=_detect, interval=_detect_interval),
    read_affected=_read_affected,
    log_enrichers={"garage_s3": _log_enricher},
    capabilities=(Capability("garage.admin.v1", "garage"),),
    investigations=(
        InvestigationSpec(
            name="health",
            title="garage daemon restarts and maintenance load",
            run=run_health,
        ),
    ),
)

register_integration(GARAGE_INTEGRATION)
