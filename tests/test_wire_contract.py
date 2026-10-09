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
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import pytest

import fitness.wire_contract as fitness_module
from fitness.__main__ import load_baseline
from fitness.wire_contract import WIRE_CONTRACT_PATH, check_wire_contract
from stormpulse.agent.wire_contract import (
    CONTRACT_DIGEST_COVERS,
    ENVELOPE_ENTRY,
    SCHEMA,
    VECTOR_SECRET_HEX,
    build_wire_contract,
    command_wire_entry,
    contract_digest,
    render_wire_contract,
    wire_contract_commands,
    wire_contract_digest,
    wire_contract_integrations,
    wire_contract_messages,
    wire_contract_vectors,
)
from stormpulse.auth import sign_envelope
from stormpulse.caddy.commands import build_caddy_specs
from stormpulse.caddy.config import CaddyConfig
from stormpulse.commands.registry import COMMAND_REGISTRY
from stormpulse.config import CommandSpec, ParamDef
from stormpulse.events import ENVELOPE_FIELDS, declared_events
from stormpulse.garage.commands import build_garage_specs
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import GarageBucket, GarageKeyRef, GarageState
from stormpulse.protocol import (
    MESSAGES,
    SPEAKS,
    AckPayload,
    CommandProgressPayload,
    ContainerInfo,
    Direction,
    Envelope,
    LogBatchPayload,
    MessageType,
    MetricsPayload,
    RegisterPayload,
    dataclass_wire_shape,
    dataclass_wire_types,
    make_metrics_push,
)
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
    ``pytest`` alone still learns the artifact is stale. The baseline's own
    lines are the harness's business, subtracted here as it subtracts them.
    """
    assert [v for v in check_wire_contract() if v not in load_baseline()] == []


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
    with pytest.raises(TypeError, match="dataclass_wire_types requires a dataclass"):
        dataclass_wire_types(int)


# -- the typed walker: what the shape walker deliberately does not see ------


def test_types_report_json_types_and_walk_nested_classes() -> None:
    classes = dataclass_wire_types(MetricsPayload)
    assert set(classes) == {"MetricsPayload", "ContainerInfo", "IntegrationReport"}
    metrics = classes["MetricsPayload"]
    numbers = [name for name, f in metrics.items() if f["type"] == "number"]
    assert len(numbers) == 10
    assert metrics["containers"] == {
        "type": "array",
        "nullable": False,
        "required": True,
    }
    assert metrics["integrations"] == {
        "type": "object",
        "nullable": True,
        "required": False,
    }
    assert classes["ContainerInfo"]["name"]["type"] == "string"


def test_types_emit_a_union_as_a_sorted_list() -> None:
    @dataclass(frozen=True)
    class Mixed:
        position: str | int
        maybe: int | str | None = None

    fields_ = dataclass_wire_types(Mixed)["Mixed"]
    assert fields_["position"] == {
        "type": ["integer", "string"],
        "nullable": False,
        "required": True,
    }
    assert fields_["maybe"] == {
        "type": ["integer", "string"],
        "nullable": True,
        "required": False,
    }
    assert dataclass_wire_types(LogBatchPayload)["LogBatchPayload"]["from_position"][
        "type"
    ] == [
        "integer",
        "string",
    ]


def test_types_distinguish_nullable_from_required() -> None:
    """``X | None`` is nullable; a default makes a field optional. Independent axes."""

    @dataclass(frozen=True)
    class Axes:
        plain: int
        nullable_but_required: int | None
        defaulted: int = 0
        both: int | None = None
        factory: list[str] = field(default_factory=list)

    fields_ = dataclass_wire_types(Axes)["Axes"]
    assert fields_["plain"] == {"type": "integer", "nullable": False, "required": True}
    assert fields_["nullable_but_required"] == {
        "type": "integer",
        "nullable": True,
        "required": True,
    }
    assert fields_["defaulted"] == {
        "type": "integer",
        "nullable": False,
        "required": False,
    }
    assert fields_["both"] == {"type": "integer", "nullable": True, "required": False}
    assert fields_["factory"] == {"type": "array", "nullable": False, "required": False}
    register = dataclass_wire_types(RegisterPayload)["RegisterPayload"]
    assert register["version"] == {
        "type": "string",
        "nullable": False,
        "required": True,
    }
    # CORE-011 d2: what the agent speaks and emits is required on every register.
    assert register["speaks"] == {"type": "array", "nullable": False, "required": True}
    assert register["contract_digest"] == {
        "type": "string",
        "nullable": False,
        "required": True,
    }


def test_register_declares_fifteen_required_fields_and_its_two_nested_classes() -> None:
    """CORE-011 d7: the node's whole self-declaration, every field required,
    ``unsealed_since`` the only nullable one, ``components`` typed by ``Component``
    and each ``integrations`` value by ``IntegrationReport`` (d3: no key the
    control plane reads by name is left undeclared)."""
    entry = wire_contract_messages()["register"]
    declared = entry["classes"]["RegisterPayload"]
    assert len(declared) == 15
    assert [name for name, f in declared.items() if not f["required"]] == []
    assert [name for name, f in declared.items() if f["nullable"]] == ["unsealed_since"]
    assert declared["wire_contract"]["type"] == "string"
    assert declared["components"] == {
        "type": "array",
        "nullable": False,
        "required": True,
    }
    assert set(entry["classes"]) == {
        "RegisterPayload",
        "Component",
        "IntegrationReport",
    }
    assert entry["classes"]["Component"] == {
        name: {"type": "string", "nullable": False, "required": True}
        for name in ("name", "version", "digest")
    }
    report = entry["classes"]["IntegrationReport"]
    assert [name for name, f in report.items() if not f["required"]] == []
    assert {name: f["type"] for name, f in report.items()} == {
        "status": "string",
        "disabled_reason": "string",
        "state": "object",
        "command_specs_digest": "string",
        "commands_fenced": "string",
        "generation": "integer",
        "complete": "boolean",
        "last_full_read_at": "string",
        "changed_ids": "array",
    }
    assert [name for name, f in report.items() if f["nullable"]] == [
        "disabled_reason",
        "state",
        "commands_fenced",
    ]
    # The same class under metrics.push: one declaration, two carriers.
    metrics = wire_contract_messages()["metrics.push"]["classes"]
    assert metrics["IntegrationReport"] == report


@dataclass(frozen=True)
class _Leaf:
    flag: bool


@dataclass(frozen=True)
class _Branch:
    """Module-level so ``get_type_hints`` can resolve the forward references."""

    leaf: _Leaf
    leaves: list[_Leaf]
    maybe_leaf: _Leaf | None
    tags: tuple[str, ...]
    counts: dict[str, int]


def test_types_name_a_nested_dataclass_and_scalar_containers() -> None:
    classes = dataclass_wire_types(_Branch)
    assert set(classes) == {"_Branch", "_Leaf"}
    types = {name: f["type"] for name, f in classes["_Branch"].items()}
    assert types == {
        "leaf": "_Leaf",
        "leaves": "array",
        "maybe_leaf": "_Leaf",
        "tags": "array",
        "counts": "object",
    }
    assert classes["_Branch"]["maybe_leaf"]["nullable"] is True
    assert classes["_Leaf"]["flag"]["type"] == "boolean"


def test_types_see_the_retype_the_shape_walker_ignores() -> None:
    """The counterpart of the digest test above: a type change is visible here."""

    @dataclass(frozen=True)
    class Before:
        count: int

    @dataclass(frozen=True)
    class Retyped:
        count: str

    @dataclass(frozen=True)
    class Renamed:
        total: int

    before = dataclass_wire_types(Before)["Before"]
    assert before != dataclass_wire_types(Retyped)["Retyped"]
    assert before != dataclass_wire_types(Renamed)["Renamed"]


def test_types_refuse_an_annotation_with_no_wire_type() -> None:
    """A field the vocabulary cannot name fails loudly instead of being certified as something."""

    @dataclass(frozen=True)
    class Stamped:
        when: datetime

    with pytest.raises(TypeError, match="no wire type"):
        dataclass_wire_types(Stamped)
    # Unless the caller says what its serializer writes instead (Envelope.ts).
    assert dataclass_wire_types(Stamped, formatted={"when": "string"})["Stamped"] == {
        "when": {"type": "string", "nullable": False, "required": True}
    }


def test_types_name_a_str_enum_as_the_string_it_serializes_to() -> None:
    @dataclass(frozen=True)
    class Tagged:
        kind: MessageType
        maybe: Direction | None = None

    fields_ = dataclass_wire_types(Tagged)["Tagged"]
    assert fields_["kind"] == {"type": "string", "nullable": False, "required": True}
    assert fields_["maybe"] == {"type": "string", "nullable": True, "required": False}


def test_types_cover_every_catalogued_message_payload() -> None:
    for spec in MESSAGES.values():
        assert spec.payload.__name__ in dataclass_wire_types(spec.payload)


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
        "messages",
        "commands",
        "contract_digest",
        "vectors",
    }
    assert SCHEMA == 4


# -- the messages section and the v2 digest (CORE-011 decision 3) ----------


def test_messages_section_lists_the_envelope_and_every_catalogued_type() -> None:
    messages = wire_contract_messages()
    assert set(messages) == {ENVELOPE_ENTRY} | {t.value for t in MessageType}
    assert len(messages) == len(MESSAGES) + 1 == 21


def test_envelope_entry_is_walked_from_the_class_as_to_dict_writes_it() -> None:
    """Six fields, each a triple like any payload's, so a retype of ``v`` or an
    Optional ``agent_id`` lands here; ``ts`` and ``type`` are the strings
    ``Envelope.to_dict`` writes, not the datetime and enum the class holds."""
    envelope = wire_contract_messages()[ENVELOPE_ENTRY]
    assert envelope["root"] == "Envelope"
    assert "direction" not in envelope
    plain = {"nullable": False, "required": True}
    assert envelope["classes"] == {
        "Envelope": {
            "v": {"type": "integer", **plain},
            "type": {"type": "string", **plain},
            "id": {"type": "string", **plain},
            "ts": {"type": "string", **plain},
            "agent_id": {"type": "string", **plain},
            "payload": {"type": "object", **plain},
        }
    }
    # ``received`` is the parsed frame kept for the MAC, never written to the wire.
    assert set(envelope["classes"]["Envelope"]) == {
        f.name for f in fields(Envelope)
    } - {"received"}


def test_messages_entry_carries_direction_root_and_typed_classes() -> None:
    messages = wire_contract_messages()
    progress = messages["command.progress"]
    assert progress["direction"] == "agent->dashboard"
    assert progress["root"] == "CommandProgressPayload"
    assert progress["classes"] == dataclass_wire_types(CommandProgressPayload)
    assert messages["command.request"]["direction"] == "dashboard->agent"
    # Empty acks each repeat AckPayload's classes: an entry reads on its own.
    acks = [t.value for t, spec in MESSAGES.items() if spec.payload is AckPayload]
    assert len(acks) == 3
    assert "register.ok" not in acks
    for name in acks:
        assert messages[name]["classes"] == dataclass_wire_types(AckPayload)


def test_notes_name_the_one_entry_where_the_wire_differs_from_the_class() -> None:
    """``metrics.push`` carries less than the class (``jobs`` absent, never
    null). Nothing else: ``command.result`` lost its note when the extras
    moved under the declared ``result`` field (CORE-011 d9)."""
    messages = wire_contract_messages()
    noted = {name for name, entry in messages.items() if "_note" in entry}
    assert noted == {"metrics.push"}
    assert "jobs is omitted, never null" in messages["metrics.push"]["_note"]
    assert messages["command.result"]["classes"]["CommandResultPayload"]["result"] == {
        "type": "object",
        "nullable": True,
        "required": False,
    }
    assert messages["command.result"]["classes"]["CommandResultPayload"]["error"] == {
        "type": "ResultError",
        "nullable": True,
        "required": False,
    }
    assert set(messages["command.result"]["classes"]["ResultError"]) == {
        "code",
        "http_status",
        "upstream_code",
        "message",
    }


def test_contract_digest_is_reproducible_from_the_artifact_alone() -> None:
    """The v2 crux, same as the v1 one: a consumer holding only the file
    recomputes ``contract_digest`` from the three sections it covers."""
    artifact = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert CONTRACT_DIGEST_COVERS == ("integrations", "messages", "commands")
    covered = {key: artifact[key] for key in CONTRACT_DIGEST_COVERS}
    assert canonical_digest(covered) == artifact["contract_digest"]
    assert contract_digest(artifact) == artifact["contract_digest"]
    # And the live classes, never the file, produce the same value.
    assert contract_digest() == artifact["contract_digest"]


def test_contract_digest_moves_with_messages_but_the_v1_digest_does_not() -> None:
    artifact = build_wire_contract()
    renamed = json.loads(json.dumps(artifact))
    classes = renamed["messages"]["command.progress"]["classes"]
    classes["CommandProgressPayload"]["renamed_field"] = classes[
        "CommandProgressPayload"
    ].pop("bytes_freed")
    assert contract_digest(renamed) != artifact["contract_digest"]
    assert canonical_digest(renamed[renamed["digest_covers"]]) == artifact["digest"]


# -- the golden MAC vectors (CORE-011 decision 4) ---------------------------

_SIGNED_TYPES = {MessageType.COMMAND_REQUEST.value, MessageType.COMMAND_SEQUENCE.value}


def test_vectors_sign_with_the_published_key() -> None:
    """A consumer holding only the file reproduces each ``hmac`` from the
    envelope and the key with ``sign_envelope``; the frame itself parses at
    the spoken version into the typed payload it names. Never ``verify_envelope``:
    freshness refuses a fixed ``ts``, which is the point of a vector."""
    vectors = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))["vectors"]
    assert set(vectors) == _SIGNED_TYPES
    for name, vector in vectors.items():
        assert set(vector) == {"secret", "envelope", "hmac"}
        key = bytes.fromhex(vector["secret"])
        assert key == bytes(range(32))
        assert sign_envelope(vector["envelope"], key) == vector["hmac"]
        assert vector["envelope"]["payload"]["hmac"] == vector["hmac"]
        frame = Envelope.from_json(json.dumps(vector["envelope"]))
        assert frame.v == SPEAKS[-1]
        assert frame.type.value == name
        payload = MESSAGES[frame.type].payload.from_dict(frame.payload)
        assert payload.hmac == vector["hmac"]  # type: ignore[attr-defined]


def test_vectors_are_fixed_so_regeneration_is_a_no_op() -> None:
    """No fresh id, ts or nonce: two renders agree byte for byte."""
    assert wire_contract_vectors() == wire_contract_vectors()
    assert VECTOR_SECRET_HEX == bytes(range(32)).hex()


def test_vectors_sit_outside_both_digests() -> None:
    """The digests hash the shape, never an example of it: a vector that moves
    (a new test key, a new fixed nonce) is not a contract change."""
    artifact = build_wire_contract()
    moved = json.loads(json.dumps(artifact))
    for vector in moved["vectors"].values():
        vector["hmac"] = "0" * 64
        vector["envelope"]["payload"]["hmac"] = "0" * 64
    assert "vectors" not in CONTRACT_DIGEST_COVERS
    assert contract_digest(moved) == artifact["contract_digest"]
    assert canonical_digest(moved[moved["digest_covers"]]) == artifact["digest"]


def test_fitness_fails_on_a_hand_edited_or_missing_vector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    artifact["vectors"]["command.request"]["hmac"] = "0" * 64
    del artifact["vectors"]["command.sequence"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert (
        "vectors.command.request: declared vector is not what the live signer "
        "writes; regenerate" in "\n".join(violations)
    )
    assert any(
        "vectors: vector 'command.sequence' is signed but not declared" in v
        for v in violations
    )
    assert not any("digest" in v for v in violations)


# -- the command surface: declared, outside the digest ---------------------

# The digest deployed agents advertise and consumers compare. Pinned literally:
# the commands section must never move it, and a deliberate move is a fleet event.
_INTEGRATIONS_DIGEST = (
    "sha256:2a36d610664c613cacf6fae9d14e38caf718601449aed32a1931419a7ece9dd6"
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


def test_fitness_fails_on_a_result_shape_drifting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    clear = artifact["commands"]["garage_bucket_clear"]["result"]
    clear["classes"]["ClearBucketResult"]["deleted_count"]["type"] = "string"
    del clear["classes"]["ClearBucketResult"]["bytes_freed"]
    artifact["commands"]["garage_walk_bucket_stats"]["result"] = None
    artifact["commands"]["git_pull"]["result"] = {"root": "Nope", "classes": {}}
    artifact["commands"]["garage_bucket_set_quota"]["result"]["root"] = "QuotaFigures"
    violations = _check_with(monkeypatch, tmp_path, artifact)
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


# -- Function 9 covers messages (CORE-011 decision 3) ----------------------


def test_fitness_names_the_class_when_a_message_field_is_renamed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The brief's own prediction: a rename without regenerating names
    ``messages.command.progress.CommandProgressPayload`` and the field."""
    artifact = build_wire_contract()
    progress = artifact["messages"]["command.progress"]["classes"]
    progress["CommandProgressPayload"]["freed_bytes"] = progress[
        "CommandProgressPayload"
    ].pop("bytes_freed")
    violations = _check_with(monkeypatch, tmp_path, artifact)
    at = "messages.command.progress.CommandProgressPayload"
    assert f"{at}: declared field 'freed_bytes' is no longer emitted" in "\n".join(
        violations
    )
    assert any(f"{at}: emits undeclared field 'bytes_freed'" in v for v in violations)


