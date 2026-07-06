"""Tests for the ``stormpulse rclone init`` subcommand.

Mirrors tests/caddy/test_caddy_init.py: unit tests around each helper,
then end-to-end orchestration with mocked binary detection and prompts.
The section it writes must parse back into a valid RcloneConfig.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.config import ConfigError
from stormpulse.rclone.config import parse_rclone_config
from stormpulse.rclone.init import (
    append_rclone_section,
    find_rclone_binary,
    has_rclone_section,
    remove_rclone_section,
    run_rclone_init,
)
from stormpulse.init import InitError


def _ok_version(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="rclone v1.74.2", stderr="")


# ---------------------------------------------------------------------------
# find_rclone_binary
# ---------------------------------------------------------------------------


class TestFindRcloneBinary:
    def test_returns_path_when_binary_runs(self, tmp_path: Path) -> None:
        fake = tmp_path / "rclone"
        fake.write_text("#!/bin/sh\n")
        with patch("stormpulse.rclone.init.subprocess.run", _ok_version):
            assert find_rclone_binary(str(fake)) == str(fake)

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert find_rclone_binary(str(tmp_path / "nope")) is None

    def test_returns_none_when_version_exits_nonzero(self, tmp_path: Path) -> None:
        fake = tmp_path / "rclone"
        fake.write_text("#!/bin/sh\n")

        def fail(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

        with patch("stormpulse.rclone.init.subprocess.run", fail):
            assert find_rclone_binary(str(fake)) is None

    def test_returns_none_when_binary_hangs(self, tmp_path: Path) -> None:
        fake = tmp_path / "rclone"
        fake.write_text("#!/bin/sh\n")

        def hang(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="rclone", timeout=15)

        with patch("stormpulse.rclone.init.subprocess.run", hang):
            assert find_rclone_binary(str(fake)) is None


# ---------------------------------------------------------------------------
# TOML section helpers
# ---------------------------------------------------------------------------


class TestSectionHelpers:
    def test_has_section_false_when_absent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        assert has_rclone_section(cfg) is False

    def test_has_section_true_when_present(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n\n[rclone]\nenabled = true\n')
        assert has_rclone_section(cfg) is True

    def test_remove_section_preserves_siblings(self) -> None:
        lines = [
            "[agent]\n",
            'id = "x"\n',
            "\n",
            "[rclone]\n",
            "enabled = true\n",
            "\n",
            "[metrics]\n",
            "interval = 60\n",
        ]
        result = remove_rclone_section(lines)
        assert "[rclone]\n" not in result
        assert "[agent]\n" in result
        assert "[metrics]\n" in result


# ---------------------------------------------------------------------------
# append_rclone_section
# ---------------------------------------------------------------------------


class TestAppendSection:
    def test_appends_and_round_trips_to_valid_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        append_rclone_section(cfg, binary_path="/usr/bin/rclone")
        import tomllib

        raw = tomllib.loads(cfg.read_text())
        parsed = parse_rclone_config(raw["rclone"])
        assert parsed.enabled is True
        assert parsed.binary_path == "/usr/bin/rclone"

    def test_existing_section_without_force_is_error(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text("[rclone]\nenabled = true\n")
        with pytest.raises(InitError):
            append_rclone_section(cfg, binary_path="/usr/bin/rclone")

    def test_force_replaces_existing_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[rclone]\nenabled = true\nbinary_path = "/old/rclone"\n')
        append_rclone_section(cfg, binary_path="/new/rclone", force=True)
        import tomllib

        raw = tomllib.loads(cfg.read_text())
        assert raw["rclone"]["binary_path"] == "/new/rclone"


# ---------------------------------------------------------------------------
# run_rclone_init orchestration
# ---------------------------------------------------------------------------


class TestRunRcloneInit:
    def test_writes_section_on_confirm(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        with (
            patch("stormpulse.rclone.init.find_rclone_binary", return_value="/usr/bin/rclone"),
            patch("stormpulse.rclone.init.prompt_confirm", return_value=True),
            patch("stormpulse.rclone.init.prompt", return_value="/usr/bin/rclone"),
            patch("stormpulse.rclone.init.detect_mode") as mode,
            patch("stormpulse.rclone.init.restart_or_hint"),
        ):
            from stormpulse.init.mode import InstallMode

            mode.return_value = InstallMode.SYSTEM
            run_rclone_init(cfg)
        assert has_rclone_section(cfg) is True

    def test_aborts_without_confirm(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        with (
            patch("stormpulse.rclone.init.find_rclone_binary", return_value="/usr/bin/rclone"),
            patch("stormpulse.rclone.init.prompt_confirm", return_value=False),
        ):
            run_rclone_init(cfg)
        assert has_rclone_section(cfg) is False

    def test_missing_binary_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        with patch("stormpulse.rclone.init.find_rclone_binary", return_value=None):
            with pytest.raises(InitError):
                run_rclone_init(cfg)

    def test_unwritable_config_dir_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        with patch("stormpulse.rclone.init.os.access", return_value=False):
            with pytest.raises(InitError):
                run_rclone_init(cfg)
