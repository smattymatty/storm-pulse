"""The typed walker and the unit check: JSON types, nullable against required,
closed enums, and the rule that a numeric field names its unit (CORE-011 d3, d5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

import pytest

import fitness.wire_contract as fitness_module
from fitness.__main__ import load_baseline
from fitness.wire_contract import check_wire_contract
from stormpulse.agent.wire_contract import (
    ENVELOPE_ENTRY,
    build_wire_contract,
    wire_contract_messages,
)
from stormpulse.protocol import (
    MESSAGES,
    Direction,
    LogBatchPayload,
    MessageType,
    MetricsPayload,
    RegisterPayload,
    dataclass_wire_types,
)
from stormpulse.protocol.reasons import CancelReason, FailureReason

# -- the typed walker: what the shape walker deliberately does not see -------


def test_types_report_json_types_and_walk_nested_classes() -> None:
    classes = dataclass_wire_types(MetricsPayload)
    assert set(classes) == {
        "MetricsPayload",
        "ContainerInfo",
        "IntegrationReport",
        "DiskInfo",
    }
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
    # Every real mount, optional so an older agent's payload still decodes.
    assert metrics["disks"] == {"type": "array", "nullable": True, "required": False}
    assert classes["DiskInfo"]["total_bytes"]["type"] == "integer"
    assert classes["DiskInfo"]["device"]["required"] is True


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


def test_register_declares_fifteen_required_fields() -> None:
    """CORE-011 d7: the node's whole self-declaration, every field required,
    ``unsealed_since`` the only nullable one, ``components`` an array."""
    declared = wire_contract_messages()["register"]["classes"]["RegisterPayload"]
    assert len(declared) == 15
    assert [name for name, f in declared.items() if not f["required"]] == []
    assert [name for name, f in declared.items() if f["nullable"]] == ["unsealed_since"]
    assert declared["wire_contract"]["type"] == "string"
    assert declared["components"] == {
        "type": "array",
        "nullable": False,
        "required": True,
    }


def test_register_declares_its_two_nested_classes() -> None:
    """CORE-011 d7: ``components`` typed by ``Component`` and each ``integrations``
    value by ``IntegrationReport`` (d3: no key the control plane reads by name is
    left undeclared); the report is one declaration under two carriers."""
    entry = wire_contract_messages()["register"]
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
    assert fields_["kind"] == {
        "type": "string",
        "nullable": False,
        "required": True,
        "enum": list(MessageType),
    }
    assert fields_["maybe"] == {
        "type": "string",
        "nullable": True,
        "required": False,
        "enum": list(Direction),
    }


def test_closed_reason_enums_reach_the_artifact_in_declaration_order() -> None:
    """CORE-011 d3: an enum is closed and declared, so a consumer reads the
    members from the file, never from a stderr substring. The sibling ``enum``
    key leaves ``type`` a plain string for every reader that predates it."""
    messages = build_wire_contract()["messages"]
    result = messages["command.result"]["classes"]["CommandResultPayload"]
    cancelled = messages["command.cancelled"]["classes"]["CommandCancelledPayload"]
    failure, cancel = result["failure_reason"], cancelled["reason"]
    assert failure["type"] == "string" and failure["enum"] == list(FailureReason)
    assert cancel["type"] == "string" and cancel["enum"] == list(CancelReason)
    assert "unknown_command" in failure["enum"]
    # A plain string field declares no vocabulary at all.
    assert "enum" not in result["request_id"]


def test_a_formatted_field_declares_no_enum_even_when_the_class_holds_one() -> None:
    """``formatted`` says a serializer rewrites the field (``Envelope.ts``), so the
    class-level enum is not what reaches the wire and must not be declared."""

    @dataclass(frozen=True)
    class Stamped:
        kind: MessageType
        other: MessageType

    fields_ = dataclass_wire_types(Stamped, formatted={"kind": "string"})["Stamped"]
    assert fields_["kind"] == {"type": "string", "nullable": False, "required": True}
    assert fields_["other"]["enum"] == list(MessageType)


def test_types_cover_every_catalogued_message_payload() -> None:
    for spec in MESSAGES.values():
        assert spec.payload.__name__ in dataclass_wire_types(spec.payload)


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


# -- the unit check (CORE-011 d3, d5) ----------------------------------------


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
