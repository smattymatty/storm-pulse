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
from stormpulse.agent.event_spool import SEGMENT_FORMAT
from stormpulse.auth import sign_envelope
from stormpulse.commands.registry import COMMAND_REGISTRY
from stormpulse.config import CommandSpec, ParamDef
from stormpulse.events import declared_events, declared_integration_events
from stormpulse.garage.wire_shape import garage_wire_shape
from stormpulse.integrations import integration_command_specs, registered_integrations
from stormpulse.protocol import (
    MESSAGES,
    SPEAKS,
    Envelope,
    MessageType,
    dataclass_wire_types,
)
from stormpulse.sdk.declaration import canonical_digest

# Version of the ARTIFACT's envelope, not its shape: bumped only when a top-level
# key changes (consumer-breaking). 2: ``commands``. 3: ``messages`` and
# ``contract_digest``. 4: ``vectors``. 5: ``integration_events``.
SCHEMA = 5

# Which top-level key the v1 digest covers, carried IN the artifact so a consumer
# holding only the file can reproduce it. Deployed v1 agents compare this digest
# at connect, so it stays on ``integrations`` alone through the overlap (CORE-011 d1).
DIGEST_COVERS = "integrations"

# The sections the v2 ``contract_digest`` hashes together, in this order of keys.
CONTRACT_DIGEST_COVERS = ("integrations", "messages", "commands")

# The ``messages`` entry for the envelope every message rides in: walked like a
# payload, except ``ts``, which ``Envelope.to_dict`` formats to a string.
ENVELOPE_ENTRY = "envelope"
_ENVELOPE_FORMATTED = {"ts": "string"}

# Where the wire carries something the class does not say; a note leaves with
# the decision that closes the gap (d9 moved command.result's extras under ``result``).
_NOTES: dict[MessageType, str] = {
    MessageType.METRICS_PUSH: (
        "jobs is omitted, never null, when the agent runs no job manager"
    ),
}


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
        if not integ.in_release_catalog:
            continue
        if integ.specs is None and integ.collect_state is None:
            continue
        if integ.declared_config is None:
            raise LookupError(
                f"Integration {integ.id!r} declares no declared_config, so its "
                "commands cannot be listed without a host (Function 5 pins this)"
            )
        parsed = integ.parse_config(dict(integ.declared_config))
        specs.update(integration_command_specs(integ, parsed))
    return specs


def command_wire_entry(spec: CommandSpec) -> dict[str, Any]:
    """One command's declared surface, no argv: its params and their validators,
    and the shape of its ``result`` (root class and every class it reaches,
    typed like a message), null for a command that returns none."""
    return {
        "params": {name: _param_entry(p) for name, p in sorted(spec.params.items())},
        "result": (
            None
            if spec.result is None
            else {
                "root": spec.result.__name__,
                "classes": dataclass_wire_types(spec.result),
            }
        ),
    }


def _param_entry(pdef: ParamDef) -> dict[str, Any]:
    # A secret's default would put the secret in a public file; it never rides.
    # The schema is copied: the artifact must never alias a live declaration.
    entry: dict[str, Any] = {
        "type": pdef.wire_type,
        "pattern": pdef.pattern,
        "max_bytes": pdef.max_bytes,
        "schema": copy.deepcopy(pdef.schema),
        "secret": pdef.secret,
    }
    if pdef.default is not None and not pdef.secret:
        entry["default"] = pdef.default
    return entry


def wire_contract_messages() -> dict[str, Any]:
    """Every message type in the catalogue, typed field by field (CORE-011 d3).

    ``envelope`` first (no direction: it travels both ways), then one entry per
    ``MessageType``: its direction, the payload class at the root and every
    class reachable from it with each field's JSON type, nullability and
    optionality. Empty acks each repeat ``AckPayload``: nothing is hoisted.
    """
    messages: dict[str, Any] = {
        ENVELOPE_ENTRY: {
            "root": Envelope.__name__,
            "classes": dataclass_wire_types(Envelope, formatted=_ENVELOPE_FORMATTED),
        }
    }
    for msg_type, spec in MESSAGES.items():
        entry: dict[str, Any] = {
            "direction": spec.direction.value,
            "root": spec.payload.__name__,
            "classes": dataclass_wire_types(spec.payload),
        }
        if msg_type in _NOTES:
            entry["_note"] = _NOTES[msg_type]
        if msg_type is MessageType.EVENTS_BATCH:
            entry["sources"] = wire_contract_event_sources()
        messages[msg_type.value] = entry
    return messages


