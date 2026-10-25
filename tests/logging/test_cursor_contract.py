"""The cursor contract every tailer owes the log loop (STAB-01, STAB-02).

One suite over the four tailers: real files, real SQLite, and real stand-in
``docker`` and ``journalctl`` executables, never a faked host. Each case states
an invariant on what a restarted tailer delivers next; a kind that cannot honour
one is a strict expected failure with its reason.
"""

from __future__ import annotations

import itertools
import json
import os
import stat
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stormpulse.agent.log_batches import PendingBatches
from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.shipper import Batch, LogShipper
from stormpulse.logging.tailer import (
    DockerTailer,
    JournaldTailer,
    LogTailer,
    StreamingDockerTailer,
)

# Every class defining confirm_shipped belongs here.
TAILERS = (LogTailer, DockerTailer, StreamingDockerTailer, JournaldTailer)

_DOCKER_SEED = "2026-01-01T00:00:00.000000Z"

# Prints the container's records at or after --since; --follow prints later ones.
_FAKE_DOCKER = """\
import sys, time
from pathlib import Path
args = sys.argv[1:]
since = args[args.index("--since") + 1]
path, seen = Path({root!r}) / (args[-1] + ".log"), 0
for _ in range(3000 if "--follow" in args else 1):
    lines = path.read_text().split(chr(10))[:-1]  # complete lines only
    for line in lines[seen:]:
        if line[:27] >= since:
            print(line, flush=True)
    seen = len(lines)
    time.sleep(0.02)
"""

# Honours --lines 1 and --after-cursor by position in the unit's record file.
_FAKE_JOURNALCTL = """\
import json, sys
from pathlib import Path
args = sys.argv[1:]
unit = args[args.index("--unit") + 1]
path = Path({root!r}) / (unit + ".jsonl")
records = path.read_text().splitlines() if path.exists() else []
if "--lines" in args:
    records = records[-1:]
else:
    cursor = args[args.index("--after-cursor") + 1]
    cursors = [json.loads(r)["__CURSOR"] for r in records]
    if cursor not in cursors:
        sys.exit("Failed to seek to cursor")
    records = records[cursors.index(cursor) + 1:]
for record in records:
    print(record)
"""


@dataclass
class World:
    """One host: a position store, stand-in binaries, and every tailer opened."""

    root: Path
    store: LogPositionStore
    clock: Iterator[int] = field(default_factory=itertools.count)
    opened: list[Any] = field(default_factory=list)
    pending: PendingBatches = field(default_factory=PendingBatches)


@dataclass
class Loop:
    """A shipper as ``log_loop`` drives it: each batch stays pending until its ACK."""

    shipper: LogShipper
    pending: PendingBatches
    issued: list[tuple[Batch, str]] = field(default_factory=list)

    def collect_batch(self) -> Batch | None:
        batch = self.shipper.collect_batch(pending=self.pending.receipts())
        if batch is not None:
            batch_id = str(uuid.uuid4())
            self.issued.append((batch, batch_id))
            self.pending.add(batch_id, batch.receipt)
        return batch

    def ack(self, batch: Batch) -> None:
        """Resolve the batch, then confirm even a duplicate: the tailer must refuse it."""
        for issued, batch_id in self.issued:
            if issued is batch:
                self.pending.pop(batch_id)
        self.shipper.confirm(batch.receipt)


@dataclass
class Source:
    """One log group: append records, rotate its source, (re)start its shipper."""

    world: World
    group: LogGroupConfig
    tailer_cls: type[Any]
    write: Callable[[list[str]], None]
    rotate: Callable[[str], None]

    def shipper(self) -> Loop:
        """A fresh tailer over the same store: what an agent restart builds."""
        tailer = self.tailer_cls(self.group, self.world.store)
        self.world.opened.append(tailer)
        return Loop(LogShipper(self.group, tailer), self.world.pending)

    def restart(self) -> Loop:
        """An agent restart: tailers closed, nothing left pending."""
        for tailer in self.world.opened:
            if isinstance(tailer, StreamingDockerTailer):
                tailer.close()
        self.world.pending = PendingBatches()
        return self.shipper()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("journalctl", _FAKE_JOURNALCTL)):
        exe = bin_dir / name
        exe.write_text(f"#!{sys.executable}\n" + body.format(root=str(tmp_path)))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(StreamingDockerTailer, "_RESPAWN_DELAY_SECONDS", 0.0)
    w = World(root=tmp_path, store=LogPositionStore(tmp_path / "pos.db"))
    yield w
    for tailer in w.opened:
        if isinstance(tailer, StreamingDockerTailer):
            tailer.close()
    w.store.close()


def _group(
    tailer_cls: type[Any], name: str, root: Path, filt: str, limit: int
) -> LogGroupConfig:
    kind = {
        LogTailer: ("file", "stormpulse"),
        DockerTailer: ("docker", "docker_raw"),
        StreamingDockerTailer: ("docker_stream", "docker_raw"),
        JournaldTailer: ("journald", "journald"),
    }[tailer_cls]
    return LogGroupConfig(
        name=name,
        enabled=True,
        source_type=kind[0],
        source_path=root / f"{name}.log" if tailer_cls is LogTailer else Path(""),
        filter_contains=filt,
        parser=kind[1],
        ship_interval_seconds=0.1,
        max_lines_per_batch=limit,
        container_name=name,
        docker_binary=str(root / "bin" / "docker"),
        unit=f"{name}.service",
    )


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.writelines(line + "\n" for line in lines)


