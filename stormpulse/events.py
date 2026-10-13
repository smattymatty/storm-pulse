"""Wide events, buffered in-process, shipped as ``events.batch``, released on ack;
overflow drops oldest and says so in ``dropped_events``. An undeclared field
refuses the whole event. Foundation-tier: plain dicts, no protocol types. The
agent's own sources carry ids, never identities (no user ids, no IPs); an
integration's source carries what its declaration names. ``walk`` folds routine
calls into one ``walk_summary``; failures keep their own event."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import ContextDecorator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_BUFFERED_EVENTS = 4096
MAX_BATCH_EVENTS = 500
MAX_IN_FLIGHT_BATCHES = 32
_MAX_ERROR_CHARS = 500
# Free text from a process or the control plane; capped so it cannot bloat the wire.
_CAPPED_FIELDS = frozenset({"error", "reason"})

_SOURCE = "events"
# Stamped by emit or the buffer on every event, never declared per kind.
ENVELOPE_FIELDS = frozenset({"ts", "source", "kind", "seq", "trigger", "command_ref"})
_WALK_FIELDS = frozenset(
    {"calls", "failures", "slowest_endpoint", "slowest_ms", "total_ms"}
)
_DROPPED_FIELDS = frozenset({"dropped", "undeclared"})
_NACKED_FIELDS = frozenset({"batch_id", "dropped", "reason"})

# (source, kind) -> the fields an event may carry. Declared at import by the
# module that emits, per item by walk(); the artifact publishes it.
_REGISTRY: dict[tuple[str, str], frozenset[str]] = {}
# The same, for sources an integration declared on this host. Kept apart so the
# artifact publishes them outside the contract digest (per host, never pinned).
_INTEGRATION_REGISTRY: dict[tuple[str, str], frozenset[str]] = {}
# Resolved spool directory -> the integration source that feeds from it.
_SPOOL_DIRS: dict[Path, str] = {}


@dataclass(frozen=True, slots=True)
class EventSource:
    """One integration's declared source: its kinds and their fields, and the
    spool directory its emitter writes segments into (``agent.event_spool``)."""

    name: str
    kinds: Mapping[str, frozenset[str]]
    spool_dir: Path


def declare_event(source: str, kind: str, fields: Iterable[str]) -> None:
    """Declare the shape of one (source, kind); a second call adds fields.
    An event carrying any other field is refused whole and counted as
    ``undeclared`` beside ``dropped``; the buffer stamps ``seq`` (CORE-011 d6)."""
    key = (source, kind)
    _REGISTRY[key] = _REGISTRY.get(key, frozenset()) | frozenset(fields)


def declare_integration_source(source: EventSource) -> None:
    """Admit an integration's declared source; a name or spool directory any
    source already holds is refused, so none borrows another's shape or files."""
    taken = {name for name, _kind in _REGISTRY} | {
        name for name, _kind in _INTEGRATION_REGISTRY
    }
    if source.name in taken:
        raise ValueError(f"event source {source.name!r} is already declared")
    if source.spool_dir in _SPOOL_DIRS:
        holder = _SPOOL_DIRS[source.spool_dir]
        raise ValueError(f"spool {source.spool_dir} is already fed by {holder!r}")
    _SPOOL_DIRS[source.spool_dir] = source.name
    for kind, fields in source.kinds.items():
        _INTEGRATION_REGISTRY[(source.name, kind)] = frozenset(fields)


def declared_events() -> dict[str, dict[str, list[str]]]:
    """The registry as ``{source: {kind: [fields]}}``, sorted, for the artifact."""
    return _as_artifact(_REGISTRY)


def declared_integration_events() -> dict[str, dict[str, list[str]]]:
    """This host's integration sources, shaped like ``declared_events``."""
    return _as_artifact(_INTEGRATION_REGISTRY)


def _as_artifact(
    registry: Mapping[tuple[str, str], frozenset[str]],
) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for (source, kind), fields in sorted(registry.items()):
        out.setdefault(source, {})[kind] = sorted(fields)
    return out


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


def walk(*, source: str, item: str) -> _WalkScope:
    """Fold every ``record_call`` inside into one ``walk_summary`` on exit.

    A success carrying ``<item>_id`` counts as ``<item>s_read``. The summary
    emits when the walk read, dropped (``record_dropped``) or failed; a walk
    that only made calls (a quiet membership list, a topology-only tick)
    emits nothing. Its shape is declared here, per item, for the artifact.
    """
    declare_event(
        source, "walk_summary", _WALK_FIELDS | {f"{item}s_read", f"{item}s_dropped"}
    )
    return _WalkScope(source, item)


