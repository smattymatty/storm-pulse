"""Function 3: no shell execution.

Security Architecture, Layer 4 - no subprocess (or any other) call
in stormpulse/ passes ``shell=True``. The codebase is currently clean;
this check is a regression guard against future creep.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"


def check_no_shell() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT.parent)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    violations.append(f"{rel}:{node.lineno} call passes shell=True")
    return violations
