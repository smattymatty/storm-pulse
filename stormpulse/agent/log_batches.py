"""In-flight log-batch tracking: batch_id → (group, target_position, sent_at), with stale-pruning by timeout."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Entry:
    group: str
    to_position: int | str
    sent_at: float


class PendingBatches:
    """Tracks log batches in flight to the dashboard awaiting ack.

    All time values are ``time.monotonic()`` — wall-clock jumps must not
    cause stale-pruning churn or premature drops.
    """

    def __init__(self, ack_timeout_seconds: float = 30.0) -> None:
        self._ack_timeout = ack_timeout_seconds
        self._entries: dict[str, _Entry] = {}

    def add(self, batch_id: str, group: str, to_position: int | str) -> None:
        """Record a batch as in flight."""
        self._entries[batch_id] = _Entry(group, to_position, time.monotonic())

    def pop(self, batch_id: str) -> tuple[str, int | str] | None:
        """Resolve an ack. Returns ``(group, to_position)`` or ``None`` if unknown."""
        entry = self._entries.pop(batch_id, None)
        if entry is None:
            return None
        return entry.group, entry.to_position

    def prune_stale(self, *, now: float | None = None) -> int:
        """Drop entries whose ack has not arrived within the timeout window.

        Returns the number of entries dropped. The dashboard re-acks fresh
        sends, so a dropped entry simply means the next batch for the same
        group will retransmit the same range — at-least-once, not at-most-once.
        """
        cut = (now if now is not None else time.monotonic()) - self._ack_timeout
        stale = [bid for bid, e in self._entries.items() if e.sent_at < cut]
        for bid in stale:
            del self._entries[bid]
        return len(stale)

    def __contains__(self, batch_id: object) -> bool:
        return batch_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)
