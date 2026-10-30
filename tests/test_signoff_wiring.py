"""Tests for what consumes the seal: registry gating, the signoff CLI, the install step."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.commands import CommandSpec, build_registry
from stormpulse.signoff import (
    SignoffState,
)

# ---------------------------------------------------------------------------
# build_registry: signoff_sealed excludes run_verify_block and run_apply_block
# ---------------------------------------------------------------------------


def test_build_registry_includes_seal_gated_commands_when_unsealed() -> None:
    registry = build_registry({}, signoff_sealed=False)
    assert "run_verify_block" in registry
    assert "run_apply_block" in registry


def test_build_registry_excludes_seal_gated_commands_when_sealed() -> None:
    registry = build_registry({}, signoff_sealed=True)
    assert "run_verify_block" not in registry
    assert "run_apply_block" not in registry
    # The other built-ins must still be present. Seal only closes the
    # verify and apply hatches, not the rest of the registry.
    assert "git_pull" in registry
    assert "docker_logs" in registry


def test_build_registry_seal_default_is_unsealed() -> None:
    # Existing callers that don't pass the kwarg keep the prior shape.
    registry = build_registry({})
    assert "run_verify_block" in registry
    assert "run_apply_block" in registry


def test_build_registry_sealed_plus_disabled_still_excludes_both() -> None:
    registry = build_registry(
        {},
        disabled=frozenset({"git_pull"}),
        signoff_sealed=True,
    )
    assert "run_verify_block" not in registry
    assert "run_apply_block" not in registry
    assert "git_pull" not in registry
    assert "docker_logs" in registry


def test_build_registry_sealed_does_not_block_config_command_named_similarly() -> None:
    # Only the built-in seal-gated commands are auto-disabled. A
    # config-defined command with a different name keeps working when
    # sealed.
    custom = CommandSpec(
        group="custom",
        command=["/bin/echo", "ok"],
        timeout=5,
        description="custom",
    )
    registry = build_registry({"my_verify": custom}, signoff_sealed=True)
    assert "my_verify" in registry
    assert "run_verify_block" not in registry
    assert "run_apply_block" not in registry


# ---------------------------------------------------------------------------
# CLI: seal/unseal/status round-trip
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path: Path) -> Path:
    """Write a config TOML with just enough plumbing for load_config()."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    compose = project_dir / "docker-compose.yml"
    compose.write_text("services: {}\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "stormpulse.db"
    creds = tmp_path / "creds"
    creds.mkdir()
    # The CLI only needs storage.db_path to resolve the state dir; we
    # still have to satisfy the other required sections.
    cfg = tmp_path / "stormpulse.toml"
    cfg.write_text(
        f"""
[agent]
id = "test-agent"
pulse_token = "test-token"

[dashboard]
url = "wss://example.test/pulse"
reconnect_min_seconds = 1
reconnect_max_seconds = 30
heartbeat_interval_seconds = 30

[auth]
hmac_secret = "{creds / 'hmac.key'}"
command_max_age_seconds = 60

[tls]
ca_cert = "{creds / 'ca.pem'}"
client_cert = "{creds / 'cert.pem'}"
client_key = "{creds / 'key.pem'}"

[metrics]
push_interval_seconds = 10
collect_containers = false

[project]
project_dir = "{project_dir}"
compose_file = "{compose}"
docker_service_name = "web"

[storage]
db_path = "{db_path}"
"""
    )
    return cfg


def test_cli_unseal_rejects_wrong_hostname(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from stormpulse.cli.signoff import cmd_signoff_seal, cmd_signoff_unseal

    cfg = _write_minimal_config(tmp_path)
    cmd_signoff_seal(argparse.Namespace(config=str(cfg)))
    capsys.readouterr()  # drain

    with pytest.raises(SystemExit) as excinfo:
        cmd_signoff_unseal(
            argparse.Namespace(
                config=str(cfg),
                confirm_hostname="definitely-wrong",
            ),
        )
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "did not match" in err


def test_cli_unseal_non_tty_without_flag_exits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When stdin isn't a TTY and no --confirm-hostname is passed, refuse."""
    from stormpulse.cli.signoff import cmd_signoff_seal, cmd_signoff_unseal

    cfg = _write_minimal_config(tmp_path)
    cmd_signoff_seal(argparse.Namespace(config=str(cfg)))
    capsys.readouterr()

    with (
        patch("stormpulse.cli.signoff.sys.stdin") as fake_stdin,
        pytest.raises(SystemExit) as excinfo,
    ):
        fake_stdin.isatty.return_value = False
        cmd_signoff_unseal(
            argparse.Namespace(config=str(cfg), confirm_hostname=None),
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "interactive confirmation" in err


def test_cli_signoff_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import socket

    from stormpulse.cli.signoff import (
        cmd_signoff_seal,
        cmd_signoff_status,
        cmd_signoff_unseal,
    )

    cfg = _write_minimal_config(tmp_path)
    hostname = socket.gethostname()

    # status -> sealed (state dir is fresh, no marker)
    cmd_signoff_status(argparse.Namespace(config=str(cfg)))
    out = capsys.readouterr().out
    assert "SEALED" in out
    assert "UNSEALED" not in out
    assert "run_verify_block" in out

    # seal on a fresh dir -> already sealed
    cmd_signoff_seal(argparse.Namespace(config=str(cfg)))
    out = capsys.readouterr().out
    assert "Already sealed" in out

    # unseal via the automation-confirmation path
    cmd_signoff_unseal(
        argparse.Namespace(config=str(cfg), confirm_hostname=hostname),
    )
    out = capsys.readouterr().out
    assert "Unsealed" in out

    # unseal again -> idempotent
    cmd_signoff_unseal(
        argparse.Namespace(config=str(cfg), confirm_hostname=hostname),
    )
    out = capsys.readouterr().out
    assert "Already unsealed" in out

    cmd_signoff_status(argparse.Namespace(config=str(cfg)))
    assert "UNSEALED" in capsys.readouterr().out

    cmd_signoff_seal(argparse.Namespace(config=str(cfg)))
    out = capsys.readouterr().out
    assert "Sealed." in out


# ---------------------------------------------------------------------------
# Install-time seal: stormpulse init must ship a sealed agent
# ---------------------------------------------------------------------------


def test_init_step_seals_a_freshly_installed_agent(tmp_path: Path) -> None:
    """The registered init step closes the verify hatch on first install."""
    from stormpulse.signoff.init import signoff_init_step

    cfg = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    seal_file = state_dir / "signoff.sealed"
    assert not seal_file.exists()

    signoff_init_step(cfg)
    assert seal_file.exists()


def test_init_step_is_registered_with_the_orchestrator() -> None:
    """Importing the module wires its step into the init registry."""
    import stormpulse.signoff.init  # noqa: F401 - side-effect import
    from stormpulse.init.registry import registered_init_steps
    from stormpulse.signoff.init import signoff_init_step

    assert signoff_init_step in registered_init_steps()


def test_init_step_runs_on_migration() -> None:
    import stormpulse.signoff.init  # noqa: F401 - side-effect import
    from stormpulse.init.registry import migration_init_steps
    from stormpulse.signoff.init import signoff_init_step

    assert signoff_init_step in migration_init_steps()


def test_init_step_reseals_an_unsealed_agent(tmp_path: Path) -> None:
    from stormpulse.signoff.init import signoff_init_step

    cfg = _write_minimal_config(tmp_path)
    state = SignoffState(tmp_path / "state")
    state.unseal()
    signoff_init_step(cfg)
    assert state.is_sealed() is True
    assert state.path.exists()
