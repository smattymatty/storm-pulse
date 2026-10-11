"""The messages section: every catalogued type with its direction, root and
typed classes, the event sources, and Function 9's lines when one drifts
(CORE-011 decisions 3, 6 and 9).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace
from typing import Any

import fitness.wire_contract as fitness_module
from stormpulse.agent.wire_contract import (
    ENVELOPE_ENTRY,
    build_wire_contract,
    wire_contract_messages,
    wire_contract_vectors,
)
from stormpulse.events import ENVELOPE_FIELDS, declared_events
from stormpulse.protocol import (
    MESSAGES,
    AckPayload,
    CommandProgressPayload,
    ContainerInfo,
    Envelope,
    IntegrationReport,
    MessageType,
    MetricsPayload,
    dataclass_wire_types,
    make_metrics_push,
)
from stormpulse.protocol.reasons import CancelReason

# -- the messages section (CORE-011 decision 3) ------------------------------


def test_messages_section_lists_the_envelope_and_every_catalogued_type() -> None:
    messages = wire_contract_messages()
    assert set(messages) == {ENVELOPE_ENTRY} | {t.value for t in MessageType}
    assert len(messages) == len(MESSAGES) + 1 == 21


def test_envelope_entry_is_walked_from_the_class_as_to_dict_writes_it() -> None:
    """Six fields, each a triple like any payload's, so a retype of ``v`` or an
    Optional ``agent_id`` lands here; ``ts`` and ``type`` are the strings
    ``Envelope.to_dict`` writes, ``type`` with its closed members declared."""
    envelope = wire_contract_messages()[ENVELOPE_ENTRY]
    assert envelope["root"] == "Envelope"
    assert "direction" not in envelope
    plain = {"nullable": False, "required": True}
    assert envelope["classes"] == {
        "Envelope": {
            "v": {"type": "integer", **plain},
            "type": {"type": "string", "enum": list(MessageType), **plain},
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
    report = IntegrationReport(
        status="live",
        disabled_reason=None,
        state={},
        command_specs_digest="",
        commands_fenced=None,
        generation=1,
        complete=True,
        last_full_read_at="2026-10-09T00:00:00Z",
        changed_ids=[],
    )
    sent = make_metrics_push(
        "a", replace(metrics, integrations={"garage": report}, jobs={"pending": 1})
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


# -- cancelled and steps (CORE-011 d9) ---------------------------------------


def test_command_cancelled_is_catalogued_outbound_with_its_three_fields() -> None:
    entry = wire_contract_messages()["command.cancelled"]
    plain = {"nullable": False, "required": True}
    assert entry["direction"] == "agent->dashboard"
    assert entry["root"] == "CommandCancelledPayload"
    assert entry["classes"] == {
        "CommandCancelledPayload": {
            "request_id": {"type": "string", **plain},
            "command": {"type": "string", **plain},
            "reason": {"type": "string", "enum": list(CancelReason), **plain},
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


# -- the event sources (CORE-011 decision 6) ---------------------------------


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
        for fields_ in kinds.values():  # skylos: ignore[SKY-P403] a walk over the declared catalogue, bounded by its size
            assert fields_ == sorted(fields_)
            assert not set(fields_) & ENVELOPE_FIELDS
    assert [name for name, entry in messages.items() if "sources" in entry] == [
        "events.batch"
    ]


def test_fitness_fails_on_a_declared_kind_missing_from_the_file(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """One kind dropped from a listed source, one whole source dropped: a line
    per missing kind, naming its source."""
    artifact = build_wire_contract()
    sources = artifact["messages"]["events.batch"]["sources"]
    del sources["events"]["events_nacked"]
    del sources["cert"]
    violations = check_with(artifact)
    at = "messages.events.batch.sources"
    for line in (
        f"{at}.events: kind 'events_nacked' is declared but not listed",
        f"{at}.cert: kind 'cert_renew_failed' is declared but not listed",
        f"{at}.cert: kind 'cert_renew_succeeded' is declared but not listed",
    ):
        assert any(line in v for v in violations), line
    assert not any("dropped_events" in v for v in violations)


def test_fitness_fails_on_a_listed_kind_nobody_declares_or_whose_fields_drift(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    sources = artifact["messages"]["events.batch"]["sources"]
    sources["ws"]["retired"] = ["reason"]
    sources["ws"]["reconnect"] = ["category", "reason"]
    violations = check_with(artifact)
    at = "messages.events.batch.sources.ws"
    assert any(
        f"{at}: listed kind 'retired' is no longer declared" in v for v in violations
    )
    assert any(
        f"{at}.reconnect: declared fields ['category', 'reason'] != emitted "
        "['category', 'error', 'reason']" in v
        for v in violations
    )


# -- Function 9 covers messages ----------------------------------------------


def test_fitness_names_the_class_when_a_message_field_is_renamed(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """The brief's own prediction: a rename without regenerating names
    ``messages.command.progress.CommandProgressPayload`` and the field."""
    artifact = build_wire_contract()
    progress = artifact["messages"]["command.progress"]["classes"]
    progress["CommandProgressPayload"]["freed_bytes"] = progress[
        "CommandProgressPayload"
    ].pop("bytes_freed")
    violations = check_with(artifact)
    at = "messages.command.progress.CommandProgressPayload"
    assert f"{at}: declared field 'freed_bytes' is no longer emitted" in "\n".join(
        violations
    )
    assert any(f"{at}: emits undeclared field 'bytes_freed'" in v for v in violations)


def test_fitness_names_the_attribute_when_a_message_field_retypes(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    freed = artifact["messages"]["command.progress"]["classes"][
        "CommandProgressPayload"
    ]["bytes_freed"]
    freed["type"] = "number"
    freed["nullable"] = False
    freed["required"] = True
    violations = check_with(artifact)
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
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    del artifact["messages"]["metrics.push"]["classes"]["ContainerInfo"]
    violations = check_with(artifact)
    assert any(
        "messages.metrics.push: class 'ContainerInfo' is emitted but not declared" in v
        for v in violations
    )


def test_fitness_names_a_declared_message_class_the_payload_no_longer_reaches(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """The other half of the undeclared-class test: a class that left the
    payload (a nested type removed) is named at its message, not left to rot."""
    artifact = build_wire_contract()
    artifact["messages"]["metrics.push"]["classes"]["LegacyInfo"] = {
        "name": {"type": "string", "nullable": False, "required": True}
    }
    violations = check_with(artifact)
    assert any(
        "messages.metrics.push: declared class 'LegacyInfo' no longer exists" in v
        for v in violations
    )


def test_fitness_fails_on_the_envelope_retyping(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    envelope = artifact["messages"]["envelope"]["classes"]["Envelope"]
    envelope["v"]["type"] = "string"
    del envelope["ts"]
    violations = check_with(artifact)
    assert any(
        "messages.envelope.Envelope.v: declared type 'string' != emitted 'integer'" in v
        for v in violations
    )
    assert any(
        "messages.envelope.Envelope: emits undeclared field 'ts'" in v
        for v in violations
    )


def test_fitness_fails_on_a_message_direction_or_root_drifting(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    artifact["messages"]["command.progress"]["direction"] = "dashboard->agent"
    artifact["messages"]["command.progress"]["root"] = "AckPayload"
    del artifact["messages"]["metrics.push"]["_note"]
    violations = check_with(artifact)
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
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    del artifact["messages"]["command.progress"]
    artifact["messages"]["command.retired"] = {
        "direction": "agent->dashboard",
        "root": "AckPayload",
        "classes": {},
    }
    violations = check_with(artifact)
    assert any(
        "messages: message 'command.progress' is catalogued but not declared" in v
        for v in violations
    )
    assert any(
        "messages: declared message 'command.retired' is no longer catalogued" in v
        for v in violations
    )


def test_fitness_names_every_message_when_the_file_predates_the_section(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """A schema-2 file has no ``messages`` or ``contract_digest``: one line per
    catalogued message plus the envelope, no crash on the missing keys."""
    artifact = build_wire_contract()
    del artifact["messages"]
    del artifact["contract_digest"]
    artifact["schema"] = 2
    violations = check_with(artifact)
    undeclared = [v for v in violations if "is catalogued but not declared" in v]
    assert len(undeclared) == len(wire_contract_messages()) == 21


def test_fitness_reports_a_hand_broken_messages_section_instead_of_crashing(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """A null entry, a field written as a bare string, a section that is a
    list: each is a line the author can act on, never a traceback from the
    harness, which would hide every other finding behind it."""
    artifact = build_wire_contract()
    artifact["messages"]["heartbeat"] = None
    artifact["messages"]["command.progress"]["classes"]["CommandProgressPayload"][
        "bytes_freed"
    ] = "integer"
    joined = "\n".join(check_with(artifact))
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
    violations = check_with(artifact)
    assert any(v.startswith("messages is not an object") for v in violations)
    assert not any("catalogued but not declared" in v for v in violations)
