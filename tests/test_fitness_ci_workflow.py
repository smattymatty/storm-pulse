"""Function 12 (CI runs what `make check` runs) catches each drift it exists for."""

from __future__ import annotations

from pathlib import Path

import pytest

import fitness.ci_workflow as fn12

MAKEFILE = """check: quality security test
gates: gates-clone security test
CLONE_TARGETS := quality
"""
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


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_the_real_tree_is_clean() -> None:
    assert fn12.check_ci_workflow() == []


def test_the_fixture_is_clean(tree: Path) -> None:
    assert fn12.check_ci_workflow() == []


def test_a_check_target_ci_never_calls(tree: Path) -> None:
    edit(fn12.TEST_WORKFLOW, "make VENV= security test", "make VENV= security")
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: `make check` runs test, CI never does"
    ]


def test_a_check_target_gates_never_runs(tree: Path) -> None:
    edit(
        fn12.MAKEFILE,
        "check: quality security test",
        "check: quality security test mypy",
    )
    edit(
        fn12.TEST_WORKFLOW, "make VENV= security test", "make VENV= security test mypy"
    )
    assert fn12.check_ci_workflow() == [
        "Makefile: `make check` runs mypy, `make gates` never does"
    ]


def test_gates_running_quality_outside_the_clone(tree: Path) -> None:
    edit(fn12.MAKEFILE, "gates: gates-clone", "gates: quality gates-clone")
    assert fn12.check_ci_workflow() == [
        "Makefile: `make gates` runs quality, `make check` never does"
    ]


def test_a_clone_recipe_that_skips_a_check_target(tree: Path) -> None:
    edit(fn12.MAKEFILE, "CLONE_TARGETS := quality", "CLONE_TARGETS := comments-diff")
    assert fn12.check_ci_workflow() == [
        "Makefile: `make check` runs quality, `make gates` never does"
    ]


def test_a_target_named_like_check_is_not_check(tree: Path) -> None:
    (tree / "Makefile").write_text(
        "pre-release-check: release-smoke\npre-gates: wheel\n" + MAKEFILE,
        encoding="utf-8",
    )
    assert fn12.check_ci_workflow() == []


def test_a_tool_run_directly(tree: Path) -> None:
    edit(
        fn12.TEST_WORKFLOW,
        "      - run: make VENV= security test\n",
        "      - run: make VENV= security test\n      - run: skylos . --sca\n",
    )
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: runs skylos directly; call its make target"
    ]


def test_a_job_off_the_docker_runner(tree: Path) -> None:
    edit(
        fn12.TEST_WORKFLOW,
        "  security:\n    runs-on: docker",
        "  security:\n    runs-on: heavy",
    )
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: job security runs on heavy, not docker"
    ]


def test_two_images_in_one_workflow(tree: Path) -> None:
    edit(
        fn12.TEST_WORKFLOW,
        "  security:\n    runs-on: docker\n",
        "  security:\n    runs-on: docker\n    container:\n      image: python:3.12\n",
    )
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: images disagree: git.example/storm-ci-python:3.12-2, python:3.12"
    ]


def test_an_empty_parse_fails_loudly(tree: Path) -> None:
    (tree / "workflows" / "test.yml").write_text("name: Tests\n", encoding="utf-8")
    (tree / "Makefile").write_text("all:\n", encoding="utf-8")
    assert fn12.check_ci_workflow() == [
        "workflows/test.yml: no job names a container image",
        "workflows/test.yml: no jobs parsed",
        "Makefile: no `check:` prerequisites parsed",
        "Makefile: no `gates:` prerequisites parsed",
        "Makefile: no `CLONE_TARGETS` parsed",
    ]
