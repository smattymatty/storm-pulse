"""Tests for the in-flight log-batch tracker."""

from __future__ import annotations

import asyncio
import time

import pytest

from stormpulse.agent.log_batches import PendingBatches
from stormpulse.logging.receipt import Receipt


def _receipt(group: str, to: int | str) -> Receipt:
    return Receipt(group, "src", None, 0, to)


def test_add_and_pop_round_trip() -> None:
    tracker = PendingBatches()
    receipt = _receipt("syslog", 4242)
    tracker.add("b1", receipt)
    assert "b1" in tracker

    popped = tracker.pop("b1")
    assert popped == _receipt("syslog", 4242)
    assert "b1" not in tracker
    assert tracker.pop("b1") is None


def test_pop_unknown_returns_none() -> None:
    tracker = PendingBatches()
    assert tracker.pop("never-added") is None


def test_prune_stale_drops_entries_past_timeout() -> None:
    tracker = PendingBatches(ack_timeout_seconds=10.0)
    tracker.add("old", _receipt("g", 1))
    tracker.add("fresh", _receipt("g", 2))

    # Pretend we're checking 20 s in the future - the entries were added at
    # current monotonic, so both are 20 s old then. The fresh entry is
    # within the second prune window we set up below.
    now = time.monotonic() + 20
    dropped = tracker.prune_stale(now=now)
    assert dropped == 2
    assert len(tracker) == 0


def test_prune_stale_keeps_recent_entries() -> None:
    tracker = PendingBatches(ack_timeout_seconds=30.0)
    tracker.add("recent", _receipt("g", 1))

    # Five seconds later is well within the 30 s window.
    dropped = tracker.prune_stale(now=time.monotonic() + 5)
    assert dropped == 0
    assert "recent" in tracker


def test_string_positions_supported() -> None:
    tracker = PendingBatches()
    tracker.add("b1", _receipt("docker", "abc-123"))
    assert tracker.pop("b1") == _receipt("docker", "abc-123")


def test_receipts_snapshots_what_is_still_in_flight() -> None:
    tracker = PendingBatches()
    first, second = _receipt("g", 1), _receipt("g", 2)
    tracker.add("b1", first)
    tracker.add("b2", second)
    tracker.pop("b1")
    assert tracker.receipts() == (second,)


def test_in_flight_names_the_group_until_its_batch_is_popped() -> None:
    tracker = PendingBatches()
    tracker.add("b1", _receipt("guard", 7))
    assert tracker.in_flight("guard")
    assert not tracker.in_flight("caddy")
    tracker.pop("b1")
    assert not tracker.in_flight("guard")


@pytest.mark.asyncio
async def test_a_pop_wakes_a_waiter_on_that_group_only() -> None:
    """The production order: the loop waits first, the ACK lands later."""
    tracker = PendingBatches()
    tracker.add("b1", _receipt("guard", 7))
    tracker.add("b2", _receipt("caddy", 7))
    waiter = asyncio.ensure_future(tracker.wait_released("guard"))
    await asyncio.sleep(0.02)
    tracker.pop("b2")
    await asyncio.sleep(0.02)
    assert not waiter.done(), "another group's ACK must not wake this one"
    tracker.pop("b1")
    await asyncio.wait_for(waiter, 0.05)


@pytest.mark.asyncio
async def test_a_refused_confirm_pops_without_waking_the_waiter() -> None:
    """A checkpoint that did not move re-reads on the interval, not at ACK pace."""
    tracker = PendingBatches()
    tracker.add("b1", _receipt("guard", 7))
    waiter = asyncio.ensure_future(tracker.wait_released("guard"))
    assert tracker.pop("b1", release=False) == _receipt("guard", 7)
    assert not tracker.in_flight("guard")
    await asyncio.sleep(0.02)
    assert not waiter.done()
    waiter.cancel()


@pytest.mark.asyncio
async def test_a_prune_wakes_the_waiter_too() -> None:
    tracker = PendingBatches(ack_timeout_seconds=0.0)
    tracker.add("b1", _receipt("guard", 7))
    waiter = asyncio.ensure_future(tracker.wait_released("guard"))
    await asyncio.sleep(0.02)
    assert tracker.prune_stale(now=time.monotonic() + 1) == 1
    await asyncio.wait_for(waiter, 0.05)
