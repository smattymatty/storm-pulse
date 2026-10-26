"""Function 20 (every hatch execution rechecks the seal) catches each drift it exists for."""

from __future__ import annotations

from typing import Any

import pytest

from fitness import hatch_reseal
from fitness.hatch_reseal import check_hatch_rechecks_seal
from stormpulse.agent import dispatch


def test_live_dispatch_is_clean() -> None:
    assert check_hatch_rechecks_seal() == []


def test_a_gate_that_never_refuses_fails_both_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch, "refusal_for", lambda *_a: None)
    violations = check_hatch_rechecks_seal()
    assert (
        "sequence run_verify_block (stop_on_failure=False): spawned /bin/bash "
        "after an earlier step resealed"
    ) in violations
    assert (
        "request run_apply_block: spawned /bin/bash after a reseal before dispatch"
    ) in violations
    assert len(violations) == 6  # 2 hatches x (2 sequence + 1 single)


def test_a_probe_that_never_reaches_the_shell_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = hatch_reseal._Probe.run

    def blind(self: Any, argv: list[str], *a: Any, **k: Any) -> Any:
        return real(self, ["/usr/bin/true"], *a, **k)

    monkeypatch.setattr(hatch_reseal._Probe, "run", blind)
    violations = check_hatch_rechecks_seal()
    assert (
        "request run_verify_block: unsealed control never reached the shell; "
        "probe is blind"
    ) in violations
    assert len(violations) == 6
