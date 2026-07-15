"""Readiness resolution: the four-state model and dependency checks (P2, CORE-007).

Framework layer. Derives an integration's ``available`` / ``configured`` /
``enabled`` / ``ready`` state and, when asked to probe, the live status of each
capability it declares. The states up to ``enabled`` are derived with **no host
I/O** so ``config check`` can use them; only the ``ready`` step touches the host,
and it does so through the integration's ``preconditions`` (single-sourced, I8),
optionally refined by a ``readiness`` probe.

The dependency resolver enforces "capability, not id" (I9): a requirement is
satisfied only when the target reaches the requested state AND the named
capability is live.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from stormpulse.config import ConfigError
from stormpulse.integrations.registry import Integration, registered_integrations
from stormpulse.sdk import (
    CapabilityLiveness,
    CapabilityStatus,
    DependencyRequirement,
    ReadinessReport,
    ReadinessState,
)

# Host-provided meta-capabilities and the set this Pulse version actually offers.
# In P2 neither the command surface (P4) nor the external-state surface (P3) is
# offered, so both resolve "unmet" with an honest message (C14/I14). This is the
# forward-safe hook the #7 SDK-version design lands on.
HOST_CAPABILITIES: frozenset[str] = frozenset(
    {"pulse.integration.commands.v1", "pulse.integration.state.v1"}
)
OFFERED_HOST_CAPABILITIES: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DependencyResult:
    """The outcome of resolving one ``DependencyRequirement``."""

    satisfied: bool
    reason: str | None = None
    repair: str | None = None


def _declared_capabilities(integ: Integration) -> tuple[str, ...]:
    return tuple(c.token for c in (integ.capabilities or ()))


def _capabilities_unknown(integ: Integration) -> tuple[CapabilityStatus, ...]:
    return tuple(
        CapabilityStatus(token, CapabilityLiveness.UNKNOWN)
        for token in _declared_capabilities(integ)
    )


def _capabilities_from_baseline(
    integ: Integration, baseline_reason: str | None
) -> tuple[CapabilityStatus, ...]:
    """Default capability liveness when no dedicated probe is declared: every
    capability is live iff the baseline host check passed, else unmet with the
    baseline reason. This is the single-source path (I8)."""
    if baseline_reason is None:
        return tuple(
            CapabilityStatus(token, CapabilityLiveness.LIVE)
            for token in _declared_capabilities(integ)
        )
    return tuple(
        CapabilityStatus(token, CapabilityLiveness.UNMET, reason=baseline_reason)
        for token in _declared_capabilities(integ)
    )


def resolve_readiness(
    integ: Integration, raw: dict[str, object] | None, *, run_probe: bool
) -> ReadinessReport:
    """Derive one integration's readiness. ``run_probe=False`` stops at
    ``enabled`` and performs no host I/O (the ``config check`` path)."""
    if raw is None:
        return ReadinessReport(integ.id, ReadinessState.AVAILABLE)
    try:
        parsed = integ.parse_config(raw)
    except ConfigError as exc:
        return ReadinessReport(
            integ.id, ReadinessState.AVAILABLE, reason=f"config error: {exc}"
        )
    if not integ.enabled(parsed):
        return ReadinessReport(
            integ.id, ReadinessState.CONFIGURED, reason="enabled = false"
        )
    if not run_probe:
        return ReadinessReport(
            integ.id,
            ReadinessState.ENABLED,
            capabilities=_capabilities_unknown(integ),
        )
    # Host-touching from here. Baseline liveness is the boot gate (single-sourced).
    baseline_reason = (
        integ.preconditions(parsed) if integ.preconditions is not None else None
    )
    caps = (
        integ.readiness(parsed)
        if integ.readiness is not None
        else _capabilities_from_baseline(integ, baseline_reason)
    )
    if baseline_reason is None:
        return ReadinessReport(integ.id, ReadinessState.READY, capabilities=caps)
    return ReadinessReport(
        integ.id, ReadinessState.ENABLED, capabilities=caps, reason=baseline_reason
    )


def resolve_all(
    integrations_config: Mapping[str, dict[str, object]], *, run_probe: bool
) -> dict[str, ReadinessReport]:
    """Readiness for every registered integration, keyed by id."""
    return {
        integ.id: resolve_readiness(
            integ, dict(integrations_config.get(integ.id) or {}) or None, run_probe=run_probe
        )
        for integ in registered_integrations()
    }


def resolve_dependency(
    req: DependencyRequirement, readiness: Mapping[str, ReadinessReport]
) -> DependencyResult:
    """Resolve one dependency requirement against the readiness map (I9)."""
    report = readiness.get(req.id)
    if report is None:
        return DependencyResult(
            False,
            reason=f"required integration {req.id!r} is not present",
            repair=f"stormpulse {req.id} init",
        )
    if report.state < req.state:
        return DependencyResult(
            False,
            reason=(
                f"{req.id!r} is {report.state.name.lower()}, "
                f"requires {req.state.name.lower()}"
                + (f" ({report.reason})" if report.reason else "")
            ),
            repair=f"stormpulse integration doctor {req.id}",
        )
    if req.capability is not None:
        match = next(
            (c for c in report.capabilities if c.token == req.capability), None
        )
        if match is None:
            return DependencyResult(
                False,
                reason=f"{req.id!r} does not provide capability {req.capability!r}",
            )
        if match.liveness is not CapabilityLiveness.LIVE:
            return DependencyResult(
                False,
                reason=match.reason
                or f"capability {req.capability!r} is {match.liveness.value}",
                repair=match.repair,
            )
    return DependencyResult(True)


def resolve_host_capability(token: str) -> CapabilityStatus:
    """Resolve a required host-provided capability against what this Pulse version
    offers. Unknown or not-yet-offered tokens report an honest message (C14)."""
    if token in OFFERED_HOST_CAPABILITIES:
        return CapabilityStatus(token, CapabilityLiveness.LIVE)
    if token in HOST_CAPABILITIES:
        return CapabilityStatus(
            token,
            CapabilityLiveness.UNMET,
            reason="not offered by this Pulse version (arrives in a later phase)",
        )
    return CapabilityStatus(
        token, CapabilityLiveness.UNMET, reason=f"unknown host capability {token!r}"
    )


def capability_provider_conflicts(
    integrations: Iterable[Integration] | None = None,
) -> dict[str, str]:
    """Map each integration id that would lose a capability-provider collision to a
    reason. First declarer (registration order) wins; a later declarer of the same
    token is a boot refusal (I13, sibling of the enricher-parser disjointness rule).
    """
    owners: dict[str, str] = {}
    losers: dict[str, str] = {}
    for integ in registered_integrations() if integrations is None else integrations:
        for cap in integ.capabilities or ():
            owner = owners.setdefault(cap.token, integ.id)
            if owner != integ.id:
                losers[integ.id] = (
                    f"capability {cap.token!r} is already provided by {owner!r} "
                    f"(one provider per capability, CORE-007 readiness graph)"
                )
    return losers
