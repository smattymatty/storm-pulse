"""SDK-purity and wizard-topology fences (P2, CORE-007 / CORE-000).

Two things import-linter cannot see on its own are checked here by AST:

1. ``stormpulse/sdk/`` is pure Foundation: it imports **no** other ``stormpulse``
   module (only sibling ``stormpulse.sdk`` submodules) and carries no host-mutation
   primitive (``subprocess`` / ``importlib`` / ``runpy`` / ``eval`` / ``exec`` /
   ``os.system``). This is the fence that lets external plugin code and the future
   dynamic loader trust the layer.

2. ``stormpulse/wizard/`` imports only Foundation and its own package: never a
   Feature (the Caddy drop-in is dispatched through the provider registry, not a
   ``from stormpulse.caddy`` import).

An AST check, not a substring grep, so it does not fire on strings/comments and
does catch ``from x import y`` forms.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
_SDK_DIR = _ROOT / "sdk"
_WIZARD_DIR = _ROOT / "wizard"

# Foundation modules the wizard engine (Framework) may import, plus its own package.
_WIZARD_ALLOWED = {
    "stormpulse.sdk",
    "stormpulse.config",
    "stormpulse.protocol",
    "stormpulse.events",
    "stormpulse.wizard",
}
_SDK_FORBIDDEN_IMPORT_ROOTS = {"subprocess", "importlib", "runpy"}
_SDK_FORBIDDEN_CALLS = {"eval", "exec"}


def _imported_modules(source: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module))
    return out


def _forbidden_calls(source: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SDK_FORBIDDEN_CALLS:
            out.append((node.lineno, f"{node.func.id}()"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "system"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            out.append((node.lineno, "os.system()"))
    return out


def _under_stormpulse(module: str) -> bool:
    return module == "stormpulse" or module.startswith("stormpulse.")


def _top_two(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else module


def check_wizard_sdk() -> list[str]:
    """Return violation strings; empty means clean."""
    violations: list[str] = []

    # 1. SDK purity.
    if _SDK_DIR.is_dir():
        for path in sorted(_SDK_DIR.rglob("*.py")):
            rel = str(path.relative_to(_ROOT.parent))
            source = path.read_text(encoding="utf-8")
            for lineno, module in _imported_modules(source):
                if module.split(".")[0] in _SDK_FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"{rel}:{lineno} sdk imports host primitive {module!r}")
                if _under_stormpulse(module) and not module.startswith("stormpulse.sdk"):
                    violations.append(f"{rel}:{lineno} sdk imports non-SDK module {module!r} (must stay pure Foundation)")
            for lineno, call in _forbidden_calls(source):
                violations.append(f"{rel}:{lineno} sdk calls {call}")

    # 2. Wizard imports only Foundation + its own package.
    if _WIZARD_DIR.is_dir():
        for path in sorted(_WIZARD_DIR.rglob("*.py")):
            rel = str(path.relative_to(_ROOT.parent))
            source = path.read_text(encoding="utf-8")
            for lineno, module in _imported_modules(source):
                if _under_stormpulse(module) and _top_two(module) not in _WIZARD_ALLOWED:
                    violations.append(
                        f"{rel}:{lineno} wizard imports {module!r}; the engine may import "
                        "only Foundation + its own package (dispatch via the provider registry)"
                    )

    return violations
