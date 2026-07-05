"""Function 6: the merge-primitive fence (CORE-005 decision 11). ``with_items`` is
defined by many state types, CALLED by one module (agent/integrations_runtime.py);
a bypass merge is the silent lost-update bug, so it fails CI instead."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
ALLOWED = ROOT / "agent" / "integrations_runtime.py"


def check_merge_fence() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if path == ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT.parent)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "with_items"
            ):
                violations.append(
                    f"{rel}:{node.lineno} calls .with_items() outside "
                    "agent/integrations_runtime.py (CORE-005 decision 11: "
                    "one shared merge primitive)"
                )
    return violations
