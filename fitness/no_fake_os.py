"""Function 17: tests run as the user they run as.

A test that patches ``os.geteuid``, ``os.access`` or ``pwd`` proves the branch it
chose, not the host it runs on; CI as root caught two such tests passing for the
wrong reason. Each file reports its count, so the baseline holds debt per file.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Who the process is and what it may touch. Fault injection (os.replace) is not a fake host.
IDENTITY = (
    r"(os\W+(geteuid|getuid|getegid|getgid|getgroups|access|stat|lstat)|pwd|grp)\b"
)
FAKE = re.compile(
    rf"(monkeypatch\.setattr|patch(\.object)?)\(\s*[\"']?[\w.]*?\b{IDENTITY}"
)
# This rule's own test fixtures have to spell the fakes.
SKIP_FILES = {"tests/test_fitness_no_fake_os.py"}


def check_no_fake_os() -> list[str]:
    """Return one violation per test file that fakes the host's identity, with its count."""
    violations: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        count = sum(1 for line in text.splitlines() if FAKE.search(line))
        if count:
            violations.append(
                f"{rel}: {count} fake(s) of the host's identity; run as the real user"
            )
    return violations
