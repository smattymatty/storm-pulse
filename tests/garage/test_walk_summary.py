"""One summary per walk, and no failure hidden inside it.

A walk (the periodic collect, or a targeted batch) folds its successful admin
calls into one ``walk_summary``, emitted when it read, dropped or failed; a
quiet diff minute or a topology-only tick emits nothing. Every failed call (no
status, or >= 400) keeps its own ``admin_call`` event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stormpulse import events
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import collect_garage_state, read_buckets_by_id
from stormpulse.garage.state_reader import GarageStateReader
from tests.garage.state_reader_support import Clock

ADMIN_URL = "http://127.0.0.1:3903"
NODE = {
    "id": "a" * 64,
    "hostname": "garage-one",
    "addr": "10.0.0.1:3901",
    "garageVersion": "v2.3.0",
    "isUp": True,
    "role": {"zone": "z1", "capacity": 3_000_000_000_000, "tags": []},
    "dataPartition": {"available": 2_800_000_000_000, "total": 3_000_000_000_000},
}


def _bucket_id(i: int) -> str:
    return f"{i:064x}"


def _config(*, hint_file: str = "") -> GarageConfig:
    # An unreadable hint file turns hints off but keeps the hinted cadences.
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/tmp/garage.toml"),
        admin_url=ADMIN_URL,
        admin_token="tok",
        hint_file=hint_file,
    )


@dataclass
class _FakeGarage:
    """The fake's live state: the test edits ``ids`` and reads ``calls``."""

    ids: list[str]
    calls: list[str] = field(default_factory=list)
    down: bool = False


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload


_TOPOLOGY: dict[str, Any] = {
    "GetClusterStatus": {"nodes": [NODE]},
    "GetClusterStatistics": {"totalObjectCount": 0},
}


def _install_fake_garage(
    monkeypatch: pytest.MonkeyPatch,
    n: int,
    *,
    not_found: frozenset[str] | set[str] = frozenset(),
    unreachable: frozenset[str] | set[str] = frozenset(),
) -> _FakeGarage:
    """A Garage with ``n`` buckets; named ids answer 404 or drop the connection."""
    fake = _FakeGarage(ids=[_bucket_id(i) for i in range(n)])

    def _bucket_info(bucket_id: str) -> _Resp:
        if bucket_id in unreachable:
            raise OSError("connection reset")
        if bucket_id in not_found:
            return _Resp(404, {"code": "NoSuchBucket"})
        return _Resp(200, {"id": bucket_id, "objects": 1, "bytes": 1})

    class _Conn:
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            self._path = ""

        def request(self, method: str, path: str, **_: Any) -> None:
            self._path = path

        def getresponse(self) -> _Resp:
            endpoint = self._path.split("?", 1)[0].rsplit("/", 1)[-1]
            fake.calls.append(endpoint)
            if fake.down:
                raise OSError("connection refused")
            if endpoint == "GetBucketInfo":
                return _bucket_info(self._path.rsplit("id=", 1)[-1])
            if endpoint == "ListBuckets":
                return _Resp(200, [{"id": b} for b in fake.ids])
            return _Resp(200, _TOPOLOGY.get(endpoint, []))  # ListKeys: []

        def close(self) -> None:
            pass

    monkeypatch.setattr("http.client.HTTPConnection", _Conn)
    return fake


def _drain() -> list[dict[str, Any]]:
    return events.buffer().drain("t")


@pytest.mark.parametrize(
    ("n", "not_found", "unreachable"),
    [
        (1, {0}, set()),
        (1000, {7, 500}, {999}),
        (1000, set(), set()),
    ],
)
def test_walk_emits_one_summary_plus_one_event_per_failure(
    monkeypatch: pytest.MonkeyPatch,
    n: int,
    not_found: set[int],
    unreachable: set[int],
) -> None:
    failing = {_bucket_id(i) for i in not_found | unreachable}
    _install_fake_garage(
        monkeypatch,
        n,
        not_found={_bucket_id(i) for i in not_found},
        unreachable={_bucket_id(i) for i in unreachable},
    )
    k = len(failing)

    assert GarageStateReader().collect(_config()) is not None

    batch = _drain()
    summaries = [e for e in batch if e["kind"] == "walk_summary"]
    calls = [e for e in batch if e["kind"] == "admin_call"]
    assert len(batch) == 1 + k
    assert len(summaries) == 1
    assert {e["bucket_id"] for e in calls} == failing
    summary = summaries[0]
    assert summary["failures"] == k
    # Topology (status, statistics, keys) + ListBuckets + one read per bucket.
    assert summary["calls"] == 4 + n
    assert summary["buckets_read"] == n - k


