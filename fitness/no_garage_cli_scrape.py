"""Function 5: no Garage CLI scraping outside the migration allowlist.

GARAGE-001 replaces Garage CLI text-scraping with the admin HTTP API one
operation at a time. ``stormpulse/garage/parse.py`` holds the CLI-stdout
parsers; a module consumes scraping only by importing a ``parse_*`` function
from it (the dataclass/exception exports like ``GaragePeer`` are types, not
scraping). This ratchet pins that importer set: an unmigrated operation may
scrape, a migrated one may never regress, and the allowlist only shrinks.
When it empties, ``parse.py`` is deleted and this check goes with it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
_PARSE_MODULE = "stormpulse.garage.parse"
_PARSE_PARENT = "stormpulse.garage"

# Modules still permitted to import a Garage CLI-stdout parser, one entry
# per unmigrated GARAGE-001 operation. Remove an entry the moment its
# operation moves to the admin API; the stale-entry check below then fails
# until you do, so the allowlist can only shrink.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "stormpulse/garage/provision_bucket.py",  # provisioning
        "stormpulse/garage/provision_additional_key.py",  # provisioning
        "stormpulse/garage/delete_provisioned_bucket.py",  # provisioning
        "stormpulse/garage/rotate_key.py",  # provisioning
        "stormpulse/garage/state.py",  # node-telemetry leg (status/key list)
    }
)


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
    scrapers: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT.parent).as_posix()
        if _imports_scraper(tree):
            scrapers.add(rel)
            if rel not in ALLOWLIST:
                violations.append(
                    f"{rel} imports a garage.parse CLI scraper but is not in "
                    "the GARAGE-001 migration allowlist"
                )
    for rel in sorted(ALLOWLIST - scrapers):
        violations.append(
            f"{rel} is allowlisted but no longer imports a garage.parse "
            "scraper - remove it from ALLOWLIST (the ratchet only shrinks)"
        )
    return violations
