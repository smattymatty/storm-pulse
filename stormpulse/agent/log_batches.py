"""In-flight log-batch tracking: batch_id → (receipt, sent_at), with stale-pruning by timeout."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from stormpulse.logging.receipt import Receipt


@dataclass(frozen=True, slots=True)
class _Entry:
    receipt: Receipt
    sent_at: float


class PendingBatches:
    """Tracks log batches in flight; uses ``time.monotonic()`` so wall-clock jumps don't perturb pruning.
    Loop-thread only: a worker holds ``receipts()`` snapshots, never the tracker."""

    def __init__(self, ack_timeout_seconds: float = 30.0) -> None:
        self._ack_timeout = ack_timeout_seconds
        self._entries: dict[str, _Entry] = {}
        # Per group: set when a batch leaves the tracker (ACKed or pruned), so a
        # loop waiting to read again wakes on the ACK, not on the interval.
        self._released: dict[str, asyncio.Event] = {}

    def _release(self, group: str) -> asyncio.Event:
        return self._released.setdefault(group, asyncio.Event())

    def add(self, batch_id: str, receipt: Receipt) -> None:
        """Record a batch as in flight with the receipt of the read it carries."""
        self._entries[batch_id] = _Entry(receipt, time.monotonic())
        self._release(receipt.group).clear()

    def pop(self, batch_id: str, *, release: bool = True) -> Receipt | None:
        """Resolve an ack. Returns the batch's receipt, or ``None`` if unknown.
        ``release`` False (a refused confirm) leaves the group's waiter to its
        interval: the checkpoint did not move, so a re-read at ACK pace would spin."""
        entry = self._entries.pop(batch_id, None)
        if entry is None:
            return None
        if release:
            self._release(entry.receipt.group).set()
        return entry.receipt

    def in_flight(self, group: str) -> bool:
        """Whether ``group`` has a batch awaiting its ACK."""
        return any(e.receipt.group == group for e in self._entries.values())

    async def wait_released(self, group: str) -> None:
        """Wait until a batch of ``group`` is ACKed or pruned; the caller bounds it."""
        await self._release(group).wait()

    def peek(self, batch_id: str) -> Receipt | None:
        """The batch's receipt, still pending: confirm it, then ``pop``, so no read sees it lost."""
        entry = self._entries.get(batch_id)
        return None if entry is None else entry.receipt

    def receipts(self) -> tuple[Receipt, ...]:
        """Every receipt still awaiting its ACK, as a snapshot a worker thread may hold."""
        return tuple(e.receipt for e in self._entries.values())

    def prune_stale(self, *, now: float | None = None) -> int:
        """Drop entries past the timeout; returns count. Dropped batches retransmit (at-least-once)."""
        cut = (now if now is not None else time.monotonic()) - self._ack_timeout
        stale = [bid for bid, e in self._entries.items() if e.sent_at < cut]
        for bid in stale:
            self._release(self._entries.pop(bid).receipt.group).set()
        return len(stale)

    def __contains__(self, batch_id: object) -> bool:
        return batch_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)
