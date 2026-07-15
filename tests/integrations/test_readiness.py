"""Readiness graph: state derivation, single-sourced baseline, capability/dependency
resolution, host-capability honesty, and provider disjointness (P2, CORE-007)."""

from __future__ import annotations

from stormpulse.config import ConfigError
from stormpulse.integrations.readiness import (
    capability_provider_conflicts,
    resolve_dependency,
    resolve_host_capability,
    resolve_readiness,
)
from stormpulse.integrations.registry import Integration
from stormpulse.sdk import (
    Capability,
    CapabilityLiveness,
    DependencyRequirement,
    ReadinessReport,
    ReadinessState,
)


class _Cfg:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _make(
    *,
    parse_ok: bool = True,
    enabled: bool = True,
    precondition_reason: str | None = None,
    counter: list[str] | None = None,
    caps: tuple[Capability, ...] = (Capability("demo.thing.v1", "demo"),),
) -> Integration:
    def parse(raw: dict[str, object]) -> _Cfg:
        if not parse_ok:
            raise ConfigError("bad demo config")
        return _Cfg(enabled=enabled)

    def is_enabled(cfg: _Cfg) -> bool:
        return cfg.enabled

    def preconditions(cfg: _Cfg) -> str | None:
        if counter is not None:
            counter.append("probed")
        return precondition_reason

    return Integration(
        id="demo",
        parse_config=parse,
        enabled=is_enabled,
        preconditions=preconditions,
        capabilities=caps,
    )


def test_available_when_no_section() -> None:
    r = resolve_readiness(_make(), None, run_probe=True)
    assert r.state is ReadinessState.AVAILABLE


def test_available_with_reason_when_parse_fails() -> None:
    r = resolve_readiness(_make(parse_ok=False), {}, run_probe=True)
    assert r.state is ReadinessState.AVAILABLE
    assert r.reason is not None and "config error" in r.reason


def test_configured_when_enabled_false() -> None:
    r = resolve_readiness(_make(enabled=False), {}, run_probe=True)
    assert r.state is ReadinessState.CONFIGURED
    assert r.reason == "enabled = false"


def test_enabled_when_probe_not_run_and_no_host_io() -> None:
    # config-check path: run_probe=False MUST NOT call preconditions (I7/T04).
    calls: list[str] = []
    r = resolve_readiness(_make(counter=calls), {}, run_probe=False)
    assert r.state is ReadinessState.ENABLED
    assert calls == []  # no host probe
    # capabilities reported as unknown (not evaluated)
    assert all(c.liveness is CapabilityLiveness.UNKNOWN for c in r.capabilities)


def test_ready_when_preconditions_pass() -> None:
    calls: list[str] = []
    r = resolve_readiness(_make(counter=calls), {}, run_probe=True)
    assert r.state is ReadinessState.READY
    assert calls == ["probed"]  # host probe ran on the readiness path
    assert r.capabilities[0].liveness is CapabilityLiveness.LIVE


def test_enabled_not_ready_when_precondition_fails() -> None:
    r = resolve_readiness(_make(precondition_reason="garage unreachable"), {}, run_probe=True)
    assert r.state is ReadinessState.ENABLED
    assert r.reason == "garage unreachable"
    assert r.capabilities[0].liveness is CapabilityLiveness.UNMET


def test_readiness_baseline_agrees_with_preconditions() -> None:
    # C4/I8: readiness `ready` iff preconditions returns None, on the same fixture.
    for reason in (None, "some host failure"):
        integ = _make(precondition_reason=reason)
        parsed = integ.parse_config({})
        precondition_none = integ.preconditions(parsed) is None  # type: ignore[misc]
        report = resolve_readiness(integ, {}, run_probe=True)
        assert (report.state is ReadinessState.READY) == precondition_none


def test_dependency_capability_unmet_while_integration_ready() -> None:
    # I9/C3: integration is READY but the specific capability is not live.
    from stormpulse.sdk import CapabilityStatus

    report = ReadinessReport(
        "garage",
        ReadinessState.READY,
        capabilities=(
            CapabilityStatus(
                "garage.admin.v1", CapabilityLiveness.UNMET, reason="admin token cannot list buckets"
            ),
        ),
    )
    result = resolve_dependency(
        DependencyRequirement("garage", ReadinessState.READY, "garage.admin.v1"),
        {"garage": report},
    )
    assert result.satisfied is False
    assert result.reason is not None and "admin token" in result.reason


def test_dependency_satisfied_when_state_and_capability_live() -> None:
    from stormpulse.sdk import CapabilityStatus

    report = ReadinessReport(
        "garage",
        ReadinessState.READY,
        capabilities=(CapabilityStatus("garage.admin.v1", CapabilityLiveness.LIVE),),
    )
    result = resolve_dependency(
        DependencyRequirement("garage", ReadinessState.READY, "garage.admin.v1"),
        {"garage": report},
    )
    assert result.satisfied is True


def test_dependency_unmet_when_state_too_low() -> None:
    report = ReadinessReport("garage", ReadinessState.ENABLED, reason="unreachable")
    result = resolve_dependency(DependencyRequirement("garage"), {"garage": report})
    assert result.satisfied is False
    assert result.reason is not None and "enabled" in result.reason


def test_host_capability_not_offered_reports_honestly() -> None:
    # C14/I14: P2 offers neither command nor state host capability.
    for token in ("pulse.integration.commands.v1", "pulse.integration.state.v1"):
        status = resolve_host_capability(token)
        assert status.liveness is CapabilityLiveness.UNMET
        assert status.reason is not None and "not offered by this Pulse version" in status.reason


def test_duplicate_capability_provider_is_a_conflict() -> None:
    # I13/T07: two built-ins declaring the same token; second loses.
    a = _make(caps=(Capability("shared.cap.v1", "a"),))
    b = _make(caps=(Capability("shared.cap.v1", "b"),))
    object.__setattr__(a, "id", "a")
    object.__setattr__(b, "id", "b")
    losers = capability_provider_conflicts([a, b])
    assert "b" in losers and "a" not in losers
