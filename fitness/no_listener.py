"""Function 10: the agent never listens on a port.

Security Architecture, Layer 1, and the claim is published: the wiki's
Security-Architecture page states "the agent never listens on a port; all
connections are outbound to the dashboard". A customer or a reviewer can
check that claim by reading this source tree, so it must stay true by
mechanism and not by discipline. This check is the mechanism.

Two things are refused in ``stormpulse/``:

- A server-side socket call: ``bind``, ``listen``, ``serve_forever``,
  ``create_server``, ``start_server``, or ``socketserver``/``http.server``
  construction. Matched on the attribute name, so it fires on any receiver.
- An import of a module whose only purpose is to accept connections.

The false positive worth knowing about: ``bind`` is also a perfectly
ordinary method name. Matching the attribute alone is deliberately blunt,
because a blunt refusal on a security claim is the right trade, and the
baseline file is the documented escape hatch for a real exception.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"

# Attribute names that only appear when something is accepting connections.
LISTENING_CALLS = frozenset({
    "bind",
    "listen",
    "serve_forever",
    "create_server",
    "start_server",
    "create_unix_server",
})

# Modules that exist to accept connections. An import is enough to fail:
# there is no legitimate reason for the agent to hold one.
LISTENING_MODULES = frozenset({
    "socketserver",
    "http.server",
    "wsgiref",
    "xmlrpc.server",
})


def _module_violation(node: ast.AST, rel: Path) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in LISTENING_MODULES:
                return f"{rel}:{node.lineno} imports {alias.name}"
    if isinstance(node, ast.ImportFrom) and node.module in LISTENING_MODULES:
        return f"{rel}:{node.lineno} imports from {node.module}"
    return None


def check_no_listener() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT.parent)
        for node in ast.walk(tree):
            found = _module_violation(node, rel)
            if found is not None:
                violations.append(found)
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in LISTENING_CALLS:
                violations.append(
                    f"{rel}:{node.lineno} calls {name}(), which accepts connections"
                )
    return violations
