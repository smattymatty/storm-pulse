"""Function 12: CI runs what `make check` runs, on one image, on one runner.

Every check is defined once, in the Makefile, and CI calls it by name; a
command re-spelled in YAML drifts from the one a contributor runs locally.
An empty parse is a violation, so a renamed file cannot pass vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".forgejo" / "workflows"
TEST_WORKFLOW = WORKFLOWS / "test.yml"
MAKEFILE = ROOT / "Makefile"
# `gates` is `check` with these diff-scoped targets moved into `gates-clone`.
CLONE_TARGETS = {"quality", "comments-diff"}

CI_IMAGE = re.compile(r"storm-ci-python:(\S+)")
JOB_RUNNER = re.compile(r"^  ([a-z][a-z0-9-]*):\n    runs-on: (\S+)", re.MULTILINE)
MAKE_CALL = re.compile(r"\bmake\b([^\n]*)")
DIRECT_TOOL = re.compile(
    r"^\s*(?:-\s*)?(?:run:\s*)?(skylos|pytest|mypy|lint-imports|python -m)\b",
    re.MULTILINE,
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_ci_workflow() -> list[str]:
    """Return violation strings; empty list means clean."""
    if not TEST_WORKFLOW.is_file():
        return [f"{TEST_WORKFLOW.relative_to(ROOT)} is missing"]
    test_yml = _text(TEST_WORKFLOW)
    rel = TEST_WORKFLOW.relative_to(ROOT)
    return [
        *_one_image(),
        *_one_runner(rel, test_yml),
        *_every_check_target_runs(rel, test_yml),
        *_gates_is_check(),
        *(
            f"{rel}: runs {tool} directly; call its make target"
            for tool in sorted(set(DIRECT_TOOL.findall(test_yml)))
        ),
    ]


def _one_image() -> list[str]:
    tags = {
        f"{path.name}:{tag}"
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for tag in CI_IMAGE.findall(_text(path))
    }
    distinct = {t.split(":", 1)[1] for t in tags}
    if not distinct:
        return ["no workflow names the storm-ci-python image"]
    if len(distinct) > 1:
        return [f"storm-ci-python tags disagree: {', '.join(sorted(tags))}"]
    return []


def _one_runner(rel: Path, text: str) -> list[str]:
    jobs = JOB_RUNNER.findall(text)
    if not jobs:
        return [f"{rel}: no jobs parsed"]
    return [
        f"{rel}: job {job} runs on {label}, not docker"
        for job, label in jobs
        if label != "docker"
    ]


def _prereqs(target: str) -> set[str]:
    match = re.search(rf"^{target}:(.*)$", _text(MAKEFILE), re.MULTILINE)
    return set(match.group(1).split()) if match else set()


def _gates_is_check() -> list[str]:
    gates = _prereqs("gates")
    if not gates:
        return ["Makefile: no `gates:` prerequisites parsed"]
    want = (_prereqs("check") - CLONE_TARGETS) | {"gates-clone"}
    return [
        *(
            f"Makefile: `make check` runs {t}, `make gates` never does"
            for t in sorted(want - gates)
        ),
        *(
            f"Makefile: `make gates` runs {t}, `make check` never does"
            for t in sorted(gates - want)
        ),
    ]


def _every_check_target_runs(rel: Path, text: str) -> list[str]:
    wanted = _prereqs("check")
    if not wanted:
        return ["Makefile: no `check:` prerequisites parsed"]
    called = {
        t for args in MAKE_CALL.findall(text) for t in args.split() if "=" not in t
    }
    return [
        f"{rel}: `make check` runs {t}, CI never does" for t in sorted(wanted - called)
    ]
