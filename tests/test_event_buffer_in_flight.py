"""The event buffer's in-flight batches: ack, nack, requeue on reconnect, and
eviction when the control plane never answers."""

from __future__ import annotations

from stormpulse import events
from stormpulse.events import EventBuffer


def _three_batches_in_flight() -> EventBuffer:
    """Events 1-6 in flight as batches of sizes 1, 2, 3, which tell batch order
    from in-batch order (STAB-05), and event 7 queued behind them unsent."""
    buf = EventBuffer()
    for i in range(1, 7):
        buf.append({"kind": str(i)})
    for batch_id, size in (("b1", 1), ("b2", 2), ("b3", 3)):
        buf.drain(batch_id, max_events=size)
    buf.append({"kind": "7"})
    return buf


def test_ack_releases_batch() -> None:
    buf = EventBuffer()
    buf.append({"kind": "a"})
    batch = buf.drain("b1")
    assert len(batch) == 1
    assert buf.ack("b1") is True
    assert buf.ack("b1") is False
    assert buf.requeue_unacked() == 0


def test_nack_drops_the_batch_and_counts_it() -> None:
    buf = EventBuffer()
    buf.append({"kind": "a"})
    buf.append({"kind": "b"})
    buf.drain("b1")
    assert buf.nack("b1") == 2
    assert buf.nack("b1") is None
    assert buf.ack("b1") is False
    assert buf.requeue_unacked() == 0
    assert buf.drain("b2") == []  # never requeued, never tallied as dropped


def test_nack_of_an_unknown_batch_leaves_in_flight_alone() -> None:
    buf = EventBuffer()
    buf.append({"kind": "a"})
    buf.drain("b1")
    assert buf.nack("never-sent") is None
    assert buf.ack("b1") is True


def test_unacked_batches_requeue_oldest_first_ahead_of_unsent() -> None:
    buf = _three_batches_in_flight()
    assert buf.requeue_unacked() == 6
    kinds = [e["kind"] for e in buf.drain("b4")]
    assert kinds == ["1", "2", "3", "4", "5", "6", "7"]


def test_acked_middle_batch_leaves_its_neighbours_in_order() -> None:
    buf = _three_batches_in_flight()
    assert buf.ack("b2") is True
    assert buf.requeue_unacked() == 4
    kinds = [e["kind"] for e in buf.drain("b4")]
    assert kinds == ["1", "4", "5", "6", "7"]


def test_requeue_overflow_drops_oldest_of_the_rebuilt_stream() -> None:
    # Requeued 1-4 plus unsent 5-6 overflow a cap of 4: 1 and 2 go.
    buf = EventBuffer(max_events=4)
    for i in range(1, 5):
        buf.append({"kind": str(i)})
    buf.drain("b1", max_events=2)
    buf.drain("b2", max_events=2)
    buf.append({"kind": "5"})
    buf.append({"kind": "6"})
    assert buf.requeue_unacked() == 4  # requeued, drops not netted out
    batch = buf.drain("b3")
    assert batch[0]["kind"] == "dropped_events"
    assert batch[0]["dropped"] == 2
    assert [e["kind"] for e in batch[1:]] == ["3", "4", "5", "6"]
    assert [e["seq"] for e in batch[1:]] == [3, 4, 5, 6]


def test_repeated_reconnects_never_duplicate() -> None:
    buf = EventBuffer()
    for i in range(1, 4):
        buf.append({"kind": str(i)})
    buf.drain("b1", max_events=2)
    assert buf.requeue_unacked() == 2
    assert buf.requeue_unacked() == 0
    buf.drain("b2", max_events=2)
    assert buf.requeue_unacked() == 2
    kinds = [e["kind"] for e in buf.drain("b3")]
    assert kinds == ["1", "2", "3"]


def test_repeated_reconnects_keep_original_seqs() -> None:
    buf = EventBuffer()
    for i in range(1, 4):
        buf.append({"kind": str(i)})
    stamped = [e["seq"] for e in buf.drain("b1")]
    for attempt in ("b2", "b3"):
        buf.requeue_unacked()
        assert [e["seq"] for e in buf.drain(attempt)] == stamped
    buf.append({"kind": "4"})
    assert buf.drain("b4")[0]["seq"] == stamped[-1] + 1


def test_requeue_after_in_flight_eviction_keeps_send_order() -> None:
    # Real ids are uuid4: order must come from send order, never the id
    # ("b10" sorts before "b2"). Eviction's loss and requeue's add up.
    buf = EventBuffer(max_events=events.MAX_IN_FLIGHT_BATCHES - 1)
    for i in range(events.MAX_IN_FLIGHT_BATCHES + 1):
        buf.append({"kind": f"e{i}"})
        buf.drain(f"b{i}")
    assert buf.requeue_unacked() == events.MAX_IN_FLIGHT_BATCHES
    batch = buf.drain("final", max_events=100)
    assert (batch[0]["kind"], batch[0]["dropped"]) == ("dropped_events", 2)
    survivors = range(2, events.MAX_IN_FLIGHT_BATCHES + 1)
    assert [e["kind"] for e in batch[1:]] == [f"e{i}" for i in survivors]


def test_empty_drain_tracks_nothing_in_flight() -> None:
    buf = EventBuffer()
    assert buf.drain("b1") == []
    assert buf.ack("b1") is False


def test_never_acking_server_cannot_grow_memory_unbounded() -> None:
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
