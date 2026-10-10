"""Tests for the wide-event buffer and emit API (``stormpulse.events``)."""

from __future__ import annotations

from typing import Any

import pytest

from stormpulse import events
from stormpulse.events import EventBuffer


@pytest.fixture(autouse=True)
def _own_kind() -> Any:
    """A kind of this file's own, so no test leans on which agent module
    another test imported; the registry is restored after, since the
    artifact publishes it (``tests/wire_contract/``)."""
    before = dict(events._REGISTRY)
    events.declare_event("probe", "ping", {"n", "note"})
    yield
    events._REGISTRY.clear()
    events._REGISTRY.update(before)


def _drain() -> list[dict[str, Any]]:
    return events.buffer().drain("t")


class TestEmit:
    def test_emit_stamps_ts_source_kind(self) -> None:
        events.emit("admin_call", source="garage_admin", endpoint="GetBucketInfo")
        batch = events.buffer().drain("b1")
        assert len(batch) == 1
        e = batch[0]
        assert e["kind"] == "admin_call"
        assert e["source"] == "garage_admin"
        assert e["endpoint"] == "GetBucketInfo"
        assert e["ts"].endswith("Z")

    def test_emit_elides_empty_fields(self) -> None:
        events.emit("job_result", source="jobs", failure_reason="", error=None)
        e = events.buffer().drain("b1")[0]
        assert "failure_reason" not in e
        assert "error" not in e

    def test_emit_caps_error_text(self) -> None:
        events.emit("job_result", source="jobs", error="x" * 10_000)
        e = events.buffer().drain("b1")[0]
        assert len(e["error"]) == 500

    def test_emit_caps_reason_text_like_error(self) -> None:
        events.emit("events_nacked", source="events", reason="x" * 10_000)
        e = events.buffer().drain("b1")[0]
        assert len(e["reason"]) == 500

    def test_emit_stamps_trigger_from_contextvar(self) -> None:
        token = events.trigger_var.set("periodic_walk")
        try:
            events.emit("admin_call", source="garage_admin")
        finally:
            events.trigger_var.reset(token)
        events.emit("admin_call", source="garage_admin")
        batch = events.buffer().drain("b1")
        assert batch[0]["trigger"] == "periodic_walk"
        assert "trigger" not in batch[1]

    def test_emit_stamps_command_ref_from_contextvar(self) -> None:
        token = events.command_ref_var.set("pc-123")
        try:
            # An admin call inside a job inherits the job's ref...
            events.emit("admin_call", source="garage_admin")
            # ...but an explicit ref (the job_result's own) always wins.
            events.emit("job_result", source="jobs", command_ref="pc-123")
        finally:
            events.command_ref_var.reset(token)
        events.emit("admin_call", source="garage_admin")
        batch = events.buffer().drain("b1")
        assert batch[0]["command_ref"] == "pc-123"
        assert batch[1]["command_ref"] == "pc-123"
        assert "command_ref" not in batch[2]


