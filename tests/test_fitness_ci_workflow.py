"""Function 12 (CI runs what `make check` runs) catches each drift it exists for."""

from __future__ import annotations

from pathlib import Path

import pytest

import fitness.ci_workflow as fn12

MAKEFILE = "check: quality security test\n"
WORKFLOW = """jobs:
  quality:
    runs-on: docker
    container:
      image: git.example/storm-ci-python:3.12-2
    steps:
      - run: make VENV= BASE="$BASE" quality
  security:
    runs-on: docker
    steps:
      - run: make VENV= security test
"""


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "workflows").mkdir()
    (tmp_path / "Makefile").write_text(MAKEFILE, encoding="utf-8")
    (tmp_path / "workflows" / "test.yml").write_text(WORKFLOW, encoding="utf-8")
    monkeypatch.setattr(fn12, "ROOT", tmp_path)
    monkeypatch.setattr(fn12, "WORKFLOWS", tmp_path / "workflows")
    monkeypatch.setattr(fn12, "TEST_WORKFLOW", tmp_path / "workflows" / "test.yml")
    monkeypatch.setattr(fn12, "MAKEFILE", tmp_path / "Makefile")
    return tmp_path


def edit(tree: Path, old: str, new: str) -> None:
    path = tree / "workflows" / "test.yml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_the_real_tree_is_clean() -> None:
    assert fn12.check_ci_workflow() == []


def test_the_fixture_is_clean(tree: Path) -> None:
    assert fn12.check_ci_workflow() == []


def test_a_check_target_ci_never_calls(tree: Path) -> None:
    edit(tree, "make VENV= security test", "make VENV= security")
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: `make check` runs test, CI never does"
    ]


def test_a_tool_run_directly(tree: Path) -> None:
    edit(
        tree,
        "      - run: make VENV= security test\n",
        "      - run: make VENV= security test\n      - run: skylos . --sca\n",
    )
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: runs skylos directly; call its make target"
    ]


def test_a_job_off_the_docker_runner(tree: Path) -> None:
    edit(tree, "  security:\n    runs-on: docker", "  security:\n    runs-on: heavy")
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: job security runs on heavy, not docker"
    ]


def test_two_image_tags(tree: Path) -> None:
    (tree / "workflows" / "cadence.yml").write_text(
        "image: git.example/storm-ci-python:3.12-3\n", encoding="utf-8"
    )
    assert fn12.check_ci_workflow() == [
        "storm-ci-python tags disagree: cadence.yml:3.12-3, test.yml:3.12-2"
    ]


def test_an_empty_parse_fails_loudly(tree: Path) -> None:
    (tree / "workflows" / "test.yml").write_text("name: Tests\n", encoding="utf-8")
    (tree / "Makefile").write_text("all:\n", encoding="utf-8")
    assert fn12.check_ci_workflow() == [
        "no workflow names the storm-ci-python image",
        "workflows/test.yml: no jobs parsed",
        "Makefile: no `check:` prerequisites parsed",
    ]
