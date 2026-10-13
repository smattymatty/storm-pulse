"""An integration's declared source, fed through its spool onto ``events.batch``
(CORE-011 d6): declared in the artifact outside the digest, refused whole on an
undeclared field, deleted only once every event it carried is settled."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from stormpulse import events
from stormpulse.agent import Agent, loops
from stormpulse.agent.acks import handle_events_batch_ack, handle_events_batch_nack
from stormpulse.agent.event_spool import (
    MAX_RECORD_BYTES,
    MAX_SEGMENT_BYTES,
    MAX_SEGMENT_EVENTS,
    SpoolFeeder,
    spool_feeders,
)
from stormpulse.agent.external_adapters import _event_source
from stormpulse.agent.wire_contract import build_wire_contract, contract_digest
from stormpulse.protocol import Envelope, MessageType
from stormpulse.sdk import SdkEventSource
from stormpulse.sdk.declaration import _EVENT_ENVELOPE
from tests.helpers import envelope

SOURCE = "probe_gate"


@pytest.fixture(autouse=True)
def _own_registry() -> Iterator[None]:
    registry, spools = dict(events._INTEGRATION_REGISTRY), dict(events._SPOOL_DIRS)
    yield
    events._INTEGRATION_REGISTRY.clear()
    events._INTEGRATION_REGISTRY.update(registry)
    events._SPOOL_DIRS.clear()
    events._SPOOL_DIRS.update(spools)


@pytest.fixture
def spool(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "spool"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    yield path
    if path.is_dir() and not path.is_symlink():
        path.chmod(0o700)  # a test may close it; let tmp cleanup in


def _declared(spool: Path, name: str = SOURCE) -> events.EventSource:
    declared = SdkEventSource(
        source=name,
        kinds={"request_seen": ("record_id", "status", "duration_ms")},
        spool_dir=str(spool),
    )
    source = _event_source(declared)
    assert source is not None
    return source


def _runtime(status: str, source: events.EventSource | None) -> Any:
    return SimpleNamespace(
        status=status, descriptor=SimpleNamespace(event_source=source)
    )


@pytest.fixture
def feeder(spool: Path) -> SpoolFeeder:
    return spool_feeders({"probe": _runtime("live", _declared(spool))})["probe"]


def _segment(spool: Path, name: str, *records: dict[str, Any], schema: int = 1) -> Path:
    lines = [json.dumps({"schema": schema}), *(json.dumps(r) for r in records)]
    tmp = spool / f".{name}.tmp"
    tmp.write_text("\n".join(lines) + "\n")
    path = spool / name
    os.rename(tmp, path)
    return path


def _seen(record_id: str, **extra: Any) -> dict[str, Any]:
    return {"kind": "request_seen", "record_id": record_id, "status": 200, **extra}


def _ack(batch_id: str) -> Envelope:
    return envelope(MessageType.EVENTS_BATCH_ACK, {"batch_id": batch_id})


def _nack(batch_id: str) -> Envelope:
    payload = {"batch_id": batch_id, "reason": "shape refused"}
    return envelope(MessageType.EVENTS_BATCH_NACK, payload)


# -- the artifact ------------------------------------------------------------


def test_the_artifact_gains_the_declared_source_outside_the_digest(
    feeder: SpoolFeeder,
) -> None:
    artifact = build_wire_contract()
    assert artifact["integration_events"]["sources"] == {
        SOURCE: {"request_seen": ["duration_ms", "record_id", "status"]}
    }
    assert SOURCE not in artifact["messages"]["events.batch"]["sources"]
    events._INTEGRATION_REGISTRY.clear()
    assert contract_digest() == artifact["contract_digest"]


def test_a_source_name_already_declared_is_refused(spool: Path) -> None:
    taken = _event_source(
        SdkEventSource(source="jobs", kinds={"x": ()}, spool_dir=str(spool))
    )
    assert spool_feeders({"taken": _runtime("live", taken)}) == {}
    assert "jobs" not in events.declared_integration_events()


def test_a_spool_already_fed_is_refused_for_the_second_source(
    feeder: SpoolFeeder, spool: Path
) -> None:
    alias = spool.parent / "alias"
    alias.symlink_to(spool.parent)
    other = _declared(alias / spool.name, name="other_gate")
    assert spool_feeders({"other": _runtime("live", other)}) == {}
    assert list(events.declared_integration_events()) == [SOURCE]


@pytest.mark.parametrize(
    ("arrange", "problem"),
    [
        (lambda s: s.chmod(0o755), "open to others"),
        (lambda s: s.chmod(0o500), "not writable"),
        (lambda s: s.rmdir(), "No such file"),
        (lambda s: (s.rmdir(), s.symlink_to(s.parent)), "symlink"),
    ],
    ids=["open", "read-only", "missing", "symlink"],
)
def test_a_spool_the_agent_should_not_feed_from_refuses_its_source(
    spool: Path, arrange: Any, problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    arrange(spool)
    source = _declared(spool)
    assert spool_feeders({"probe": _runtime("live", source)}) == {}
    assert problem in caplog.text
    assert SOURCE not in events.declared_integration_events()


@pytest.mark.parametrize(
    ("kinds", "spool", "problem"),
    [
        ({"seen": ("seq",)}, "/spool", "envelope fields"),
        ({"seen": ("ok",)}, "spool", "absolute"),
        ({"Seen": ("ok",)}, "/spool", "names must match"),
        ({}, "/spool", "no kinds"),
        ({"seen": ("ok",)}, "/a/../spool", "normalised"),
        ({"seen": ("ok",)}, "/spool/", "normalised"),
        ({"seen": ("ok",)}, "/", "root directory"),
    ],
)
def test_the_sdk_refuses_a_bad_declaration(
    kinds: dict[str, tuple[str, ...]], spool: str, problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        SdkEventSource(source="gate", kinds=kinds, spool_dir=spool)


def test_the_sdk_envelope_mirrors_the_agent_envelope() -> None:
    """The SDK imports nothing from the agent, so its copy is pinned here."""
    assert _EVENT_ENVELOPE == events.ENVELOPE_FIELDS


# -- feeding, shipping, settling ---------------------------------------------


@pytest.mark.asyncio
async def test_a_spooled_event_ships_on_events_batch_and_its_segment_goes_on_ack(
    feeder: SpoolFeeder, spool: Path, agent: Agent, shutdown: asyncio.Event
) -> None:
    seg = _segment(spool, "0001.jsonl", _seen("r1", ts="2026-10-13T00:00:00Z"))
    await asyncio.to_thread(feeder.tick)
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=sent.append)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.gather(loops.events_loop(agent, ws), stop_soon())
    (frame,) = (json.loads(m) for m in sent)
    assert frame["type"] == "events.batch"
    (event,) = frame["payload"]["events"]
    assert (event["source"], event["kind"], event["record_id"]) == (
        SOURCE,
        "request_seen",
        "r1",
    )
    assert event["ts"] == "2026-10-13T00:00:00Z" and event["seq"] >= 1
    feeder.tick()
    assert seg.exists(), "deleted before the ack"
    await handle_events_batch_ack(agent, ws, _ack(frame["payload"]["batch_id"]))
    feeder.tick()
    assert not seg.exists()


def test_an_undeclared_field_refuses_the_event_and_counts_it(
    feeder: SpoolFeeder, spool: Path
) -> None:
    _segment(
        spool,
        "0001.jsonl",
        _seen("r1"),
        _seen("r2", client_ip="10.0.0.1"),
        _seen("r3", source="jobs"),
        _seen("r4", status={"nested": 1}),
        {"kind": "unknown_kind", "record_id": "r5"},
        _seen("r6", ts="yesterday"),
        _seen("r7", duration_ms=float("nan")),
    )
    feeder.tick()
    synthesized, *shipped = events.buffer().drain("b")
    assert synthesized["kind"] == "dropped_events"
    assert synthesized["undeclared"] == 6
    assert [e["record_id"] for e in shipped] == ["r1"]
    assert "client_ip" not in json.dumps(shipped)


def test_an_unknown_schema_is_refused_whole_and_deleted(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = _segment(spool, "0001.jsonl", _seen("r1"), _seen("r2"), schema=2)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 2
    assert not seg.exists()


@pytest.mark.asyncio
async def test_a_nacked_segment_is_deleted_and_the_loss_counted(
    feeder: SpoolFeeder, spool: Path, agent: Agent
) -> None:
    seg = _segment(spool, "0001.jsonl", _seen("r1"), _seen("r2"))
    feeder.tick()
    events.buffer().drain("b1")
    await handle_events_batch_nack(agent, AsyncMock(), _nack("b1"))
    feeder.tick()
    assert not seg.exists()
    (nacked,) = events.buffer().drain("b2")
    assert (nacked["kind"], nacked["dropped"]) == ("events_nacked", 2)


def test_a_full_buffer_leaves_the_segment_unread(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    for _ in range(buf.room() - MAX_SEGMENT_EVENTS + 1):
        buf.append({"kind": "filler"})
    seg = _segment(spool, "0001.jsonl", _seen("r1"))
    feeder.tick()
    assert seg.exists()
    assert all(e["kind"] == "filler" for e in buf.drain("b", max_events=10_000))
    feeder.tick()
    assert [e["record_id"] for e in buf.drain("b2")] == ["r1"]


def test_a_reconnect_requeue_keeps_the_segment(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = _segment(spool, "0001.jsonl", _seen("r1"))
    feeder.tick()
    events.buffer().drain("b1")
    events.buffer().requeue_unacked()
    feeder.tick()
    assert seg.exists()
    assert [e["record_id"] for e in events.buffer().drain("b2")] == ["r1"]


def test_temp_files_and_symlinks_are_never_read(
    feeder: SpoolFeeder, spool: Path
) -> None:
    (spool / ".0001.jsonl.tmp").write_text('{"schema": 1}\n')
    outside = spool.parent / "outside.jsonl"
    outside.write_text(
        json.dumps({"schema": 1}) + "\n" + json.dumps(_seen("r1")) + "\n"
    )
    (spool / "0002.jsonl").symlink_to(outside)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 1
    assert (spool / ".0001.jsonl.tmp").exists()
    assert outside.exists()
    assert not (spool / "0002.jsonl").exists()


# -- wiring -------------------------------------------------------------------


def test_only_a_live_integration_with_a_source_gets_a_feeder_and_a_declaration(
    tmp_path: Path,
) -> None:
    spools = [tmp_path / n for n in ("fed", "off")]
    for path in spools:
        path.mkdir(mode=0o700)
    fed, off = (_declared(p, name=f"{p.name}_gate") for p in spools)
    feeders = spool_feeders(
        {
            "fed": _runtime("live", fed),
            "off": _runtime("disabled", off),
            "mute": _runtime("live", None),
        }
    )
    assert list(feeders) == ["fed"]
    assert feeders["fed"].source.name == "fed_gate"
    assert list(events.declared_integration_events()) == ["fed_gate"]


@pytest.mark.asyncio
async def test_the_spool_loop_ticks_until_shutdown(
    feeder: SpoolFeeder, spool: Path, agent: Agent, shutdown: asyncio.Event
) -> None:
    _segment(spool, "0001.jsonl", _seen("r1"))

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.gather(loops.event_spool_loop(agent, feeder), stop_soon())
    assert [e["record_id"] for e in events.buffer().drain("b")] == ["r1"]


def test_an_oversize_segment_is_refused_whole_and_counted_once(
    feeder: SpoolFeeder, spool: Path
) -> None:
    big = [_seen(f"{n:04}" + "r" * 3000) for n in range(400)]
    assert 400 * 3000 > MAX_SEGMENT_BYTES
    seg = _segment(spool, "0001.jsonl", *big)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")  # never a truncated prefix
    assert synthesized["undeclared"] == 1
    assert not seg.exists()


def test_a_record_over_the_byte_cap_is_refused(
    feeder: SpoolFeeder, spool: Path
) -> None:
    _segment(spool, "0001.jsonl", _seen("r1"), _seen("x" * MAX_RECORD_BYTES))
    feeder.tick()
    synthesized, shipped = events.buffer().drain("b")
    assert (synthesized["undeclared"], shipped["record_id"]) == (1, "r1")


def test_a_segment_nobody_the_spool_trusts_owns_is_refused(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stranger = SimpleNamespace(st_uid=4242, st_gid=4242)
    feeder = SpoolFeeder(feeder.source, stranger)  # type: ignore[arg-type]
    monkeypatch.setattr(os, "geteuid", lambda: 4243)
    seg = _segment(spool, "0001.jsonl", _seen("r1"))
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 1
    assert not seg.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes from a 0500 directory")
def test_a_segment_that_cannot_be_deleted_ships_once(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """A spool the agent cannot write to after boot parks the settled segment
    instead of re-reading it every tick."""
    _segment(spool, "0001.jsonl", _seen("r1"))
    spool.chmod(0o500)
    buf = events.buffer()
    shipped = []
    for n in range(3):
        feeder.tick()
        batch = [e for e in buf.drain(f"b{n}") if e["kind"] == "request_seen"]
        shipped += batch
        buf.ack(f"b{n}")
    assert [e["record_id"] for e in shipped] == ["r1"]


def test_a_directory_named_like_a_segment_is_counted_once(
    feeder: SpoolFeeder, spool: Path
) -> None:
    (spool / "0001.jsonl").mkdir()
    (spool / "0001.jsonl" / "keep").write_text("")
    for _ in range(3):
        feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 1


# -- hunted: the declaration's path, the refusals, settling ----------------------


def test_the_translator_carries_the_declared_source_onto_the_integration(
    spool: Path,
) -> None:
    """Without this hop no adapter's source reaches ``spool_feeders``."""
    from stormpulse.agent.external_adapters import _translate
    from stormpulse.integrations.external.model import SealedGrantV1
    from stormpulse.sdk import SdkIntegration

    sdk = SdkIntegration(
        id="probe",
        parse_config=lambda c: c,
        enabled=lambda c: True,
        event_source=SdkEventSource(
            source=SOURCE, kinds={"seen": ("record_id",)}, spool_dir=str(spool)
        ),
    )
    grant = SealedGrantV1(
        agent_id="a",
        integration_id=sdk.id,
        publisher_fingerprint="sha256:" + "ab" * 32,
        package_digest="sha256:" + "cd" * 32,
        manifest_digest="sha256:" + "ef" * 32,
        granted_capabilities=(),
        sealed_at="2026-10-13T00:00:00Z",
    )
    assert _translate(sdk, grant).event_source == events.EventSource(
        name=SOURCE, kinds={"seen": frozenset({"record_id"})}, spool_dir=spool
    )


