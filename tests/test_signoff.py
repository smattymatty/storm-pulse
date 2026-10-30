"""Tests for the sign-off seal state: marker presence, unseal time, its display."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stormpulse.signoff import (
    SignoffState,
    format_unsealed_duration,
    state_dir_from_db_path,
)

# ---------------------------------------------------------------------------
# SignoffState: marker-presence semantics (CORE-004 d6)
# ---------------------------------------------------------------------------


def test_signoff_state_starts_sealed(tmp_path: Path) -> None:
    # A fresh, wiped or migrated state dir holds no marker: sealed.
    state = SignoffState(tmp_path)
    assert state.is_sealed() is True
    assert state.unsealed_since() is None


def test_signoff_state_seal_writes_the_legacy_flag_file(tmp_path: Path) -> None:
    # An agent older than 0.8.3 reads signoff.sealed, so a downgrade stays sealed.
    state = SignoffState(tmp_path)
    assert state.seal() is False
    assert state.is_sealed() is True
    assert state.path.exists()
    assert state.path.name == "signoff.sealed"


def test_signoff_state_seal_is_idempotent(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.unseal()
    assert state.seal() is True
    # Second call must return False (no state change), still sealed.
    assert state.seal() is False
    assert state.is_sealed() is True


def test_signoff_state_unseal_removes_flag_file(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    assert state.unseal() is True
    assert state.is_sealed() is False
    assert not state.path.exists()


def test_signoff_state_unseal_is_idempotent(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    assert state.unseal() is True
    assert state.unseal() is False
    assert state.is_sealed() is False


def test_signoff_state_creates_state_dir_if_missing(tmp_path: Path) -> None:
    # SignoffState should create the parent directory on seal so a
    # fresh install (no nonce DB yet) doesn't blow up the CLI.
    fresh = tmp_path / "does" / "not" / "exist"
    state = SignoffState(fresh)
    state.seal()
    assert fresh.is_dir()
    assert state.path.exists()


def test_signoff_state_rereads_on_each_call(tmp_path: Path) -> None:
    # The dispatch-time recheck depends on is_sealed() actually
    # re-statting the path on every call (not caching a startup value).
    state = SignoffState(tmp_path)
    assert state.is_sealed() is True
    # External actor (operator running the CLI) writes the marker.
    state.unsealed_at_path.write_text("2026-10-30T00:00:00+00:00")
    assert state.is_sealed() is False
    # And the reverse.
    state.unsealed_at_path.unlink()
    assert state.is_sealed() is True


def test_deleting_the_legacy_flag_file_does_not_unseal(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    state.path.unlink()
    assert state.is_sealed() is True


def test_wiping_the_state_dir_reseals(tmp_path: Path) -> None:
    import shutil

    state = SignoffState(tmp_path / "state")
    state.unseal()
    shutil.rmtree(tmp_path / "state")
    assert state.is_sealed() is True


def test_state_dir_from_db_path_returns_parent(tmp_path: Path) -> None:
    db = tmp_path / "stormpulse.db"
    assert state_dir_from_db_path(db) == tmp_path


# ---------------------------------------------------------------------------
# SignoffState: unsealed_since timestamp tracking
# ---------------------------------------------------------------------------


def test_unsealed_since_is_none_when_sealed(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    assert state.unsealed_since() is None


def test_unsealed_since_records_transition_timestamp(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    before = datetime.now(UTC) - timedelta(seconds=1)
    state.unseal()
    after = datetime.now(UTC) + timedelta(seconds=1)

    since = state.unsealed_since()
    assert since is not None
    assert before <= since <= after


def test_unsealed_since_survives_marker_only_state(tmp_path: Path) -> None:
    """Sealing after an unseal removes the marker; unseal again starts fresh."""
    state = SignoffState(tmp_path)
    state.seal()
    state.unseal()
    first = state.unsealed_since()
    state.seal()
    assert state.unsealed_since() is None
    # Brief delay so the new timestamp differs from the original.
    time.sleep(0.01)
    state.unseal()
    second = state.unsealed_since()
    assert first is not None
    assert second is not None
    assert second > first


def test_unsealed_since_returns_none_on_corrupt_marker(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    state.unseal()
    # Corrupt the marker file
    state.unsealed_at_path.write_text("not an ISO timestamp")
    assert state.unsealed_since() is None


def test_seal_clears_unsealed_at_marker(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    state.unseal()
    assert state.unsealed_at_path.exists()
    state.seal()
    assert not state.unsealed_at_path.exists()


# ---------------------------------------------------------------------------
# format_unsealed_duration
# ---------------------------------------------------------------------------


def test_format_unsealed_duration_none_returns_unknown() -> None:
    assert format_unsealed_duration(None) == "unknown"


def test_format_unsealed_duration_minutes() -> None:
    now = datetime.now(UTC)
    assert format_unsealed_duration(now - timedelta(minutes=12)) == "12m"


def test_format_unsealed_duration_hours_and_minutes() -> None:
    now = datetime.now(UTC)
    assert format_unsealed_duration(now - timedelta(hours=3, minutes=14)) == "3h 14m"


def test_format_unsealed_duration_days_and_hours() -> None:
    now = datetime.now(UTC)
    out = format_unsealed_duration(now - timedelta(days=4, hours=7, minutes=22))
    assert out == "4d 7h"


def test_format_unsealed_duration_below_a_minute() -> None:
    assert format_unsealed_duration(datetime.now(UTC)) == "<1m"


def test_format_unsealed_duration_future_clock_skew() -> None:
    future = datetime.now(UTC) + timedelta(minutes=5)
    assert format_unsealed_duration(future) == "<1m"
