"""The declared wire shape, its digest, and the properties CORE-008 rests on.

Written before the implementation, deliberately. The failure this file exists to
catch is silent: if the artifact's ``digest`` field is not reproducible from the
artifact's own bytes, a consumer that vendors the file can never reproduce it,
every comparison mismatches forever, and under the consumer's design that means
its reconcile refuses forever. Nothing in the agent would look wrong.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import fitness.wire_contract as fitness_module
from fitness.wire_contract import WIRE_CONTRACT_PATH, check_wire_contract
from stormpulse.agent.wire_contract import (
    SCHEMA,
    build_wire_contract,
    command_wire_entry,
    render_wire_contract,
    wire_contract_commands,
    wire_contract_digest,
    wire_contract_integrations,
)
from stormpulse.caddy.commands import build_caddy_specs
from stormpulse.caddy.config import CaddyConfig
from stormpulse.commands.registry import COMMAND_REGISTRY
from stormpulse.config import CommandSpec, ParamDef
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import GarageBucket, GarageKeyRef, GarageState
from stormpulse.protocol import dataclass_wire_shape
from stormpulse.rclone.commands import build_rclone_specs
from stormpulse.rclone.config import RcloneConfig
from stormpulse.sdk.declaration import canonical_digest

# -- the crux: both ends must hash identical bytes -------------------------


def test_artifact_digest_is_reproducible_from_the_artifact_alone() -> None:
    """A consumer holding ONLY the file can recompute the digest it carries.

    This is the property the whole two-ended design rests on. The consumer has
    no dataclasses, only bytes, so if the carried digest is not a function of
    the carried shape the consumer is locked out permanently.
    """
    artifact = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))
    recomputed = canonical_digest(artifact[artifact["digest_covers"]])
    assert recomputed == artifact["digest"]


def test_artifact_is_self_describing_about_what_the_digest_covers() -> None:
    """``digest_covers`` names a real top-level key, so the recompute above is
    not a convention a consumer has to be told out of band."""
    artifact = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert artifact["digest_covers"] in artifact


def test_checked_in_artifact_matches_the_live_dataclasses() -> None:
    """Function 9's property, asserted from the test suite too.

    The fitness runner is the gate; this is here so a contributor running
    ``pytest`` alone still learns the artifact is stale.
    """
    assert check_wire_contract() == []


def test_rendered_artifact_is_byte_identical_to_the_checked_in_file() -> None:
    """Regenerating must be a no-op, or every unrelated commit carries churn."""
    assert render_wire_contract() == WIRE_CONTRACT_PATH.read_text(encoding="utf-8")


# -- the digest is derived from the classes, never from the file ------------


def test_runtime_digest_equals_the_live_shape_hashed() -> None:
    assert wire_contract_digest() == canonical_digest(wire_contract_integrations())


def test_agent_wire_contract_module_cannot_read_the_artifact() -> None:
    """CORE-008 decision 4, enforced structurally rather than by convention.

    A digest read from a file can be stale with respect to the process that
    sends it. This asserts the runtime module has no way to read one: no
    filesystem import, no ``open``, no ``__file__``. Changing the runtime path
    to consult the artifact fails here before it can ship a stale advertisement.
    """
    from stormpulse.agent import wire_contract as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "pathlib" not in imported
    assert "os" not in imported
    assert "io" not in imported

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called
    assert "__file__" not in source


# -- what the shape covers, and what it deliberately does not --------------


def test_shape_walks_every_dataclass_reachable_from_the_root() -> None:
    classes = wire_contract_integrations()["garage"]["classes"]
    assert set(classes) == {
        "GarageState",
        "GarageBucket",
        "GarageKeyRef",
        "GaragePeer",
        "GarageAdminMetric",
    }


def test_shape_records_nesting_by_class_name() -> None:
    classes = wire_contract_integrations()["garage"]["classes"]
    assert classes["GarageState"]["buckets"] == "GarageBucket"
    assert classes["GarageState"]["peers"] == "GaragePeer"
    assert classes["GarageBucket"]["keys"] == "GarageKeyRef"
    # A tuple of plain strings is a leaf, not a nesting.
    assert classes["GarageKeyRef"]["bucket_local_aliases"] is None


def test_shape_field_names_match_what_asdict_actually_emits() -> None:
    """The artifact must describe the bytes on the wire, not a parallel belief.

    ``GarageState.to_dict`` is ``asdict``, so the emitted JSON keys ARE the
    dataclass field names, recursively. If the walker and ``asdict`` ever
    disagree, the published contract is a lie in exactly the way this unit
    exists to prevent.
    """
    state = GarageState(
        node_id="n1",
        hostname="host",
        zone="dc1",
        capacity_gb=1.0,
        data_avail_gb=1.0,
        version="v2",
        healthy=True,
        object_count=0,
        buckets=[
            GarageBucket(
                id="b1",
                alias="a",
                size_bytes=0,
                object_count=0,
                keys=[GarageKeyRef(key_id="k", key_name="n", permissions="RW")],
                website_access=False,
                website_index_document="index.html",
                website_error_document=None,
                quota_max_size_bytes=None,
                quota_max_objects=None,
            )
        ],
        keys=[],
        peers=[],
    )
    emitted = state.to_dict()
    classes = wire_contract_integrations()["garage"]["classes"]

    assert set(emitted) == set(classes["GarageState"])
    assert set(emitted["buckets"][0]) == set(classes["GarageBucket"])
    assert set(emitted["buckets"][0]["keys"][0]) == set(classes["GarageKeyRef"])


def test_digest_ignores_types_and_defaults_but_not_names() -> None:
    """CORE-008 decision 5, stated as a test: names and nesting, nothing else.

    A consumer that reads an unchanged digest as proof of semantic stability
    has misread it, so the scope had better be exactly what the ADR claims.
    """

    @dataclass(frozen=True)
    class Before:
        count: int
        label: str

    @dataclass(frozen=True)
    class Retyped:
        count: str  # units/type changed, meaning changed, names did not
        label: str

    @dataclass(frozen=True)
    class Renamed:
        total: int  # a rename
        label: str

    before = dataclass_wire_shape(Before)["Before"]
    # A type change is invisible to the digest, and saying so is the decision.
    assert before == dataclass_wire_shape(Retyped)["Retyped"]
    assert canonical_digest(before) == canonical_digest(
        dataclass_wire_shape(Retyped)["Retyped"]
    )
    # A rename is not.
    assert before != dataclass_wire_shape(Renamed)["Renamed"]
    assert canonical_digest(before) != canonical_digest(
        dataclass_wire_shape(Renamed)["Renamed"]
    )


def test_walker_refuses_a_non_dataclass_root() -> None:
    with pytest.raises(TypeError):
        dataclass_wire_shape(int)


# -- the artifact's own shape ----------------------------------------------


def test_artifact_carries_no_timestamp() -> None:
    """Deliberate: a generated-at stamp would churn the file on every
    regeneration and force Function 9 to ignore a field, which is how a
    contract check learns to ignore things."""
    rendered = render_wire_contract()
    assert "generated" not in rendered
    assert "timestamp" not in rendered


def test_artifact_top_level_keys_are_stable() -> None:
    """A new top-level key is a consumer-breaking event and bumps SCHEMA with it."""
    assert set(build_wire_contract()) == {
        "schema",
        "digest",
        "digest_covers",
        "integrations",
        "commands",
    }
    assert SCHEMA == 2


# -- the command surface: declared, outside the digest ---------------------

# The digest deployed agents advertise and consumers compare. Pinned literally:
# the commands section must never move it, and a deliberate move is a fleet event.
_INTEGRATIONS_DIGEST = (
    "sha256:f7ba4418f3d174c758c9e10449d9127b1c8b8765f2f9c21c4f9d349279451e55"
)


def _every_buildable_command() -> set[str]:
    """The registry's full surface, derived from the builders directly, seal and
    ``disabled_commands`` ignored, so the contract is checked against a second route."""
    garage = GarageConfig(
        enabled=True,
        container_name="g",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/etc/garage.toml"),
    )
    caddy = CaddyConfig(
        enabled=True,
        admin_url="http://localhost:2019",
        main_caddyfile=Path("/etc/caddy/Caddyfile"),
        drop_in_path=Path("/etc/caddy/x.caddy"),
    )
    return (
        set(COMMAND_REGISTRY)
        | set(build_garage_specs(garage))
        | {"garage_refresh"}
        | set(build_caddy_specs(caddy))
        | set(build_rclone_specs(RcloneConfig(enabled=True)))
    )


def test_commands_section_lists_every_command_the_registry_can_build() -> None:
    commands = wire_contract_commands()
    assert set(commands) == _every_buildable_command()
    # The sealed hatches are declared even though a sealed agent drops them.
    assert {"run_verify_block", "run_apply_block"} <= set(commands)


def test_commands_carry_each_params_validator() -> None:
    entry = wire_contract_commands()["docker_logs"]["params"]
    assert entry["tail_lines"] == {
        "pattern": "[0-9]{1,5}",
        "max_bytes": None,
        "schema": None,
        "secret": False,
        "default": "100",
    }
    assert "default" not in entry["docker_service_name"]
    assert wire_contract_commands()["garage_refresh"] == {"params": {}}


def test_commands_carry_a_params_schema() -> None:
    tenants = wire_contract_commands()["buckets_custom_domain_caddy_sync"]["params"][
        "tenants"
    ]
    assert tenants["schema"] == {
        "type": "object",
        "entries": {
            "key_pattern": "[A-Za-z0-9_-]{1,64}",
            "value": {"type": "string", "max_bytes": 16384},
        },
    }


def test_commands_publish_the_cors_rules_shape() -> None:
    # The dispatcher pins the rule shape it sends from here, not from a fake.
    commands = wire_contract_commands()
    assert set(commands["garage_bucket_cors_get"]["params"]) == {"bucket_id"}
    rules = commands["garage_bucket_cors_set"]["params"]["rules"]
    assert rules["max_bytes"] == 65536 and rules["secret"] is False
    assert set(rules["schema"]["items"]["keys"]) == {
        "ID",
        "MaxAgeSeconds",
        "AllowedOrigin",
        "AllowedMethod",
        "AllowedHeader",
        "ExposeHeader",
    }
    assert (
        rules["schema"]
        == commands["garage_bucket_cors_set"]["params"]["expected_rules"]["schema"]
    )


def test_commands_never_carry_a_secret_default() -> None:
    spec = CommandSpec(
        group="g",
        command=["/bin/true", "{secret_key}"],
        timeout=1,
        params={
            "secret_key": ParamDef(
                placeholder="secret_key", default="hunter2", pattern=".+", secret=True
            )
        },
    )
    entry = command_wire_entry(spec)["params"]["secret_key"]
    assert entry["secret"] is True
    assert "default" not in entry
    assert "hunter2" not in json.dumps(command_wire_entry(spec))


def test_commands_are_outside_the_digest() -> None:
    artifact = build_wire_contract()
    assert artifact["digest_covers"] == "integrations"
    assert artifact["digest"] == _INTEGRATIONS_DIGEST
    assert wire_contract_digest() == _INTEGRATIONS_DIGEST


def _check_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact: dict[str, Any]
) -> list[str]:
    path = tmp_path / "wire-contract.json"
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(fitness_module, "WIRE_CONTRACT_PATH", path)
    return check_wire_contract()


def test_fitness_fails_on_a_command_missing_from_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    del artifact["commands"]["garage_bucket_set_quota"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "'garage_bucket_set_quota' is registered but not declared" in v
        for v in violations
    )


def test_fitness_fails_on_a_declared_command_that_no_longer_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    artifact["commands"]["garage_bucket_set_cors"] = {"params": {}}
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "'garage_bucket_set_cors' is no longer registered" in v for v in violations
    )


def test_fitness_fails_on_a_params_schema_drifting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    tenants = artifact["commands"]["buckets_custom_domain_caddy_sync"]["params"][
        "tenants"
    ]
    tenants["schema"]["entries"]["value"]["max_bytes"] = 1
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        v.startswith("commands.buckets_custom_domain_caddy_sync.tenants:")
        for v in violations
    )


def test_fitness_fails_on_a_params_validator_drifting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    artifact["commands"]["garage_bucket_set_quota"]["params"]["max_size"]["pattern"] = (
        ".*"
    )
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        v.startswith("commands.garage_bucket_set_quota.max_size:") for v in violations
    )


def test_command_wire_entry_never_aliases_the_live_schema() -> None:
    # A consumer (or the fitness test above) that edits the artifact must not
    # be editing the registry's own declaration through a shared dict.
    spec = build_garage_specs(
        GarageConfig(
            enabled=True,
            container_name="g",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=Path("/etc/garage.toml"),
        )
    )["garage_bucket_cors_set"]
    entry = command_wire_entry(spec)["params"]["rules"]["schema"]
    entry["items"]["required"].clear()
    live = spec.params["rules"].schema
    assert live is not None
    assert live["items"]["required"] == ["AllowedOrigin", "AllowedMethod"]


def test_fitness_names_every_command_when_the_file_predates_the_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A schema-1 file has no ``commands`` key at all: the check reports one
    violation per registered command instead of crashing on the missing key."""
    artifact = build_wire_contract()
    del artifact["commands"]
    artifact["schema"] = 1
    violations = _check_with(monkeypatch, tmp_path, artifact)
    undeclared = [v for v in violations if "is registered but not declared" in v]
    assert len(undeclared) == len(wire_contract_commands())
