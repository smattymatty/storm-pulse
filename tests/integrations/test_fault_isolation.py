"""An integration's ordinary callback failure cannot disable its siblings."""

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

import pytest

from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.cli import config_check
from stormpulse.cli import integration as integration_cli
from stormpulse.config import CommandSpec
from stormpulse.integrations import Integration
from stormpulse.integrations.readiness import readiness_failed, resolve_readiness
from stormpulse.sdk import Capability, CapabilityLiveness
from tests.helpers import build_config


def broken(_config: object) -> NoReturn:
    raise RuntimeError("secret=DO_NOT_REPORT")


def descriptor() -> Integration:
    return Integration(
        "demo",
        lambda c: c,
        lambda c: True,
        specs=lambda c: {
            "demo_ping": CommandSpec(group="demo", command=["/bin/true"], timeout=5)
        },
        capabilities=(Capability("demo.check.v1", "demo"),),
    )


@pytest.mark.parametrize("stage", ["parse_config", "enabled", "preconditions", "specs"])
def test_boot_contains_failure_and_keeps_healthy_sibling(
    tmp_path: Path, stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    changes: dict[str, Any] = {stage: broken}
    failing = replace(descriptor(), **changes)
    healthy = Integration(
        "healthy",
        lambda c: c,
        lambda c: True,
        specs=lambda c: {
            "healthy_ping": CommandSpec(
                group="healthy", command=["/bin/true"], timeout=5
            )
        },
    )
    config = replace(build_config(tmp_path), integrations={"demo": {}, "healthy": {}})
    with (
        patch(
            "stormpulse.agent.bootstrap.registered_integrations",
            return_value=(failing, healthy),
        ),
        patch(
            "stormpulse.agent.bootstrap.load_and_register_external",
            return_value=frozenset(),
        ),
    ):
        deps = build_agent_dependencies(
            config, signoff_sealed=True, log_position_store=None
        )
    assert deps.integrations["demo"].status == "disabled_error"
    assert deps.integrations["healthy"].status == "live"
    assert "demo_ping" not in deps.registry and "healthy_ping" in deps.registry
    reason = deps.integrations["demo"].disabled_reason or ""
    assert "doctor demo" in reason and "RuntimeError" in reason
    assert "DO_NOT_REPORT" not in reason + caplog.text


@pytest.mark.parametrize(
    "stage", ["parse_config", "enabled", "preconditions", "readiness"]
)
def test_failed_check_is_not_ready_or_live(stage: str) -> None:
    changes: dict[str, Any] = {stage: broken}
    report = resolve_readiness(replace(descriptor(), **changes), {}, run_probe=True)
    assert not report.baseline_live
    assert readiness_failed(report)
    assert all(c.liveness is not CapabilityLiveness.LIVE for c in report.capabilities)
    assert "DO_NOT_REPORT" not in (report.reason or "")


@pytest.mark.parametrize("value", [None, "false", 0, []])
def test_wrong_enabled_return_is_failure_not_choice(value: object) -> None:
    def callback(_config: object) -> Any:
        return value

    report = resolve_readiness(
        replace(descriptor(), enabled=callback), {}, run_probe=False
    )
    assert readiness_failed(report)
    choice = resolve_readiness(
        replace(descriptor(), enabled=lambda c: False), {}, run_probe=True
    )
    assert not readiness_failed(choice)


@pytest.mark.parametrize("error", [SystemExit, KeyboardInterrupt])
def test_process_control_is_not_contained(error: type[BaseException]) -> None:
    def interrupt(_config: object) -> bool:
        raise error()

    with pytest.raises(error):
        resolve_readiness(replace(descriptor(), enabled=interrupt), {}, run_probe=False)


def test_config_check_contains_failure_without_probing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = replace(build_config(tmp_path), integrations={"demo": {}})
    integ = replace(descriptor(), enabled=broken)
    with (
        patch("stormpulse.config.load_config", return_value=config),
        patch.object(type(config), "validate_paths"),
        patch("stormpulse.integrations.registered_integrations", return_value=(integ,)),
        patch.object(
            config_check, "_load_external_reporting_failures", return_value=frozenset()
        ),
    ):
        config_check.cmd_config_check(
            argparse.Namespace(config=tmp_path / "config.toml")
        )
    output = capsys.readouterr().out
    assert "disabled_error" in output and "doctor demo" in output
    assert "DO_NOT_REPORT" not in output


def test_doctor_returns_error_for_failed_enablement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = resolve_readiness(
        replace(descriptor(), enabled=broken), {}, run_probe=True
    )
    args = argparse.Namespace(
        integration_command="doctor", integration_id=None, recover=False, json=True
    )
    with patch(
        "stormpulse.integrations.readiness.resolve_all", return_value={"demo": report}
    ):
        assert (
            integration_cli.run(
                args,
                state_dir=tmp_path,
                agent_id="agent",
                integrations_config={"demo": {}},
            )
            == 4
        )
    assert "doctor demo" in capsys.readouterr().out


@pytest.mark.parametrize('value', [42, '', '   '])
def test_invalid_precondition_reason_is_actionable_failure(value: object) -> None:
    def callback(_config: object) -> Any:
        return value

    report = resolve_readiness(
        replace(descriptor(), preconditions=callback), {}, run_probe=True
    )
    assert readiness_failed(report)
    assert 'doctor demo' in (report.reason or '')
