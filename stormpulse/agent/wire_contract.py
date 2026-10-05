"""The declared wire contract this agent emits, and the digest it advertises.

Every publishing Integration declares its state shape in its own package; this
module assembles them (siblings never import each other, CORE-000), lists the
command surface a consumer dispatches into, and digests the state shape from
the live classes, never the filesystem (CORE-008 decision 4).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import stormpulse.agent.integrations_manifest  # noqa: F401  (registers in-tree Integrations)
from stormpulse.commands.registry import COMMAND_REGISTRY
from stormpulse.config import CommandSpec, ParamDef
from stormpulse.garage.wire_shape import garage_wire_shape
from stormpulse.integrations import integration_command_specs, registered_integrations
from stormpulse.sdk.declaration import canonical_digest

# Version of the ARTIFACT's own envelope, not of the shape it carries: bumped only
# when a top-level key changes (a consumer-breaking event), which the digest is
# deliberately not for. 2: the ``commands`` section joined the envelope.
SCHEMA = 2

# Which top-level key the digest covers, carried IN the artifact so a consumer
# holding only the file can reproduce it. Everything else (schema, digest, the
# command surface) is envelope; deployed agents compare this digest at connect.
DIGEST_COVERS = "integrations"


def wire_contract_integrations() -> dict[str, Any]:
    """Every publishing Integration's declared shape, keyed by integration id.

    One entry today. A second Integration that starts publishing a state blob
    adds itself here, which is the point of the map: its arrival changes the
    digest, and a consumer finds out at the next connect.
    """
    return {"garage": garage_wire_shape()}


def wire_contract_commands() -> dict[str, Any]:
    """Every command the registry could build, keyed by name.

    Built-ins plus each in-tree Integration's surface plus its synthesized
    refresh, ignoring the seal and ``disabled_commands``: this is what the
    agent CAN accept, not what one node does. Node-local ``[commands]`` and
    external adapters are per host and stay out. Outside the digest on purpose.
    """
    specs = all_command_specs()
    return {name: command_wire_entry(spec) for name, spec in sorted(specs.items())}


def all_command_specs() -> dict[str, CommandSpec]:
    """Built-ins plus every in-tree Integration's specs, argv included."""
    specs: dict[str, CommandSpec] = dict(COMMAND_REGISTRY)
    for integ in registered_integrations():
        if integ.declared_config is None:
            raise LookupError(
                f"Integration {integ.id!r} declares no declared_config, so its "
                "commands cannot be listed without a host (Function 5 pins this)"
            )
        parsed = integ.parse_config(dict(integ.declared_config))
        specs.update(integration_command_specs(integ, parsed))
    return specs


def command_wire_entry(spec: CommandSpec) -> dict[str, Any]:
    """One command's declared surface: its params and their validators, no argv."""
    return {
        "params": {name: _param_entry(p) for name, p in sorted(spec.params.items())}
    }


def _param_entry(pdef: ParamDef) -> dict[str, Any]:
    # A secret's default would put the secret in a public file; it never rides.
    # The schema is copied: the artifact must never alias a live declaration.
    entry: dict[str, Any] = {
        "pattern": pdef.pattern,
        "max_bytes": pdef.max_bytes,
        "schema": copy.deepcopy(pdef.schema),
        "secret": pdef.secret,
    }
    if pdef.default is not None and not pdef.secret:
        entry["default"] = pdef.default
    return entry


def wire_contract_digest() -> str:
    """The digest this process advertises on register.

    True of the agent that sent it by construction: it hashes the classes that
    are about to do the serializing, in this interpreter, right now.
    """
    return canonical_digest(wire_contract_integrations())


def build_wire_contract() -> dict[str, Any]:
    """The full artifact, digest included."""
    integrations = wire_contract_integrations()
    return {
        "schema": SCHEMA,
        "digest": canonical_digest(integrations),
        "digest_covers": DIGEST_COVERS,
        "integrations": integrations,
        "commands": wire_contract_commands(),
    }


def render_wire_contract() -> str:
    """The artifact as the exact text the checked-in file holds.

    Indented and key-sorted so a rename lands in review as a readable diff to a
    contract, which is the entire consequence CORE-008 is buying. Carries no
    timestamp: a generated-at stamp would churn the file on every regeneration
    and force the fitness check to learn to ignore a field.
    """
    return (
        json.dumps(build_wire_contract(), indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    )