def test_a_refused_source_costs_only_itself(tmp_path: Path) -> None:
    bad, good = tmp_path / "bad", tmp_path / "good"
    bad.mkdir(mode=0o755)
    bad.chmod(0o755)
    good.mkdir(mode=0o700)
    feeders = spool_feeders(
        {
            "bad": _runtime("live", _declared(bad, name="bad_gate")),
            "good": _runtime("live", _declared(good, name="good_gate")),
        }
    )
    assert list(feeders) == ["good"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root owns nothing foreign here")
def test_a_spool_owned_by_another_user_refuses_its_source(
    spool: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert spool_feeders({"probe": _runtime("live", _declared(spool))}) == {}
    assert "owned by uid" in caplog.text


def test_a_spool_that_is_a_file_refuses_its_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "spool"
    path.write_text("")
    path.chmod(0o700)  # passes every other check, so only the type refuses it
    assert spool_feeders({"probe": _runtime("live", _declared(path))}) == {}
    assert "not a directory" in caplog.text


def test_a_segment_carrying_the_spool_group_is_read(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setgid spool lets an emitter running as another user hand in work."""
    seg = _segment(spool, "0001.jsonl", _seen("r1"))
    dir_stat = SimpleNamespace(st_uid=4242, st_gid=seg.stat().st_gid)
    feeder = SpoolFeeder(feeder.source, dir_stat)  # type: ignore[arg-type]
    monkeypatch.setattr(os, "geteuid", lambda: 4243)
    feeder.tick()
    assert [e["record_id"] for e in events.buffer().drain("b")] == ["r1"]


def test_a_segment_over_the_event_cap_is_refused_whole(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = _segment(
        spool, "0001.jsonl", *(_seen(str(n)) for n in range(MAX_SEGMENT_EVENTS + 1))
    )
    feeder.tick()
    (synthesized,) = events.buffer().drain("b", max_events=10_000)
    assert synthesized["undeclared"] == MAX_SEGMENT_EVENTS + 1
    assert not seg.exists()


def test_lines_that_are_not_json_objects_are_refused_one_by_one(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = spool / "0001.jsonl"
    lines = ['{"schema": 1}', "not json", "[1, 2]", json.dumps(_seen("r1"))]
    seg.write_text("\n".join(lines) + "\n")
    feeder.tick()
    synthesized, shipped = events.buffer().drain("b")
    assert (synthesized["undeclared"], shipped["record_id"]) == (2, "r1")


@pytest.mark.parametrize("header", ["[1]", "not json", "{}"])
def test_a_header_that_is_not_a_schema_object_refuses_the_segment(
    feeder: SpoolFeeder, spool: Path, header: str
) -> None:
    seg = spool / "0001.jsonl"
    seg.write_text(header + "\n" + json.dumps(_seen("r1")) + "\n")
    feeder.tick()
    drained = events.buffer().drain("b")
    assert [e["kind"] for e in drained] == ["dropped_events"]
    assert not seg.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_segment_waits_for_the_next_tick(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """Unreadable now is not refused: no count, no delete, read once it opens."""
    seg = _segment(spool, "0001.jsonl", _seen("r1"))
    seg.chmod(0o000)
    feeder.tick()
    assert seg.exists()
    assert events.buffer().drain("b1") == []
    seg.chmod(0o600)
    feeder.tick()
    assert [e["record_id"] for e in events.buffer().drain("b2")] == ["r1"]


def test_a_segment_split_across_batches_goes_only_when_both_are_acked(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    buf.append({"kind": "filler"})
    seg = _segment(
        spool, "0001.jsonl", *(_seen(str(n)) for n in range(MAX_SEGMENT_EVENTS))
    )
    feeder.tick()
    first, second = buf.drain("b1"), buf.drain("b2")
    assert (len(first), len(second)) == (MAX_SEGMENT_EVENTS, 1)
    buf.ack("b1")
    feeder.tick()
    assert seg.exists(), "deleted with its last event still in flight"
    buf.ack("b2")
    feeder.tick()
    assert not seg.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes from a 0500 directory")
def test_a_parked_segment_rewritten_by_the_emitter_is_read_afresh(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    _segment(spool, "0001.jsonl", _seen("r1"))
    feeder.tick()
    buf.drain("b1")
    buf.ack("b1")
    spool.chmod(0o500)
    feeder.tick()  # settled, cannot delete: parked
    spool.chmod(0o700)
    _segment(spool, "0001.jsonl", _seen("r2"))  # a new inode under the name
    feeder.tick()
    assert [e["record_id"] for e in buf.drain("b2")] == ["r2"]
