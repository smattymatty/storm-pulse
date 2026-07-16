"""Durable apply journal + wizard-apply lock + crash recovery (P2, CORE-007).

Framework layer. Before each mutation's forward op, the engine appends a journal
entry - carrying the captured pre-image bytes - and fsyncs it. A crash mid-apply
therefore leaves a durable, self-describing record under the state area that a
fresh ``doctor`` process can report and recover from, without the original
process's memory. On a clean commit or full in-process rollback the journal is
finalized (removed); its presence at rest means an apply was interrupted.

Recovery restores the file-based kinds (claim_toml / install / systemd unit) in
reverse order. Provider- and service-manager kinds (caddy, restart) are recorded
but not auto-recovered out-of-process; they are reported for operator review.

The wizard-apply lock is a per-open-fd ``flock``; it is never held across a call
that takes the same lock (I11, the P1 self-deadlock hazard).
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from stormpulse.wizard.toml_edit import restore_or_remove

_LOCK_TIMEOUT_POLLS = 100  # 100 * 0.1s = 10s, matching the P1 state lock budget


def _wizard_dir(state_dir: Path) -> Path:
    return state_dir / "wizard"


def _active_path(state_dir: Path) -> Path:
    return _wizard_dir(state_dir) / "active.json"


def _lock_path(state_dir: Path) -> Path:
    return _wizard_dir(state_dir) / "lock"


def _ensure_dir(state_dir: Path) -> Path:
    d = _wizard_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


@contextmanager
def wizard_lock(state_dir: Path) -> Iterator[None]:
    """Serialize wizard applies with a per-fd advisory lock. Non-nested (I11)."""
    _ensure_dir(state_dir)
    fd = os.open(_lock_path(state_dir), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        for _ in range(_LOCK_TIMEOUT_POLLS):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # noqa: PERF203
                time.sleep(0.1)
        else:
            raise TimeoutError("wizard-apply lock unavailable after 10s")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One journaled step: enough for a fresh process to compensate a file kind."""

    index: int
    kind: str
    target: str
    recover_path: str | None
    pre_image_b64: str | None
    recover_mode: int

    @property
    def pre_image(self) -> bytes | None:
        return base64.b64decode(self.pre_image_b64) if self.pre_image_b64 is not None else None


class Journal:
    """The durable apply journal for one plan application."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._path = _active_path(state_dir)
        self._entries: list[JournalEntry] = []
        self._meta: dict[str, object] = {}

    def begin(self, *, agent_id: str, integration_id: str, sdk_api: int, summary: str) -> None:
        _ensure_dir(self._state_dir)
        self._meta = {
            "agent_id": agent_id,
            "integration_id": integration_id,
            "sdk_api": sdk_api,
            "summary": summary,
        }
        self._flush()

    def record(
        self,
        *,
        index: int,
        kind: str,
        target: str,
        recover_path: str | None,
        pre_image: bytes | None,
        recover_mode: int,
    ) -> None:
        """Append a step entry and fsync BEFORE the caller runs the forward op."""
        self._entries.append(
            JournalEntry(
                index=index,
                kind=kind,
                target=target,
                recover_path=recover_path,
                pre_image_b64=base64.b64encode(pre_image).decode("ascii")
                if pre_image is not None
                else None,
                recover_mode=recover_mode,
            )
        )
        self._flush()

    def finalize(self) -> None:
        """Remove the journal after a clean commit or a completed in-process
        rollback; its absence means no apply is in flight."""
        self._path.unlink(missing_ok=True)

    def _flush(self) -> None:
        payload = {
            "schema_version": 1,
            "meta": self._meta,
            "entries": [
                {
                    "index": e.index,
                    "kind": e.kind,
                    "target": e.target,
                    "recover_path": e.recover_path,
                    "pre_image_b64": e.pre_image_b64,
                    "recover_mode": e.recover_mode,
                }
                for e in self._entries
            ],
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with open(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._path)
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def read_pending(state_dir: Path) -> list[JournalEntry] | None:
    """Return the entries of an interrupted apply, or ``None`` if the journal is
    absent (no apply in flight). For ``doctor`` reporting."""
    path = _active_path(state_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return [
        JournalEntry(
            index=int(e["index"]),
            kind=str(e["kind"]),
            target=str(e["target"]),
            recover_path=e["recover_path"],
            pre_image_b64=e["pre_image_b64"],
            recover_mode=int(e["recover_mode"]),
        )
        for e in payload.get("entries", [])
    ]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """The outcome of recovering an interrupted apply."""

    recovered: tuple[str, ...]
    manual: tuple[str, ...]


def recover(state_dir: Path) -> RecoveryResult | None:
    """Recover an interrupted apply by restoring file-based pre-images in reverse
    order, then finalizing the journal. Provider/service-manager kinds are reported
    for manual review. Returns ``None`` if there is nothing to recover."""
    entries = read_pending(state_dir)
    if entries is None:
        return None
    recovered: list[str] = []
    manual: list[str] = []
    for entry in sorted(entries, key=lambda e: e.index, reverse=True):
        if entry.recover_path is not None:
            restore_or_remove(Path(entry.recover_path), entry.pre_image, entry.recover_mode or 0o644)
            recovered.append(f"{entry.kind}:{entry.target}")
        else:
            manual.append(f"{entry.kind}:{entry.target}")
    _active_path(state_dir).unlink(missing_ok=True)
    return RecoveryResult(recovered=tuple(recovered), manual=tuple(manual))