class TestRegistry:
    def test_two_emits_carry_consecutive_seq(self) -> None:
        events.emit("ping", source="probe", n=1)
        events.emit("ping", source="probe", n=2)
        assert [e["seq"] for e in _drain()] == [1, 2]

    def test_the_synthesized_dropped_event_takes_the_next_seq(self) -> None:
        buf = EventBuffer(max_events=1)
        buf.append({"kind": "a"})
        buf.append({"kind": "b"})  # drops "a" (seq 1)
        synthesized, kept = buf.drain("b1")
        assert (synthesized["kind"], synthesized["seq"]) == ("dropped_events", 3)
        assert kept["seq"] == 2

    def test_a_requeued_event_keeps_its_seq(self) -> None:
        buf = EventBuffer()
        buf.append({"kind": "a"})
        buf.drain("b1")
        buf.requeue_unacked()
        buf.append({"kind": "b"})
        assert [e["seq"] for e in buf.drain("b2")] == [1, 2]

    def test_an_undeclared_field_is_dropped_and_counted_on_the_next_drain(
        self,
    ) -> None:
        events.emit("ping", source="probe", n=1, user_ip="10.0.0.1")
        first = _drain()
        assert [e["kind"] for e in first] == ["dropped_events", "ping"]
        assert "user_ip" not in first[1]
        assert first[0]["undeclared"] == 1
        assert first[0]["dropped"] == 0
        events.emit("ping", source="probe", n=2)
        assert [e["kind"] for e in _drain()] == ["ping"]  # the count was reset

    def test_an_undeclared_kind_ships_bare_and_counts_every_field(self) -> None:
        events.emit("nobody_declared_me", source="probe", a=1, b=2, c="")
        synthesized, bare = _drain()
        assert synthesized["undeclared"] == 2  # the empty field was elided, not counted
        assert set(bare) == {"ts", "source", "kind", "seq"}

    def test_a_declaration_binds_to_its_source_not_to_the_kind(self) -> None:
        """Another module reusing a declared kind name declares nothing: its
        fields are undeclared for its own source, dropped and counted, so one
        declaration cannot be borrowed to carry an identifier."""
        events.emit("ping", source="someone_else", n=1)
        synthesized, bare = _drain()
        assert synthesized["undeclared"] == 1
        assert "n" not in bare
        assert (bare["source"], bare["kind"]) == ("someone_else", "ping")

    def test_command_ref_rides_without_a_declaration(self) -> None:
        events.emit("ping", source="probe", command_ref="pc-1")
        (e,) = _drain()
        assert e["command_ref"] == "pc-1"

    def test_a_second_declaration_adds_fields(self) -> None:
        events.declare_event("probe", "pong", {"a"})
        events.declare_event("probe", "pong", {"b"})
        assert events.declared_events()["probe"]["pong"] == ["a", "b"]

    def test_declared_events_are_sorted_for_the_artifact(self) -> None:
        events.declare_event("zprobe", "zeta", {"b", "a"})
        events.declare_event("zprobe", "alpha", set())
        assert list(events.declared_events()["zprobe"]) == ["alpha", "zeta"]
        assert events.declared_events()["zprobe"]["zeta"] == ["a", "b"]

    def test_a_walk_declares_its_summary_per_item(self) -> None:
        events.walk(source="probe", item="widget")
        assert events.declared_events()["probe"]["walk_summary"] == [
            "calls",
            "failures",
            "slowest_endpoint",
            "slowest_ms",
            "total_ms",
            "widgets_dropped",
            "widgets_read",
        ]

    def test_the_walk_summary_passes_its_own_filter(self) -> None:
        with events.walk(source="garage_admin", item="bucket"):
            _call("GetBucketInfo", 3, bucket_id="b1")
        (summary,) = _drain()
        assert summary["buckets_read"] == 1
        assert summary["slowest_endpoint"] == "GetBucketInfo"

    def test_a_decorated_walk_gets_a_fresh_scope_per_call(self) -> None:
        scope = events.walk(source="garage_admin", item="bucket")

        @scope
        def read(depth: int) -> None:
            if depth:
                read(depth - 1)  # the nested call joins this walk
            _call("GetBucketInfo", 1, bucket_id=f"b{depth}")

        read(1)
        (summary,) = _drain()
        assert summary["buckets_read"] == 2

    def test_the_synthesized_event_carries_only_declared_fields(self) -> None:
        buf = EventBuffer(max_events=1)
        buf.append({"kind": "a"})
        buf.append({"kind": "b"})
        synthesized = buf.drain("b1")[0]
        declared = set(events.declared_events()["events"]["dropped_events"])
        assert set(synthesized) - events.ENVELOPE_FIELDS == declared


class TestEventBuffer:
    def test_ack_releases_batch(self) -> None:
        buf = EventBuffer()
        buf.append({"kind": "a"})
        batch = buf.drain("b1")
        assert len(batch) == 1
        assert buf.ack("b1") is True
        assert buf.ack("b1") is False
        assert buf.requeue_unacked() == 0

    def test_nack_drops_the_batch_and_counts_it(self) -> None:
        buf = EventBuffer()
        buf.append({"kind": "a"})
        buf.append({"kind": "b"})
        buf.drain("b1")
        assert buf.nack("b1") == 2
        assert buf.nack("b1") is None
        assert buf.ack("b1") is False
        assert buf.requeue_unacked() == 0
        assert buf.drain("b2") == []  # never requeued, never tallied as dropped

    def test_nack_of_an_unknown_batch_leaves_in_flight_alone(self) -> None:
        buf = EventBuffer()
        buf.append({"kind": "a"})
        buf.drain("b1")
        assert buf.nack("never-sent") is None
        assert buf.ack("b1") is True

    def test_unacked_batch_requeues_in_order(self) -> None:
        buf = EventBuffer()
        buf.append({"kind": "a"})
        buf.append({"kind": "b"})
        buf.drain("b1")
        buf.append({"kind": "c"})
        assert buf.requeue_unacked() == 2
        batch = buf.drain("b2")
        assert [e["kind"] for e in batch] == ["a", "b", "c"]

    def test_overflow_drops_oldest_and_is_never_silent(self) -> None:
        buf = EventBuffer(max_events=2)
        buf.append({"kind": "a"})
        buf.append({"kind": "b"})
        buf.append({"kind": "c"})  # drops "a"
        batch = buf.drain("b1")
        assert batch[0]["kind"] == "dropped_events"
        assert batch[0]["dropped"] == 1
        assert [e["kind"] for e in batch[1:]] == ["b", "c"]

    def test_drain_respects_batch_cap(self) -> None:
        buf = EventBuffer()
        for i in range(5):
            buf.append({"kind": f"e{i}"})
        first = buf.drain("b1", max_events=3)
        second = buf.drain("b2", max_events=3)
        assert len(first) == 3
        assert len(second) == 2

    def test_empty_drain_tracks_nothing_in_flight(self) -> None:
        buf = EventBuffer()
        assert buf.drain("b1") == []
        assert buf.ack("b1") is False

    def test_never_acking_server_cannot_grow_memory_unbounded(self) -> None:
        # Wrong deploy order (old website never acks events.batch): the
        # oldest in-flight batch is evicted past the cap, counted as
        # dropped, and the loss surfaces on the next drain.
        buf = EventBuffer()
        for i in range(events.MAX_IN_FLIGHT_BATCHES + 1):
            buf.append({"kind": f"e{i}"})
            buf.drain(f"b{i}")
        assert buf.ack("b0") is False  # evicted, not just unacked
        buf.append({"kind": "fresh"})
        batch = buf.drain("final")
        assert batch[0]["kind"] == "dropped_events"
        assert batch[0]["dropped"] == 1


