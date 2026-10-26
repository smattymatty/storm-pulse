"""In-flight log-batch tracking: batch_id → (receipt, sent_at), with stale-pruning by timeout."""

from __future__ import annotations

import time
from dataclasses import dataclass

from stormpulse.logging.receipt import Receipt


@dataclass(frozen=True, slots=True)
class _Entry:
    receipt: Receipt
    sent_at: float


class PendingBatches:
    """Tracks log batches in flight; uses ``time.monotonic()`` so wall-clock jumps don't perturb pruning."""

    def __init__(self, ack_timeout_seconds: float = 30.0) -> None:
        self._ack_timeout = ack_timeout_seconds
        self._entries: dict[str, _Entry] = {}

    def add(self, batch_id: str, receipt: Receipt) -> None:
        """Record a batch as in flight with the receipt of the read it carries."""
        self._entries[batch_id] = _Entry(receipt, time.monotonic())

    def pop(self, batch_id: str) -> Receipt | None:
        """Resolve an ack. Returns the batch's receipt, or ``None`` if unknown."""
        entry = self._entries.pop(batch_id, None)
        return None if entry is None else entry.receipt

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
            del self._entries[bid]
        return len(stale)

    def __contains__(self, batch_id: object) -> bool:
        return batch_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)
