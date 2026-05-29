"""Tests for stormpulse.garage.preconditions (ADR GARAGE-000).

Each check is exercised independently with mocked subprocess output.
``run_preconditions`` is exercised end-to-end to confirm short-circuit
ordering: substrate is cheapest and runs first; version is the handshake;
rpc_secret is the full round-trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from stormpulse.config import GarageConfig
from stormpulse.garage import preconditions
from stormpulse.garage.state import GarageState


def _make_config(tmp_path: Path) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=tmp_path / "garage.toml",
        state_push_interval_seconds=30,
    )


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# Note: the original GARAGE-000 included a substrate precondition that
# asserted /var/lib/garage/{meta,data} were ZFS mounts. It was dropped
# when CELLAR-003 was amended (alpha provider's LVM-ext4 topology made
# ZFS-on-clean-disk unworkable; durability moved up to garage.toml).
# See CELLAR-003 amendment + the preconditions.py module docstring.


# ---- check_garage_version -------------------------------------------


class TestCheckGarageVersion:
    def test_v2_passes(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(0, stdout="garage v2.2.0\n"),
        ):
            assert preconditions.check_garage_version(cfg) is None

    def test_v1_fails(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(0, stdout="garage v1.0.1\n"),
        ):
            assert preconditions.check_garage_version(cfg) == "garage_version_unsupported"

    def test_v0_fails(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(0, stdout="garage v0.9.4\n"),
        ):
            assert preconditions.check_garage_version(cfg) == "garage_version_unsupported"

    def test_v3_fails(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(0, stdout="garage v3.0.0\n"),
        ):
            assert preconditions.check_garage_version(cfg) == "garage_version_unsupported"

    def test_container_down_fails_unreachable(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(1, stderr="Error: No such container: garaged"),
        ):
            assert preconditions.check_garage_version(cfg) == "garage_unreachable"

    def test_docker_missing_fails_unreachable(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            side_effect=FileNotFoundError("docker"),
        ):
            assert preconditions.check_garage_version(cfg) == "garage_unreachable"


# ---- check_rpc_secret -----------------------------------------------


class TestCheckRpcSecret:
    def test_status_ok_passes(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(0, stdout="==== HEALTHY NODES ====\n..."),
        ):
            assert preconditions.check_rpc_secret(cfg) is None

    def test_auth_failure_classifies_secret(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(
                1, stderr=(
                    "ServerConn::run: Handshake error: performing handshake: "
                    "failed opening client secret box"
                ),
            ),
        ):
            assert preconditions.check_rpc_secret(cfg) == "rpc_secret_unauthenticated"

    def test_handshake_failure_classifies_secret(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(1, stderr="handshake failed"),
        ):
            assert preconditions.check_rpc_secret(cfg) == "rpc_secret_unauthenticated"

    def test_unrelated_nonzero_classifies_unreachable(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            return_value=_completed(1, stderr="connection refused"),
        ):
            assert preconditions.check_rpc_secret(cfg) == "garage_unreachable"

    def test_timeout_classifies_unreachable(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.preconditions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["garage", "status"], timeout=15),
        ):
            assert preconditions.check_rpc_secret(cfg) == "garage_unreachable"


# ---- run_preconditions orchestrator ---------------------------------


class TestRunPreconditions:
    def test_all_pass_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch.multiple(
            preconditions,
            check_garage_version=lambda c: None,
            check_rpc_secret=lambda c: None,
        ):
            assert preconditions.run_preconditions(cfg) is None

    def test_version_short_circuits_rpc(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        rpc_calls = []

        def fake_rpc(_c: GarageConfig) -> str | None:
            rpc_calls.append(1)
            return None

        with patch.multiple(
            preconditions,
            check_garage_version=lambda c: "garage_version_unsupported",
            check_rpc_secret=fake_rpc,
        ):
            assert preconditions.run_preconditions(cfg) == "garage_version_unsupported"
        assert rpc_calls == [], "rpc check ran despite version failure"

    def test_rpc_failure_returns_named_reason(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch.multiple(
            preconditions,
            check_garage_version=lambda c: None,
            check_rpc_secret=lambda c: "rpc_secret_unauthenticated",
        ):
            assert preconditions.run_preconditions(cfg) == "rpc_secret_unauthenticated"


# ---- GarageState.disabled() factory ---------------------------------


class TestGarageStateDisabled:
    def test_disabled_sentinel_carries_reason(self) -> None:
        state = GarageState.disabled("garage_unreachable")
        assert state.disabled_reason == "garage_unreachable"
        assert state.healthy is False
        assert state.node_id == ""
        assert state.buckets == []
        assert state.keys == []
        assert state.peers == []

    def test_disabled_round_trips_to_dict(self) -> None:
        state = GarageState.disabled("garage_unreachable")
        as_dict = state.to_dict()
        assert as_dict["disabled_reason"] == "garage_unreachable"
        assert as_dict["healthy"] is False


# ---- Bootstrap integration: AgentDependencies carries the reason ----


class TestBootstrapWiring:
    def test_disabled_reason_propagates_to_agent_dependencies(
        self, tmp_path: Path,
    ) -> None:
        from stormpulse.agent import bootstrap
        from tests.helpers import build_config, build_garage_config

        cfg = build_config(tmp_path, garage=build_garage_config(tmp_path))
        with patch.object(
            bootstrap, "run_garage_preconditions",
            return_value="garage_unreachable",
        ):
            deps = bootstrap.build_agent_dependencies(
                cfg, signoff_sealed=False, log_position_store=None,
            )
        assert deps.garage_disabled_reason == "garage_unreachable"
        # Garage command set is absent when preconditions fail.
        assert not any(k.startswith("garage_") for k in deps.registry)

    def test_passing_preconditions_registers_garage_commands(
        self, tmp_path: Path,
    ) -> None:
        from stormpulse.agent import bootstrap
        from tests.helpers import build_config, build_garage_config

        cfg = build_config(tmp_path, garage=build_garage_config(tmp_path))
        with patch.object(
            bootstrap, "run_garage_preconditions", return_value=None,
        ):
            deps = bootstrap.build_agent_dependencies(
                cfg, signoff_sealed=False, log_position_store=None,
            )
        assert deps.garage_disabled_reason is None
        assert any(k.startswith("garage_") for k in deps.registry)