class _WalkScope(ContextDecorator):
    """One ``walk``, as a ``with`` block or a decorator (a fresh scope per call).
    A nested scope joins the outer one and emits nothing of its own."""

    def __init__(self, source: str, item: str) -> None:
        self._source = source
        self._item = item
        self._totals: _Walk | None = None
        self._token: Token[_Walk | None] | None = None
        self._start = 0.0

    def _recreate_cm(self) -> _WalkScope:
        return _WalkScope(self._source, self._item)

    def __enter__(self) -> None:
        if _walk_var.get() is not None:
            return
        self._totals = _Walk(item=self._item)
        self._token = _walk_var.set(self._totals)
        self._start = time.monotonic()

    def __exit__(self, *exc: object) -> None:
        totals, token = self._totals, self._token
        if totals is None or token is None:
            return
        _walk_var.reset(token)
        self._totals = self._token = None
        if totals.items_read or totals.dropped or totals.failures:
            emit(
                "walk_summary",
                source=self._source,
                calls=totals.calls,
                failures=totals.failures,
                slowest_endpoint=totals.slowest_endpoint,
                slowest_ms=totals.slowest_ms,
                total_ms=int((time.monotonic() - self._start) * 1000.0),
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

    ``append`` stamps ``seq``, the one place it is assigned, so the synthesized
    ``dropped_events`` carries one too and a requeued batch keeps its own.
    ``drain`` moves events into an in-flight slot keyed by batch id; ``ack``
    discards them; ``requeue_unacked`` puts every in-flight batch back in front.
    """

    def __init__(self, max_events: int = MAX_BUFFERED_EVENTS) -> None:
        self._lock = threading.Lock()
        self._buf: deque[dict[str, Any]] = deque()
        self._max = max_events
        self._seq = 0
        self._dropped = 0
        self._undeclared = 0
        self._in_flight: dict[str, list[dict[str, Any]]] = {}

    def _stamp(self, event: dict[str, Any]) -> dict[str, Any]:
        """Assign the next ``seq``; the caller holds the lock."""
        self._seq += 1
        event["seq"] = self._seq
        return event

    def append(self, event: dict[str, Any]) -> None:
        """Buffer one event, dropping the oldest when full."""
        self.extend([event])

    def extend(self, batch: list[dict[str, Any]], *, refused: int = 0) -> range:
        """Buffer *batch* in order under one lock, counting *refused* events as
        ``undeclared``; returns the seqs stamped, contiguous by construction."""
        with self._lock:
            first = self._seq + 1
            self._undeclared += refused
            for event in batch:
                self._stamp(event)
                if len(self._buf) >= self._max:
                    self._buf.popleft()
                    self._dropped += 1
                self._buf.append(event)
            return range(first, self._seq + 1)

    def refuse(self) -> None:
        """Count one event refused for an undeclared shape (CORE-011 d6)."""
        with self._lock:
            self._undeclared += 1

    def room(self) -> int:
        """Events the buffer takes before it starts dropping the oldest."""
        with self._lock:
            return self._max - len(self._buf)

    def holds_any(self, seqs: range) -> bool:
        """Whether any event in *seqs* is still buffered or in flight, i.e.
        not yet acked, nacked or dropped."""
        with self._lock:
            held = (e for batch in self._in_flight.values() for e in batch)
            return any(e.get("seq") in seqs for e in (*self._buf, *held))

    def drain(
        self, batch_id: str, max_events: int = MAX_BATCH_EVENTS
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self._dropped or self._undeclared:
                self._buf.appendleft(
                    self._stamp(
                        {
                            "ts": now_iso(),
                            "source": _SOURCE,
                            "kind": "dropped_events",
                            "dropped": self._dropped,
                            "undeclared": self._undeclared,
                        }
                    )
                )
                self._dropped = 0
                self._undeclared = 0
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

    def nack(self, batch_id: str) -> int | None:
        """Drop a refused in-flight batch; the count dropped, ``None`` if unknown.

        Never requeued: a batch the control plane refused would be refused
        again. The loss rides on ``events_nacked``, not the ``dropped`` tally.
        """
        with self._lock:
            batch = self._in_flight.pop(batch_id, None)
            return None if batch is None else len(batch)

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
declare_event(_SOURCE, "dropped_events", _DROPPED_FIELDS)
declare_event(_SOURCE, "events_nacked", _NACKED_FIELDS)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def emit(kind: str, *, source: str, **fields: Any) -> None:
    """Record one wide event. Cheap, non-blocking, never touches the network.

    ``None`` and empty-string fields are elided; an event carrying a field its
    (source, kind) never declared is refused whole and counted, so a forbidden
    identifier cannot reach the wire; ``error`` and ``reason`` text is capped at
    ``_MAX_ERROR_CHARS`` so stderr cannot bloat it."""
    event: dict[str, Any] = {"ts": now_iso(), "source": source, "kind": kind}
    trigger = trigger_var.get()
    if trigger:
        event["trigger"] = trigger
    command_ref = fields.pop("command_ref", "") or command_ref_var.get()
    if command_ref:
        event["command_ref"] = command_ref
    if admit(event, fields):
        _BUFFER.append(event)
    else:
        _BUFFER.refuse()


def admit(event: dict[str, Any], fields: Mapping[str, Any]) -> bool:
    """Copy *fields* into *event* if its (source, kind) declares every one;
    ``False``, leaving the event to be refused, if any is undeclared."""
    key = (event["source"], event["kind"])
    declared = _REGISTRY.get(key)
    if declared is None:
        declared = _INTEGRATION_REGISTRY.get(key)
    if declared is None:
        return False
    kept: dict[str, Any] = {}
    for name, value in fields.items():
        if value is None or value == "":
            continue
        if name not in declared:
            return False
        kept[name] = str(value)[:_MAX_ERROR_CHARS] if name in _CAPPED_FIELDS else value
    event.update(kept)
    return True


def buffer() -> EventBuffer:
    """The process-lifetime event buffer (one per agent, like the admin meter)."""
    return _BUFFER
