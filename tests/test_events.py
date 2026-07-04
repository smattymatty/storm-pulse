"""Tests for the wide-event buffer and emit API (``stormpulse.events``)."""

from __future__ import annotations

from stormpulse import events
from stormpulse.events import EventBuffer


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

    def test_emit_stamps_trigger_from_contextvar(self) -> None:
        token = events.trigger_var.set("detector")
        try:
            events.emit("admin_call", source="garage_admin")
        finally:
            events.trigger_var.reset(token)
        events.emit("admin_call", source="garage_admin")
        batch = events.buffer().drain("b1")
        assert batch[0]["trigger"] == "detector"
        assert "trigger" not in batch[1]


class TestEventBuffer:
    def test_ack_releases_batch(self) -> None:
        buf = EventBuffer()
        buf.append({"kind": "a"})
        batch = buf.drain("b1")
        assert len(batch) == 1
        assert buf.ack("b1") is True
        assert buf.ack("b1") is False
        assert buf.requeue_unacked() == 0

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