def test_fitness_names_the_attribute_when_a_message_field_retypes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    freed = artifact["messages"]["command.progress"]["classes"][
        "CommandProgressPayload"
    ]["bytes_freed"]
    freed["type"] = "number"
    freed["nullable"] = False
    freed["required"] = True
    violations = _check_with(monkeypatch, tmp_path, artifact)
    at = "messages.command.progress.CommandProgressPayload.bytes_freed"
    assert any(
        f"{at}: declared type 'number' != emitted 'integer'" in v for v in violations
    )
    assert any(
        f"{at}: declared nullable False != emitted True" in v for v in violations
    )
    assert any(
        f"{at}: declared required True != emitted False" in v for v in violations
    )


def test_fitness_fails_on_an_undeclared_nested_message_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    del artifact["messages"]["metrics.push"]["classes"]["ContainerInfo"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "messages.metrics.push: class 'ContainerInfo' is emitted but not declared" in v
        for v in violations
    )


def test_fitness_fails_on_the_envelope_retyping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    envelope = artifact["messages"]["envelope"]["classes"]["Envelope"]
    envelope["v"]["type"] = "string"
    del envelope["ts"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "messages.envelope.Envelope.v: declared type 'string' != emitted 'integer'" in v
        for v in violations
    )
    assert any(
        "messages.envelope.Envelope: emits undeclared field 'ts'" in v
        for v in violations
    )