def _no_generation(tailer_cls: type[Any]) -> Callable[[str], None]:
    def rotate(_how: str) -> None:
        raise NotImplementedError(f"{tailer_cls.__name__} sees no source generation")

    return rotate


def _file_source(world: World, group: LogGroupConfig) -> Source:
    path = group.source_path
    path.touch()

    def write(messages: list[str]) -> None:
        _append(
            path,
            [
                json.dumps(
                    {
                        "ts": "2026-10-24T10:00:00Z",
                        "level": "INFO",
                        "message": m,
                        "event_type": "connection",
                    }
                )
                for m in messages
            ],
        )

    def rotate(how: str) -> None:
        if how == "rename":
            path.rename(path.with_name(path.name + ".1"))
            path.touch()
        else:
            path.write_text("")  # truncation in place keeps the inode

    return Source(world, group, LogTailer, write, rotate)


def _journal_record(cursor: str, timestamp: str, message: str) -> str:
    return json.dumps(
        {"__CURSOR": cursor, "__REALTIME_TIMESTAMP": timestamp, "MESSAGE": message}
    )


def _journald_source(world: World, group: LogGroupConfig) -> Source:
    records = world.root / f"{group.unit}.jsonl"
    seed = f"s={uuid.uuid4().hex}"
    _append(records, [_journal_record(seed, "1", "seed")])
    world.store.set_cursor(group.name, group.unit, seed)

    def write(messages: list[str]) -> None:
        base = 1_760_000_000_000_000
        _append(
            records,
            [
                _journal_record(
                    f"s={uuid.uuid4().hex}", str(base + next(world.clock)), m
                )
                for m in messages
            ],
        )

    return Source(world, group, JournaldTailer, write, _no_generation(JournaldTailer))


def _docker_source(
    world: World, group: LogGroupConfig, tailer_cls: type[Any]
) -> Source:
    records = world.root / f"{group.name}.log"
    records.touch()
    world.store.set_docker_ts(group.name, group.container_name, _DOCKER_SEED)

    def write(messages: list[str]) -> None:
        start = datetime(2026, 10, 24, tzinfo=UTC)
        _append(
            records,
            [
                f"{start + timedelta(seconds=next(world.clock)):%Y-%m-%dT%H:%M:%S.%fZ} {m}"
                for m in messages
            ],
        )

    return Source(world, group, tailer_cls, write, _no_generation(tailer_cls))


def make_source(
    world: World, tailer_cls: type[Any], name: str = "g", filt: str = "", limit: int = 2
) -> Source:
    """Seed the group's checkpoint and wire its record writer and rotation."""
    group = _group(tailer_cls, name, world.root, filt, limit)
    if tailer_cls is LogTailer:
        return _file_source(world, group)
    if tailer_cls is JournaldTailer:
        return _journald_source(world, group)
    return _docker_source(world, group, tailer_cls)


def _messages(batch: Batch | None) -> list[str]:
    return [] if batch is None else [entry["message"] for entry in batch.lines]


def _ack(loop: Loop, batch: Batch) -> None:
    """The ACK path as the agent runs it: the shipper pairs the receipt with its tailer."""
    loop.ack(batch)


def _first_unacked(written: list[str], acked: set[str]) -> str:
    return next(m for m in written if m not in acked)


def _cases(**xfail: str) -> list[Any]:
    """TAILERS as params; a named kind is a strict expected failure with its reason."""
    return [
        pytest.param(
            cls,
            id=cls.__name__,
            marks=pytest.mark.xfail(strict=True, reason=xfail[cls.__name__]),
        )
        if cls.__name__ in xfail
        else pytest.param(cls, id=cls.__name__)
        for cls in TAILERS
    ]


# STAB-01: intentional exclusions consume their range.


@pytest.mark.parametrize("tailer_cls", _cases())
def test_a_wholly_filtered_read_still_makes_progress(
    world: World, tailer_cls: type[Any]
) -> None:
    src = make_source(world, tailer_cls, filt="needle", limit=2)
    src.write(["skip1", "skip2", "needle"])
    shipper = src.shipper()
    shipped: list[str] = []
    for _ in range(4):
        batch = shipper.collect_batch()
        if batch is not None:
            shipped += _messages(batch)
            _ack(shipper, batch)
    assert "needle" in shipped


@pytest.mark.parametrize("tailer_cls", _cases())
def test_filtered_progress_survives_a_restart_every_read(
    world: World, tailer_cls: type[Any]
) -> None:
    src = make_source(world, tailer_cls, filt="needle", limit=2)
    src.write(["skip1", "skip2", "needle"])
    shipped: list[str] = []
    for _ in range(4):
        shipper = src.restart()
        batch = shipper.collect_batch()
        if batch is not None:
            shipped += _messages(batch)
            _ack(shipper, batch)
    assert "needle" in shipped


