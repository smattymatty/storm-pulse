"""Function 18: every tailer is held to the cursor contract.

Mechanizes ``_architecture/tasks/0.8.1-stability.md`` STAB-01 and STAB-02: a
class defining ``confirm_shipped`` moves a checkpoint on an ACK, so it belongs
in ``TAILERS`` of the cursor-contract suite. Both sides are read by AST.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = "tests/logging/test_cursor_contract.py"


def _defines_confirm(node: ast.ClassDef) -> bool:
    """True when the class body itself defines ``confirm_shipped``."""
    methods = {
        f.name
        for f in node.body
        if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return "confirm_shipped" in methods


def _classes(path: Path) -> Iterator[ast.ClassDef]:
    """Every class defined anywhere in one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))


def _confirming_classes() -> list[str]:
    """Each class in stormpulse/ that defines confirm_shipped, as 'path:Name'."""
    return [
        f"{path.relative_to(ROOT).as_posix()}:{node.name}"
        for path in sorted((ROOT / "stormpulse").rglob("*.py"))
        for node in _classes(path)
        if _defines_confirm(node)
    ]


def _suite_tailers() -> set[str] | None:
    """Names in the suite's module-level TAILERS tuple or list; None if absent."""
    path = ROOT / SUITE
    if not path.is_file():
        return None
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "TAILERS" for t in node.targets)
            and isinstance(node.value, ast.Tuple | ast.List)
        ):
            return {e.id for e in node.value.elts if isinstance(e, ast.Name)}
    return None


def check_cursor_contract() -> list[str]:
    """Return one violation per confirming class the suite does not parametrize."""
    tailers = _suite_tailers()
    if tailers is None:
        return [
            f"{SUITE}: no module-level TAILERS tuple; the cursor contract is unheld"
        ]
    return [
        f"{where}: defines confirm_shipped but is not in {SUITE} TAILERS"
        for where in _confirming_classes()
        if where.rsplit(":", 1)[1] not in tailers
    ]
