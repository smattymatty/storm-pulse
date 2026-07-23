"""Tests for the integration CLI: exit codes, JSON output, hostname confirmation."""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from stormpulse.cli import integration as cli
from stormpulse.integrations.external import install, trust
from tests.integrations.external._helpers import (
    approve,
    installed_dir,
    keypair,
    make_package,
    state_dir,
)


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "json": True,
        "config": "unused",
        "integration_command": None,
        "publisher_command": None,
        "source": None,
        "integration_id": None,
        "key_file": None,
        "label": None,
        "fingerprint": None,
        "confirm_hostname": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _run(args: argparse.Namespace, state: Path) -> int:
    return cli.run(args, state_dir=state, agent_id="agent-1")


def _real_parser() -> argparse.ArgumentParser:
    """The actual CLI parser, so tests exercise argv parsing + cmd_integration,
    not just run() with a hand-built Namespace (the gap that hid the crash)."""
    parser = argparse.ArgumentParser(prog="stormpulse")
    sub = parser.add_subparsers(dest="command")
    cli.add_integration_subparser(sub)
    return parser


_DIGEST = "sha256:" + "0" * 64


def test_bare_publisher_subgroup_is_usage_error_not_crash() -> None:
    # Regression: `stormpulse integration publisher` (no add/list/revoke) used to
    # AttributeError on args.config; it must be a clean usage error.
    with pytest.raises(SystemExit) as exc:
        _real_parser().parse_args(["integration", "publisher"])
    assert exc.value.code == 2


def test_cmd_integration_missing_config_is_usage_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Defense for any config-less subcommand path: usage + exit, never a crash.
    args = argparse.Namespace(integration_command="publisher")  # no config attr
    with pytest.raises(SystemExit) as exc:
        cli.cmd_integration(args)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["integration", "list"],
        ["integration", "grants"],
        ["integration", "inspect", "/src"],
        ["integration", "install", "/src"],
        ["integration", "seal", _DIGEST],
        ["integration", "revoke", _DIGEST, "--capability", "command_contributor"],
        ["integration", "rollback", "obs", _DIGEST],
        ["integration", "doctor"],
        ["integration", "init", "someadapter"],
        ["integration", "publisher", "list"],
        ["integration", "publisher", "revoke", _DIGEST],
    ],
)
def test_every_integration_subcommand_carries_config(argv: list[str]) -> None:
    # Every leaf must apply the --config default, so cmd_integration can load it
    # without the AttributeError this whole test class exists to prevent. A future
    # subcommand that forgets _add_common fails here, not on the operator's shell.
    args = _real_parser().parse_args(argv)
    assert getattr(args, "config", None) is not None


def test_cli_inspect_valid_is_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    make_package(src, private, fingerprint)
    code = _run(_args(integration_command="inspect", source=str(src)), state)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["operation"] == "inspect"
    assert payload["result"]["trust_status"] == "trusted"


def test_cli_install_valid_is_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    make_package(src, private, fingerprint)
    assert _run(_args(integration_command="install", source=str(src)), state) == 0


def test_cli_install_unknown_publisher_is_exit_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)  # publisher not approved
    src = tmp_path / "src"
    make_package(src, private, fingerprint)
    code = _run(_args(integration_command="install", source=str(src)), state)
    assert code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["findings"][0]["code"] == "F7"


def test_cli_install_bad_structure_is_exit_3(tmp_path: Path) -> None:
    code = _run(_args(integration_command="install", source=str(tmp_path / "nope")), state_dir(tmp_path))
    assert code == 3


def test_cli_doctor_error_is_exit_5(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = make_package(src, private, fingerprint)
    install.commit_install(src, state_dir=state, agent_id="a")
    target = installed_dir(state, package_digest)
    os.chmod(target, 0o755)
    (target / "injected.py").write_bytes(b"x")
    assert _run(_args(integration_command="doctor"), state) == 5


def test_cli_publisher_add_wrong_hostname_is_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private, _fingerprint = keypair()
    state = state_dir(tmp_path)
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_file = tmp_path / "key.raw"
    key_file.write_bytes(raw)
    code = _run(
        _args(
            integration_command="publisher",
            publisher_command="add",
            key_file=str(key_file),
            label="k",
            confirm_hostname="not-this-host",
        ),
        state,
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"][0]["code"] == "F8"
    assert trust.list_publishers(state) == []  # no write on a failed confirmation


def test_cli_publisher_add_correct_hostname_works(tmp_path: Path) -> None:
    private, _fingerprint = keypair()
    state = state_dir(tmp_path)
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_file = tmp_path / "key.raw"
    key_file.write_bytes(raw)
    code = _run(
        _args(
            integration_command="publisher",
            publisher_command="add",
            key_file=str(key_file),
            label="k",
            confirm_hostname=socket.gethostname(),
        ),
        state,
    )
    assert code == 0
    assert len(trust.list_publishers(state)) == 1


def test_cli_failure_json_never_leaks_absolute_source_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "deep" / "secret_source_dir"  # does not exist -> F1
    _run(_args(integration_command="install", source=str(src)), state_dir(tmp_path))
    out = capsys.readouterr().out
    assert str(src) not in out
    assert "secret_source_dir" not in out
