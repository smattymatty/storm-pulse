"""Write ``log-line-contract.json`` from the declared log-line shapes.

Run after changing what any parser emits: `make log-line-contract`. The
generated file is checked in and reviewed like source, because a consumer in
another repository vendors it and asserts against it.

Deliberately NOT part of ``wire-contract.json``: that artifact's digest is
advertised at register and gates a destructive-sweep refusal, so a logging
change must not be able to move it.

Always exits 0, including when it rewrote the file. The drift check belongs to
the test suite, which already fails; a generator that also failed would be a
second gate on one condition.
"""

from __future__ import annotations

import sys
from pathlib import Path

from stormpulse.logging.wire_shape import render_log_line_contract

LOG_LINE_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "log-line-contract.json"


def main() -> int:
    rendered = render_log_line_contract()
    previous = (
        LOG_LINE_CONTRACT_PATH.read_text(encoding="utf-8")
        if LOG_LINE_CONTRACT_PATH.is_file()
        else None
    )
    if previous == rendered:
        print(f"{LOG_LINE_CONTRACT_PATH.name} is up to date.", file=sys.stderr)
        return 0

    LOG_LINE_CONTRACT_PATH.write_text(rendered, encoding="utf-8")
    verb = "Wrote" if previous is None else "Updated"
    print(
        f"{verb} {LOG_LINE_CONTRACT_PATH.name}. Review the diff: it is a change "
        "to a published contract, and a consumer in another repo asserts "
        "against it.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
