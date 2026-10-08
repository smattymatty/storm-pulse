"""Garage as the reference Integration (CORE-005): wires garage's
capability functions into one registered contract; the manifest import fires it."""

from __future__ import annotations

from collections.abc import Mapping

from stormpulse.garage import discover as garage_discover
from stormpulse.garage import state as garage_state
from stormpulse.garage.bucket_resolver import BucketIdResolver
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig, parse_garage_config
from stormpulse.garage.investigate import run_health
from stormpulse.garage.preconditions import run_preconditions
from stormpulse.garage.state import GarageBucket, GarageState
from stormpulse.integrations import (
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


# One stateful reader per process: the periodic loop, on-demand refresh and the
# post-mutation hook share it, so its cadences and cache persist across
# reconnects. Discovery uses the full ``collect_garage_state`` (see ``_discover``).
_state_reader = garage_state.GarageStateReader()


def _collect_state(config: GarageConfig) -> GarageState | None:
    return _state_reader.collect(config)


def _collect_state_fresh(config: GarageConfig) -> GarageState | None:
    """On-demand ``garage_refresh`` path: re-read topology and sweep now, so an
    operator's change (capacity, zones, buckets) is visible immediately."""
    return _state_reader.collect(config, fresh=True)


def _discover(config: GarageConfig) -> GarageState | None:
    return garage_discover.discover_garage(config)


def _read_affected(
    config: GarageConfig, state: GarageState, params: Mapping[str, str]
) -> list[GarageBucket]:
    """Post-mutation targeted re-read: plan the affected ids, cap the fan-out, read only those."""
    ids = garage_state.affected_bucket_ids(params, state)
    if not ids:
        return []
    capped = garage_state.cap_targeted_reads(ids, context="Post-mutation")
    return _state_reader.read_buckets(config, capped)


def _log_enricher(state: object) -> BucketIdResolver:
    """Tick-fresh ``(key_id, name) -> bucket_id`` map for ``garage_s3`` lines
    ; a None/foreign state builds the honest empty resolver."""
    return BucketIdResolver.from_state(
        state if isinstance(state, GarageState) else None
    )


GARAGE_INTEGRATION = Integration(
    id="garage",
    parse_config=parse_garage_config,
    enabled=_enabled,
    preconditions=_preconditions,
    specs=build_garage_specs,
    discover=_discover,
    collect_state=_collect_state,
    collect_state_fresh=_collect_state_fresh,
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
    declared_config={
        "enabled": True,
        "container_name": "garage",
        "garage_binary": "/garage",
        "docker_binary": "/usr/bin/docker",
        "config_path": "/etc/garage/garage.toml",
    },
)

register_integration(GARAGE_INTEGRATION)
