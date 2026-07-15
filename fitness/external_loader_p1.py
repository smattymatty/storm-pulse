"""External-loader no-execution fence (CORE-007).

The P1 external loader identifies, verifies, installs, and inspects packages but
never imports or executes their code. This is an AST check (not a substring grep,
which would fire on ``literal_eval`` and miss ``getattr``-obfuscated calls): it
fails if anything in ``integrations/external/`` or the loader CLI glue imports
``importlib``/``runpy``, calls ``eval``/``exec``, or mutates ``sys.path``.

Its value is catching *accidental* reintroduction during later P2/P3 work, when
someone will legitimately add ``importlib`` to a neighboring module.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
_SCOPE_DIRS = [_ROOT / "integrations" / "external"]
_SCOPE_FILES = [_ROOT / "cli" / "integration.py"]

_FORBIDDEN_IMPORT_ROOTS = {"importlib", "runpy"}
_FORBIDDEN_CALLS = {"eval", "exec"}
_SYS_PATH_MUTATORS = {"append", "insert", "extend"}


def check_external_loader_no_execution() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in _scoped_files():
        rel = str(path.relative_to(_ROOT.parent))
        violations.extend(scan_source(rel, path.read_text(encoding="utf-8")))
    return violations


def _scoped_files() -> list[Path]:
    files: list[Path] = []
    for directory in _SCOPE_DIRS:
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    files.extend(path for path in _SCOPE_FILES if path.is_file())
    return files


def scan_source(rel: str, source: str) -> list[str]:
    """Scan one module's source for forbidden execution primitives."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"{rel}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _FORBIDDEN_IMPORT_ROOTS:
                violations.append(f"{rel}:{node.lineno} imports from {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            violations.append(f"{rel}:{node.lineno} calls {node.func.id}()")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SYS_PATH_MUTATORS
            and _is_sys_path(node.func.value)
        ):
            violations.append(f"{rel}:{node.lineno} mutates sys.path")
        elif isinstance(node, ast.Assign):
            violations.extend(
                f"{rel}:{node.lineno} assigns sys.path" for target in node.targets if _is_sys_path(target)
            )
        elif isinstance(node, ast.AugAssign) and _is_sys_path(node.target):
            violations.append(f"{rel}:{node.lineno} augments sys.path")
    return violations


def _is_sys_path(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )
