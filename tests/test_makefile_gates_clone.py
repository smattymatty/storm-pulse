"""`make gates-clone` gates the tree as it will be committed, outside this checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parent.parent / "Makefile"
SKYLOS = """#!/bin/sh
touch skylos-was-here
{ echo "cwd $(pwd)"; git diff --name-only "$4"...HEAD; } >> "$GATES_LOG"
! git diff "$4"...HEAD | grep -q VIOLATION
"""
PYTHON = """#!/bin/sh
echo "comments $*" >> "$GATES_LOG"
"""

pytestmark = pytest.mark.skipif(
    not (shutil.which("make") and shutil.which("git")), reason="needs make and git"
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in list(os.environ):
        if key.startswith(("GIT_", "MAKE")):
            monkeypatch.delenv(key)
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copy(MAKEFILE, root / "Makefile")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    for name in ("tracked.py", "staged.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Gates test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    (root / "ahead.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "ahead.py")
    git(root, "commit", "-qm", "ahead")
    (root / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    (root / "staged.py").write_text("x = 2\n", encoding="utf-8")
    git(root, "add", "staged.py")
    (root / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    (root / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    for name, body in (("skylos", SKYLOS), ("python", PYTHON)):
        stub = tmp_path / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("GATES_LOG", str(tmp_path / "gates.log"))
    return root


def gates_clone(root: Path) -> subprocess.CompletedProcess[str]:
    stubs = root.parent
    return subprocess.run(
        [
            "make",
            "gates-clone",
            f"SKYLOS={stubs / 'skylos'}",
            f"PYTHON={stubs / 'python'}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_quality_and_comments_see_the_uncommitted_tree(checkout: Path) -> None:
    before = git(checkout, "status", "--short", "--ignored")
    base = git(checkout, "rev-parse", "origin/main").strip()
    result = gates_clone(checkout)
    assert result.returncode == 0, result.stderr
    log = (checkout.parent / "gates.log").read_text(encoding="utf-8").splitlines()
    assert log[0] != f"cwd {checkout}"
    assert sorted(log[1:-1]) == ["ahead.py", "staged.py", "tracked.py", "untracked.py"]
    assert log[-1] == f"comments scripts/comment_blocks.py --diff {base}"
    assert git(checkout, "status", "--short", "--ignored") == before


@pytest.mark.parametrize("name", ["tracked.py", "untracked.py"])
def test_an_uncommitted_violation_turns_it_red(checkout: Path, name: str) -> None:
    (checkout / name).write_text("VIOLATION = 1\n", encoding="utf-8")
    assert gates_clone(checkout).returncode != 0
