"""Function 14: the agent never holds a publisher signing key.

Package signing lives in the repo-root ``authoring/`` package, outside the
agent wheel (CORE-007). ``stormpulse/`` may verify signatures, but never imports
``authoring`` or names an Ed25519 private key. The agent's own mTLS key is its
transport credential, not a signing key, and is outside this check.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"


def check_no_signing_key(root: Path = ROOT) -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(_file_violations(path, path.relative_to(root.parent)))
    return violations


def _file_violations(path: Path, rel: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        line = getattr(node, "lineno", 0)
        if _imports_authoring(node):
            found.append(f"{rel}:{line} imports the release-side signer")
        elif _names_signing_key(node):
            found.append(f"{rel}:{line} names an Ed25519 private key")
    return found


def _imports_authoring(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "authoring"
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] == "authoring" for a in node.names)
    return False


def _names_signing_key(node: ast.AST) -> bool:
    name = getattr(node, "id", None) or getattr(node, "attr", None)
    return name == "Ed25519PrivateKey"
