"""Tests for stormpulse.init.mode -- install mode detection."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.init.mode import (
    InstallMode,
    ModeError,
    detect_mode,
    resolve_mode,
    rootless_socket_path,
    validate_mode_for_euid,
)


class TestRootlessSocketPath:
    def test_returns_path_when_xdg_runtime_dir_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        result = rootless_socket_path()
        assert result == Path("/run/user/1000/docker.sock")

    def test_returns_none_when_xdg_runtime_dir_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert rootless_socket_path() is None


class TestDetectMode:
    @patch("stormpulse.init.mode.os.geteuid", return_value=0)
    def test_returns_system_as_root(self, _mock: object) -> None:
        assert detect_mode() is InstallMode.SYSTEM

    @patch("stormpulse.init.mode.os.geteuid", return_value=1000)
    def test_returns_user_as_non_root(self, _mock: object) -> None:
        assert detect_mode() is InstallMode.USER

    @patch("stormpulse.init.mode.os.geteuid", return_value=1000)
    def test_user_default_ignores_missing_docker_socket(self, _mock: object, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression: pre-0.1.9 the probe would return SYSTEM here and
        # then validate_mode_for_euid would reject the install. Detect
        # must now key off EUID alone so a fresh box -- where rootless
        # dockerd isn't up yet -- still defaults to user mode.
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert detect_mode() is InstallMode.USER


class TestResolveMode:
    @patch("stormpulse.init.mode.os.geteuid", return_value=0)
    def test_forced_user_overrides_detection(self, _mock: object) -> None:
        assert resolve_mode(InstallMode.USER) is InstallMode.USER

    @patch("stormpulse.init.mode.os.geteuid", return_value=1000)
    def test_forced_system_overrides_detection(self, _mock: object) -> None:
        assert resolve_mode(InstallMode.SYSTEM) is InstallMode.SYSTEM

    @patch("stormpulse.init.mode.os.geteuid", return_value=1000)
    def test_none_falls_through_to_detect(self, _mock: object) -> None:
        assert resolve_mode(None) is InstallMode.USER


class TestValidateModeForEuid:
    @patch("stormpulse.init.mode.os.geteuid", return_value=0)
    def test_system_as_root_ok(self, _mock: object) -> None:
        # Should not raise.
        validate_mode_for_euid(InstallMode.SYSTEM)

    @patch("stormpulse.init.mode.os.geteuid", return_value=1000)
    def test_user_as_non_root_ok(self, _mock: object) -> None:
        validate_mode_for_euid(InstallMode.USER)

    @patch("stormpulse.init.mode.os.geteuid", return_value=0)
    def test_user_as_root_refuses(self, _mock: object) -> None:
        with pytest.raises(ModeError, match="Rerun without sudo"):
            validate_mode_for_euid(InstallMode.USER)

    @patch("stormpulse.init.mode.os.geteuid", return_value=1000)
    def test_system_as_non_root_refuses(self, _mock: object) -> None:
        with pytest.raises(ModeError, match="needs root"):
            validate_mode_for_euid(InstallMode.SYSTEM)
