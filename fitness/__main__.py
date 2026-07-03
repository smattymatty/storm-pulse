"""Fitness suite harness.

Runs Functions 2 through 6 (CORE-001 defines 2-4; CORE-005 governance adds
5 and 6). Function 1 (layer topology) is enforced separately by
``lint-imports`` (see Makefile target ``fitness``).

Discipline: run every check, report every violation, exit non-zero
on any. Never fail-fast - a run that stops at the first violation
hides the others.

Baseline: ``fitness/baseline.txt`` suppresses known violations by
exact match. The rule is that the baseline only shrinks; new
violations are fixed, not parked.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fitness.dependency_allowlist import check_dependencies
from fitness.integration_contract import check_integration_contract
from fitness.merge_fence import check_merge_fence
from fitness.no_shell import check_no_shell
from fitness.private_imports import check_private_imports

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.txt"


def load_baseline() -> set[str]:
    if not BASELINE_PATH.is_file():
        return set()
    return {
        line.strip()
        for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def main() -> int:
    baseline = load_baseline()
    findings: list[tuple[str, list[str]]] = []
    total = 0
    for label, check in [
        ("Function 2 - no cross-boundary private imports", check_private_imports),
        ("Function 3 - no shell=True", check_no_shell),
        ("Function 4 - runtime dependency allowlist", check_dependencies),
        ("Function 5 - integration contract", check_integration_contract),
        ("Function 6 - merge-primitive fence", check_merge_fence),
    ]:
        violations = [v for v in check() if v not in baseline]
        findings.append((label, violations))
        total += len(violations)

    if total == 0:
        print("Fitness: all checks passed.", file=sys.stderr)
        return 0

    print(f"Fitness: {total} violation(s).", file=sys.stderr)
    for label, violations in findings:
        if not violations:
            print(f"\n  [PASS] {label}", file=sys.stderr)
            continue
        print(f"\n  [FAIL] {label} - {len(violations)} violation(s):", file=sys.stderr)
        for v in violations:
            print(f"    {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
