"""Unit tests for the admin-API call meter (observability).

The meter wraps ``admin_api._request`` to record every admin call's latency in a
trailing time window keyed by endpoint, so the agent reports admin-API call-rate
and p95 latency per target node - the signal the 2026-06-27 saturation incident
had no graph for. Pure window math here; ``now`` is injected so the tests carry
no real clock.
"""

from __future__ import annotations

from pathlib import Path

from stormpulse.garage import admin_api, state
from stormpulse.garage.admin_api import (
    AdminCallStats,
    _AdminCallMeter,
    _percentile,
)
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import (
    GarageAdminMetric,
    GaragePeer,
    _read_admin_metrics,
)


def _config(admin_url: str) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url=admin_url,
    )


def _peer(node_id: str) -> GaragePeer:
    return GaragePeer(
        node_id=node_id,
        hostname="h",
        address="127.0.0.1",
        zone="z",
        capacity_gb=1.0,
        data_avail_gb=1.0,
        data_avail_percent=100.0,
        version="2.3",
        healthy=True,
    )


class TestPercentile:
    def test_empty_is_zero(self) -> None:
        assert _percentile([], 0.95) == 0.0

    def test_p95_nearest_rank_of_100(self) -> None:
        # ceil(0.95 * 100) = 95 -> the 95th value (1-indexed) = 95.0
        assert _percentile([float(n) for n in range(1, 101)], 0.95) == 95.0

    def test_p95_of_small_sample_rounds_up(self) -> None:
        # ceil(0.95 * 4) = 4 -> the max. A handful of calls => p95 ~ max, which is
        # the honest read at this sample size.
        assert _percentile([10.0, 20.0, 30.0, 40.0], 0.95) == 40.0

    def test_single_sample(self) -> None:
        assert _percentile([7.0], 0.95) == 7.0


class TestAdminCallMeter:
    def test_rate_is_count_over_window(self) -> None:
        meter = _AdminCallMeter(window_seconds=300.0)
        for i in range(30):
            meter.record("http://node", duration_ms=float(i), now=1000.0 + i)
        stats = meter.snapshot(now=1100.0)["http://node"]
        assert stats.sample_count == 30
        assert stats.calls_per_sec == 30 / 300.0
        # p95 of durations 0..29: ceil(0.95*30)=29 -> 29th value (1-indexed) = 28.0
        assert stats.p95_latency_ms == 28.0

    def test_old_samples_evicted_by_age(self) -> None:
        meter = _AdminCallMeter(window_seconds=300.0)
        meter.record("http://node", duration_ms=5.0, now=0.0)  # ancient
        meter.record("http://node", duration_ms=9.0, now=1000.0)
        stats = meter.snapshot(now=1000.0)["http://node"]
        # The now=0.0 sample is >300s before now=1000.0, evicted.
        assert stats.sample_count == 1
        assert stats.p95_latency_ms == 9.0

    def test_endpoint_fully_aged_out_is_absent(self) -> None:
        meter = _AdminCallMeter(window_seconds=300.0)
        meter.record("http://node", duration_ms=5.0, now=0.0)
        assert meter.snapshot(now=10_000.0) == {}

    def test_keys_by_endpoint(self) -> None:
        meter = _AdminCallMeter(window_seconds=300.0)
        meter.record("http://a", duration_ms=1.0, now=10.0)
        meter.record("http://b", duration_ms=2.0, now=10.0)
        snap = meter.snapshot(now=10.0)
        assert set(snap) == {"http://a", "http://b"}

    def test_empty_meter(self) -> None:
        assert _AdminCallMeter().snapshot(now=0.0) == {}


class TestReadAdminMetrics:
    """The garage state read folds the meter into per-target-node telemetry."""

    def test_dispatch_endpoint_maps_to_node_id(self, monkeypatch) -> None:
        # The one endpoint equal to config.admin_url is attributed to the node
        # this read composed, never collapsed to a hardcoded dispatch identity.
        monkeypatch.setattr(
            admin_api,
            "admin_call_stats",
            lambda: {
                "http://127.0.0.1:3903": AdminCallStats(
                    sample_count=12, calls_per_sec=0.04, p95_latency_ms=85.0
                )
            },
        )
        node = _peer("n" * 64)
        metrics = _read_admin_metrics(_config("http://127.0.0.1:3903"), node)
        assert metrics == (
            GarageAdminMetric(
                target_node_id="n" * 64,
                calls_per_sec=0.04,
                p95_latency_ms=85.0,
                sample_count=12,
            ),
        )

    def test_other_endpoint_keeps_its_endpoint_id(self, monkeypatch) -> None:
        # A future multi-endpoint dispatch: an endpoint that is not the local
        # admin_url keeps its endpoint string, not the dispatch node id.
        monkeypatch.setattr(
            admin_api,
            "admin_call_stats",
            lambda: {
                "http://peer-2:3903": AdminCallStats(
                    sample_count=3, calls_per_sec=0.01, p95_latency_ms=12.0
                )
            },
        )
        node = _peer("n" * 64)
        metrics = _read_admin_metrics(_config("http://127.0.0.1:3903"), node)
        assert metrics[0].target_node_id == "http://peer-2:3903"

    def test_state_carries_admin_metrics_in_to_dict(self) -> None:
        # The field rides the wire blob via to_dict (asdict serializes the nested
        # frozen dataclass to a plain dict; the tuple JSON-serializes to a list
        # on the wire, like every other sequence field).
        st = state.GarageState(
            node_id="n",
            hostname="h",
            zone="z",
            capacity_gb=1.0,
            data_avail_gb=1.0,
            version="2.3",
            healthy=True,
            object_count=0,
            buckets=[],
            keys=[],
            peers=[],
            admin_metrics=(
                GarageAdminMetric(
                    target_node_id="n",
                    calls_per_sec=0.04,
                    p95_latency_ms=85.0,
                    sample_count=12,
                ),
            ),
        )
        assert st.to_dict()["admin_metrics"] == (
            {
                "target_node_id": "n",
                "calls_per_sec": 0.04,
                "p95_latency_ms": 85.0,
                "sample_count": 12,
            },
        )
