"""Fitness suite harness: Functions 2 through 12, every check, every violation.

CORE-001 defines 1-4 (1 is ``lint-imports``), CORE-005 adds 5-6, CORE-007 7-8,
CORE-008 9; 10-11 guard what readers are told; 12 holds CI to `make check`.
Never fail-fast: one stop hides the rest. ``fitness/baseline.txt`` suppresses
known violations by exact match and only shrinks.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fitness.ci_workflow import check_ci_workflow
from fitness.dependency_allowlist import check_dependencies
from fitness.external_loader_p1 import check_external_loader_no_execution
from fitness.integration_contract import check_integration_contract
from fitness.merge_fence import check_merge_fence
from fitness.no_listener import check_no_listener
from fitness.no_shell import check_no_shell
from fitness.private_imports import check_private_imports
from fitness.self_contained_docs import check_self_contained_docs
from fitness.wire_contract import check_wire_contract
from fitness.wizard_sdk_p2 import check_wizard_sdk

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
        (
            "Function 7 - external loader no-execution",
            check_external_loader_no_execution,
        ),
        ("Function 8 - wizard SDK purity and topology", check_wizard_sdk),
        ("Function 9 - declared wire shape", check_wire_contract),
        ("Function 10 - no listening socket", check_no_listener),
        (
            "Function 11 - docs and comments stand on their own",
            check_self_contained_docs,
        ),
        ("Function 12 - CI runs make check", check_ci_workflow),
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
