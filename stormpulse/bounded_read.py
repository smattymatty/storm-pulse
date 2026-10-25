"""Read a small file another process wrote, past the checks that keep it from
steering the reader: never through a symlink, a regular file, an owner the
caller trusts, and at most ``cap`` bytes (one byte past the cap, race-free)."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from enum import StrEnum


class Refusal(StrEnum):
    """Why a file was not read; values match the callers' own refusal names."""

    SYMLINK = "symlink"
    NOT_REGULAR = "not_regular"
    FOREIGN_OWNER = "foreign_owner"
    TOO_LARGE = "too_large"


class RefusedFile(Exception):
    """The file failed a check; ``detail`` reads after the file's name, ``st``
    is the refused file's fstat (``None`` for a symlink, never opened)."""

    def __init__(
        self, refusal: Refusal, detail: str, st: os.stat_result | None = None
    ) -> None:
        super().__init__(detail)
        self.refusal = refusal
        self.detail = detail
        self.st = st


def read_bounded(
    path: str | os.PathLike[str],
    cap: int,
    owner_ok: Callable[[os.stat_result], bool],
) -> tuple[bytes, os.stat_result]:
    """The file's bytes and the fstat of the file they came from; ``RefusedFile``
    on a failed check, ``OSError`` when unreadable. O_NONBLOCK keeps a FIFO
    from blocking the open."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RefusedFile(Refusal.SYMLINK, "is a symlink") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RefusedFile(Refusal.NOT_REGULAR, "is not a regular file", st)
        if not owner_ok(st):
            raise RefusedFile(Refusal.FOREIGN_OWNER, f"owned by uid {st.st_uid}", st)
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(cap + 1)  # skylos: ignore[SKY-P401] bounded by cap
    finally:
        os.close(fd)
    if len(raw) > cap:
        raise RefusedFile(Refusal.TOO_LARGE, f"exceeds {cap} bytes", st)
    return raw, st
