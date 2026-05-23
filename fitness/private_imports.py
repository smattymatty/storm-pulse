"""Function 2: no cross-boundary private imports.

CORE-000 Rule 2 - a single-underscore-prefixed name is private to its
defining module and may not be imported by any other module. Dunder
names (``__version__``, ``__all__``, etc.) are exempt: they are public
module metadata by convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"


def check_private_imports() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT.parent)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            # intra-package: relative import (level > 0) or absolute under stormpulse.
            if not (node.level > 0 or mod.startswith("stormpulse")):
                continue
            for alias in node.names:
                name = alias.name
                is_dunder = name.startswith("__") and name.endswith("__")
                if name.startswith("_") and not is_dunder:
                    src = ("." * node.level) + mod
                    violations.append(
                        f"{rel}:{node.lineno} imports private {name!r} from {src!r}"
                    )
    return violations
