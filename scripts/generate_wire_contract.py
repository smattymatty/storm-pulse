"""Write ``wire-contract.json`` from the live dataclasses.

Run after changing anything an Integration emits, or any command's params:
`make wire-contract`. The file is checked in and reviewed like source. Always
exits 0, even after rewriting: drift detection is Function 9's job, and a
second gate on the same condition teaches the operator to ignore one of them.
"""

from __future__ import annotations

import sys

from fitness.wire_contract import WIRE_CONTRACT_PATH
from stormpulse.agent.wire_contract import render_wire_contract


def main() -> int:
    rendered = render_wire_contract()
    previous = (
        WIRE_CONTRACT_PATH.read_text(encoding="utf-8")
        if WIRE_CONTRACT_PATH.is_file()
        else None
    )
    if previous == rendered:
        print(f"{WIRE_CONTRACT_PATH.name} is up to date.", file=sys.stderr)
        return 0

    WIRE_CONTRACT_PATH.write_text(rendered, encoding="utf-8")
    verb = "Wrote" if previous is None else "Updated"
    print(
        f"{verb} {WIRE_CONTRACT_PATH.name}. Review the diff: it is a change to a "
        "published contract, and every consumer reads it.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
