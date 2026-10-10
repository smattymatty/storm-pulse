"""Tests for the CORE-005 Integration contract: registry, envelope, zero-edit add."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

import stormpulse.integrations.registry as registry
from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.agent.integrations_runtime import (
    IntegrationRuntime,
    build_integrations_payload,
    integration_components,
)
from stormpulse.agent.wire_contract import wire_contract_commands
from stormpulse.config import CommandSpec
from stormpulse.integrations import (
    Integration,
    integration_command_specs,
    register_integration,
    registered_integrations,
)
from stormpulse.protocol import Component, IntegrationReport
from stormpulse.sdk import SdkCommandSpec, SdkComponent, command_specs_digest
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
    return Integration(
        id=integ_id, parse_config=lambda raw: raw, enabled=lambda c: True
    )


# The push envelope (CORE-011 d5) of a runtime nothing has read or pushed yet.
_UNPUSHED = {
    "generation": 0,
    "complete": True,
    "last_full_read_at": "",
    "changed_ids": [],
}


def test_envelope_live_carries_state_blob() -> None:
    class _State:
        def to_dict(self) -> dict[str, object]:
            return {"k": 1}

    rt = IntegrationRuntime("x", "live", None, {}, _desc("x"), state=_State())
    out = build_integrations_payload({"x": rt})
    assert out["x"] == {
        "status": "live",
        "disabled_reason": None,
        "state": {"k": 1},
        "command_specs_digest": "",
        "commands_fenced": None,
        **_UNPUSHED,
    }


def test_envelope_live_without_state_is_empty_object() -> None:
    rt = IntegrationRuntime("x", "live", None, {}, _desc("x"), state=None)
    out = build_integrations_payload({"x": rt})
    assert out["x"]["state"] == {}


def test_envelope_disabled_error_has_reason_and_null_state() -> None:
    rt = IntegrationRuntime("x", "disabled_error", "boom", {}, _desc("x"))
    out = build_integrations_payload({"x": rt})
    assert out["x"] == {
        "status": "disabled_error",
        "disabled_reason": "boom",
        "state": None,
        "command_specs_digest": "",
        "commands_fenced": None,
        **_UNPUSHED,
    }


def test_envelope_disabled_choice_has_null_reason_and_state() -> None:
    rt = IntegrationRuntime("x", "disabled_choice", None, {}, _desc("x"))
    out = build_integrations_payload({"x": rt})
    assert out["x"] == {
        "status": "disabled_choice",
        "disabled_reason": None,
        "state": None,
        "command_specs_digest": "",
        "commands_fenced": None,
        **_UNPUSHED,
    }


def test_envelope_carries_the_command_receipt() -> None:
    """Per-integration declaration (CORE-011 d4): the digest a grant pins and why
    commands are withheld ride every entry, exactly as the runtime holds them."""
    rt = IntegrationRuntime(
        "x",
        "live",
        None,
        {},
        _desc("x"),
        command_specs_digest="sha256:" + "ab" * 32,
        commands_fenced="command_contributor not granted",
    )
    out = build_integrations_payload({"x": rt})
    assert out["x"]["command_specs_digest"] == "sha256:" + "ab" * 32
    assert out["x"]["commands_fenced"] == "command_contributor not granted"
    # The entry is the declared class, key for key (CORE-011 d3): what the
    # artifact names under messages.register is what rides the wire.
    assert set(out["x"]) == {f.name for f in fields(IntegrationReport)}
    assert (
        IntegrationReport.from_dict(out["x"]).commands_fenced
        == "command_contributor not granted"
    )


# ---------------------------------------------------------------------------
# register.components: what each live Integration declares (CORE-011 d7)
# ---------------------------------------------------------------------------


def _with_hook(integ_id: str, hook: Any) -> Integration:
    return Integration(
        id=integ_id,
        parse_config=lambda raw: raw,
        enabled=lambda c: True,
        components=hook,
    )


def test_components_come_from_live_hooks_with_config_and_state() -> None:
    seen: list[tuple[Any, Any]] = []

    def hook(config: Any, state: Any) -> tuple[SdkComponent, ...]:
        seen.append((config, state))
        return (SdkComponent("daemon", "1.2.3", "sha256:" + "cd" * 32),)

    rt = IntegrationRuntime("x", "live", None, {"k": 1}, _with_hook("x", hook))
    assert integration_components({"x": rt}) == [
        Component("daemon", "1.2.3", "sha256:" + "cd" * 32)
    ]
    assert seen == [({"k": 1}, None)]


def test_components_skip_disabled_and_hookless_integrations() -> None:
    hook = lambda config, state: (SdkComponent("daemon", "1", ""),)  # noqa: E731
    disabled = IntegrationRuntime(
        "d", "disabled_error", "boom", None, _with_hook("d", hook)
    )
    hookless = IntegrationRuntime("h", "live", None, {}, _desc("h"))
    assert integration_components({"d": disabled, "h": hookless}) == []


def test_components_hook_that_raises_costs_its_entries_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def bad(config: Any, state: Any) -> tuple[SdkComponent, ...]:
        raise RuntimeError("adapter bug")

    bad_rt = IntegrationRuntime("bad", "live", None, {}, _with_hook("bad", bad))
    good_rt = IntegrationRuntime(
        "good",
        "live",
        None,
        {},
        _with_hook("good", lambda c, s: (SdkComponent("g", "2", ""),)),
    )
    with caplog.at_level("WARNING"):
        got = integration_components({"bad": bad_rt, "good": good_rt})
    assert got == [Component("g", "2", "")]
    assert "'bad'" in caplog.text and "components hook failed" in caplog.text


@pytest.mark.parametrize(
    "returned",
    [
        ({"name": "x", "version": "1", "digest": ""},),
        None,
        SdkComponent("bare", "1", ""),
        (SdkComponent("daemon", 3, ""),),  # type: ignore[arg-type]
        (SdkComponent("daemon", "1", None),),  # type: ignore[arg-type]
    ],
)
def test_components_hook_returning_the_wrong_shape_costs_its_entries_only(
    returned: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The hook is ungated adapter code: a wrong shape (not SdkComponents, or one
    with a non-string field) is logged and skipped, never a raise through register."""
    bad_rt = IntegrationRuntime(
        "bad", "live", None, {}, _with_hook("bad", lambda c, s: returned)
    )
    good_rt = IntegrationRuntime(
        "good",
        "live",
        None,
        {},
        _with_hook("good", lambda c, s: (SdkComponent("g", "2", ""),)),
    )
    with caplog.at_level("WARNING"):
        got = integration_components({"bad": bad_rt, "good": good_rt})
    assert got == [Component("g", "2", "")]
    assert "'bad'" in caplog.text and "components hook failed" in caplog.text


