"""Function 11: docs and comments stand on their own.

A contributor reads this repository without Storm's others beside it. A line
that points at one of them instead of describing the behaviour leaves a hole
in the text. The far end of the agent's wire is "the control plane" (see
CONTEXT.md); this walks every tracked text file for lines that point elsewhere.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".skylos", "dist"}
# History stays as written; the glossary line that tells people what to avoid,
# this rule's own pattern and its test fixtures all have to spell the words.
SKIP_FILES = {
    "CHANGELOG.md",
    "self_contained_docs.py",
    "test_fitness_self_contained_docs.py",
}
ALLOW_LINE = "_Avoid_"

PRIVATE = re.compile(
    r"\bthe website\b|\bwebsite's\b|\bwebsite (repo|tree|dashboard)\b"
    r"|Projects/StormDevelopments/|storm-workstation-kit",
    re.IGNORECASE,
)


def check_self_contained_docs() -> list[str]:
    """Return one `path:line` violation per line that points at another repository."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES or path.name in SKIP_FILES:
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ALLOW_LINE in line or not PRIVATE.search(line):
                continue
            rel = path.relative_to(ROOT)
            violations.append(
                f"{rel}:{number} points at another repository; describe it, the far end is 'the control plane'"
            )
    return violations
