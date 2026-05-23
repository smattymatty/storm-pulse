"""Function 4: runtime dependency allowlist.

Security Architecture supply-chain - the agent ships with exactly three
runtime dependencies. Two assertions:

  (a) ``[project.dependencies]`` in pyproject.toml is a subset of the
      allowlist.
  (b) No module in ``stormpulse/`` imports a third-party top-level
      package outside that allowlist plus the standard library. Part
      (b) catches the "pip install X and import it without declaring"
      bypass that part (a) alone would miss.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORMPULSE = ROOT / "stormpulse"
PYPROJECT = ROOT / "pyproject.toml"

ALLOWED: frozenset[str] = frozenset({"websockets", "psutil", "cryptography"})
STDLIB: frozenset[str] = frozenset(sys.stdlib_module_names)


def _declared_dependencies() -> set[str]:
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = raw.get("project", {}).get("dependencies", [])
    declared: set[str] = set()
    for spec in deps:
        # Strip environment marker, then take the project name (everything
        # before the first comparison operator).
        name = spec.split(";", 1)[0]
        for op in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(op, 1)[0]
        declared.add(name.strip())
    return declared


def check_dependencies() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []

    # Part (a): pyproject runtime deps subset of allowlist.
    declared = _declared_dependencies()
    extra = declared - ALLOWED
    if extra:
        violations.append(
            f"pyproject.toml [project.dependencies] declares non-allowlisted: "
            f"{sorted(extra)} (allowed: {sorted(ALLOWED)})"
        )

    # Part (b): stormpulse/ imports of stdlib + allowlist + stormpulse only.
    permitted = STDLIB | ALLOWED | {"stormpulse"}
    for path in sorted(STORMPULSE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top not in permitted:
                        violations.append(
                            f"{rel}:{node.lineno} imports {alias.name!r} "
                            f"(top-level package not in allowlist)"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue  # intra-package relative import
                mod = node.module or ""
                top = mod.split(".", 1)[0]
                if top and top not in permitted:
                    violations.append(
                        f"{rel}:{node.lineno} from {mod!r} import ... "
                        f"(top-level package not in allowlist)"
                    )
    return violations
