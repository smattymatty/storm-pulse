"""``failure_reason`` is the closed ``FailureReason`` set (CORE-011 d9). The
type holds every seam mypy sees (``JobOutcome``, the ``_failure`` helpers,
``failure_outcome``); this holds the ones it does not, an ``events.emit``
keyword among them: a reason is spelled as a member, never a string literal."""

from __future__ import annotations

import ast
from pathlib import Path

from stormpulse.protocol import FailureReason

_PACKAGE = Path(__file__).resolve().parent.parent / "stormpulse"
# A reason is written as the ``failure_reason=`` keyword of any call, or as the
# first argument of a ``_failure(...)`` / ``failure_outcome(...)`` helper.
_POSITIONAL_REASON_CALLS = {"_failure", "failure_outcome"}


def _strings(node: ast.AST) -> set[str]:
    """Every string constant under *node*: a bare literal, or each arm of a ternary."""
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def written_literals(module: Path) -> dict[str, int]:
    """``{literal: line}`` for every reason the module writes as a string."""
    found: dict[str, int] = {}
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "failure_reason":
                found.update(dict.fromkeys(_strings(kw.value), kw.value.lineno))
        if _call_name(node) in _POSITIONAL_REASON_CALLS and node.args:
            found.update(dict.fromkeys(_strings(node.args[0]), node.args[0].lineno))
    return found


def test_no_reason_is_written_as_a_literal() -> None:
    """A string where a member belongs passes at runtime (the payload lifts it)
    and fails here, so a typo cannot ride to a node as ``ValueError``."""
    literals = [
        f"{module.relative_to(_PACKAGE.parent)}:{line}: {literal!r}"
        for module in sorted(_PACKAGE.rglob("*.py"))
        for literal, line in written_literals(module).items()
    ]
    assert literals == []


def test_every_member_the_tree_sends_is_declared() -> None:
    """Every ``FailureReason.X`` the package names resolves, so a member removed
    from the enum is caught here as well as by mypy; bucket_cors' four were
    missing from the first cut and only the type found them."""
    named: set[str] = set()
    for module in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):  # skylos: ignore[SKY-P403] bounded by the package
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "FailureReason"
            ):
                named.add(node.attr)
    assert named <= {m.name for m in FailureReason}, named - {
        m.name for m in FailureReason
    }
    assert {
        "OS_ERROR",
        "BUCKET_NOT_EMPTY",
        "PERSIST_FAILED",
        "UNPARSEABLE_OUTPUT",
        "CORS_RULES_STALE",
    } <= named


def test_the_dispatch_refusals_are_members() -> None:
    """The names dispatch answers with, so a consumer can branch on them."""
    for name in (
        "unknown_command",
        "forbidden",
        "cancelled",
        "integration_failed",
        "rclone_error",
    ):
        assert FailureReason(name) is getattr(FailureReason, name.upper())
