"""Fixtures shared by the JobManager tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stormpulse.commands import jobs
from tests.helpers import FakeWire


@pytest.fixture
def wire() -> FakeWire:
    return FakeWire()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The jobs module's monotonic clock, at 0.0 until the test moves it."""
    now = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now
