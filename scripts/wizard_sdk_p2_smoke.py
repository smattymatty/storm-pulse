#!/usr/bin/env python3
"""Executable P2 smoke test (CORE-007 readiness graph + wizard SDK).

Drives the readiness resolver, SDK purity, the mutation engine (commit /
rolled_back / partial_rollback), and the rclone port equivalence end to end, then
asserts no P3/P4 surface leaked in. Exits non-zero unless every assertion holds;
the final line is exactly ``WIZARD_SDK_P2_SMOKE_OK``.

Run: ``.venv/bin/python scripts/wizard_sdk_p2_smoke.py``
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from stormpulse.integrations.readiness import resolve_readiness
from stormpulse.integrations.registry import Integration
from stormpulse.rclone.init import append_rclone_section
from stormpulse.rclone.wizard import RCLONE_WIZARD
from stormpulse.sdk import (
    SDK_API,
    Answer,
    CaddyDropIn,
    Capability,
    CapabilityLiveness,
    ClaimTomlSection,
    InitContext,
    InitPlan,
    ReadinessState,
    RestartOrReload,
    VerifyProbe,
    answers_from,
)
from stormpulse.wizard import ApplyEnv, apply_plan
from stormpulse.wizard.receipt import (
    STATUS_COMMITTED,
    STATUS_PARTIAL_ROLLBACK,
    STATUS_ROLLED_BACK,
)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Cfg:
    enabled = True


def _synthetic_integration(counter: list[str]) -> Integration:
    def parse(raw: dict[str, object]) -> _Cfg:
        return _Cfg()

    def enabled(cfg: _Cfg) -> bool:
        return True

    def preconditions(cfg: _Cfg) -> str | None:
        counter.append("probe")
        return None

    return Integration(
        id="synthetic",
        parse_config=parse,
        enabled=enabled,
        preconditions=preconditions,
        capabilities=(Capability("synthetic.thing.v1", "synthetic"),),
    )


def _check_readiness() -> None:
    calls: list[str] = []
    integ = _synthetic_integration(calls)
    # config-check path: no host probe.
    report = resolve_readiness(integ, {}, run_probe=False)
    assert report.state is ReadinessState.ENABLED, report.state
    assert calls == [], f"config-check path probed the host: {calls}"
    # readiness path: host probe runs; capability live.
    report = resolve_readiness(integ, {}, run_probe=True)
    assert report.state is ReadinessState.READY
    assert calls == ["probe"]
    assert report.capabilities[0].liveness is CapabilityLiveness.LIVE


def _check_sdk_purity() -> None:
    code = (
        "import stormpulse.sdk, sys;"
        "print('\\n'.join(m for m in sys.modules"
        " if m.startswith('stormpulse.') and not m.startswith('stormpulse.sdk')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    leaked = [m for m in out.stdout.splitlines() if m]
    assert leaked == [], f"stormpulse.sdk leaked modules: {leaked}"


def _env(root: Path, **kw: object) -> ApplyEnv:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "stormpulse.toml"
    cfg.write_text('[core]\nagent_id = "x"\n', encoding="utf-8")
    (root / "base").mkdir(exist_ok=True)
    (root / "units").mkdir(exist_ok=True)
    return ApplyEnv(
        config_path=cfg,
        base_dir=root / "base",
        systemd_user_dir=root / "units",
        state_dir=root / "state",
        **kw,  # type: ignore[arg-type]
    )


class _FailingProvider:
    def capture(self, m: CaddyDropIn, env: ApplyEnv) -> bytes | None:
        return None

    def forward(self, m: CaddyDropIn, env: ApplyEnv) -> None:
        (env.base_dir / m.drop_in_name).write_text(m.content, encoding="utf-8")

    def verify(self, m: CaddyDropIn, env: ApplyEnv) -> bool:
        return False  # force rollback

    def compensate(self, m: CaddyDropIn, env: ApplyEnv, pre: bytes | None) -> None:
        raise RuntimeError("reload failed")  # force partial_rollback


def _check_engine(tmp: Path) -> None:
    data = b"guard"
    # commit
    env = _env(tmp / "a", restart=lambda u, a: None, health=lambda u: True)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}), RestartOrReload("stormpulse")),
        summary="commit",
    )
    assert apply_plan(plan, env, agent_id="a").status == STATUS_COMMITTED

    # rolled_back (probe fails)
    env2 = _env(tmp / "b", probe=lambda c: False)
    original = env2.config_path.read_bytes()
    plan2 = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}), VerifyProbe("x.y.v1")),
        summary="rollback",
    )
    assert apply_plan(plan2, env2, agent_id="a").status == STATUS_ROLLED_BACK
    assert env2.config_path.read_bytes() == original

    # partial_rollback (compensation fails, loud)
    env3 = _env(tmp / "c", providers={"caddy.drop_in.v1": _FailingProvider()})
    plan3 = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(CaddyDropIn("d.caddy", "x"),),
        summary="partial",
    )
    receipt = apply_plan(plan3, env3, agent_id="a")
    assert receipt.status == STATUS_PARTIAL_ROLLBACK
    assert receipt.failure is not None and "compensation failed" in receipt.failure


def _check_rclone_equivalence(tmp: Path) -> None:
    base = '[core]\nagent_id = "x"\n'
    legacy = tmp / "legacy.toml"
    legacy.write_text(base, encoding="utf-8")
    append_rclone_section(legacy, binary_path="/usr/bin/rclone")

    sdk = tmp / "sdk.toml"
    sdk.write_text(base, encoding="utf-8")
    env = ApplyEnv(
        config_path=sdk,
        base_dir=tmp / "base2",
        systemd_user_dir=tmp / "units2",
        state_dir=tmp / "state2",
        restart=lambda u, a: None,
        health=lambda u: True,
    )
    answers = answers_from([Answer("binary_path", "/usr/bin/rclone"), Answer("as_runner", "yes")])
    plan = RCLONE_WIZARD.plan(answers, InitContext(mode="user", config_path=str(sdk)))
    assert apply_plan(plan, env, agent_id="a").status == STATUS_COMMITTED
    assert sdk.read_bytes() == legacy.read_bytes(), "rclone SDK config differs from legacy"


def _check_no_p3_p4_surface() -> None:
    # Neither the command surface (P4) nor the external-state surface (P3) is
    # offered by this Pulse version: both host capabilities resolve unmet, and the
    # SDK is still at v1. Proves P2 did not quietly light up a later phase.
    from stormpulse.integrations.readiness import resolve_host_capability

    assert SDK_API == 1
    for token in ("pulse.integration.commands.v1", "pulse.integration.state.v1"):
        status = resolve_host_capability(token)
        assert status.liveness is CapabilityLiveness.UNMET, token


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _check_readiness()
        _check_sdk_purity()
        _check_engine(tmp)
        _check_rclone_equivalence(tmp)
        _check_no_p3_p4_surface()
    print("WIZARD_SDK_P2_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
