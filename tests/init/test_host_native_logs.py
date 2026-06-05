"""Tests for the shared host-native-Caddy log_group offer helper."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.init.host_native_logs import (
    DEFAULT_CADDY_ACCESS_LOG,
    offer_caddy_log_group,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "stormpulse.toml"
    p.write_text("# baseline\n")
    return p


def _patch_log_path_exists(exists: bool):
    return patch.object(
        type(DEFAULT_CADDY_ACCESS_LOG), "exists", return_value=exists, autospec=False
    )


def test_no_signal_no_offer(cfg: Path) -> None:
    """No [caddy] section and no access log file → silent no-op, returns False."""
    with _patch_log_path_exists(False):
        assert offer_caddy_log_group(cfg) is False
    assert "log_groups" not in cfg.read_text()


def test_log_file_signal_appends_on_confirm(cfg: Path) -> None:
    """Log file exists, no [caddy] section → prompt, append on yes."""
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=True),
    ):
        assert offer_caddy_log_group(cfg) is True
    raw = tomllib.loads(cfg.read_text())
    assert any(g["name"] == "caddy" for g in raw["log_groups"])
    caddy_group = next(g for g in raw["log_groups"] if g["name"] == "caddy")
    assert caddy_group["source_type"] == "file"
    assert caddy_group["parser"] == "caddy_json"
    assert caddy_group["path"] == str(DEFAULT_CADDY_ACCESS_LOG)


def test_section_signal_appends_on_confirm(cfg: Path) -> None:
    """[caddy] section present, no log file → prompt, append on yes."""
    cfg.write_text(
        '[caddy]\nenabled = true\nadmin_url = "http://localhost:2019"\n'
        'main_caddyfile = "/etc/caddy/Caddyfile"\n'
        'drop_in_path = "/etc/caddy/conf.d/x.caddy"\n'
    )
    with (
        _patch_log_path_exists(False),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=True),
    ):
        assert offer_caddy_log_group(cfg) is True
    raw = tomllib.loads(cfg.read_text())
    assert any(g["name"] == "caddy" for g in raw["log_groups"])


def test_decline_does_not_append(cfg: Path) -> None:
    """Signal present, operator declines → returns False, no append."""
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=False),
    ):
        assert offer_caddy_log_group(cfg) is False
    assert "log_groups" not in cfg.read_text()


def test_idempotent_skip_when_already_present(cfg: Path) -> None:
    """Caddy log_group already in TOML → silent no-op, no prompt, returns False."""
    cfg.write_text(
        '[[log_groups]]\nname = "caddy"\nenabled = true\nsource_type = "file"\n'
        'path = "/var/log/caddy/access.log"\nparser = "caddy_json"\n'
        "ship_interval_seconds = 10\nmax_lines_per_batch = 200\n"
        "retention_days = 90\n"
    )
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm") as mock_prompt,
    ):
        assert offer_caddy_log_group(cfg) is False
        mock_prompt.assert_not_called()
