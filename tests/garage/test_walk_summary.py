"""One summary per walk, and no failure hidden inside it.

A walk (the periodic collect, or a targeted batch) folds its successful admin
calls into one ``walk_summary``; every failed call (no status, or >= 400) keeps
its own ``admin_call`` event. Outside a walk every call emits, as before.
Driven through a fake ``HTTPConnection`` so the real ``_request`` runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stormpulse import events
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import (
    GarageStateReader,
    collect_garage_state,
    read_buckets_by_id,
)

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


def _config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/tmp/garage.toml"),
        admin_url=ADMIN_URL,
        admin_token="tok",
    )


def _install_fake_garage(
    monkeypatch: pytest.MonkeyPatch,
    n: int,
    *,
    not_found: frozenset[str] | set[str] = frozenset(),
    unreachable: frozenset[str] | set[str] = frozenset(),
) -> None:
    """A Garage with ``n`` buckets; named ids answer 404 or drop the connection."""
    ids = [_bucket_id(i) for i in range(n)]

    class _Resp:
        def __init__(self, status: int, payload: Any) -> None:
            self.status = status
            self._payload = json.dumps(payload).encode()

        def read(self) -> bytes:
            return self._payload

    fixed: dict[str, Any] = {
        "ListBuckets": [{"id": b} for b in ids],
        "GetClusterStatus": {"nodes": [NODE]},
        "GetClusterStatistics": {"totalObjectCount": 0},
    }

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
            if endpoint == "GetBucketInfo":
                return _bucket_info(self._path.rsplit("id=", 1)[-1])
            return _Resp(200, fixed.get(endpoint, []))  # ListKeys: []

        def close(self) -> None:
            pass

    monkeypatch.setattr("http.client.HTTPConnection", _Conn)


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
