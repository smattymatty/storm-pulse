"""Read an integration's spool of event segments onto the events buffer.

An emitter in any language writes a dot-named temp file in its spool directory
and renames it to ``<name>.jsonl``: a ``{"schema": 1}`` header line, then one
event per line. The agent reads a segment only while the buffer has room and
deletes it once none of its events is buffered or in flight."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stormpulse import events
from stormpulse.agent.integrations_runtime import STATUS_LIVE, IntegrationRuntime
from stormpulse.bounded_read import RefusedFile, read_bounded

logger = logging.getLogger(__name__)

SEGMENT_SCHEMA = 1
MAX_SEGMENT_EVENTS = events.MAX_BATCH_EVENTS
MAX_SEGMENT_BYTES = 1024 * 1024
# One record line, so a full batch stays near half a MiB on the wire.
MAX_RECORD_BYTES = 1024
MAX_SPOOL_BYTES = 64 * 1024 * 1024
MAX_SPOOL_AGE_SECONDS = 7 * 24 * 3600
_SEGMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jsonl$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_SCALARS = (str, int, float, bool)

# Published in the artifact (``integration_events.segment``) for emitters.
SEGMENT_FORMAT: dict[str, Any] = {
    "schema": SEGMENT_SCHEMA,
    "encoding": "utf-8 JSON lines",
    "header": {"schema": SEGMENT_SCHEMA},
    "record": {
        "kind": "string, a kind the source declares",
        "ts": "string, optional, RFC 3339 UTC with Z; the agent stamps it when absent",
        "fields": "the kind's declared fields at top level, scalar values only",
    },
    "name_pattern": _SEGMENT_NAME.pattern,
    "write": "write a dot-named temp file in the spool directory, fsync, rename",
    "max_events": MAX_SEGMENT_EVENTS,
    "max_bytes": MAX_SEGMENT_BYTES,
    "max_record_bytes": MAX_RECORD_BYTES,
    "directory": (
        "a real directory, owned by root or the agent, closed to others (no "
        "o+rwx), readable and writable by the agent (under a unit's "
        "ReadWritePaths), fed by one source; otherwise the source is refused "
        "at boot. A segment is owned by the agent, root or the directory's "
        "owner, or carries the directory's group (a setgid directory gives it)"
    ),
    "emitter_bound": {
        "max_spool_bytes": MAX_SPOOL_BYTES,
        "max_age_seconds": MAX_SPOOL_AGE_SECONDS,
        "rule": (
            "the emitter deletes its oldest segments past either bound; the "
            "agent reads only while its integration is live and acks arrive, "
            "so this is the spool's only bound when they do not"
        ),
    },
    "refusals": (
        "an unknown schema is refused whole, each of its events counted; an "
        "oversize, foreign-owned or irregular segment is refused whole and "
        "counted once; an event over max_record_bytes, with an undeclared kind "
        "or field, an envelope field or a non-scalar or non-finite value is "
        "refused; counts land in dropped_events.undeclared"
    ),
    "delete": "after every event it carried is acked, nacked or dropped",
}


class SpoolRefused(Exception):
    """A declared spool directory the agent will not feed from."""


def spool_feeders(
    integrations: Mapping[str, IntegrationRuntime],
) -> dict[str, SpoolFeeder]:
    """One feeder per live Integration declaring an event source. The source is
    declared here with its feeder or not at all; a refusal costs that source."""
    feeders: dict[str, SpoolFeeder] = {}
    for integ_id, runtime in integrations.items():
        declared = runtime.descriptor.event_source
        if runtime.status != STATUS_LIVE or declared is None:
            continue
        try:
            source, dir_stat = _settled_source(declared)
            events.declare_integration_source(source)
        except (SpoolRefused, ValueError, OSError) as exc:
            logger.error("event source %s refused: %s", declared.name, exc)
            continue
        feeders[integ_id] = SpoolFeeder(source, dir_stat)
    return feeders


def _settled_source(
    declared: events.EventSource,
) -> tuple[events.EventSource, os.stat_result]:
    """The source with its spool resolved once, past the directory checks."""
    if os.path.islink(declared.spool_dir):
        raise SpoolRefused(f"spool {declared.spool_dir} is a symlink")
    spool = Path(os.path.realpath(declared.spool_dir))
    st = os.lstat(spool)
    if not stat.S_ISDIR(st.st_mode):
        raise SpoolRefused(f"spool {spool} is not a directory")
    if st.st_mode & stat.S_IRWXO:
        raise SpoolRefused(f"spool {spool} is open to others ({st.st_mode & 0o777:o})")
    if st.st_uid not in (0, os.geteuid()):
        raise SpoolRefused(f"spool {spool} is owned by uid {st.st_uid}")
    if not os.access(spool, os.R_OK | os.W_OK | os.X_OK):
        raise SpoolRefused(f"spool {spool} is not writable by the agent")
    return dataclasses.replace(declared, spool_dir=spool), st


class SpoolFeeder:
    """One declared source's spool. Owned by the agent across sessions, so a
    reconnect never re-reads a segment it already buffered; a restart does,
    and the consumer dedupes on the emitter's own record id."""

    def __init__(self, source: events.EventSource, dir_stat: os.stat_result) -> None:
        self.source = source
        self._dir_uid = dir_stat.st_uid
        self._dir_gid = dir_stat.st_gid
        self._pending: dict[str, range] = {}
        # Settled but not deletable: name -> (inode, mtime_ns), skipped until
        # the file changes, so a stuck segment never ships twice.
        self._stuck: dict[str, tuple[int, int]] = {}

    def tick(self) -> None:
        """Settle segments, then read new ones while the buffer has room."""
        buf = events.buffer()
        for name, seqs in list(self._pending.items()):
            if not buf.holds_any(seqs):
                del self._pending[name]
                self._settle(name)
        for name in self._segments():
            if buf.room() < MAX_SEGMENT_EVENTS:
                return
            read = self._read(self.source.spool_dir / name)
            if read is None:
                continue
            admitted, refused = read
            seqs = buf.extend(admitted, refused=refused)
            if seqs:
                self._pending[name] = seqs
            else:
                self._settle(name)

    def _segments(self) -> list[str]:
        try:
            names = os.listdir(self.source.spool_dir)
        except OSError as exc:
            logger.debug("event spool %s unreadable: %s", self.source.name, exc)
            return []
        listed = {n for n in names if _SEGMENT_NAME.match(n)}
        for name, parked in list(self._stuck.items()):
            if _identity(self.source.spool_dir / name) != parked:
                del self._stuck[name]  # gone or rewritten: read it afresh
        return sorted(listed - self._pending.keys() - self._stuck.keys())

    def _read(self, path: Path) -> tuple[list[dict[str, Any]], int] | None:
        """The segment's admitted events and the count it refused; ``None``
        leaves it for the next tick (unreadable now, perhaps not later)."""
        try:
            raw = read_bounded(path, MAX_SEGMENT_BYTES, self._owner_ok)
        except RefusedFile as exc:
            logger.warning("event segment %s refused: %s", path.name, exc.detail)
            return [], 1
        except OSError as exc:
            logger.warning("event segment %s unreadable: %s", path.name, exc)
            return None
        lines = [line for line in raw.split(b"\n") if line.strip()]
        records = lines[1:]
        if not lines or _header(lines[0]) != SEGMENT_SCHEMA:
            logger.warning("event segment %s refused: unknown schema", path.name)
            return [], len(records)
        if len(records) > MAX_SEGMENT_EVENTS:
            logger.warning(
                "event segment %s refused: over %d events",
                path.name,
                MAX_SEGMENT_EVENTS,
            )
            return [], len(records)
        admitted = [e for e in (self._event(r) for r in records) if e is not None]
        return admitted, len(records) - len(admitted)

    def _owner_ok(self, st: os.stat_result) -> bool:
        trusted = (0, os.geteuid(), self._dir_uid)
        return st.st_uid in trusted or st.st_gid == self._dir_gid

    def _event(self, line: bytes) -> dict[str, Any] | None:
        if len(line) > MAX_RECORD_BYTES:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        kind, ts = record.pop("kind", None), record.pop("ts", None)
        if not isinstance(kind, str) or not _valid_ts(ts):
            return None
        if not all(_scalar(v) for v in record.values()):
            return None
        event = {"ts": ts or events.now_iso(), "source": self.source.name}
        event["kind"] = kind
        return event if events.admit(event, record) else None

    def _settle(self, name: str) -> None:
        """Delete a segment whose events are all settled; one that cannot go is
        parked in ``_stuck`` with one warning, never read again unchanged."""
        path = self.source.spool_dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            return  # already gone: settled either way
        except OSError as exc:
            identity = _identity(path)
            if identity is not None:
                self._stuck[name] = identity
            logger.warning("event segment %s not deleted, parked: %s", name, exc)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


def _valid_ts(ts: object) -> bool:
    return ts is None or (isinstance(ts, str) and _TS.match(ts) is not None)


def _scalar(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, _SCALARS)


def _header(line: bytes) -> object:
    try:
        header = json.loads(line)
    except ValueError:
        return None
    return header.get("schema") if isinstance(header, dict) else None
