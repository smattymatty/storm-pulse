"""Tests for the CORE-005 Integration contract: registry, envelope, zero-edit add."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

import stormpulse.integrations.registry as registry
from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.agent.integrations_runtime import (
    IntegrationRuntime,
    build_integrations_payload,
)
from stormpulse.config import CommandSpec
from stormpulse.integrations import (
    Integration,
    register_integration,
    registered_integrations,
)
from tests.helpers import build_config


@pytest.fixture
def isolated_registry() -> Generator[None, None, None]:
    """Save and restore the global Integration registry around a test."""
    saved = list(registry._integrations)
    try:
        yield
    finally:
        registry._integrations[:] = saved


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_garage_and_caddy_are_registered() -> None:
    # The manifest is imported transitively by bootstrap; both reference
    # integrations must be on the registry.
    ids = {integ.id for integ in registered_integrations()}
    assert {"garage", "caddy"} <= ids


def test_register_integration_is_idempotent_by_id(isolated_registry: None) -> None:
    before = len(registered_integrations())
    integ = Integration(id="dup", parse_config=lambda raw: raw, enabled=lambda c: True)
    register_integration(integ)
    register_integration(integ)
    ids = [i.id for i in registered_integrations()]
    assert ids.count("dup") == 1
    assert len(registered_integrations()) == before + 1


# ---------------------------------------------------------------------------
# Wire envelope (core-005-integrations-wire.md)
# ---------------------------------------------------------------------------


def _desc(integ_id: str) -> Integration:
    return Integration(id=integ_id, parse_config=lambda raw: raw, enabled=lambda c: True)


def test_envelope_live_carries_state_blob() -> None:
    class _State:
        def to_dict(self) -> dict[str, object]:
            return {"k": 1}

    rt = IntegrationRuntime("x", "live", None, {}, _desc("x"), state=_State())
    out = build_integrations_payload({"x": rt})
    assert out["x"] == {"status": "live", "disabled_reason": None, "state": {"k": 1}}


def test_envelope_live_without_state_is_empty_object() -> None:
    rt = IntegrationRuntime("x", "live", None, {}, _desc("x"), state=None)
    out = build_integrations_payload({"x": rt})
    assert out["x"]["state"] == {}


def test_envelope_disabled_error_has_reason_and_null_state() -> None:
    rt = IntegrationRuntime("x", "disabled_error", "boom", {}, _desc("x"))
    out = build_integrations_payload({"x": rt})
    assert out["x"] == {"status": "disabled_error", "disabled_reason": "boom", "state": None}


def test_envelope_disabled_choice_has_null_reason_and_state() -> None:
    rt = IntegrationRuntime("x", "disabled_choice", None, {}, _desc("x"))
    out = build_integrations_payload({"x": rt})
    assert out["x"] == {"status": "disabled_choice", "disabled_reason": None, "state": None}


# ---------------------------------------------------------------------------
# Zero-edit third integration (CORE-005 acceptance)
# ---------------------------------------------------------------------------


def test_notional_third_integration_resolves_through_bootstrap(
    tmp_path: Path, isolated_registry: None
) -> None:
    """A third integration is one register_integration call - bootstrap is a loop.

    No edit to build_agent_dependencies is needed for it to parse, gate, merge
    commands, and produce a live runtime.
    """

    def _commands(config: dict[str, object]) -> dict[str, CommandSpec]:
        return {
            "notional_ping": CommandSpec(
                group="notional", command=["/bin/true"], timeout=5
            )
        }

    register_integration(
        Integration(
            id="notional",
            parse_config=lambda raw: raw,
            enabled=lambda c: bool(c.get("enabled")),
            specs=_commands,
        )
    )
    cfg = build_config(tmp_path, integrations={"notional": {"enabled": True}})
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None
    )
    assert deps.integrations["notional"].status == "live"
    assert "notional_ping" in deps.registry


def test_absent_integration_not_reported(tmp_path: Path) -> None:
    # garage/caddy are registered but absent from this config: they must not
    # appear in the runtime set (spec: absent from config => not on the wire).
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None
    )
    assert deps.integrations == {}
