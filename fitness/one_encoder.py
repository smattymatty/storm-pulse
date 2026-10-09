"""Function 15: one compact JSON encoder.

A compact ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` is a
spelling of canonical bytes, and two of them disagree on flags the day one is
edited. ``stormpulse.sdk.declaration.canonical_json`` is the one; a site that
cannot use it yet is a ``baseline.txt`` line, which only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
HOME = "stormpulse/sdk/declaration.py"


def check_one_encoder(root: Path = ROOT) -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root.parent))
        if rel == HOME:
            continue
        violations.extend(
            f"{rel}:{line} spells its own compact JSON; use canonical_json"
            for line in _compact_sorted_dumps(path)
        )
    return violations


def _compact_sorted_dumps(path: Path) -> list[int]:
    found: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and _is_dumps(node.func):
            flags = {kw.arg: kw.value for kw in node.keywords}
            if _is_true(flags.get("sort_keys")) and _is_compact(
                flags.get("separators")
            ):
                found.append(node.lineno)
    return found


def _is_dumps(func: ast.AST) -> bool:
    return isinstance(func, ast.Attribute) and func.attr == "dumps"


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_compact(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
        return False
    values = [e.value for e in node.elts if isinstance(e, ast.Constant)]
    return values == [",", ":"]
