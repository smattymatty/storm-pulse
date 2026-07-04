"""Wide-event emission for the events plane.

One structured record per unit of agent work: an admin-API call, a job
result, a reconnect. Events land in a bounded in-process buffer and ship
to the control plane as ``events.batch`` envelopes; the dashboard ack
releases them (the log-shipping pattern), so a connection flap never
loses the events that describe it. A full buffer drops oldest and the
next drain prepends a ``dropped_events`` event - truncation is never
silent.

Foundation-tier on purpose: Features (garage), Framework (commands), and
the Entry loops all emit, so this module sits beside protocol/config
where every layer may import it downward. It holds plain dicts and no
protocol types; the envelope maker lives in ``protocol``.

Ids, never identities: events carry bucket/key/command ids only. No user
ids, no IPs. Nothing is aggregated here - rate and percentiles are
computed at read time, control-plane side, so tomorrow's question is
still answerable from yesterday's data.
"""

from __future__ import annotations

import threading
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

MAX_BUFFERED_EVENTS = 4096
MAX_BATCH_EVENTS = 500
MAX_IN_FLIGHT_BATCHES = 32
_MAX_ERROR_CHARS = 500

# What triggered the work an event describes (periodic_walk, detector,
# job). Set by the owning loop or the job manager; propagates into
# ``asyncio.to_thread`` calls via contextvars, so a garage admin call
# made three frames down still knows why it ran.
trigger_var: ContextVar[str] = ContextVar("stormpulse_event_trigger", default="")

# Which command the work belongs to (the dispatch request id). Set by
# the job manager beside ``trigger_var``, so every admin call a job's
# handler makes carries the ref that stitches the command's whole story
# together at read time. An explicit ``command_ref=`` field wins.
command_ref_var: ContextVar[str] = ContextVar(
    "stormpulse_event_command_ref", default=""
)


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
