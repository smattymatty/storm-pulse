#!/usr/bin/env python3
"""Assert pyproject.toml [project].version matches the top CHANGELOG.md entry.

Run before ``uv publish`` per CORE-002. Exits 0 on match, 1 on mismatch.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"

# Matches "## [0.1.6] - 2026-05-18" with version captured. Skips
# "## [Unreleased]" (no date) so the top released entry wins.
_RELEASED_HEADING = re.compile(
    r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}",
    re.MULTILINE,
)


def main() -> int:
    pyproject = tomllib.loads(PYPROJECT.read_text())
    try:
        pyproject_version = pyproject["project"]["version"]
    except KeyError:
        print(f"ERROR: [project].version not found in {PYPROJECT}", file=sys.stderr)
        return 1

    match = _RELEASED_HEADING.search(CHANGELOG.read_text())
    if not match:
        print(f"ERROR: no released entry found in {CHANGELOG}", file=sys.stderr)
        return 1
    changelog_version = match.group(1)

    if pyproject_version != changelog_version:
        print(
            f"ERROR: version mismatch\n"
            f"  pyproject.toml: {pyproject_version}\n"
            f"  CHANGELOG.md:   {changelog_version}",
            file=sys.stderr,
        )
        return 1

    print(f"ok - version {pyproject_version} agrees between pyproject.toml and CHANGELOG.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
