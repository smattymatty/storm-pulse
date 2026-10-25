"""Function 18 (every tailer under the cursor contract) catches a tailer left out."""

from __future__ import annotations

from pathlib import Path

import pytest

import fitness.__main__ as fitness_main
import fitness.cursor_contract as fn18

CONFIRMING = "class A:\n    def confirm_shipped(self, p: int) -> None: ...\n"


def _tree(tmp_path: Path, source: str, suite: str | None) -> None:
    (tmp_path / "stormpulse").mkdir()
    (tmp_path / "stormpulse" / "t.py").write_text(source, encoding="utf-8")
    if suite is not None:
        (tmp_path / "tests" / "logging").mkdir(parents=True)
        (tmp_path / fn18.SUITE).write_text(suite, encoding="utf-8")


def test_the_tree_holds_every_tailer() -> None:
    assert fn18.check_cursor_contract() == []


def test_a_confirming_class_outside_tailers_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        CONFIRMING + "class B:\n    def confirm_shipped(self, p: int) -> None: ...\n"
    )
    source += "class C:\n    def read(self) -> None: ...\n"
    _tree(tmp_path, source, "TAILERS = (A, C)\n")
    monkeypatch.setattr(fn18, "ROOT", tmp_path)
    assert fn18.check_cursor_contract() == [
        f"stormpulse/t.py:B: defines confirm_shipped but is not in {fn18.SUITE} TAILERS"
    ]


@pytest.mark.parametrize(
    "suite", [None, "OTHERS = (A,)\n", "def f():\n    TAILERS = (A,)\n"]
)
def test_a_missing_tailers_list_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suite: str | None
) -> None:
    _tree(tmp_path, CONFIRMING, suite)
    monkeypatch.setattr(fn18, "ROOT", tmp_path)
    assert fn18.check_cursor_contract() == [
        f"{fn18.SUITE}: no module-level TAILERS tuple; the cursor contract is unheld"
    ]


def test_a_list_of_tailers_holds_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path, CONFIRMING, "TAILERS = [A]\n")
    monkeypatch.setattr(fn18, "ROOT", tmp_path)
    assert fn18.check_cursor_contract() == []


def test_a_nested_async_confirm_shipped_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "class Outer:\n    class Inner:\n        async def confirm_shipped(self) -> None: ...\n"
    _tree(tmp_path, source, "TAILERS = (Outer,)\n")
    monkeypatch.setattr(fn18, "ROOT", tmp_path)
    assert fn18.check_cursor_contract() == [
        f"stormpulse/t.py:Inner: defines confirm_shipped but is not in {fn18.SUITE} TAILERS"
    ]


def test_the_harness_fails_on_a_cursor_contract_violation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        fitness_main, "check_cursor_contract", lambda: ["dropped tailer"]
    )
    assert fitness_main.main() == 1
    err = capsys.readouterr().err
    assert "[FAIL] Function 18 - every tailer under the cursor contract" in err
    assert "dropped tailer" in err