def test_fitness_fails_on_a_message_direction_or_root_drifting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    artifact["messages"]["command.progress"]["direction"] = "dashboard->agent"
    artifact["messages"]["command.progress"]["root"] = "AckPayload"
    del artifact["messages"]["metrics.push"]["_note"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "messages.command.progress: declared direction 'dashboard->agent' != emitted "
        "'agent->dashboard'" in v
        for v in violations
    )
    assert any(
        "messages.command.progress: declared root 'AckPayload' != emitted "
        "'CommandProgressPayload'" in v
        for v in violations
    )
    assert any(
        v.startswith("messages.metrics.push: declared _note None != emitted")
        for v in violations
    )


def test_fitness_fails_on_a_message_missing_or_uncatalogued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    del artifact["messages"]["command.progress"]
    artifact["messages"]["command.retired"] = {
        "direction": "agent->dashboard",
        "root": "AckPayload",
        "classes": {},
    }
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "messages: message 'command.progress' is catalogued but not declared" in v
        for v in violations
    )
    assert any(
        "messages: declared message 'command.retired' is no longer catalogued" in v
        for v in violations
    )


def test_fitness_names_every_message_when_the_file_predates_the_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A schema-2 file has no ``messages`` or ``contract_digest``: one line per
    catalogued message plus the envelope, no crash on the missing keys."""
    artifact = build_wire_contract()
    del artifact["messages"]
    del artifact["contract_digest"]
    artifact["schema"] = 2
    violations = _check_with(monkeypatch, tmp_path, artifact)
    undeclared = [v for v in violations if "is catalogued but not declared" in v]
    assert len(undeclared) == len(wire_contract_messages()) == 21


def test_fitness_fails_on_a_hand_edited_contract_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The brief's second prediction: the shape agrees, the v2 digest does not,
    and the file alone cannot reproduce what it carries."""
    artifact = build_wire_contract()
    live = artifact["contract_digest"]
    artifact["contract_digest"] = "sha256:" + "0" * 64
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "carries a contract_digest that does not match "
        "('integrations', 'messages', 'commands'), the content it covers, so a "
        "consumer holding only this file cannot reproduce it" in v
        for v in violations
    )
    assert any(
        f"declared contract_digest sha256:{'0' * 64} != live contract_digest {live}"
        in v
        for v in violations
    )
    assert not any("digest_covers" in v or "declared digest " in v for v in violations)


