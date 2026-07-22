"""Shared on-disk primitives for the P1 loader.

State-dir layout, one advisory lock over every mutation, atomic write with
fsync, canonical JSON, and a UTC timestamp. Trust records, package trees, and
receipts all go through these, so they never re-implement durable writes and
never interleave.

POSIX only: relies on ``fcntl.flock`` and directory ``fsync``.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stormpulse.integrations.external.model import FailureCode, PackageError

_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.1


def integrations_root(state_dir: Path) -> Path:
    return state_dir / "integrations"


def publishers_dir(state_dir: Path) -> Path:
    return integrations_root(state_dir) / "publishers"


def packages_dir(state_dir: Path) -> Path:
    return integrations_root(state_dir) / "packages" / "sha256"


def receipts_dir(state_dir: Path) -> Path:
    return integrations_root(state_dir) / "receipts"


def grants_dir(state_dir: Path) -> Path:
    return integrations_root(state_dir) / "grants"


def tmp_dir(state_dir: Path) -> Path:
    return integrations_root(state_dir) / "tmp"


def lock_path(state_dir: Path) -> Path:
    return integrations_root(state_dir) / "lock"


def ensure_layout(state_dir: Path) -> None:
    """Create the integrations state tree with private modes (idempotent)."""
    for directory in (
        integrations_root(state_dir),
        publishers_dir(state_dir),
        packages_dir(state_dir),
        receipts_dir(state_dir),
        grants_dir(state_dir),
        tmp_dir(state_dir),
    ):
        directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(directory, _DIR_MODE)  # defeat umask on freshly-created dirs


def canonical_json(obj: Any) -> bytes:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def atomic_write(path: Path, data: bytes, *, mode: int = _PRIVATE_FILE_MODE) -> None:
    """Write ``data`` at ``path`` atomically: temp + fsync + rename + parent fsync."""
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with open(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(parent)


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def state_lock(state_dir: Path) -> Iterator[None]:
    """Hold the single advisory lock for every state mutation.

    Polls up to ``_LOCK_TIMEOUT_SECONDS`` for the exclusive lock, then raises
    F12. There is no fairness guarantee between contenders.
    """
    ensure_layout(state_dir)
    fd = os.open(lock_path(state_dir), os.O_CREAT | os.O_RDWR, _PRIVATE_FILE_MODE)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PackageError(
                        FailureCode.F12, "integration state lock is unavailable"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
