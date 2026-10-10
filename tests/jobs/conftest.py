"""Fixtures shared by the JobManager tests."""

from __future__ import annotations

import pytest

from tests.helpers import FakeWire


@pytest.fixture
def wire() -> FakeWire:
    return FakeWire()
