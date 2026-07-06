"""Tests for rclone config parsing and the binary precondition."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from stormpulse.config import ConfigError
from stormpulse.rclone import preconditions
from stormpulse.rclone.config import parse_rclone_config


def test_parse_minimal_config_uses_default_binary() -> None:
    config = parse_rclone_config({"enabled": True})
    assert config.enabled is True
    assert config.binary_path == "/usr/bin/rclone"


def test_parse_binary_path_override() -> None:
    config = parse_rclone_config({"enabled": True, "binary_path": "/opt/bin/rclone"})
    assert config.binary_path == "/opt/bin/rclone"


def test_parse_rejects_relative_binary_path() -> None:
    with pytest.raises(ConfigError):
        parse_rclone_config({"enabled": True, "binary_path": "rclone"})


def test_parse_requires_enabled() -> None:
    with pytest.raises(ConfigError):
        parse_rclone_config({})


def test_precondition_missing_binary_names_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("/usr/bin/rclone")

    monkeypatch.setattr("stormpulse.rclone.preconditions.subprocess.run", raise_missing)
    config = parse_rclone_config({"enabled": True})
    assert preconditions.run_preconditions(config) == "rclone_unavailable"


def test_precondition_nonzero_exit_names_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr("stormpulse.rclone.preconditions.subprocess.run", fake_run)
    config = parse_rclone_config({"enabled": True})
    assert preconditions.run_preconditions(config) == "rclone_unavailable"


def test_precondition_passes_on_clean_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="rclone v1.74.2", stderr=""
        )

    monkeypatch.setattr("stormpulse.rclone.preconditions.subprocess.run", fake_run)
    config = parse_rclone_config({"enabled": True})
    assert preconditions.run_preconditions(config) is None
