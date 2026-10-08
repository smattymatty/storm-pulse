"""The hint reader's trust contract: one test per refusal, plus the good read."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from stormpulse.garage.hint import MAX_HINT_BYTES, Refusal, read_hint

NOW = 1_760_000_000.0
ID_A = "a" * 64
ID_B = "0123456789abcdef" * 4


def _write(path: Path, doc: object) -> str:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _hint(tmp_path: Path, **overrides: object) -> str:
    doc = {"version": 1, "written_at": NOW, "bucket_ids": [ID_A, ID_B]}
    doc.update(overrides)
    return _write(tmp_path / "hint.json", doc)


def test_good_file_yields_its_ids(tmp_path: Path) -> None:
    got = read_hint(_hint(tmp_path), now=NOW)
    assert got.refusal is None
    assert got.bucket_ids == (ID_A, ID_B)


def test_empty_file_is_a_heartbeat_not_a_refusal(tmp_path: Path) -> None:
    got = read_hint(_hint(tmp_path, bucket_ids=[]), now=NOW)
    assert got.refusal is None
    assert got.bucket_ids == ()


def test_missing_file_is_unreadable(tmp_path: Path) -> None:
    got = read_hint(str(tmp_path / "absent.json"), now=NOW)
    assert got.refusal is Refusal.UNREADABLE


def test_symlink_refused(tmp_path: Path) -> None:
    target = _hint(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    got = read_hint(str(link), now=NOW)
    assert got.refusal is Refusal.SYMLINK
    assert got.bucket_ids == ()


def test_fifo_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "hint.fifo"
    os.mkfifo(fifo)

    def _hung(signum: int, frame: object) -> None:
        raise TimeoutError("open blocked on a FIFO with no writer")

    previous = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(2)
    try:
        got = read_hint(str(fifo), now=NOW)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert got.refusal is Refusal.NOT_REGULAR


def test_directory_refused(tmp_path: Path) -> None:
    got = read_hint(str(tmp_path), now=NOW)
    assert got.refusal is Refusal.NOT_REGULAR


def test_foreign_owner_refused(tmp_path: Path) -> None:
    got = read_hint(_hint(tmp_path), now=NOW, uid=os.geteuid() + 1)
    assert got.refusal is Refusal.FOREIGN_OWNER
    assert got.bucket_ids == ()


def test_oversize_refused(tmp_path: Path) -> None:
    path = tmp_path / "hint.json"
    path.write_bytes(b" " * (MAX_HINT_BYTES + 1))
    got = read_hint(str(path), now=NOW)
    assert got.refusal is Refusal.TOO_LARGE


def test_file_at_the_cap_is_read(tmp_path: Path) -> None:
    body = json.dumps({"version": 1, "written_at": NOW, "bucket_ids": [ID_A]})
    path = tmp_path / "hint.json"
    path.write_bytes(body.encode() + b" " * (MAX_HINT_BYTES - len(body)))
    assert read_hint(str(path), now=NOW).bucket_ids == (ID_A,)


@pytest.mark.parametrize(
    "doc",
    [
        "not json",
        [ID_A],
        {"version": 2, "written_at": NOW, "bucket_ids": [ID_A]},
        {"version": True, "written_at": NOW, "bucket_ids": [ID_A]},
        {"written_at": NOW, "bucket_ids": [ID_A]},
        {"version": 1, "bucket_ids": [ID_A]},
        {"version": 1, "written_at": "now", "bucket_ids": [ID_A]},
        {"version": 1, "written_at": NOW, "bucket_ids": ID_A},
    ],
)
def test_schema_violation_refused(tmp_path: Path, doc: object) -> None:
    path = tmp_path / "hint.json"
    if isinstance(doc, str):
        path.write_text(doc, encoding="utf-8")
    else:
        _write(path, doc)
    got = read_hint(str(path), now=NOW)
    assert got.refusal is Refusal.BAD_SCHEMA


@pytest.mark.parametrize("offset", [-61.0, 6.0])
def test_stale_or_future_refused(tmp_path: Path, offset: float) -> None:
    got = read_hint(_hint(tmp_path, written_at=NOW + offset), now=NOW)
    assert got.refusal is Refusal.STALE


@pytest.mark.parametrize("offset", [-60.0, 5.0])
def test_age_bounds_are_inclusive(tmp_path: Path, offset: float) -> None:
    got = read_hint(_hint(tmp_path, written_at=NOW + offset), now=NOW)
    assert got.refusal is None


@pytest.mark.parametrize(
    "bad",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "abc", "", 42, None],
)
def test_non_64_hex_id_refused_whole_file(tmp_path: Path, bad: object) -> None:
    # A short id would become a Garage prefix search; refuse rather than search.
    got = read_hint(_hint(tmp_path, bucket_ids=[ID_A, bad]), now=NOW)
    assert got.refusal is Refusal.BAD_ID
    assert got.bucket_ids == ()


def test_nan_written_at_refused(tmp_path: Path) -> None:
    # json.loads accepts NaN; it would make a dead writer's file fresh forever.
    path = tmp_path / "hint.json"
    path.write_text(f'{{"version": 1, "written_at": NaN, "bucket_ids": ["{ID_A}"]}}')
    assert read_hint(str(path), now=NOW).refusal is not None
