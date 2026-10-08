"""Hint file: another process on this box names bucket ids worth re-reading.

``read_hint`` trusts the file only past seven checks; any refusal yields no ids,
so a bad file can cost a re-read but never steer one.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard

MAX_HINT_BYTES = 64 * 1024
MAX_HINT_AGE_SECONDS = 60.0
MAX_HINT_SKEW_SECONDS = 5.0
HINT_VERSION = 1
# Full ids only: a shorter string would become a Garage prefix search.
_BUCKET_ID = re.compile(r"[0-9a-f]{64}")


class Refusal(StrEnum):
    """Why a hint file was not trusted; the caller logs each once."""

    UNREADABLE = "unreadable"
    SYMLINK = "symlink"
    NOT_REGULAR = "not_regular"
    FOREIGN_OWNER = "foreign_owner"
    TOO_LARGE = "too_large"
    BAD_SCHEMA = "bad_schema"
    STALE = "stale"
    BAD_ID = "bad_id"


@dataclass(frozen=True, slots=True)
class HintRead:
    """Ids to re-read, or the refusal that left none."""

    bucket_ids: frozenset[str] = frozenset()
    refusal: Refusal | None = None
    detail: str = ""


def _refuse(refusal: Refusal, detail: str) -> HintRead:
    return HintRead(refusal=refusal, detail=detail)


def read_hint(
    path: str, *, now: float | None = None, uid: int | None = None
) -> HintRead:
    """Read one hint file; return its ids or the first check it failed.

    Checks: not a symlink, a regular file, owned by ``uid`` (default: ours),
    at most 64 KiB, schema version 1, ``written_at`` within 60 s past / 5 s
    future of ``now``, every id 64 lowercase hex. O_NONBLOCK keeps a FIFO
    from blocking the open.
    """
    now = time.time() if now is None else now
    uid = os.geteuid() if uid is None else uid
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return _refuse(Refusal.SYMLINK, "hint file is a symlink")
        return _refuse(Refusal.UNREADABLE, exc.strerror or str(exc))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _refuse(Refusal.NOT_REGULAR, "hint file is not a regular file")
        if st.st_uid != uid:
            return _refuse(Refusal.FOREIGN_OWNER, f"hint file owned by uid {st.st_uid}")
        # Read one byte past the cap: race-free, unlike trusting st_size.
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_HINT_BYTES + 1)
    except OSError as exc:
        return _refuse(Refusal.UNREADABLE, exc.strerror or str(exc))
    finally:
        os.close(fd)
    if len(raw) > MAX_HINT_BYTES:
        return _refuse(Refusal.TOO_LARGE, f"hint file exceeds {MAX_HINT_BYTES} bytes")
    return _parse(raw, now)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse(raw: bytes, now: float) -> HintRead:
    try:
        doc = json.loads(raw)
    except ValueError:
        return _refuse(Refusal.BAD_SCHEMA, "hint file is not JSON")
    if not isinstance(doc, dict):
        return _refuse(Refusal.BAD_SCHEMA, "hint file is not an object")
    version = doc.get("version")
    written_at = doc.get("written_at")
    ids = doc.get("bucket_ids")
    if not (_is_number(version) and version == HINT_VERSION):
        return _refuse(Refusal.BAD_SCHEMA, f"unsupported version {version!r}")
    if not _is_number(written_at) or not isinstance(ids, list):
        return _refuse(
            Refusal.BAD_SCHEMA, "written_at or bucket_ids missing or mistyped"
        )
    age = now - written_at
    # A NaN fails every comparison, so the bound is written as the range it must be in.
    if not (-MAX_HINT_SKEW_SECONDS <= age <= MAX_HINT_AGE_SECONDS):
        return _refuse(Refusal.STALE, f"written_at is {age:.0f}s old")
    for bucket_id in ids:
        if not (isinstance(bucket_id, str) and _BUCKET_ID.fullmatch(bucket_id)):
            return _refuse(Refusal.BAD_ID, "hint names a non-64-hex bucket id")
    return HintRead(bucket_ids=frozenset(ids))
