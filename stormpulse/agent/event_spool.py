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
_ASIDE = ".settling."  # settling's private name for a segment it holds
_SEGMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jsonl$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_SCALARS = (str, int, float, bool)
# The file a segment was read from: (inode, mtime_ns) of its own fstat.
Identity = tuple[int, int]

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
        raise _refused(declared.spool_dir, "is a symlink")
    spool = Path(os.path.realpath(declared.spool_dir))
    st = os.lstat(spool)
    if not stat.S_ISDIR(st.st_mode):
        raise _refused(spool, "is not a directory")
    if st.st_mode & stat.S_IRWXO:
        raise _refused(spool, f"is open to others ({st.st_mode & 0o777:o})")
    if st.st_uid not in (0, os.geteuid()):
        raise _refused(spool, f"is owned by uid {st.st_uid}")
    if not os.access(spool, os.R_OK | os.W_OK | os.X_OK):
        raise _refused(spool, "is not writable by the agent")
    return dataclasses.replace(declared, spool_dir=spool), st


def _refused(spool: object, problem: str) -> SpoolRefused:
    return SpoolRefused(f"spool {spool} {problem}")


class SpoolFeeder:
    """One declared source's spool. Owned by the agent across sessions, so a
    reconnect never re-reads a segment it already buffered; a restart does,
    and the consumer dedupes on the emitter's own record id."""

    def __init__(self, source: events.EventSource, dir_stat: os.stat_result) -> None:
        self.source = source
        self._dir_uid = dir_stat.st_uid
        self._dir_gid = dir_stat.st_gid
        # name -> (the file read, its event seqs); settling acts on that file.
        self._pending: dict[str, tuple[Identity | None, range]] = {}
        # Settled but not deletable: name -> the file read, skipped until the
        # name holds another file, so a stuck segment never ships twice.
        self._stuck: dict[str, Identity] = {}
        self._recover_asides()

    def _recover_asides(self) -> None:
        """A crash mid-settle strands a file aside; it goes back under its name
        before anything is read (a re-read is the consumer's dedupe to absorb)."""
        spool = self.source.spool_dir
        try:
            names = os.listdir(spool)
        except OSError:
            return
        for aside in names:
            name = aside.removeprefix(_ASIDE)
            if name != aside and _SEGMENT_NAME.match(name):
                self._put_back(spool / aside, spool / name)

    def tick(self) -> None:
        """Settle segments, then read new ones while the buffer has room."""
        buf = events.buffer()
        for name, (read_id, seqs) in list(self._pending.items()):
            if not buf.holds_any(seqs):
                del self._pending[name]
                self._settle(name, read_id)
        for name in self._segments():
            if buf.room() < MAX_SEGMENT_EVENTS:
                return
            read = self._read(self.source.spool_dir / name)
            if read is None:
                continue
            read_id, admitted, refused = read
            seqs = buf.extend(admitted, refused=refused)
            if seqs:
                self._pending[name] = (read_id, seqs)
            else:
                self._settle(name, read_id)

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

    def _read(
        self, path: Path
    ) -> tuple[Identity | None, list[dict[str, Any]], int] | None:
        """The file read, its admitted events and the count it refused; ``None``
        leaves it for the next tick (unreadable now, perhaps not later)."""
        try:
            raw, st = read_bounded(path, MAX_SEGMENT_BYTES, self._owner_ok)
        except RefusedFile as exc:
            logger.warning("event segment %s refused: %s", path.name, exc.detail)
            # A symlink is never opened: its lstat is all there is.
            st_id = _stat_id(exc.st) if exc.st else _identity(path)
            return st_id, [], 1
        except OSError as exc:
            logger.warning("event segment %s unreadable: %s", path.name, exc)
            return None
        return (_stat_id(st), *self._parse(path.name, raw))

    def _parse(self, name: str, raw: bytes) -> tuple[list[dict[str, Any]], int]:
        lines = [line for line in raw.split(b"\n") if line.strip()]
        records = lines[1:]
        problem = _segment_problem(lines)
        if problem is not None:
            logger.warning("event segment %s refused: %s", name, problem)
            return [], len(records)
        admitted = [e for e in (self._event(r) for r in records) if e is not None]
        return admitted, len(records) - len(admitted)

    def _owner_ok(self, st: os.stat_result) -> bool:
        trusted = (0, os.geteuid(), self._dir_uid)
        return st.st_uid in trusted or st.st_gid == self._dir_gid

    def _event(self, line: bytes) -> dict[str, Any] | None:
        record = _record(line)
        if record is None:
            return None
        kind, ts = record.pop("kind", None), record.pop("ts", None)
        if not isinstance(kind, str) or not _valid_ts(ts):
            return None
        if not all(_scalar(v) for v in record.values()):
            return None
        event = {"ts": ts or events.now_iso(), "source": self.source.name}
        event["kind"] = kind
        return event if events.admit(event, record) else None

    def _settle(self, name: str, read_id: Identity | None) -> None:
        """Delete the file ``read_id`` names once its events are settled. It is
        renamed aside first, so a replacement renamed in at any moment is put
        back, never deleted; one that cannot go is parked in ``_stuck``."""
        path = self.source.spool_dir / name
        aside = self.source.spool_dir / f"{_ASIDE}{name}"
        if not self._put_back(aside, path):  # never rename over an earlier aside
            self._park(name, read_id, f"{aside.name} is in the way")
            return
        # TODO(0.8.2): a directory at this name is renamed aside and cannot be
        # linked back (EPERM), so later segments at the name ship once but are
        # never deleted. Park anything not a regular file without renaming it.
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            return  # already gone: settled either way
        except OSError as exc:
            self._park(name, read_id, exc)
            return
        if read_id is not None and _identity(aside) == read_id:
            try:
                aside.unlink()
            except OSError as exc:
                self._park(name, read_id, exc)
                self._put_back(aside, path)
            return
        self._put_back(aside, path)

    def _put_back(self, aside: Path, path: Path) -> bool:
        """Return a file to its name without overwriting what took the name
        since; that newer file wins, as the emitter's own rename would have.
        True once nothing is left aside."""
        try:
            os.link(aside, path)
        except FileNotFoundError:
            return True  # nothing aside
        except FileExistsError:
            pass
        except OSError as exc:
            logger.warning("event segment %s left at %s: %s", path.name, aside, exc)
            return False
        try:
            aside.unlink()
        except OSError as exc:
            logger.warning("event segment %s also at %s: %s", path.name, aside, exc)
            return False
        return True

    def _park(self, name: str, read_id: Identity | None, why: object) -> None:
        if read_id is not None:
            self._stuck[name] = read_id
        logger.warning("event segment %s not deleted, parked: %s", name, why)


def _identity(path: Path) -> Identity | None:
    """Whatever the name holds now; compared against a read file's identity."""
    try:
        return _stat_id(os.lstat(path))
    except OSError:
        return None


def _stat_id(st: os.stat_result) -> Identity:
    return st.st_ino, st.st_mtime_ns


def _valid_ts(ts: object) -> bool:
    return ts is None or (isinstance(ts, str) and _TS.match(ts) is not None)


def _scalar(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, _SCALARS)


def _record(line: bytes) -> dict[str, Any] | None:
    if len(line) > MAX_RECORD_BYTES:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _segment_problem(lines: list[bytes]) -> str | None:
    """Why a segment is refused whole, or ``None`` to read its records."""
    if not lines or not _header_ok(lines[0]):
        return "unknown schema"
    if len(lines) - 1 > MAX_SEGMENT_EVENTS:
        return f"over {MAX_SEGMENT_EVENTS} events"
    return None


def _header_ok(line: bytes) -> bool:
    """Schema 1 only as a JSON integer: ``true`` and ``1.0`` equal 1 in Python."""
    try:
        header = json.loads(line)
    except ValueError:
        return False
    schema = header.get("schema") if isinstance(header, dict) else None
    return type(schema) is int and schema == SEGMENT_SCHEMA