# ---------------------------------------------------------------------------
# The command surface an Integration contributes: one owner, declared for the wire
# ---------------------------------------------------------------------------

_PING = CommandSpec(group="notional", command=["/bin/true"], timeout=5)
# The SDK twin of _PING: what a release author digests and a grant pins.
_PING_SDK = SdkCommandSpec(group="notional", command=["/bin/true"], timeout=5)


def test_integration_command_specs_adds_refresh_only_for_a_state_collector() -> None:
    stateless = Integration(
        id="notional",
        parse_config=lambda raw: raw,
        enabled=lambda c: True,
        specs=lambda cfg: {"notional_ping": _PING},
    )
    assert set(integration_command_specs(stateless, {})) == {"notional_ping"}
    stateful = Integration(
        id="notional",
        parse_config=lambda raw: raw,
        enabled=lambda c: True,
        specs=lambda cfg: {"notional_ping": _PING},
        collect_state=lambda cfg: None,
    )
    specs = integration_command_specs(stateful, {})
    assert set(specs) == {"notional_ping", "notional_refresh"}
    assert specs["notional_refresh"].mode == "refresh"


def test_command_contributor_without_declared_config_is_a_contract_violation(
    isolated_registry: None,
) -> None:
    """Function 5: a contributor with no host-free config cannot be declared in
    the wire contract, and the contract itself refuses to guess one."""
    from fitness.integration_contract import check_integration_contract

    register_integration(
        Integration(
            id="notional",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            specs=lambda cfg: {"notional_ping": _PING},
        )
    )
    assert any(
        "notional" in v and "declared_config" in v for v in check_integration_contract()
    )
    with pytest.raises(LookupError, match="notional"):
        wire_contract_commands()


def test_declared_config_puts_a_contributor_in_the_wire_contract(
    isolated_registry: None,
) -> None:
    register_integration(
        Integration(
            id="notional",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            specs=lambda cfg: {"notional_ping": _PING},
            declared_config={"enabled": True},
        )
    )
    assert "notional_ping" in wire_contract_commands()


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
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)
    assert deps.integrations["notional"].status == "live"
    assert "notional_ping" in deps.registry


def test_in_tree_receipt_is_the_sdk_digest_of_specs_alone(
    tmp_path: Path, isolated_registry: None
) -> None:
    """An in-tree Integration's ``command_specs_digest`` equals what a release
    author computes over the SDK twin of its ``specs``: the synthesized refresh
    stays out, so the bytes are the ones a grant would pin. Never fenced."""
    register_integration(
        Integration(
            id="notional",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            specs=lambda cfg: {"notional_ping": _PING},
            collect_state=lambda cfg: None,
        )
    )
    cfg = build_config(tmp_path, integrations={"notional": {}})
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)
    rt = deps.integrations["notional"]
    assert "notional_refresh" in deps.registry
    assert rt.command_specs_digest == command_specs_digest({"notional_ping": _PING_SDK})
    assert rt.commands_fenced is None


