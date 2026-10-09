"""Function 16: every ``emit(`` and ``record_call(`` names a declared shape."""

from __future__ import annotations

from pathlib import Path

from fitness.event_registry import check_event_registry

_DECLARED = (
    'from stormpulse import events\n'
    '_FAILED = "renew_failed"\n'
    '_FIELDS = frozenset({"reason"})\n'
    'events.declare_event("cert", _FAILED, _FIELDS | {"days_remaining"})\n'
    'events.declare_event("garage_admin", "admin_call", {"endpoint", "duration_ms", "status", "http_method"})\n'
)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "stormpulse"
    for rel, source in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return pkg


def test_the_live_agent_declares_every_event_it_emits() -> None:
    assert check_event_registry() == []


def test_a_new_emit_with_an_unlisted_kind_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"a.py": _DECLARED + 'events.emit("renew_skipped", source="cert")\n'},
    )
    assert check_event_registry(root) == [
        "stormpulse/a.py:6 emits cert/renew_skipped nobody declared"
    ]


def test_an_undeclared_field_named_through_record_call_fails(tmp_path: Path) -> None:
    call = (
        'events.record_call("admin_call", source="garage_admin", endpoint="x",'
        ' duration_ms=1, status=200, http_method="GET", caller_ip="1.2.3.4")\n'
    )
    root = _tree(tmp_path, {"a.py": _DECLARED + call})
    assert check_event_registry(root) == [
        "stormpulse/a.py:6 emits garage_admin/admin_call with undeclared field caller_ip"
    ]


def test_record_call_keywords_are_checked_like_emit_fields(tmp_path: Path) -> None:
    src = (
        'from stormpulse import events\n'
        'events.declare_event("garage_admin", "admin_call", {"http_method"})\n'
        'events.record_call("admin_call", source="garage_admin", endpoint="x",'
        ' duration_ms=1, status=200, http_method="GET")\n'
    )
    root = _tree(tmp_path, {"a.py": src})
    assert check_event_registry(root) == [
        f"stormpulse/a.py:3 emits garage_admin/admin_call with undeclared field {f}"
        for f in ("duration_ms", "endpoint", "status")
    ]


def test_constants_unions_and_the_envelope_resolve(tmp_path: Path) -> None:
    ok = (
        'events.emit(_FAILED, source="cert", reason="r", days_remaining=3,'
        ' command_ref="pc-1", **extra)\n'
    )
    root = _tree(tmp_path, {"a.py": _DECLARED + "extra = {}\n" + ok})
    assert check_event_registry(root) == []


def test_a_kind_that_is_not_a_constant_fails_outside_the_home(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "a.py": _DECLARED + 'def f(k):\n    events.emit(k, source="cert")\n',
            "events.py": 'def emit(kind, *, source, **f):\n    pass\n'
            'def fwd(kind, source):\n    emit(kind, source=source)\n',
        },
    )
    assert check_event_registry(root) == [
        "stormpulse/a.py:7 emits a kind or source that is not a string constant"
    ]
