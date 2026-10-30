"""Function 17 (tests run as the real user) catches the fakes it exists for."""

from __future__ import annotations

from pathlib import Path

import pytest

import fitness.no_fake_os as fn17
from fitness.__main__ import load_baseline


def test_the_tree_has_no_violation_outside_the_baseline() -> None:
    # The baseline is the opening debt; a new fake must fail here.
    assert [v for v in fn17.check_no_fake_os() if v not in load_baseline()] == []


def test_a_faked_identity_is_counted_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        '@patch("stormpulse.cli.run.os.geteuid", return_value=0)\n'
        'monkeypatch.setattr(os, "access", lambda *a: True)\n'
        'patch.object(pwd, "getpwnam")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(fn17, "ROOT", tmp_path)
    assert fn17.check_no_fake_os() == [
        "tests/test_x.py: 3 fake(s) of the host's identity; run as the real user"
    ]


def test_fault_injection_and_metric_sources_are_not_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_y.py").write_text(
        'monkeypatch.setattr(os, "replace", boom)\n'
        '@patch("stormpulse.metrics.os.getloadavg")\n'
        'patch("stormpulse.os_tools.geteuid")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(fn17, "ROOT", tmp_path)
    assert fn17.check_no_fake_os() == []