@pytest.mark.parametrize("tailer_cls", _cases())
def test_an_unacked_match_is_never_checkpointed_past(
    world: World, tailer_cls: type[Any]
) -> None:
    src = make_source(world, tailer_cls, filt="needle", limit=3)
    src.write(["skip1", "needle", "skip2"])
    shipper = src.shipper()
    assert "needle" in _messages(shipper.collect_batch())
    src.write(["skip3", "skip4", "skip5"])
    shipper.collect_batch()  # a later, wholly filtered read: no ACK covers needle
    restarted = src.restart()
    assert "needle" in _messages(restarted.collect_batch())


# STAB-02: a confirmation applies only to the generation and range it covers.


@pytest.mark.parametrize(
    ("tailer_cls", "how"),
    [
        pytest.param(LogTailer, "rename", id="LogTailer-rename"),
        pytest.param(LogTailer, "truncate", id="LogTailer-truncate"),
    ],
)
def test_a_late_ack_from_an_earlier_generation_moves_nothing(
    world: World, tailer_cls: type[Any], how: str
) -> None:
    src = make_source(world, tailer_cls, limit=2)
    src.write(["a1", "a2"])
    shipper = src.shipper()
    old = shipper.collect_batch()
    assert _messages(old) == ["a1", "a2"]
    src.rotate(how)
    src.write(["b1", "b2", "b3"])
    assert _messages(shipper.collect_batch())[:1] == ["b1"]
    src.write(["b4"])
    assert old is not None
    _ack(shipper, old)
    assert _messages(src.restart().collect_batch())[:1] == ["b1"]


_COPYTRUNCATE_GAP = (
    "the store keeps (offset, inode) only, so a copytruncate that regrows past the "
    "ACKed offset is invisible on read; open STAB-02 gap, 0.8.2"
)


@pytest.mark.xfail(strict=True, reason=_COPYTRUNCATE_GAP)
def test_a_truncation_regrown_past_the_checkpoint_restarts_from_its_head(
    world: World,
) -> None:
    src = make_source(world, LogTailer, limit=2)
    src.write(["a1", "a2"])
    shipper = src.shipper()
    old = shipper.collect_batch()
    assert old is not None
    _ack(shipper, old)
    src.rotate("truncate")
    src.write(["b1", "b2", "b3"])
    assert _messages(src.restart().collect_batch())[:1] == ["b1"]


_OPAQUE = "journald cursors are opaque: the spec forbids ordering them, so a stale one rewinds"


@pytest.mark.parametrize("tailer_cls", _cases(JournaldTailer=_OPAQUE))
def test_late_and_duplicate_acks_cannot_rewind(
    world: World, tailer_cls: type[Any]
) -> None:
    written = ["l1", "l2", "l3", "l4", "l5"]
    src = make_source(world, tailer_cls, limit=1)
    src.write(written)
    shipper = src.shipper()
    r1 = shipper.collect_batch()
    r1b = shipper.collect_batch()
    assert r1 is not None and r1b is not None
    _ack(shipper, r1)
    r2 = shipper.collect_batch()
    assert r2 is not None
    _ack(shipper, r2)
    _ack(shipper, r1b)  # late
    _ack(shipper, r1)  # duplicate
    acked = set(_messages(r1) + _messages(r1b) + _messages(r2))
    expected = _first_unacked(written, acked)
    assert _messages(src.restart().collect_batch())[:1] == [expected]


_READS_AHEAD = (
    "StreamingDockerTailer reads ahead from its stored ts (_spawn --since, _drain_lines); "
    "refusing an out-of-order ACK needs a read-side redesign, 0.8.2"
)


@pytest.mark.parametrize("tailer_cls", _cases(StreamingDockerTailer=_READS_AHEAD))
def test_an_out_of_order_ack_cannot_skip_an_unconfirmed_gap(
    world: World, tailer_cls: type[Any]
) -> None:
    written = ["l1", "l2", "l3"]
    src = make_source(world, tailer_cls, limit=1)
    src.write(written)
    shipper = src.shipper()
    shipper.collect_batch()  # r1: never acknowledged
    r2 = shipper.collect_batch()
    assert r2 is not None
    _ack(shipper, r2)
    expected = _first_unacked(written, set(_messages(r2)))
    assert _messages(src.restart().collect_batch())[:1] == [expected]


@pytest.mark.parametrize("tailer_cls", _cases())
def test_a_receipt_this_tailer_never_issued_changes_nothing(
    world: World, tailer_cls: type[Any]
) -> None:
    mine = make_source(world, tailer_cls, name="mine", limit=2)
    other = make_source(world, tailer_cls, name="other", limit=2)
    mine.write(["a1", "a2", "a3"])
    other.write(["b1", "b2", "b3"])
    shipper = mine.shipper()
    assert _messages(shipper.collect_batch()) == ["a1", "a2"]
    foreign = other.shipper().collect_batch()
    assert foreign is not None
    _ack(shipper, foreign)
    assert _messages(mine.restart().collect_batch())[:1] == ["a1"]
