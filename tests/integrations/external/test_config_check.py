"""``stormpulse config check`` resolves external sections the way boot does.

Receipt, 2026-09-12 on staging: the pre-flight printed ``[buckets_gate] unknown
section: ... it will be ignored at boot`` for a sealed adapter that the very
next boot logged as ``Integration 'buckets_gate' live``. The pre-flight knew
only the built-ins. These pin the three outcomes a sealed section can have and
that the report says which one it is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

import stormpulse.integrations.registry as reg
from stormpulse.cli.config_check import cmd_config_check

from ._helpers import approve, keypair, state_dir
from .test_loader import _install_and_seal

_ID = "extcheck"


@pytest.fixture(autouse=True)
def _isolate() -> object:
    saved_integrations = list(reg._integrations)
    saved_modules = set(sys.modules)
    yield
    reg._integrations[:] = saved_integrations
    for name in set(sys.modules) - saved_modules:
        del sys.modules[name]


def _write_toml(tmp_path: Path, state: Path, *, section: str = _ID) -> Path:
    """A core-valid TOML whose state dir (db_path.parent) is ``state``, with one
    ``[section]`` that only a sealed external adapter could claim."""
    for name in ("ca.pem", "agent.pem", "agent-key.pem", "hmac.key"):
        (tmp_path / name).write_text("x")
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    toml = tmp_path / "stormpulse.toml"
    toml.write_text(
        "[agent]\n"
        'id = "test-01"\n'
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n\n'
        "[dashboard]\n"
        'url = "wss://example.com/ws/"\n'
        "reconnect_min_seconds = 1\nreconnect_max_seconds = 30\n"
        "heartbeat_interval_seconds = 30\n\n"
        "[tls]\n"
        f'ca_cert = "{tmp_path / "ca.pem"}"\n'
        f'client_cert = "{tmp_path / "agent.pem"}"\n'
        f'client_key = "{tmp_path / "agent-key.pem"}"\n\n'
        "[auth]\n"
        f'hmac_secret = "{tmp_path / "hmac.key"}"\n'
        "command_max_age_seconds = 60\n\n"
        "[metrics]\npush_interval_seconds = 10\ncollect_containers = false\n\n"
        "[project]\n"
        f'project_dir = "{tmp_path}"\n'
        f'compose_file = "{compose}"\n'
        'docker_service_name = "web"\n\n'
        "[storage]\n"
        f'db_path = "{state / "stormpulse.db"}"\n\n'
        f"[{section}]\nanything = true\n"
    )
    return toml


def _run(toml: Path, capsys: pytest.CaptureFixture[str]) -> str:
    cmd_config_check(argparse.Namespace(config=str(toml)))
    return capsys.readouterr().out


def test_sealed_loadable_adapter_is_reported_live_not_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    _install_and_seal(tmp_path, state, private, fp, integration_id=_ID)

    out = _run(_write_toml(tmp_path, state), capsys)

    assert f"[{_ID}] config OK, enabled (external adapter, sealed grant)" in out
    assert "unknown section" not in out


def test_sealed_but_unloadable_adapter_prints_the_reason_and_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boot soft-disables it and ignores the section; the pre-flight must say
    both, because a silent 'unknown' is the line this file exists to retire."""
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    _install_and_seal(
        tmp_path, state, private, fp, integration_id=_ID, raise_on_import=True
    )

    out = _run(_write_toml(tmp_path, state), capsys)

    assert f"external adapter '{_ID}' failed to load" in out
    assert "boom at import" in out
    assert f"[{_ID}] unknown section" in out


def test_section_with_no_grant_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = state_dir(tmp_path)

    out = _run(_write_toml(tmp_path, state), capsys)

    assert f"[{_ID}] unknown section" in out
    assert "failed to load" not in out
