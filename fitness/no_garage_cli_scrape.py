"""Function 5: no Garage CLI scraping.

GARAGE-001 replaces Garage CLI text-scraping with the admin HTTP API.
``stormpulse/garage/parse.py`` holds the CLI-stdout parsers; a module
scrapes only by importing a ``parse_*`` function from it (the dataclass
and exception exports like ``GaragePeer`` are types, not scraping). Every
such import is a violation. The check is red until the migration is
finished and goes green the moment the last operation moves to the admin
API, at which point ``parse.py`` is deleted and this function with it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
_PARSE_MODULE = "stormpulse.garage.parse"
_PARSE_PARENT = "stormpulse.garage"


def _imports_scraper(tree: ast.AST) -> bool:
    """True if the module pulls a Garage CLI-stdout parser into scope.

    Catches the three import forms; a whole-module import is flagged
    conservatively because it grants access to every ``parse_*`` function.
    A ``parse_*``-free ``from ...parse import GaragePeer`` is a type import,
    not scraping, so it does not count.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _PARSE_MODULE and any(
                alias.name.startswith("parse_") for alias in node.names
            ):
                return True
            if node.module == _PARSE_PARENT and any(
                alias.name == "parse" for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name == _PARSE_MODULE for alias in node.names):
                return True
    return False


def check_no_garage_cli_scrape() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT.parent).as_posix()
        if _imports_scraper(tree):
            violations.append(f"{rel} imports a garage.parse CLI scraper")
    return violations
