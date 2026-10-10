"""Delivery bookkeeping uses arbitrary integration IDs and full snapshots."""

from dataclasses import dataclass
from typing import Any

from stormpulse.agent.integrations_runtime import (
    MAX_PENDING_METRICS_PUSHES,
    IntegrationRuntime,
    acknowledge_push,
    build_integrations_payload,
    discard_pending_pushes,
    discard_push,
    merge_items_into_runtime,
    replace_runtime_state,
    track_push,
)
from stormpulse.integrations import Freshness, Integration


@dataclass(frozen=True)
class Item:
    id: str
    value: int


@dataclass(frozen=True)
class MonitorState:
    items: tuple[Item, ...]
    read_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"items": {item.id: item.value for item in self.items}}

    def freshness(self) -> Freshness:
        return Freshness(changed_ids=self.read_ids)

    def with_items(self, items: list[Item]) -> "MonitorState":
        merged = {item.id: item for item in self.items}
        merged.update((item.id, item) for item in items)
        return MonitorState(tuple(merged.values()), tuple(item.id for item in items))


def runtime() -> IntegrationRuntime:
    descriptor = Integration("monitor", lambda config: config, lambda config: True)
    return IntegrationRuntime("monitor", "live", None, {}, descriptor)


def test_late_ack_does_not_clear_a_later_snapshot() -> None:
    rt = runtime()
    runtimes = {rt.id: rt}
    replace_runtime_state(rt, MonitorState((Item("A", 1),), ("A",)))
    track_push(runtimes, "first")
    merge_items_into_runtime(rt, [Item("B", 1)])
    track_push(runtimes, "second")
    assert acknowledge_push(runtimes, "first")
    assert list(rt.changed_revisions) == ["B"]
    assert build_integrations_payload(runtimes)["monitor"]["state"] == {
        "items": {"A": 1, "B": 1}
    }
    assert acknowledge_push(runtimes, "second")
    assert list(rt.changed_revisions) == []


def test_same_resource_read_after_snapshot_stays_dirty() -> None:
    for targeted in (False, True):
        rt = runtime()
        runtimes = {rt.id: rt}
        replace_runtime_state(rt, MonitorState((Item("A", 1),), ("A",)))
        track_push(runtimes, "first")
        if targeted:
            merge_items_into_runtime(rt, [Item("A", 2)])
        else:
            replace_runtime_state(rt, MonitorState((Item("A", 2),), ("A",)))
        acknowledge_push(runtimes, "first")
        assert list(rt.changed_revisions) == ["A"]
        assert rt.state is not None
        assert rt.state.to_dict() == {"items": {"A": 2}}


def test_unknown_duplicate_and_out_of_order_acks_do_not_clear_new_reads() -> None:
    rt = runtime()
    runtimes = {rt.id: rt}
    replace_runtime_state(rt, MonitorState((Item("A", 1),), ("A",)))
    track_push(runtimes, "first")
    track_push(runtimes, "second")
    assert not acknowledge_push(runtimes, "unknown")
    acknowledge_push(runtimes, "second")
    merge_items_into_runtime(rt, [Item("A", 2)])
    assert not acknowledge_push(runtimes, "second")
    acknowledge_push(runtimes, "first")
    assert list(rt.changed_revisions) == ["A"]


def test_failed_send_and_reconnect_keep_unacknowledged_reads() -> None:
    rt = runtime()
    runtimes = {rt.id: rt}
    replace_runtime_state(rt, MonitorState((Item("A", 1),), ("A",)))
    track_push(runtimes, "failed")
    discard_push(runtimes, "failed")
    assert not acknowledge_push(runtimes, "failed")
    track_push(runtimes, "old-session")
    discard_pending_pushes(runtimes)
    track_push(runtimes, "new-session")
    assert not acknowledge_push(runtimes, "old-session")
    assert list(rt.changed_revisions) == ["A"]
    assert acknowledge_push(runtimes, "new-session")
    assert list(rt.changed_revisions) == []


def test_unacknowledged_push_tracking_is_bounded_without_losing_dirty_ids() -> None:
    rt = runtime()
    runtimes = {rt.id: rt}
    replace_runtime_state(rt, MonitorState((Item("A", 1),), ("A",)))
    for index in range(1000):
        track_push(runtimes, str(index))
    assert len(rt.pending_pushes) == MAX_PENDING_METRICS_PUSHES
    assert not acknowledge_push(runtimes, "0")
    assert list(rt.changed_revisions) == ["A"]
    assert acknowledge_push(runtimes, "999")
    assert list(rt.changed_revisions) == []


def test_the_wire_owes_ids_in_first_seen_order_until_the_ack_that_carried_them() -> (
    None
):
    """``changed_ids`` on the wire is the revision keys as the runtime holds them:
    a re-read keeps its first position, the newest read comes last, and an ack
    drops only the ids its push carried. Sorting would lie about the order."""
    rt = runtime()
    runtimes = {rt.id: rt}
    replace_runtime_state(rt, MonitorState((Item("Z", 1), Item("A", 1)), ("Z", "A")))
    track_push(runtimes, "first")
    merge_items_into_runtime(rt, [Item("M", 1), Item("Z", 2)])
    assert build_integrations_payload(runtimes)["monitor"]["changed_ids"] == [
        "Z",
        "A",
        "M",
    ]
    acknowledge_push(runtimes, "first")
    assert build_integrations_payload(runtimes)["monitor"]["changed_ids"] == ["Z", "M"]