def test_fitness_names_a_wrong_digest_covers_and_still_reproduces_the_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The name is its own line; the digest is checked against the section it
    really covers, so a mislabelled but correct digest reports once."""
    artifact = build_wire_contract()
    artifact["digest_covers"] = "commands"
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert "digest_covers is 'commands', expected 'integrations'" in "\n".join(
        violations
    )
    assert not any(
        "carries a digest" in v or "declared digest " in v for v in violations
    )


def test_fitness_keeps_the_v1_digest_lines_apart_from_the_v2_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hand-edited v1 ``digest`` reports its own two lines and leaves
    ``contract_digest`` silent: the overlap's two digests fail independently."""
    artifact = build_wire_contract()
    artifact["digest"] = "sha256:" + "0" * 64
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert sum("carries a digest that does not match" in v for v in violations) == 1
    assert sum(v.startswith("declared digest ") for v in violations) == 1
    assert not any("contract_digest" in v for v in violations)


# -- the unit check and the payload the wire carries (CORE-011 d3, d5) -----

_UNIT_LINE = (
    "messages: numeric field {cls}.{field} carries no unit in its name "
    "(_bytes, _gb, _mb, _ms, _percent, _seconds) and is not a count; rename it"
)


@dataclass(frozen=True)
class _Unitless:
    """Module-level so ``get_type_hints`` resolves it; one field per rule."""

    speed: float
    rate_bytes_per_sec: int
    objects_total: int
    exit_code: int
    protocol: int
    position: int | str
    label: str
    ratio: float | None = None


