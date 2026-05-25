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
    def test_returns_user_when_rootless_socket_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create a real file at the simulated socket path. os.access(R_OK)
        # will succeed because the file exists and we own it.
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        sock = runtime / "docker.sock"
        sock.touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        assert detect_mode() is InstallMode.USER

    def test_returns_system_when_no_xdg_runtime_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert detect_mode() is InstallMode.SYSTEM

    def test_returns_system_when_socket_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # XDG_RUNTIME_DIR is set but the socket file isn't there.
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert detect_mode() is InstallMode.SYSTEM


class TestResolveMode:
    def test_forced_user_overrides_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert resolve_mode(InstallMode.USER) is InstallMode.USER

    def test_forced_system_overrides_detection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "docker.sock").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        assert resolve_mode(InstallMode.SYSTEM) is InstallMode.SYSTEM

    def test_none_falls_through_to_detect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert resolve_mode(None) is InstallMode.SYSTEM


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
