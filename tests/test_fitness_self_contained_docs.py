"""Function 11 (docs stand on their own) catches the references it exists for."""

from __future__ import annotations

from pathlib import Path

import pytest

import fitness.self_contained_docs as fn11
from fitness.__main__ import load_baseline


def test_the_tree_has_no_violation_outside_the_baseline() -> None:
    # The baseline is the opening debt; a new reference must fail here.
    assert [
        v for v in fn11.check_self_contained_docs() if v not in load_baseline()
    ] == []


def test_a_reference_to_another_repository_is_named_by_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "notes.md").write_text(
        "The control plane is fine.\nBut the website relay is not.\n",
        encoding="utf-8",
    )
    (tmp_path / "mod.py").write_text(
        "# see ~/Projects/StormDevelopments/website\n", encoding="utf-8"
    )
    monkeypatch.setattr(fn11, "ROOT", tmp_path)
    violations = fn11.check_self_contained_docs()
    assert violations == [
        "mod.py:1 points at another repository; describe it, the far end is 'the control plane'",
        "notes.md:2 points at another repository; describe it, the far end is 'the control plane'",
    ]


def test_the_glossary_may_name_the_word_it_forbids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "CONTEXT.md").write_text(
        "_Avoid_: the website (points at another repository)\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "- fixed the website relay\n", encoding="utf-8"
    )
    monkeypatch.setattr(fn11, "ROOT", tmp_path)
    assert fn11.check_self_contained_docs() == []