def wire_contract_event_sources() -> dict[str, Any]:
    """Every (source, kind) the agent declares, with the fields emit keeps
    (CORE-011 d6): ``{source: {kind: [fields]}}``, the envelope fields apart.

    Read from the registry, which the emitting modules fill at import; this
    module lives in ``stormpulse.agent``, whose package import is the agent's
    composition root, so by the time anyone calls this every emitter is loaded.
    """
    return declared_events()


def wire_contract_integration_events() -> dict[str, Any]:
    """The spool segment format an emitter writes, and this host's integration
    sources shaped like ``messages.events.batch.sources``. Per host, so outside
    both digests: the published file carries the format and no source."""
    return {
        "segment": copy.deepcopy(SEGMENT_FORMAT),
        "sources": declared_integration_events(),
    }


# Golden MAC vectors (CORE-011 d4): two fixed command envelopes signed with a
# published test key, so the control plane proves it signs the same bytes. Key is
# the byte ramp 00..1f, nothing on any node; outside both digests (shape, not example).
VECTOR_SECRET_HEX = bytes(range(32)).hex()
_VECTOR_TS = "2026-10-09T00:00:00Z"
_VECTOR_ENVELOPES: dict[MessageType, dict[str, Any]] = {
    MessageType.COMMAND_REQUEST: {
        "id": "00000000-0000-4000-8000-000000000001",
        "agent_id": "vector-agent",
        "payload": {
            "command": "git_pull",
            "params": {"branch": "main", "depth": "1"},
            "nonce": "vector-nonce-request",
        },
    },
    MessageType.COMMAND_SEQUENCE: {
        "id": "00000000-0000-4000-8000-000000000002",
        "agent_id": "vector-agent",
        "payload": {
            "sequence_id": "00000000-0000-4000-8000-000000000003",
            "steps": [
                {"command": "git_pull", "params": {}},
                {"command": "docker_logs", "params": {"tail": "100"}},
            ],
            "stop_on_failure": True,
            "nonce": "vector-nonce-sequence",
        },
    },
}


def wire_contract_vectors() -> dict[str, Any]:
    """One vector per signed message type: the frame as the wire carries it.

    ``secret`` is the key as hex, ``envelope`` the whole frame with
    ``payload.hmac`` filled, ``hmac`` the value a signer must reproduce from
    the envelope and the key. A consumer asserts on these, never on prose.
    """
    vectors: dict[str, Any] = {}
    for msg_type, fixed in _VECTOR_ENVELOPES.items():
        envelope = {
            "v": SPEAKS[-1],
            "type": msg_type.value,
            "ts": _VECTOR_TS,
            **fixed,
            "payload": dict(fixed["payload"]),
        }
        mac = sign_envelope(envelope, bytes.fromhex(VECTOR_SECRET_HEX))
        envelope["payload"]["hmac"] = mac
        vectors[msg_type.value] = {
            "secret": VECTOR_SECRET_HEX,
            "envelope": envelope,
            "hmac": mac,
        }
    return vectors


def wire_contract_digest() -> str:
    """The v1 digest a register advertises, kept through the overlap (CORE-011 d1).

    Covers ``integrations`` alone, the bytes deployed consumers compare at
    connect; ``contract_digest`` is the v2 value. True of the agent that sent it
    by construction: it hashes the classes about to do the serializing, now.
    """
    return canonical_digest(wire_contract_integrations())


def contract_digest(contract: dict[str, Any] | None = None) -> str:
    """The v2 digest: one hash over ``CONTRACT_DIGEST_COVERS`` (CORE-011 d3).

    From the live classes when called bare (what a v2 register advertises);
    from a contract document when given one, so a consumer holding only the
    file reproduces the same value from the same three sections.
    """
    if contract is None:
        contract = {
            "integrations": wire_contract_integrations(),
            "messages": wire_contract_messages(),
            "commands": wire_contract_commands(),
        }
    return canonical_digest({key: contract.get(key) for key in CONTRACT_DIGEST_COVERS})


def build_wire_contract() -> dict[str, Any]:
    """The full artifact, both digests included."""
    integrations = wire_contract_integrations()
    contract: dict[str, Any] = {
        "schema": SCHEMA,
        "digest": canonical_digest(integrations),
        "digest_covers": DIGEST_COVERS,
        "integrations": integrations,
        "messages": wire_contract_messages(),
        "commands": wire_contract_commands(),
        "vectors": wire_contract_vectors(),
        "integration_events": wire_contract_integration_events(),
    }
    contract["contract_digest"] = contract_digest(contract)
    return contract


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
