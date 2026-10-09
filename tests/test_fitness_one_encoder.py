"""Function 15 (one compact JSON encoder) and the baseline's stale-entry check."""

from __future__ import annotations

from pathlib import Path

from fitness.__main__ import stale_baseline
from fitness.one_encoder import check_one_encoder

_OFFENDER = 'import json\nx = json.dumps({}, sort_keys=True, separators=(",", ":"))\n'


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "stormpulse"
    for rel, source in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return pkg


def test_live_agent_offends_only_where_the_baseline_says() -> None:
    assert check_one_encoder() == [
        "stormpulse/integrations/external/layout.py:76 spells its own compact JSON; use canonical_json",
        "stormpulse/wizard/journal.py:157 spells its own compact JSON; use canonical_json",
        "stormpulse/wizard/receipt.py:56 spells its own compact JSON; use canonical_json",
    ]


def test_a_new_compact_sorted_dumps_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"mod.py": _OFFENDER})
    assert check_one_encoder(root) == [
        "stormpulse/mod.py:2 spells its own compact JSON; use canonical_json"
    ]


def test_the_home_encoder_and_indented_dumps_are_not_violations(tmp_path: Path) -> None:
    pretty = "import json\nx = json.dumps({}, sort_keys=True, indent=2)\n"
    root = _tree(tmp_path, {"sdk/declaration.py": _OFFENDER, "pretty.py": pretty})
    assert check_one_encoder(root) == []


def test_a_baseline_line_no_check_reports_is_stale() -> None:
    assert stale_baseline({"a", "b"}, {"a", "c"}) == ["stale baseline entry: b"]
