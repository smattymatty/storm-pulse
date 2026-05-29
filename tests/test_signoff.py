"""Tests for the sign-off seal — file-presence state, registry gating, CLI."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.commands import COMMAND_REGISTRY, CommandDef, build_registry
from stormpulse.signoff import (
    SignoffState,
    format_unsealed_duration,
    state_dir_from_db_path,
)


# ---------------------------------------------------------------------------
# SignoffState: file-presence semantics
# ---------------------------------------------------------------------------


def test_signoff_state_starts_unsealed(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    assert state.is_sealed() is False
    assert not state.path.exists()


def test_signoff_state_seal_creates_flag_file(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    assert state.seal() is True
    assert state.is_sealed() is True
    assert state.path.exists()
    assert state.path.name == "signoff.sealed"


def test_signoff_state_seal_is_idempotent(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    assert state.seal() is True
    # Second call must return False (no state change), still sealed.
    assert state.seal() is False
    assert state.is_sealed() is True


def test_signoff_state_unseal_removes_flag_file(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    assert state.unseal() is True
    assert state.is_sealed() is False
    assert not state.path.exists()


def test_signoff_state_unseal_is_idempotent(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    # Never sealed in the first place.
    assert state.unseal() is False
    assert state.is_sealed() is False


def test_signoff_state_creates_state_dir_if_missing(tmp_path: Path) -> None:
    # SignoffState should create the parent directory on seal so a
    # fresh install (no nonce DB yet) doesn't blow up the CLI.
    fresh = tmp_path / "does" / "not" / "exist"
    state = SignoffState(fresh)
    assert state.seal() is True
    assert fresh.is_dir()


def test_signoff_state_rereads_on_each_call(tmp_path: Path) -> None:
    # The dispatch-time recheck depends on is_sealed() actually
    # re-statting the path on every call (not caching a startup value).
    state = SignoffState(tmp_path)
    assert state.is_sealed() is False
    # External actor (operator running the CLI) creates the flag.
    (tmp_path / "signoff.sealed").touch()
    assert state.is_sealed() is True
    # And the reverse.
    (tmp_path / "signoff.sealed").unlink()
    assert state.is_sealed() is False


def test_state_dir_from_db_path_returns_parent(tmp_path: Path) -> None:
    db = tmp_path / "stormpulse.db"
    assert state_dir_from_db_path(db) == tmp_path


# ---------------------------------------------------------------------------
# SignoffState: unsealed_since timestamp tracking
# ---------------------------------------------------------------------------


def test_unsealed_since_is_none_when_sealed(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    assert state.unsealed_since() is None


def test_unsealed_since_records_transition_timestamp(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    state.unseal()
    after = datetime.now(timezone.utc) + timedelta(seconds=1)

    since = state.unsealed_since()
    assert since is not None
    assert before <= since <= after


def test_unsealed_since_survives_marker_only_state(tmp_path: Path) -> None:
    """Sealing after an unseal removes the marker; unseal again starts fresh."""
    state = SignoffState(tmp_path)
    state.seal()
    state.unseal()
    first = state.unsealed_since()
    state.seal()
    assert state.unsealed_since() is None
    # Brief delay so the new timestamp differs from the original.
    time.sleep(0.01)
    state.unseal()
    second = state.unsealed_since()
    assert first is not None
    assert second is not None
    assert second > first


def test_unsealed_since_returns_none_when_marker_missing(tmp_path: Path) -> None:
    """Operator who did `rm signoff.sealed` by hand leaves us without a marker."""
    # Manually create the unsealed state without going through the CLI:
    # no seal file present AND no marker file. Treat as unsealed-age-unknown.
    state = SignoffState(tmp_path)
    assert state.is_sealed() is False
    assert state.unsealed_since() is None


def test_unsealed_since_returns_none_on_corrupt_marker(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    state.unseal()
    # Corrupt the marker file
    state.unsealed_at_path.write_text("not an ISO timestamp")
    assert state.unsealed_since() is None


def test_seal_clears_unsealed_at_marker(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    state.unseal()
    assert state.unsealed_at_path.exists()
    state.seal()
    assert not state.unsealed_at_path.exists()


# ---------------------------------------------------------------------------
# format_unsealed_duration
# ---------------------------------------------------------------------------


def test_format_unsealed_duration_none_returns_unknown() -> None:
    assert format_unsealed_duration(None) == "unknown"


def test_format_unsealed_duration_minutes() -> None:
    now = datetime.now(timezone.utc)
    assert format_unsealed_duration(now - timedelta(minutes=12)) == "12m"


def test_format_unsealed_duration_hours_and_minutes() -> None:
    now = datetime.now(timezone.utc)
    assert format_unsealed_duration(now - timedelta(hours=3, minutes=14)) == "3h 14m"


def test_format_unsealed_duration_days_and_hours() -> None:
    now = datetime.now(timezone.utc)
    out = format_unsealed_duration(now - timedelta(days=4, hours=7, minutes=22))
    assert out == "4d 7h"


def test_format_unsealed_duration_below_a_minute() -> None:
    assert format_unsealed_duration(datetime.now(timezone.utc)) == "<1m"


def test_format_unsealed_duration_future_clock_skew() -> None:
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert format_unsealed_duration(future) == "<1m"


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
    custom = CommandDef(
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from stormpulse.cli.signoff import cmd_signoff_seal, cmd_signoff_unseal

    cfg = _write_minimal_config(tmp_path)
    cmd_signoff_seal(argparse.Namespace(config=str(cfg)))
    capsys.readouterr()  # drain

    with pytest.raises(SystemExit) as excinfo:
        cmd_signoff_unseal(
            argparse.Namespace(
                config=str(cfg), confirm_hostname="definitely-wrong",
            ),
        )
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "did not match" in err


def test_cli_unseal_non_tty_without_flag_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
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


def test_cli_signoff_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import socket

    from stormpulse.cli.signoff import (
        cmd_signoff_seal,
        cmd_signoff_status,
        cmd_signoff_unseal,
    )

    cfg = _write_minimal_config(tmp_path)
    hostname = socket.gethostname()

    # status -> unsealed (state dir is fresh, no seal file)
    cmd_signoff_status(argparse.Namespace(config=str(cfg)))
    out = capsys.readouterr().out
    assert "UNSEALED" in out

    # seal
    cmd_signoff_seal(argparse.Namespace(config=str(cfg)))
    out = capsys.readouterr().out
    assert "Sealed" in out

    # status -> sealed
    cmd_signoff_status(argparse.Namespace(config=str(cfg)))
    out = capsys.readouterr().out
    assert "SEALED" in out
    assert "run_verify_block" in out

    # seal again -> idempotent
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
    import stormpulse.signoff.init  # noqa: F401 — side-effect import
    from stormpulse.init.registry import registered_init_steps
    from stormpulse.signoff.init import signoff_init_step

    assert signoff_init_step in registered_init_steps()

