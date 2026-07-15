"""T31: Function 8 (wizard SDK purity + topology) catches deliberate violations."""

from __future__ import annotations

from pathlib import Path

import pytest

import fitness.wizard_sdk_p2 as fn8


def test_clean_tree_has_no_violations() -> None:
    # The real tree must pass.
    assert fn8.check_wizard_sdk() == []


def test_sdk_impurity_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    (sdk_dir / "bad.py").write_text(
        "from stormpulse.garage import state\nimport subprocess\n", encoding="utf-8"
    )
    monkeypatch.setattr(fn8, "_SDK_DIR", sdk_dir)
    monkeypatch.setattr(fn8, "_WIZARD_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(fn8, "_ROOT", tmp_path)
    violations = fn8.check_wizard_sdk()
    assert any("non-SDK module" in v for v in violations)
    assert any("host primitive" in v for v in violations)


def test_wizard_feature_import_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiz_dir = tmp_path / "wizard"
    wiz_dir.mkdir()
    (wiz_dir / "bad.py").write_text(
        "from stormpulse.caddy import sync\n", encoding="utf-8"
    )
    monkeypatch.setattr(fn8, "_WIZARD_DIR", wiz_dir)
    monkeypatch.setattr(fn8, "_SDK_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(fn8, "_ROOT", tmp_path)
    violations = fn8.check_wizard_sdk()
    assert any("wizard imports" in v and "stormpulse.caddy" in v for v in violations)
