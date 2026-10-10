"""Fixtures shared by the wire-contract tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import fitness.wire_contract as fitness_module
from fitness.wire_contract import check_wire_contract


@pytest.fixture
def check_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[dict[str, Any]], list[str]]:
    """Write an artifact where Function 9 reads it and return its violations."""

    def _check(artifact: dict[str, Any]) -> list[str]:
        path = tmp_path / "wire-contract.json"
        path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr(fitness_module, "WIRE_CONTRACT_PATH", path)
        return check_wire_contract()

    return _check