def test_unit_check_flags_a_numeric_field_with_no_unit_or_count_name() -> None:
    """The brief's prediction: ``speed: float`` fails; a unit word anywhere in
    the name, a count name (whole, or as a ``_suffix``) or the version
    ``register.ok`` picks (``protocol``, CORE-011 d2) passes."""
    entry: dict[str, Any] = {"direction": "agent->dashboard", "root": "_Unitless"}
    entry["classes"] = dataclass_wire_types(_Unitless)
    violations = fitness_module._unit_violations({"x": entry})
    assert violations == [
        _UNIT_LINE.format(cls="_Unitless", field="position"),
        _UNIT_LINE.format(cls="_Unitless", field="ratio"),
        _UNIT_LINE.format(cls="_Unitless", field="speed"),
    ]


def test_unit_check_skips_the_envelope_and_reports_a_shared_class_once() -> None:
    """``envelope.v`` is a version, not a measure; a class the acks repeat
    is one finding, not five."""
    shared = {"classes": {"Shared": {"speed": {"type": "number"}}}}
    envelope = {"classes": {"Envelope": {"v": {"type": "integer"}}}}
    messages = {ENVELOPE_ENTRY: envelope, "a.ack": shared, "b.ack": shared}
    assert fitness_module._unit_violations(messages) == [
        _UNIT_LINE.format(cls="Shared", field="speed")
    ]