def _call(endpoint: str, ms: int, *, status: int | None = 200, **fields: str) -> None:
    events.record_call(
        "admin_call",
        source="garage_admin",
        endpoint=endpoint,
        duration_ms=ms,
        status=status,
        **fields,
    )


class TestWalk:  # skylos: ignore[SKY-Q702] a pytest grouping, no shared state by design
    def test_summary_names_the_slowest_call_even_when_every_call_is_0ms(self) -> None:
        # Localhost admin calls truncate to 0 ms; the summary must still name one.
        with events.walk(source="garage_admin", item="bucket"):
            _call("ListBuckets", 0)
            _call("GetBucketInfo", 0, bucket_id="b1")
        (summary,) = events.buffer().drain("b1")
        assert summary["slowest_endpoint"] == "GetBucketInfo"
        assert summary["slowest_ms"] == 0

    def test_summary_slowest_and_total_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticks = iter([10.0, 10.25])
        monkeypatch.setattr("stormpulse.events.time.monotonic", lambda: next(ticks))
        with events.walk(source="garage_admin", item="bucket"):
            _call("ListBuckets", 5)
            _call("GetBucketInfo", 30, bucket_id="b1")
            _call("GetClusterStatus", 10)
        (summary,) = events.buffer().drain("b1")
        assert summary["slowest_endpoint"] == "GetBucketInfo"
        assert summary["slowest_ms"] == 30
        assert summary["total_ms"] == 250

    def test_a_walk_that_raises_still_summarises_and_closes(self) -> None:
        # A crashed collect must not leave the scope open, or every later
        # job-driven call folds into a summary nobody emits.
        with pytest.raises(RuntimeError):
            with events.walk(source="garage_admin", item="bucket"):
                _call("GetBucketInfo", 1, bucket_id="b1")
                raise RuntimeError("collect crashed")
        _call("UpdateBucket", 1)
        kinds = [e["kind"] for e in events.buffer().drain("b1")]
        assert kinds == ["walk_summary", "admin_call"]

    def test_a_walk_that_only_listed_emits_nothing(self) -> None:
        # A quiet membership diff: one counted call, nothing read or dropped.
        with events.walk(source="garage_admin", item="bucket"):
            _call("ListBuckets", 1)
        assert events.buffer().drain("b1") == []

    def test_a_topology_only_walk_emits_nothing(self) -> None:
        with events.walk(source="garage_admin", item="bucket"):
            _call("GetClusterStatus", 1)
            _call("GetClusterStatistics", 1)
            _call("ListKeys", 1)
        assert events.buffer().drain("b1") == []

    def test_a_drop_emits_a_summary_carrying_the_count(self) -> None:
        with events.walk(source="garage_admin", item="bucket"):
            _call("ListBuckets", 1)
            events.record_dropped(2)
        (summary,) = events.buffer().drain("b1")
        assert summary["kind"] == "walk_summary"
        assert summary["buckets_dropped"] == 2
        assert summary["buckets_read"] == 0
        assert summary["calls"] == 1

    def test_a_failure_still_emits_the_summary(self) -> None:
        with events.walk(source="garage_admin", item="bucket"):
            _call("ListBuckets", 1, status=None)
        kinds = [e["kind"] for e in events.buffer().drain("b1")]
        assert kinds == ["admin_call", "walk_summary"]

    def test_a_drop_outside_a_walk_has_no_summary_to_reach(self) -> None:
        events.record_dropped(1)
        assert events.buffer().drain("b1") == []
