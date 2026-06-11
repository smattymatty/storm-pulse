"""Tests for the shared host-native-Caddy log_group offer helper."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from stormpulse.init.host_native_logs import (
    DEFAULT_CADDY_ACCESS_LOG,
    DEFAULT_CADDY_EVENTS_LOG,
    offer_caddy_events_log_group,
    offer_caddy_log_group,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "stormpulse.toml"
    p.write_text("# baseline\n")
    return p


def _patch_log_path_exists(exists: bool) -> Any:
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
    # Must be `source_path` (the schema key the loader requires for file sources),
    # NOT `path`. Asserting `path` here is what let the 2026-06-06 crash-loop ship:
    # the template wrote `path`, the loader needs `source_path`, and this test
    # rubber-stamped the wrong key.
    assert "path" not in caddy_group
    assert caddy_group["source_path"] == str(DEFAULT_CADDY_ACCESS_LOG)


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


# ---------------------------------------------------------------------------
# offer_caddy_events_log_group - the cert-lifecycle sibling
# ---------------------------------------------------------------------------
#
# Regression, 2026-06-11: certmagic events (tls.obtain etc.) never appear in
# the access log, so without this group cert_obtained never ships and every
# custom domain sticks on CERTIFICATE PENDING while the site serves fine.


def test_events_no_signal_no_offer(cfg: Path) -> None:
    """No [caddy] section and no events log file → silent no-op."""
    with _patch_log_path_exists(False):
        assert offer_caddy_events_log_group(cfg) is False
    assert "caddy-events" not in cfg.read_text()


def test_events_file_signal_appends_on_confirm(cfg: Path) -> None:
    """events.log exists → prompt, append on yes, schema keys correct."""
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=True),
    ):
        assert offer_caddy_events_log_group(cfg) is True
    raw = tomllib.loads(cfg.read_text())
    group = next(g for g in raw["log_groups"] if g["name"] == "caddy-events")
    assert group["source_type"] == "file"
    assert group["parser"] == "caddy_json"
    # source_path, never path - same loader-schema trap as the access group.
    assert "path" not in group
    assert group["source_path"] == str(DEFAULT_CADDY_EVENTS_LOG)


def test_events_section_signal_appends_on_confirm(cfg: Path) -> None:
    """[caddy] section present, no events file → still offered."""
    cfg.write_text(
        '[caddy]\nenabled = true\nadmin_url = "http://localhost:2019"\n'
        'main_caddyfile = "/etc/caddy/Caddyfile"\n'
        'drop_in_path = "/etc/caddy/conf.d/x.caddy"\n'
    )
    with (
        _patch_log_path_exists(False),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=True),
    ):
        assert offer_caddy_events_log_group(cfg) is True
    raw = tomllib.loads(cfg.read_text())
    assert any(g["name"] == "caddy-events" for g in raw["log_groups"])


def test_events_decline_does_not_append(cfg: Path) -> None:
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=False),
    ):
        assert offer_caddy_events_log_group(cfg) is False
    assert "caddy-events" not in cfg.read_text()


def test_events_idempotent_skip_when_already_present(cfg: Path) -> None:
    cfg.write_text(
        '[[log_groups]]\nname = "caddy-events"\nenabled = true\n'
        'source_type = "file"\nsource_path = "/var/log/caddy/events.log"\n'
        'parser = "caddy_json"\nship_interval_seconds = 10\n'
        "max_lines_per_batch = 200\nretention_days = 90\n"
    )
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm") as mock_prompt,
    ):
        assert offer_caddy_events_log_group(cfg) is False
        mock_prompt.assert_not_called()


def test_events_independent_of_access_group(cfg: Path) -> None:
    """An existing access-log group does not satisfy the events check -
    both groups coexist after both offers run."""
    with (
        _patch_log_path_exists(True),
        patch("stormpulse.init.host_native_logs.prompt_confirm", return_value=True),
    ):
        assert offer_caddy_log_group(cfg) is True
        assert offer_caddy_events_log_group(cfg) is True
    raw = tomllib.loads(cfg.read_text())
    names = [g["name"] for g in raw["log_groups"]]
    assert names == ["caddy", "caddy-events"]
