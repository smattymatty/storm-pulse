"""Tests for ``stormpulse.protocol.codec``: the one ``from_dict`` reflection,
its refusals, and the nesting it derives from the annotations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import asdict, fields
from typing import Any

import pytest

from stormpulse.protocol import (
    MESSAGES,
    CommandProgressPayload,
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    Component,
    ContainerInfo,
    LogBatchPayload,
    MetricsPayload,
    ProtocolError,
    RegisterPayload,
    Step,
    WirePayload,
)
from tests.helpers import (
    FAKE_METRICS,
    register_payload,
)

# -- missing, mistyped and unknown keys --------------------------------------


def test_metrics_missing_field_raises() -> None:
    data: dict[str, Any] = {"cpu_percent": 1.0}
    with pytest.raises(ProtocolError, match="missing fields"):
        MetricsPayload.from_dict(data)


def test_metrics_containers_not_list_raises() -> None:
    data: dict[str, Any] = {
        "cpu_percent": 1.0,
        "memory_percent": 2.0,
        "memory_used_mb": 3.0,
        "memory_total_mb": 4.0,
        "disk_percent": 5.0,
        "disk_used_gb": 6.0,
        "disk_total_gb": 7.0,
        "load_avg_1m": 0.1,
        "load_avg_5m": 0.2,
        "uptime_seconds": 8.0,
        "containers": "not a list",
    }
    with pytest.raises(ProtocolError, match="expected list"):
        MetricsPayload.from_dict(data)


def test_container_info_not_dict_raises() -> None:
    with pytest.raises(ProtocolError, match="expected dict"):
        ContainerInfo.from_dict("not a dict")


def test_command_request_missing_command_raises() -> None:
    with pytest.raises(ProtocolError, match="missing fields"):
        CommandRequestPayload.from_dict({"params": {}, "hmac": "x", "nonce": "y"})


def test_register_names_every_missing_field() -> None:
    """CORE-011 d7: no register field has a default, so an empty dict is refused
    naming all fifteen, ``unsealed_since`` among them (nullable, never absent)."""
    with pytest.raises(ProtocolError, match="RegisterPayload: missing fields") as info:
        RegisterPayload.from_dict({})
    names = {f.name for f in fields(RegisterPayload)}
    assert len(names) == 15
    assert all(f"'{name}'" in str(info.value) for name in names)
    with pytest.raises(ProtocolError, match=r"missing fields: \{'unsealed_since'\}"):
        RegisterPayload.from_dict(
            {k: v for k, v in register_payload().items() if k != "unsealed_since"}
        )


def test_command_progress_payload_missing_required_raises() -> None:
    with pytest.raises(ProtocolError):
        CommandProgressPayload.from_dict(
            {
                "request_id": "req-9",
                "command": "x",
                "group": "g",
                "stage": "starting",
            }
        )


def test_command_progress_payload_ignores_unknown_keys() -> None:
    """Forward compatibility in the other direction: a newer peer may send
    fields this build has never heard of, and they are dropped, not fatal."""
    payload = CommandProgressPayload.from_dict(
        {
            "request_id": "req-9",
            "command": "rclone_migrate",
            "group": "buckets",
            "stage": "running",
            "current": 1,
            "a_field_from_the_future": 42,
        }
    )
    assert payload.current == 1


def test_extra_payload_fields_ignored() -> None:
    payload = RegisterPayload.from_dict(register_payload(extra="ignored"))
    assert payload.version == "0.1.0"
    assert not hasattr(payload, "extra")


def test_command_progress_payload_from_an_older_agents_dict() -> None:
    """A payload dict predating the transfer fields still deserializes. This
    is why adding them needed no protocol version bump."""
    payload = CommandProgressPayload.from_dict(
        {
            "request_id": "req-9",
            "command": "rclone_migrate",
            "group": "buckets",
            "stage": "running",
            "current": 1000,
            "total": 5000,
            "message": "x",
        }
    )
    assert payload.rate_bytes_per_sec is None
    assert payload.eta_seconds is None


def test_scalars_are_checked_as_declared_json_types() -> None:
    """A bool is not an int and a null is no member of ``int | str``: the scalar
    check reads the annotation and never coerces, so a union's last arm refuses."""
    good = asdict(
        LogBatchPayload(
            group="g",
            parser="p",
            batch_id="b",
            lines=[],
            dropped=0,
            from_position=1,
            to_position="2",
        )
    )
    assert LogBatchPayload.from_dict(good).to_position == "2"
    with pytest.raises(
        ProtocolError, match="LogBatchPayload.dropped: invalid field type bool"
    ):
        LogBatchPayload.from_dict({**good, "dropped": True})
    with pytest.raises(
        ProtocolError, match="from_position: invalid field type NoneType"
    ):
        LogBatchPayload.from_dict({**good, "from_position": None})


# -- nesting derived from the annotations ------------------------------------


def test_register_payload_from_dict_builds_components() -> None:
    """``components`` is the one nested class under register; each item is typed."""
    raw = [{"name": "stormpulse", "version": "0.7.0", "digest": ""}]
    payload = RegisterPayload.from_dict(register_payload(components=raw))
    assert payload.components == [Component("stormpulse", "0.7.0", "")]
    with pytest.raises(ProtocolError, match="Component: missing fields"):
        RegisterPayload.from_dict(register_payload(components=[{"name": "x"}]))
    with pytest.raises(ProtocolError, match="components: expected list"):
        RegisterPayload.from_dict(register_payload(components={}))


