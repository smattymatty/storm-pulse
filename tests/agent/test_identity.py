"""Tests for the boot identity read once per process (CORE-011 d7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stormpulse.agent.identity import BOOT_ID_PATH, boot_id, started_at

FIXTURE_ID = "9c2d4f6e-1a3b-4c5d-8e7f-0a1b2c3d4e5f"


def test_boot_id_default_path_is_the_kernels() -> None:
    assert BOOT_ID_PATH == Path("/proc/sys/kernel/random/boot_id")


def test_boot_id_reads_the_fixture_stripped(tmp_path: Path) -> None:
    """A patched ``/proc`` path yields the fixture's id, trailing newline gone."""
    proc = tmp_path / "boot_id"
    proc.write_text(FIXTURE_ID + "\n", encoding="utf-8")
    assert boot_id(proc) == FIXTURE_ID


def test_boot_id_unreadable_is_empty_string(tmp_path: Path) -> None:
    """A missing file gives ``""``: a register never fails on boot identity."""
    assert boot_id(tmp_path / "absent") == ""


def test_boot_id_undecodable_is_empty_string(tmp_path: Path) -> None:
    proc = tmp_path / "boot_id"
    proc.write_bytes(b"\xff\xfe")
    assert boot_id(proc) == ""


def test_started_at_is_iso8601_utc() -> None:
    instant = datetime(2026, 2, 21, 11, 0, 0, tzinfo=UTC)
    assert started_at(instant) == "2026-02-21T11:00:00+00:00"


def test_started_at_now_is_aware_utc() -> None:
    parsed = datetime.fromisoformat(started_at())
    assert parsed.utcoffset() == timedelta(0)
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 5
