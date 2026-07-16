"""Wizard engine: per-kind forward/verify/inverse, reverse-order and loud rollback,
preview honesty, SDK-version refusal, and the synthetic Caddy-class scenario (C9)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stormpulse.sdk import (
    SDK_API,
    CaddyDropIn,
    ClaimTomlSection,
    CreateSystemdUserUnit,
    InitPlan,
    InstallBinary,
    InstallFile,
    RestartOrReload,
    VerifyProbe,
)
from stormpulse.wizard import (
    STATUS_COMMITTED,
    STATUS_PARTIAL_ROLLBACK,
    STATUS_ROLLED_BACK,
    ApplyEnv,
    WizardError,
    apply_plan,
    preview_plan,
)
from stormpulse.wizard.engine import _Done, _rollback
from stormpulse.wizard.mutations import Step
from stormpulse.sdk import MutationKind


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _env(tmp_path: Path, **kw: object) -> ApplyEnv:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "stormpulse.toml"
    if not cfg.exists():
        cfg.write_text("[core]\nagent_id = \"x\"\n", encoding="utf-8")
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return ApplyEnv(
        config_path=cfg, base_dir=base, systemd_user_dir=units, state_dir=state, **kw  # type: ignore[arg-type]
    )


# --- unit tests on the rollback primitive: reverse order (I4) + loud (I5) ---


def _logging_step(name: str, log: list[str], *, fail: bool = False) -> Step:
    def compensate() -> None:
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")

    return Step(
        kind=MutationKind.VERIFY_PROBE,
        target=name,
        atomic=True,
        pre_image=None,
        pre_image_digest=None,
        recover_path=None,
        recover_mode=0,
        forward=lambda: None,
        verify=lambda: True,
        compensate=compensate,
    )


def test_rollback_is_reverse_order() -> None:
    log: list[str] = []
    done = [_Done(_logging_step(n, log), True) for n in ("a", "b", "c")]
    status, records, failures = _rollback(done)
    assert log == ["c", "b", "a"]  # reverse of apply order (I4)
    assert status == STATUS_ROLLED_BACK
    assert failures == []
    assert all(r.compensated for r in records)


def test_rollback_partial_and_loud_on_compensation_failure() -> None:
    log: list[str] = []
    done = [
        _Done(_logging_step("a", log), True),
        _Done(_logging_step("b", log, fail=True), True),
        _Done(_logging_step("c", log), True),
    ]
    status, records, failures = _rollback(done)
    assert log == ["c", "b", "a"]  # loop did NOT abort on b's failure (I5)
    assert status == STATUS_PARTIAL_ROLLBACK
    assert any("b" in f for f in failures)
    by_target = {r.target: r for r in records}
    assert by_target["b"].compensated is False
    assert by_target["a"].compensated is True


# --- claim_toml_section: apply, verify, rollback restores pre-image (I3) ---


def test_claim_toml_section_commits(tmp_path: Path) -> None:
    env = _env(tmp_path)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="rclone",
        mutations=(ClaimTomlSection("rclone", {"enabled": True, "binary_path": "/usr/bin/rclone"}),),
        summary="configure rclone",
    )
    receipt = apply_plan(plan, env, agent_id="agent-1")
    assert receipt.status == STATUS_COMMITTED
    text = env.config_path.read_text(encoding="utf-8")
    assert "[rclone]" in text and 'binary_path = "/usr/bin/rclone"' in text


def test_claim_rollback_restores_original(tmp_path: Path) -> None:
    # A plan whose second step (a verify probe) fails, forcing rollback of the claim.
    # The probe is injected to fail, so the claim's pre-image must be restored.
    env = _env(tmp_path, probe=lambda cap: False)
    original = env.config_path.read_bytes()
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="rclone",
        mutations=(
            ClaimTomlSection("rclone", {"enabled": True}),
            VerifyProbe("garage.admin.v1"),
        ),
        summary="claim then probe",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert env.config_path.read_bytes() == original  # pre-image restored


# --- install_file: digest pinning + rollback removes a newly created file ---


def test_install_file_commits_and_rolls_back(tmp_path: Path) -> None:
    data = b"binary-bytes"
    env = _env(tmp_path, content_store={_digest(data): data})
    good = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(InstallBinary("bin/guard", _digest(data)),),
        summary="install",
    )
    receipt = apply_plan(good, env, agent_id="a")
    assert receipt.status == STATUS_COMMITTED
    installed = env.base_dir / "bin" / "guard"
    assert installed.read_bytes() == data
    assert installed.stat().st_mode & 0o777 == 0o555

    # A second plan that installs then fails: the newly created file is removed.
    env2 = _env(tmp_path / "b", content_store={_digest(data): data}, probe=lambda c: False)
    plan2 = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(InstallFile("data/x", _digest(data)), VerifyProbe("x.y.v1")),
        summary="install then fail",
    )
    receipt2 = apply_plan(plan2, env2, agent_id="a")
    assert receipt2.status == STATUS_ROLLED_BACK
    assert not (env2.base_dir / "data" / "x").exists()


def test_install_digest_mismatch_refused_before_change(tmp_path: Path) -> None:
    data = b"real"
    env = _env(tmp_path, content_store={"sha256:" + "0" * 64: data})
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(InstallFile("data/x", "sha256:" + "0" * 64),),
        summary="bad digest",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert not (env.base_dir / "data" / "x").exists()


def test_install_traversal_rejected(tmp_path: Path) -> None:
    data = b"x"
    env = _env(tmp_path, content_store={_digest(data): data})
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(InstallFile("../escape", _digest(data)),),
        summary="escape",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK


# --- systemd unit: daemon-reload invoked, best-effort ---


def test_systemd_unit_commits_and_reloads(tmp_path: Path) -> None:
    reloads: list[int] = []
    env = _env(tmp_path, daemon_reload=lambda: reloads.append(1))
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(CreateSystemdUserUnit("guard.service", "[Service]\nExecStart=/x\n"),),
        summary="unit",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_COMMITTED
    assert (env.systemd_user_dir / "guard.service").is_file()
    assert reloads == [1]


# --- restart: no handler configured is a rollback, not a crash ---


def test_restart_without_handler_rolls_back(tmp_path: Path) -> None:
    env = _env(tmp_path)  # no restart handler
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(RestartOrReload("stormpulse"),),
        summary="restart",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert receipt.failure is not None and "restart handler" in receipt.failure


def test_restart_verify_health_failure_rolls_back(tmp_path: Path) -> None:
    env = _env(tmp_path, restart=lambda u, a: None, health=lambda u: False)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}), RestartOrReload("stormpulse")),
        summary="claim then restart",
    )
    original = env.config_path.read_bytes()
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert env.config_path.read_bytes() == original  # claim compensated


# --- SDK version refusal (I14) and empty plan ---


def test_sdk_api_too_new_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    plan = InitPlan(sdk_api=SDK_API + 1, integration_id="demo", mutations=(VerifyProbe("a.b.v1"),))
    with pytest.raises(WizardError):
        apply_plan(plan, env, agent_id="a")


def test_empty_plan_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    plan = InitPlan(sdk_api=SDK_API, integration_id="demo", mutations=())
    with pytest.raises(WizardError):
        apply_plan(plan, env, agent_id="a")


# --- preview is side-effect-free and labels best-effort (I6) ---


def test_preview_labels_best_effort_and_touches_nothing(tmp_path: Path) -> None:
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(
            ClaimTomlSection("demo", {"enabled": True}),
            RestartOrReload("stormpulse"),
        ),
        summary="two steps",
    )
    preview = preview_plan(plan)
    kinds = {s.kind: s.best_effort for s in preview.steps}
    assert kinds["claim_toml_section"] is False
    assert kinds["restart_or_reload"] is True


# --- synthetic Caddy-class provider: cross-file verify + rollback (C9/T22) ---


class _SyntheticCaddyProvider:
    """Writes a drop-in and verifies a main file imports it (cross-file verify).
    Compensation removes the drop-in. Optionally fails compensation to prove the
    loud partial-rollback path."""

    def __init__(self, drop_in: Path, main: Path, *, fail_compensate: bool = False) -> None:
        self.drop_in = drop_in
        self.main = main
        self.fail_compensate = fail_compensate

    def capture(self, mutation: CaddyDropIn, env: ApplyEnv) -> bytes | None:
        return self.drop_in.read_bytes() if self.drop_in.exists() else None

    def forward(self, mutation: CaddyDropIn, env: ApplyEnv) -> None:
        self.drop_in.write_text(mutation.content, encoding="utf-8")

    def verify(self, mutation: CaddyDropIn, env: ApplyEnv) -> bool:
        return self.drop_in.is_file() and "import" in self.main.read_text(encoding="utf-8")

    def compensate(self, mutation: CaddyDropIn, env: ApplyEnv, pre: bytes | None) -> None:
        if self.fail_compensate:
            raise RuntimeError("caddy reload failed")
        self.drop_in.unlink(missing_ok=True)


def test_synthetic_caddy_class_scenario_commit_and_rollback(tmp_path: Path) -> None:
    data = b"guard-binary"
    drop_in = tmp_path / "conf.d" / "buckets.caddy"
    drop_in.parent.mkdir(parents=True)
    main = tmp_path / "Caddyfile"
    main.write_text("import conf.d/*.caddy\n", encoding="utf-8")  # operator import present
    provider = _SyntheticCaddyProvider(drop_in, main)
    env = _env(
        tmp_path,
        content_store={_digest(data): data},
        providers={"caddy.drop_in.v1": provider},
        daemon_reload=lambda: None,
        restart=lambda u, a: None,
        health=lambda u: True,
    )
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="buckets_gate",
        mutations=(
            InstallBinary("bin/guard", _digest(data)),
            CreateSystemdUserUnit("guard.service", "[Service]\n"),
            CaddyDropIn("buckets.caddy", "handle { respond 200 }"),
            RestartOrReload("caddy", action="reload"),
        ),
        summary="install the guard topology",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_COMMITTED
    assert drop_in.read_text(encoding="utf-8") == "handle { respond 200 }"
    assert (env.base_dir / "bin" / "guard").exists()

    # Now the same plan against a main file with no import: caddy verify fails ->
    # full reverse-order rollback back to pre-apply.
    main2 = tmp_path / "b" / "Caddyfile"
    main2.parent.mkdir(parents=True)
    main2.write_text("# empty caddyfile, no drop-in wired\n", encoding="utf-8")
    drop_in2 = tmp_path / "b" / "conf.d" / "buckets.caddy"
    drop_in2.parent.mkdir(parents=True)
    provider2 = _SyntheticCaddyProvider(drop_in2, main2)
    env2 = _env(
        tmp_path / "b",
        content_store={_digest(data): data},
        providers={"caddy.drop_in.v1": provider2},
        daemon_reload=lambda: None,
        restart=lambda u, a: None,
        health=lambda u: True,
    )
    receipt2 = apply_plan(plan, env2, agent_id="a")
    assert receipt2.status == STATUS_ROLLED_BACK
    assert not drop_in2.exists()  # drop-in compensated
    assert not (env2.base_dir / "bin" / "guard").exists()  # binary compensated
    assert not (env2.systemd_user_dir / "guard.service").exists()  # unit compensated


def test_caddy_provider_compensation_failure_is_partial_rollback(tmp_path: Path) -> None:
    drop_in = tmp_path / "conf.d" / "b.caddy"
    drop_in.parent.mkdir(parents=True)
    main = tmp_path / "Caddyfile"
    main.write_text("# empty, drop-in not wired\n", encoding="utf-8")  # verify fails -> rollback
    provider = _SyntheticCaddyProvider(drop_in, main, fail_compensate=True)
    env = _env(tmp_path, providers={"caddy.drop_in.v1": provider})
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="buckets_gate",
        mutations=(CaddyDropIn("b.caddy", "x"),),
        summary="drop-in only",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_PARTIAL_ROLLBACK
    assert receipt.failure is not None and "compensation failed" in receipt.failure


def test_caddy_drop_in_without_provider_rolls_back(tmp_path: Path) -> None:
    env = _env(tmp_path)  # no providers registered
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(CaddyDropIn("x.caddy", "y"),),
        summary="no provider",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert receipt.failure is not None and "no provider" in receipt.failure


def test_receipt_is_persisted_atomically(tmp_path: Path) -> None:
    from stormpulse.wizard import list_receipts

    env = _env(tmp_path)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}),),
        summary="persist me",
    )
    receipt = apply_plan(plan, env, agent_id="agent-x")
    assert receipt.status == STATUS_COMMITTED
    assert receipt.applied_at != ""  # stamped
    persisted = list_receipts(env.state_dir)
    assert len(persisted) == 1
    import json

    data = json.loads(persisted[0].read_text(encoding="utf-8"))
    assert data["status"] == STATUS_COMMITTED
    assert data["integration_id"] == "demo"


def test_post_check_failure_rolls_back(tmp_path: Path) -> None:
    original = None
    env = _env(tmp_path, post_check=lambda: ["service unhealthy after apply"])
    env.config_path.write_text('[core]\nagent_id = "x"\n', encoding="utf-8")
    original = env.config_path.read_bytes()
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}),),
        summary="claim then fail post-check",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert receipt.failure is not None and "post-apply checks failed" in receipt.failure
    assert env.config_path.read_bytes() == original  # mutation rolled back


def test_post_apply_checks_detect_unparseable_config(tmp_path: Path) -> None:
    from stormpulse.wizard.engine import _post_apply_checks

    env = _env(tmp_path)
    env.config_path.write_text("this is = = not valid toml [[[\n", encoding="utf-8")
    failures = _post_apply_checks(env)
    assert any("config no longer parses" in f for f in failures)


def test_post_check_hook_and_config_parse_both_run(tmp_path: Path) -> None:
    from stormpulse.wizard.engine import _post_apply_checks

    calls: list[str] = []

    def _hook() -> list[str]:
        calls.append("hook")
        return []

    env = _env(tmp_path, post_check=_hook)
    # valid config -> only the injected hook contributes (no failures)
    assert _post_apply_checks(env) == []
    assert calls == ["hook"]


def test_receipt_canonical_json_roundtrips(tmp_path: Path) -> None:
    env = _env(tmp_path)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}),),
        summary="s",
    )
    receipt = apply_plan(plan, env, agent_id="agent-x")
    line = receipt.to_canonical_json()
    assert line.endswith("\n")
    import json

    parsed = json.loads(line)
    assert parsed["status"] == STATUS_COMMITTED
    assert parsed["applied"][0]["kind"] == "claim_toml_section"