def test_a_404_keeps_its_own_event_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_garage(monkeypatch, 3, not_found={_bucket_id(1)})
    read_buckets_by_id(_config(), [_bucket_id(i) for i in range(3)])
    (failed,) = [e for e in _drain() if e["kind"] == "admin_call"]
    assert failed["status"] == 404
    assert failed["bucket_id"] == _bucket_id(1)


def test_a_targeted_batch_emits_one_summary_and_an_empty_one_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_garage(monkeypatch, 2)
    read_buckets_by_id(_config(), [])
    assert _drain() == []
    read_buckets_by_id(_config(), [_bucket_id(0), _bucket_id(1)])
    (summary,) = _drain()
    assert summary["kind"] == "walk_summary"
    assert summary["buckets_read"] == 2


def test_the_full_discovery_read_is_one_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_garage(monkeypatch, 5)
    assert collect_garage_state(_config()) is not None
    assert [e["kind"] for e in _drain()] == ["walk_summary"]


def test_quiet_diff_minutes_emit_nothing_until_the_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prediction in miniature: six ticks over five quiet minutes on a
    # hinted node leave two summaries (the cold walk and the re-read), not six.
    fake = _install_fake_garage(monkeypatch, 3)
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    config = _config(hint_file="/nonexistent/hint.json")
    assert reader.collect(config) is not None
    assert [e["kind"] for e in _drain()] == ["walk_summary"]
    for clock.now in (60.0, 120.0, 180.0, 240.0):
        fake.calls.clear()
        assert reader.collect(config) is not None
        assert fake.calls == ["ListBuckets"]
        assert _drain() == []
    clock.now = 300.0
    assert reader.collect(config) is not None
    (summary,) = _drain()
    assert summary["buckets_read"] == 3
    assert summary["buckets_dropped"] == 0


def test_a_diff_that_drops_a_bucket_emits_one_summary_with_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_garage(monkeypatch, 3)
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    config = _config(hint_file="/nonexistent/hint.json")
    assert reader.collect(config) is not None
    _drain()
    del fake.ids[2]
    clock.now = 60.0
    state = reader.collect(config)
    assert state is not None
    assert [b.id for b in state.buckets] == fake.ids
    (summary,) = _drain()
    assert summary["kind"] == "walk_summary"
    assert summary["buckets_dropped"] == 1
    assert summary["buckets_read"] == 0
    assert summary["failures"] == 0
    assert summary["calls"] == 1


def test_a_reread_that_drops_a_bucket_carries_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deletion no diff minute saw reaches the plane through the re-read's
    # own summary, beside what it read.
    fake = _install_fake_garage(monkeypatch, 3)
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    config = _config(hint_file="/nonexistent/hint.json")
    assert reader.collect(config) is not None
    _drain()
    del fake.ids[0]
    clock.now = 300.0
    state = reader.collect(config)
    assert state is not None
    assert [b.id for b in state.buckets] == fake.ids
    (summary,) = _drain()
    assert summary["buckets_read"] == 2
    assert summary["buckets_dropped"] == 1


def test_a_topology_only_tick_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_garage(monkeypatch, 2)
    reader = GarageStateReader(clock=Clock())
    config = _config(hint_file="/nonexistent/hint.json")
    for _ in range(GarageStateReader.TOPOLOGY_EVERY):
        assert reader.collect(config) is not None
    _drain()
    fake.calls.clear()
    assert reader.collect(config) is not None
    assert fake.calls == ["GetClusterStatus", "GetClusterStatistics", "ListKeys"]
    assert _drain() == []


def test_a_failed_diff_still_emits_its_failure_and_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_garage(monkeypatch, 2)
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    config = _config(hint_file="/nonexistent/hint.json")
    assert reader.collect(config) is not None
    _drain()
    fake.down = True
    clock.now = 60.0
    state = reader.collect(config)
    assert state is not None  # the cache is served
    assert len(state.buckets) == 2
    failed, summary = _drain()
    assert failed["kind"] == "admin_call"
    assert failed["endpoint"] == "ListBuckets"
    assert summary["kind"] == "walk_summary"
    assert summary["failures"] == 1
    assert summary["buckets_dropped"] == 0


def test_outside_a_walk_every_call_keeps_its_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_garage(monkeypatch, 1)
    for _ in range(2):
        admin_api.get_bucket_info(
            admin_url=ADMIN_URL, admin_token="tok", bucket_ref=_bucket_id(0)
        )
    batch = _drain()
    assert [e["kind"] for e in batch] == ["admin_call", "admin_call"]
    assert all(e["status"] == 200 for e in batch)


@pytest.mark.parametrize("status", [None, 400])
def test_no_status_or_400_and_up_is_a_failure(status: int | None) -> None:
    assert events.is_failure(status)


@pytest.mark.parametrize("status", [200, 399])
def test_below_400_is_not_a_failure(status: int) -> None:
    assert not events.is_failure(status)