def test_command_sequence_steps_are_typed_and_the_wrong_shape_is_refused() -> None:
    """A step is ``Step(command, params)`` built from its dict; the names-only
    list the sequence used to carry, a step with no params and a step object
    where the list should be are each refused at the seam."""
    base = {"sequence_id": "s", "stop_on_failure": True, "hmac": "h", "nonce": "n"}
    payload = CommandSequencePayload.from_dict(
        {**base, "steps": [{"command": "git_pull", "params": {"depth": "1"}}]}
    )
    assert payload.steps == [Step(command="git_pull", params={"depth": "1"})]
    with pytest.raises(ProtocolError, match="Step: expected dict, got str"):
        CommandSequencePayload.from_dict({**base, "steps": ["git_pull"]})
    with pytest.raises(ProtocolError, match="Step: missing fields"):
        CommandSequencePayload.from_dict({**base, "steps": [{"command": "git_pull"}]})
    with pytest.raises(ProtocolError, match="steps: expected list, got dict"):
        CommandSequencePayload.from_dict({**base, "steps": {"command": "git_pull"}})


def test_from_dict_rebuilds_integration_reports_from_a_mapping() -> None:
    """``integrations`` nests ``IntegrationReport`` under a dict, not a list: the
    parser follows the annotation's container, so a register or a metrics push
    reads back as the class the artifact declares, and a null stays null."""
    from stormpulse.protocol import IntegrationReport

    report = {
        "status": "live",
        "disabled_reason": None,
        "state": {"node_id": "n"},
        "command_specs_digest": "sha256:" + "ab" * 32,
        "commands_fenced": None,
        "generation": 3,
        "complete": False,
        "last_full_read_at": "2026-10-09T00:00:00Z",
        "changed_ids": ["b1"],
    }
    register = RegisterPayload.from_dict(
        register_payload(integrations={"garage": report})
    )
    assert register.integrations == {"garage": IntegrationReport(**report)}  # type: ignore[arg-type]
    assert asdict(register)["integrations"] == {"garage": report}
    metrics = MetricsPayload.from_dict({**asdict(FAKE_METRICS), "integrations": None})
    assert metrics.integrations is None
    with pytest.raises(ProtocolError, match="integrations: expected dict"):
        RegisterPayload.from_dict(register_payload(integrations=[report]))


# -- asdict is the inverse of from_dict --------------------------------------


def test_payload_asdict_roundtrip() -> None:
    original = CommandResultPayload(
        request_id="r1",
        command="git_pull",
        group="deploy",
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=10,
    )
    d = asdict(original)
    rebuilt = CommandResultPayload.from_dict(d)
    assert rebuilt == original


def test_metrics_payload_asdict_roundtrip() -> None:
    original = MetricsPayload(
        cpu_percent=1.0,
        memory_percent=2.0,
        memory_used_mb=3.0,
        memory_total_mb=4.0,
        disk_percent=5.0,
        disk_used_gb=6.0,
        disk_total_gb=7.0,
        load_avg_1m=0.1,
        load_avg_5m=0.2,
        uptime_seconds=8.0,
        containers=[ContainerInfo(name="x", status="y", image="z")],
    )
    d = asdict(original)
    rebuilt = MetricsPayload.from_dict(d)
    assert rebuilt == original


def test_log_batch_payload_roundtrip() -> None:
    original = LogBatchPayload(
        group="storage",
        parser="garage_s3",
        batch_id="abc-123",
        lines=[{"ts": "2026-04-10T13:00:00Z", "bucket": "b1"}],
        dropped=2,
        from_position=100,
        to_position=500,
    )
    restored = LogBatchPayload.from_dict(asdict(original))
    assert restored == original


def test_log_batch_payload_missing_field_raises() -> None:
    with pytest.raises(ProtocolError):
        LogBatchPayload.from_dict({"group": "storage"})


def _leaves(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


_CATALOGUED_PAYLOADS: list[type[WirePayload]] = sorted(
    {spec.payload for spec in MESSAGES.values()}, key=lambda c: c.__name__
)


@pytest.mark.parametrize("cls", _CATALOGUED_PAYLOADS, ids=lambda c: c.__name__)
def test_every_catalogued_payload_round_trips_fully_populated(
    cls: type[WirePayload], populated: Callable[[Any], Any]
) -> None:
    """``from_dict(asdict(x)) == x`` for every payload class with every optional
    present: the nesting ``from_dict`` rebuilds comes from the annotations, so a
    new nested field needs the dataclass alone, no map and no override."""
    x = populated(cls)
    as_dict = asdict(x)
    assert None not in list(_leaves(as_dict))
    assert cls.from_dict(as_dict) == x


def test_a_float_field_takes_an_int_but_never_a_bool() -> None:
    """JSON has one number type: ``0`` where a float is declared decodes, so a
    producer writing a whole load average is read; ``true`` does not, a bool
    being an int to Python and never to the wire."""
    good = {**asdict(FAKE_METRICS), "load_avg_1m": 0, "uptime_seconds": 864000}
    payload = MetricsPayload.from_dict(good)
    assert (payload.load_avg_1m, payload.uptime_seconds) == (0, 864000)
    with pytest.raises(
        ProtocolError, match="MetricsPayload.load_avg_1m: invalid field type bool"
    ):
        MetricsPayload.from_dict({**good, "load_avg_1m": True})
