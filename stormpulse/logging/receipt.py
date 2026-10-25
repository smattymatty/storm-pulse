"""What one read covered, kept beside its batch until the ACK (STAB-02).

Internal only: the wire carries the positions, never the source identity.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileGeneration:
    """One incarnation of a log file: its inode plus a digest of its head.

    Inode alone misses truncation in place (logrotate ``copytruncate``), which
    keeps the inode; rewritten content changes the head.
    """

    inode: int
    head_len: int
    head_digest: bytes


@dataclass(frozen=True, slots=True)
class Receipt:
    """A read's claim: group, source, generation (files only) and the range read.

    The tailer that confirms it refuses any receipt whose group, source or
    generation is not its own current one.
    """

    group: str
    source: str
    generation: FileGeneration | None
    from_position: int | str
    to_position: int | str


def holds_back(pending: Receipt, read: Receipt) -> bool:
    """True when in-flight ``pending`` may carry matches a checkpoint at ``read``'s end skips.

    Same group, source and inode only (copytruncate keeps the inode). Offsets
    compare; journald cursors are opaque and Docker ts order only once parsed
    (tailer ``_covers``), so either pending holds: a later checkpoint, never a skip.
    """
    if (pending.group, pending.source) != (read.group, read.source):
        return False
    old, new = pending.generation, read.generation
    if old is not None and new is not None and old.inode != new.inode:
        return False
    start, end = pending.from_position, read.to_position
    if isinstance(start, int) and isinstance(end, int):
        return start < end
    return True
