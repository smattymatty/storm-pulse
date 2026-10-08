"""Wide-event emission: one record per unit of agent work, buffered in-process
and shipped as ``events.batch``, released on the dashboard's ack. A full buffer
drops oldest and the next drain says so with ``dropped_events``.

Foundation-tier: every layer imports it downward; plain dicts, no protocol types.
Ids, never identities: bucket/key/command ids, no user ids, no IPs. One aggregate:
an open ``walk`` folds routine calls into one ``walk_summary``; every failure
keeps its own event. Rate and percentiles stay read-time, control-plane side.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MAX_BUFFERED_EVENTS = 4096
MAX_BATCH_EVENTS = 500
MAX_IN_FLIGHT_BATCHES = 32
_MAX_ERROR_CHARS = 500

# What triggered the work an event describes (periodic_walk, job). Set
# by the owning loop or the job manager; propagates into ``to_thread``
# calls via contextvars, so a call three frames down knows why it ran.
trigger_var: ContextVar[str] = ContextVar("stormpulse_event_trigger", default="")

# Which command the work belongs to (the dispatch request id). Set by
# the job manager beside ``trigger_var``, so every admin call a job's
# handler makes carries the ref that stitches the command's whole story
# together at read time. An explicit ``command_ref=`` field wins.
command_ref_var: ContextVar[str] = ContextVar(
    "stormpulse_event_command_ref", default=""
)


@dataclass(slots=True)
class _Walk:
    """Running totals for one open walk; one thread, so no lock."""

    item: str
    calls: int = 0
    items_read: int = 0
    dropped: int = 0
    failures: int = 0
    slowest_ms: int = 0
    slowest_endpoint: str = ""


# The open walk, if any. A nested ``walk`` joins the outer one, so a
# collect that calls a targeted read still emits one summary.
_walk_var: ContextVar[_Walk | None] = ContextVar("stormpulse_event_walk", default=None)


def is_failure(status: int | None) -> bool:
    """A call failed when it got no status or a status >= 400 (a 404 counts)."""
    return status is None or status >= 400


@contextmanager
def walk(*, source: str, item: str) -> Iterator[None]:
    """Fold every ``record_call`` inside into one ``walk_summary`` on exit.

    A success carrying ``<item>_id`` counts as ``<item>s_read``. The summary
    emits when the walk read, dropped (``record_dropped``) or failed; a walk
    that only made calls (a quiet membership list, a topology-only tick)
    emits nothing. Failures are counted and still emitted on their own.
    """
    if _walk_var.get() is not None:
        yield
        return
    totals = _Walk(item=item)
    token = _walk_var.set(totals)
    start = time.monotonic()
    try:
        yield
    finally:
        _walk_var.reset(token)
        if totals.items_read or totals.dropped or totals.failures:
            emit(
                "walk_summary",
                source=source,
                calls=totals.calls,
                failures=totals.failures,
                slowest_endpoint=totals.slowest_endpoint,
                slowest_ms=totals.slowest_ms,
                total_ms=int((time.monotonic() - start) * 1000.0),
                **{
                    f"{totals.item}s_read": totals.items_read,
                    f"{totals.item}s_dropped": totals.dropped,
                },
            )


def record_call(
    kind: str,
    *,
    source: str,
    endpoint: str,
    duration_ms: int,
    status: int | None,
    **fields: Any,
) -> None:
    """Record one call: inside a walk a success folds into its summary;
    a failure, or any call outside a walk, emits its own ``kind`` event."""
    failed = is_failure(status)
    totals = _walk_var.get()
    if totals is not None:
        totals.calls += 1
        if failed:
            totals.failures += 1
        elif fields.get(f"{totals.item}_id"):
            totals.items_read += 1
        if duration_ms >= totals.slowest_ms:
            totals.slowest_ms = duration_ms
            totals.slowest_endpoint = endpoint
        if not failed:
            return
    emit(
        kind,
        source=source,
        endpoint=endpoint,
        duration_ms=duration_ms,
        status=status,
        **fields,
    )


def record_dropped(count: int) -> None:
    """Note *count* items the open walk dropped from its cache: no call is
    made for a drop, so this is how one reaches the summary. Outside a walk
    there is no summary to carry it."""
    totals = _walk_var.get()
    if totals is not None:
        totals.dropped += count


class EventBuffer:
    """Bounded, thread-safe event buffer with in-flight ack tracking.

    ``drain`` moves events into an in-flight slot keyed by batch id;
    ``ack`` discards them; ``requeue_unacked`` puts every in-flight batch
    back at the front (called when a fresh session starts, so batches
    that died with the previous connection are re-shipped).
    """

    def __init__(self, max_events: int = MAX_BUFFERED_EVENTS) -> None:
        self._lock = threading.Lock()
        self._buf: deque[dict[str, Any]] = deque()
        self._max = max_events
        self._dropped = 0
        self._in_flight: dict[str, list[dict[str, Any]]] = {}

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            if len(self._buf) >= self._max:
                self._buf.popleft()
                self._dropped += 1
            self._buf.append(event)

    def drain(
        self, batch_id: str, max_events: int = MAX_BATCH_EVENTS
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self._dropped:
                self._buf.appendleft(
                    {
                        "ts": _now_iso(),
                        "source": "events",
                        "kind": "dropped_events",
                        "dropped": self._dropped,
                    }
                )
                self._dropped = 0
            out: list[dict[str, Any]] = []
            while self._buf and len(out) < max_events:
                out.append(self._buf.popleft())
            if out:
                self._in_flight[batch_id] = out
                # A control plane that never acks (old website, wrong deploy
                # order) must not grow agent memory without bound within one
                # session: evict the oldest in-flight batch past the cap and
                # count its events as dropped, so the loss is on the record.
                while len(self._in_flight) > MAX_IN_FLIGHT_BATCHES:
                    oldest = next(iter(self._in_flight))
                    self._dropped += len(self._in_flight.pop(oldest))
            return out

    def ack(self, batch_id: str) -> bool:
        with self._lock:
            return self._in_flight.pop(batch_id, None) is not None

    def requeue_unacked(self) -> int:
        """Put every in-flight batch back at the buffer front, oldest first.

        The bound is re-enforced afterwards, dropping oldest, so a long
        outage cannot grow the buffer without limit.
        """
        with self._lock:
            requeued = 0
            for batch in self._in_flight.values():
                for event in reversed(batch):
                    self._buf.appendleft(event)
                    requeued += 1
            self._in_flight.clear()
            while len(self._buf) > self._max:
                self._buf.popleft()
                self._dropped += 1
            return requeued

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


_BUFFER = EventBuffer()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def emit(kind: str, *, source: str, **fields: Any) -> None:
    """Record one wide event. Cheap, non-blocking, never touches the network.

    ``None`` and empty-string fields are elided so events stay sparse;
    ``error`` text is capped at ``_MAX_ERROR_CHARS`` so a runaway stderr
    cannot bloat the wire.
    """
    event: dict[str, Any] = {
        "ts": _now_iso(),
        "source": source,
        "kind": kind,
    }
    trigger = trigger_var.get()
    if trigger:
        event["trigger"] = trigger
    command_ref = command_ref_var.get()
    if command_ref and not fields.get("command_ref"):
        event["command_ref"] = command_ref
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if key == "error":
            value = str(value)[:_MAX_ERROR_CHARS]
        event[key] = value
    _BUFFER.append(event)


def buffer() -> EventBuffer:
    """The process-lifetime event buffer (one per agent, like the admin meter)."""
    return _BUFFER
