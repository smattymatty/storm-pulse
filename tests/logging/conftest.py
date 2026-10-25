"""Fixtures shared by the log-shipping tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from stormpulse.logging.positions import LogPositionStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LogPositionStore]:
    """A position store closed after the test, pass or fail."""
    positions = LogPositionStore(tmp_path / "pos.db")
    yield positions
    positions.close()