def test_fitness_reports_only_the_two_unsuffixed_load_averages_today() -> None:
    """Function 9 against the live tree: the baseline's two lines, nothing else.
    Renaming a load average with its unit makes a baseline line stale; the
    harness then fails until the line is removed, so the baseline only shrinks."""
    expected = [
        _UNIT_LINE.format(cls="MetricsPayload", field=name)
        for name in ("load_avg_1m", "load_avg_5m")
    ]
    assert check_wire_contract() == expected
    assert set(expected) <= load_baseline()


def test_fitness_fails_when_a_payload_gains_an_unsuffixed_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ``check_wire_contract``: the live classes gain
    ``speed: float`` and Function 9 names it, before anyone regenerates."""
    live = build_wire_contract()
    live["messages"]["metrics.push"]["classes"]["MetricsPayload"]["speed"] = {
        "type": "number",
        "nullable": False,
        "required": True,
    }
    monkeypatch.setattr(fitness_module, "build_wire_contract", lambda: live)
    assert _UNIT_LINE.format(cls="MetricsPayload", field="speed") in (
        check_wire_contract()
    )


def test_metrics_push_entry_declares_every_key_the_push_carries() -> None:
    """Decision 5: ``integrations`` and ``jobs`` are fields, so the entry is
    what ``make_metrics_push`` sends, not a parallel belief about it."""
    metrics = MetricsPayload(
        cpu_percent=5.0,
        memory_percent=10.0,
        memory_used_mb=512.0,
        memory_total_mb=1024.0,
        disk_percent=20.0,
        disk_used_gb=8.0,
        disk_total_gb=40.0,
        load_avg_1m=0.1,
        load_avg_5m=0.2,
        uptime_seconds=100.0,
        containers=[ContainerInfo(name="n", status="running", image="i")],
    )
    sent = make_metrics_push(
        "a", metrics, integrations={"garage": {}}, job_load={"pending": 1}
    ).payload
    declared = wire_contract_messages()["metrics.push"]["classes"]["MetricsPayload"]

    assert set(sent) == set(declared)
    assert set(sent["containers"][0]) == set(
        wire_contract_messages()["metrics.push"]["classes"]["ContainerInfo"]
    )
    for optional in ("integrations", "jobs"):
        assert declared[optional] == {
            "type": "object",
            "nullable": True,
            "required": False,
        }


# -- test-hunt: the defects the first pass left open -----------------------


@dataclass(frozen=True)
class _Spelled:
    """``typing.Optional`` and ``typing.Union`` resolve to a different origin
    from ``X | None`` on 3.12; the older spelling must read as the same contract,
    not raise "no wire type"."""

    maybe: Optional[int]  # noqa: UP007, UP045
    either: Union[int, str]  # noqa: UP007
    both: Optional[Union[int, str]] = None  # noqa: UP007, UP045


def test_types_read_typing_optional_and_union_like_the_pipe_syntax() -> None:
    fields_ = dataclass_wire_types(_Spelled)["_Spelled"]
    assert fields_["maybe"] == {"type": "integer", "nullable": True, "required": True}
    assert fields_["either"] == {
        "type": ["integer", "string"],
        "nullable": False,
        "required": True,
    }
    assert fields_["both"] == {
        "type": ["integer", "string"],
        "nullable": True,
        "required": False,
    }


def test_unit_check_matches_count_and_unit_words_at_a_word_boundary_only() -> None:
    """``subtotal`` is not ``_total`` and ``numbers`` does not carry ``_mb``:
    both rules match whole ``_``-separated words, never a substring, or any
    unitless number could hide inside a longer name."""
    classes = {
        "C": {
            "subtotal": {"type": "integer"},
            "recurrent": {"type": "integer"},
            "numbers": {"type": "number"},
            "objects_total": {"type": "integer"},
            "size_mb": {"type": "number"},
        }
    }
    assert fitness_module._unit_violations({"x": {"classes": classes}}) == [
        _UNIT_LINE.format(cls="C", field="numbers"),
        _UNIT_LINE.format(cls="C", field="recurrent"),
        _UNIT_LINE.format(cls="C", field="subtotal"),
    ]


def test_fitness_reports_a_hand_broken_messages_section_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A null entry, a field written as a bare string, a section that is a
    list: each is a line the author can act on, never a traceback from the
    harness, which would hide every other finding behind it."""
    artifact = build_wire_contract()
    artifact["messages"]["heartbeat"] = None
    artifact["messages"]["command.progress"]["classes"]["CommandProgressPayload"][
        "bytes_freed"
    ] = "integer"
    joined = "\n".join(_check_with(monkeypatch, tmp_path, artifact))
    assert (
        "messages.heartbeat: class 'HeartbeatPayload' is emitted but not declared"
        in joined
    )
    assert (
        "messages.command.progress.CommandProgressPayload.bytes_freed: "
        "declared type None != emitted 'integer'"
    ) in joined

    artifact = build_wire_contract()
    artifact["messages"] = ["heartbeat"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(v.startswith("messages is not an object") for v in violations)
    assert not any("catalogued but not declared" in v for v in violations)


def test_fitness_names_a_declared_message_class_the_payload_no_longer_reaches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the undeclared-class test: a class that left the
    payload (a nested type removed) is named at its message, not left to rot."""
    artifact = build_wire_contract()
    artifact["messages"]["metrics.push"]["classes"]["LegacyInfo"] = {
        "name": {"type": "string", "nullable": False, "required": True}
    }
    violations = _check_with(monkeypatch, tmp_path, artifact)
    assert any(
        "messages.metrics.push: declared class 'LegacyInfo' no longer exists" in v
        for v in violations
    )


def test_read_at_is_declared_and_freshness_stays_off_the_blob() -> None:
    """CORE-011 d5: a bucket's ``read_at`` is part of the blob the digest covers;
    the read's own freshness rides ``IntegrationReport`` and is not declared twice."""
    classes = wire_contract_integrations()["garage"]["classes"]
    assert "read_at" in classes["GarageBucket"]
    assert not {"complete", "last_full_read_at", "changed_ids"} & set(
        classes["GarageState"]
    )


# -- the event sources (CORE-011 decision 6) --------------------------------


def test_events_batch_entry_lists_every_source_and_kind_the_agent_declares() -> None:
    """``sources`` is the registry verbatim: seven sources, ten kinds today, the
    envelope fields never among a kind's fields. No other entry carries it."""
    messages = wire_contract_messages()
    sources = messages["events.batch"]["sources"]
    assert sources == declared_events()
    assert sum(len(kinds) for kinds in sources.values()) == 10
    assert set(sources) == {
        "auth",
        "cert",
        "commands",
        "events",
        "garage_admin",
        "jobs",
        "ws",
    }
    for kinds in sources.values():
        for fields_ in kinds.values():
            assert fields_ == sorted(fields_)
            assert not set(fields_) & ENVELOPE_FIELDS
    assert [name for name, entry in messages.items() if "sources" in entry] == [
        "events.batch"
    ]


def test_sources_ride_inside_the_contract_digest_but_not_the_v1_digest() -> None:
    artifact = build_wire_contract()
    stripped = json.loads(json.dumps(artifact))
    del stripped["messages"]["events.batch"]["sources"]
    assert contract_digest(stripped) != artifact["contract_digest"]
    assert canonical_digest(stripped[stripped["digest_covers"]]) == artifact["digest"]


def test_fitness_fails_on_a_declared_kind_missing_from_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One kind dropped from a listed source, one whole source dropped: a line
    per missing kind, naming its source."""
    artifact = build_wire_contract()
    sources = artifact["messages"]["events.batch"]["sources"]
    del sources["events"]["events_nacked"]
    del sources["cert"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    at = "messages.events.batch.sources"
    for line in (
        f"{at}.events: kind 'events_nacked' is declared but not listed",
        f"{at}.cert: kind 'cert_renew_failed' is declared but not listed",
        f"{at}.cert: kind 'cert_renew_succeeded' is declared but not listed",
    ):
        assert any(line in v for v in violations), line
    assert not any("dropped_events" in v for v in violations)


def test_fitness_fails_on_a_listed_kind_nobody_declares_or_whose_fields_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = build_wire_contract()
    sources = artifact["messages"]["events.batch"]["sources"]
    sources["ws"]["retired"] = ["reason"]
    sources["ws"]["reconnect"] = ["category", "reason"]
    violations = _check_with(monkeypatch, tmp_path, artifact)
    at = "messages.events.batch.sources.ws"
    assert any(
        f"{at}: listed kind 'retired' is no longer declared" in v for v in violations
    )
    assert any(
        f"{at}.reconnect: declared fields ['category', 'reason'] != emitted "
        "['category', 'error', 'reason']" in v
        for v in violations
    )


# -- cancelled and steps (CORE-011 d9) --------------------------------------


def test_command_cancelled_is_catalogued_outbound_with_its_three_fields() -> None:
    entry = wire_contract_messages()["command.cancelled"]
    plain = {"nullable": False, "required": True}
    assert entry["direction"] == "agent->dashboard"
    assert entry["root"] == "CommandCancelledPayload"
    assert entry["classes"] == {
        "CommandCancelledPayload": {
            "request_id": {"type": "string", **plain},
            "command": {"type": "string", **plain},
            "reason": {"type": "string", **plain},
        }
    }


def test_sequence_steps_and_step_index_are_declared_and_signed() -> None:
    """``steps`` is an array of ``Step``, the names-only ``commands`` is gone;
    ``step_index`` is a nullable integer the unit check reads as a position;
    the golden sequence vector signs a step whose params are a real object."""
    messages = wire_contract_messages()
    plain = {"nullable": False, "required": True}
    sequence = messages["command.sequence"]["classes"]
    assert "commands" not in sequence["CommandSequencePayload"]
    assert sequence["CommandSequencePayload"]["steps"] == {"type": "array", **plain}
    assert sequence["Step"] == {
        "command": {"type": "string", **plain},
        "params": {"type": "object", **plain},
    }
    result = messages["command.result"]["classes"]["CommandResultPayload"]
    assert result["step_index"] == {
        "type": "integer",
        "nullable": True,
        "required": False,
    }
    assert not [
        v for v in fitness_module._unit_violations(messages) if "step_index" in v
    ]
    steps = wire_contract_vectors()["command.sequence"]["envelope"]["payload"]["steps"]
    assert [s["command"] for s in steps] == ["git_pull", "docker_logs"]
    assert steps[1]["params"] == {"tail": "100"}
