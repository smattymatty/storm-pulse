"""Hint file: another process on this box names bucket ids worth re-reading.

``read_hint`` trusts the file only past seven checks; any refusal yields no ids,
so a bad file can cost a re-read but never steer one.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard

from stormpulse.bounded_read import RefusedFile, read_bounded

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

    bucket_ids: tuple[str, ...] = ()  # the writer's order: most recent first
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
    raw = _read_bounded(path, os.geteuid() if uid is None else uid)
    if isinstance(raw, HintRead):
        return raw
    return _parse(raw, time.time() if now is None else now)


def _read_bounded(path: str, uid: int) -> bytes | HintRead:
    """The file's bytes past the first four checks, or the refusal."""
    try:
        raw, _ = read_bounded(path, MAX_HINT_BYTES, lambda st: st.st_uid == uid)
        return raw
    except RefusedFile as exc:
        return _refuse(Refusal(exc.refusal), f"hint file {exc.detail}")
    except OSError as exc:
        return _refuse(Refusal.UNREADABLE, exc.strerror or str(exc))


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load(raw: bytes) -> dict[str, object] | HintRead:
    try:
        doc = json.loads(raw)
    except ValueError:
        return _refuse(Refusal.BAD_SCHEMA, "hint file is not JSON")
    if isinstance(doc, dict):
        return doc
    return _refuse(Refusal.BAD_SCHEMA, "hint file is not an object")


def _fields(raw: bytes) -> tuple[float, list[object]] | HintRead:
    """``written_at`` and ``bucket_ids`` from a version-1 document, or the refusal."""
    doc = _load(raw)
    if isinstance(doc, HintRead):
        return doc
    version = doc.get("version")
    if not (_is_number(version) and version == HINT_VERSION):
        return _refuse(Refusal.BAD_SCHEMA, f"unsupported version {version!r}")
    written_at, ids = doc.get("written_at"), doc.get("bucket_ids")
    if not _is_number(written_at) or not isinstance(ids, list):
        return _refuse(
            Refusal.BAD_SCHEMA, "written_at or bucket_ids missing or mistyped"
        )
    return written_at, ids


def _parse(raw: bytes, now: float) -> HintRead:
    fields = _fields(raw)
    if isinstance(fields, HintRead):
        return fields
    written_at, ids = fields
    age = now - written_at
    # A NaN fails every comparison, so the bound is written as the range it must be in.
    if not (-MAX_HINT_SKEW_SECONDS <= age <= MAX_HINT_AGE_SECONDS):
        return _refuse(Refusal.STALE, f"written_at is {age:.0f}s old")
    if not all(isinstance(i, str) and _BUCKET_ID.fullmatch(i) for i in ids):
        return _refuse(Refusal.BAD_ID, "hint names a non-64-hex bucket id")
    return HintRead(bucket_ids=tuple(dict.fromkeys(str(i) for i in ids)))