def test_integration_without_commands_has_an_empty_receipt(
    tmp_path: Path, isolated_registry: None
) -> None:
    register_integration(
        Integration(id="notional", parse_config=lambda raw: raw, enabled=lambda c: True)
    )
    cfg = build_config(tmp_path, integrations={"notional": {}})
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)
    rt = deps.integrations["notional"]
    assert rt.status == "live"
    assert (rt.command_specs_digest, rt.commands_fenced) == ("", None)


def test_spec_group_must_equal_integration_id(
    tmp_path: Path, isolated_registry: None
) -> None:
    """The group == id invariant: a spec under a foreign group soft-disables its integration.

    The group is how dispatch resolves a command back to its owning Integration
    (post-success hook, refresh), so a mismatch would silently orphan those paths.
    """

    def _commands(config: dict[str, object]) -> dict[str, CommandSpec]:
        return {
            "rogue_ping": CommandSpec(
                group="somewhere-else", command=["/bin/true"], timeout=5
            )
        }

    register_integration(
        Integration(
            id="rogue",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            specs=_commands,
        )
    )
    cfg = build_config(tmp_path, integrations={"rogue": {}})
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)
    rt = deps.integrations["rogue"]
    assert rt.status == "disabled_error"
    assert rt.disabled_reason is not None
    assert "group" in rt.disabled_reason
    assert "rogue_ping" not in deps.registry


def test_duplicate_log_enricher_parser_is_a_contract_violation(
    isolated_registry: None,
) -> None:
    """Parser keys are disjoint across Integrations (CORE-005 decision 13)."""
    from fitness.integration_contract import check_integration_contract

    register_integration(
        Integration(
            id="rival",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            # garage already declares garage_s3; a second declarer is a violation.
            log_enrichers={"garage_s3": lambda state: lambda key_id, name: ""},
        )
    )
    violations = check_integration_contract()
    assert any("garage_s3" in v and "disjoint" in v for v in violations)


def test_duplicate_enricher_parser_soft_disables_later_declarer(
    tmp_path: Path, isolated_registry: None
) -> None:
    """Boot-time twin of the fitness disjointness check: the fork path where CI never ran.

    First-registered declarer (garage) stands; a later CONFIGURED declarer boots
    disabled_error with the collision named, instead of silently losing first-wins.
    """
    register_integration(
        Integration(
            id="rival",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            log_enrichers={"garage_s3": lambda state: lambda key_id, name: ""},
        )
    )
    cfg = build_config(tmp_path, integrations={"rival": {}})
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)
    rival = deps.integrations["rival"]
    assert rival.status == "disabled_error"
    assert rival.disabled_reason is not None
    assert "garage_s3" in rival.disabled_reason


def test_absent_integration_not_reported(tmp_path: Path) -> None:
    # garage/caddy are registered but absent from this config: they must not
    # appear in the runtime set (spec: absent from config => not on the wire).
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)
    assert deps.integrations == {}


def test_state_collector_without_declared_config_is_a_contract_violation(
    isolated_registry: None,
) -> None:
    """A collector with no ``specs`` still contributes ``<id>_refresh``, so it
    needs a declared_config too, or the contract has a command it cannot list."""
    from fitness.integration_contract import check_integration_contract

    register_integration(
        Integration(
            id="notional",
            parse_config=lambda raw: raw,
            enabled=lambda c: True,
            collect_state=lambda cfg: None,
        )
    )
    assert any(
        "notional" in v and "declared_config" in v for v in check_integration_contract()
    )
    with pytest.raises(LookupError, match="notional"):
        wire_contract_commands()


def test_envelope_carries_the_push_envelope_as_the_runtime_holds_it() -> None:
    """CORE-011 d5: how complete and fresh the snapshot is rides every entry,
    exactly as the runtime holds it (the owed ids are the revision keys)."""
    rt = IntegrationRuntime(
        "x",
        "live",
        None,
        {},
        _desc("x"),
        generation=7,
        complete=False,
        last_full_read_at="2026-10-09T00:00:00Z",
        changed_revisions={"b1": 1},
    )
    out = build_integrations_payload({"x": rt})
    assert out["x"]["generation"] == 7
    assert out["x"]["complete"] is False
    assert out["x"]["last_full_read_at"] == "2026-10-09T00:00:00Z"
    assert out["x"]["changed_ids"] == ["b1"]
