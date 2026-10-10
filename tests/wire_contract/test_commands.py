"""The command surface: every buildable command declared with its validators,
schemas and result shape, outside the digest, and Function 9's lines when one
drifts (CORE-011 d9).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stormpulse.agent.wire_contract import (
    build_wire_contract,
    command_wire_entry,
    wire_contract_commands,
)
from stormpulse.caddy.commands import build_caddy_specs
from stormpulse.caddy.config import CaddyConfig
from stormpulse.commands.registry import COMMAND_REGISTRY
from stormpulse.config import CommandSpec, ParamDef
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig
from stormpulse.rclone.commands import build_rclone_specs
from stormpulse.rclone.config import RcloneConfig

# -- the command surface: declared, outside the digest -----------------------


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
        "type": "string",
        "pattern": "[0-9]{1,5}",
        "max_bytes": None,
        "schema": None,
        "secret": False,
        "default": "100",
    }
    assert "default" not in entry["docker_service_name"]
    assert wire_contract_commands()["garage_refresh"] == {"params": {}, "result": None}


def test_commands_carry_a_params_schema() -> None:
    tenants = wire_contract_commands()["buckets_custom_domain_caddy_sync"]["params"][
        "tenants"
    ]
    # The value rides as the JSON type the schema's root names, default included.
    assert tenants["type"] == "object" and tenants["default"] == {}
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
    assert rules["type"] == "array"
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


def test_commands_declare_the_result_shape_a_job_returns() -> None:
    """CORE-011 d9: ``commands.<name>.result`` is the typed walk of the spec's
    result dataclass, root and classes like a message; null where none returns."""
    commands = wire_contract_commands()
    clear = commands["garage_bucket_clear"]["result"]
    assert clear["root"] == "ClearBucketResult"
    fields = clear["classes"]["ClearBucketResult"]
    assert fields["deleted_count"] == {
        "type": "integer",
        "nullable": False,
        "required": True,
    }
    assert fields["purge_key_id"] == {
        "type": "string",
        "nullable": True,
        "required": False,
    }
    # A nested class walks with its root, the same way a message's does.
    provision = commands["garage_provision_customer_bucket"]["result"]
    assert (
        provision["classes"]["ProvisionBucketResult"]["admin"]["type"]
        == "ProvisionedKey"
    )
    assert set(provision["classes"]["ProvisionedKey"]) == {
        "key_id",
        "secret",
        "key_name",
    }
    assert commands["git_pull"]["result"] is None


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


# -- Function 9 covers commands ----------------------------------------------


def test_fitness_fails_on_a_command_missing_from_the_file(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    del artifact["commands"]["garage_bucket_set_quota"]
    violations = check_with(artifact)
    assert any(
        "'garage_bucket_set_quota' is registered but not declared" in v
        for v in violations
    )


def test_fitness_fails_on_a_declared_command_that_no_longer_exists(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    artifact["commands"]["garage_bucket_set_cors"] = {"params": {}}
    violations = check_with(artifact)
    assert any(
        "'garage_bucket_set_cors' is no longer registered" in v for v in violations
    )


def test_fitness_fails_on_a_params_schema_drifting(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    tenants = artifact["commands"]["buckets_custom_domain_caddy_sync"]["params"][
        "tenants"
    ]
    tenants["schema"]["entries"]["value"]["max_bytes"] = 1
    violations = check_with(artifact)
    assert any(
        v.startswith("commands.buckets_custom_domain_caddy_sync.tenants:")
        for v in violations
    )


def test_fitness_fails_on_a_params_validator_drifting(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    artifact["commands"]["garage_bucket_set_quota"]["params"]["max_size"]["pattern"] = (
        ".*"
    )
    violations = check_with(artifact)
    assert any(
        v.startswith("commands.garage_bucket_set_quota.max_size:") for v in violations
    )


def test_fitness_fails_on_a_result_shape_drifting(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    clear = artifact["commands"]["garage_bucket_clear"]["result"]
    clear["classes"]["ClearBucketResult"]["deleted_count"]["type"] = "string"
    del clear["classes"]["ClearBucketResult"]["bytes_freed"]
    artifact["commands"]["garage_walk_bucket_stats"]["result"] = None
    artifact["commands"]["git_pull"]["result"] = {"root": "Nope", "classes": {}}
    artifact["commands"]["garage_bucket_set_quota"]["result"]["root"] = "QuotaFigures"
    violations = check_with(artifact)
    assert (
        "commands.garage_bucket_set_quota.result: declared root 'QuotaFigures' != "
        "emitted 'SetQuotaResult'; regenerate with `make wire-contract` and commit "
        "the result"
    ) in violations
    assert (
        "commands.garage_bucket_clear.result.ClearBucketResult.deleted_count: declared "
        "type 'string' != emitted 'integer'; regenerate with `make wire-contract` and "
        "commit the result"
    ) in violations
    assert any(
        "commands.garage_bucket_clear.result.ClearBucketResult: emits undeclared "
        "field 'bytes_freed'" in v
        for v in violations
    )
    assert any(
        v.startswith("commands.garage_walk_bucket_stats.result: declared shape None")
        for v in violations
    )
    assert any(
        v.startswith(
            "commands.git_pull.result: declared shape {'classes': {}, 'root': 'Nope'} "
            "!= emitted None"
        )
        for v in violations
    )


def test_fitness_names_every_command_when_the_file_predates_the_section(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """A schema-1 file has no ``commands`` key at all: the check reports one
    violation per registered command instead of crashing on the missing key."""
    artifact = build_wire_contract()
    del artifact["commands"]
    artifact["schema"] = 1
    violations = check_with(artifact)
    undeclared = [v for v in violations if "is registered but not declared" in v]
    assert len(undeclared) == len(wire_contract_commands())
