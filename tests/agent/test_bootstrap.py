"""Tests for ``stormpulse.agent.bootstrap.build_agent_dependencies``.

The bootstrap function is where every per-process runtime object the
agent uses gets composed: the command registry, the long-running
handler factories, and the log shippers. Anything that can fail at
startup (missing Caddy drop-in, etc.) fails here, not inside
``Agent.__init__``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.config import CaddyConfig, Config, ConfigError, LogGroupConfig

from tests.helpers import build_config, build_garage_config


# ---------------------------------------------------------------------------
# Registry assembly
# ---------------------------------------------------------------------------


def test_no_features_yields_built_in_commands_only(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    # Built-in commands include git_pull and docker_logs at minimum.
    assert "git_pull" in deps.registry
    assert "docker_logs" in deps.registry
    # No feature commands when no feature is configured.
    assert not any(name.startswith("garage_") for name in deps.registry)
    assert not any(name.startswith("cellar_") for name in deps.registry)


def test_garage_enabled_merges_garage_commands(tmp_path: Path) -> None:
    garage = build_garage_config(tmp_path)
    cfg = build_config(tmp_path, garage=garage)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert "garage_refresh" in deps.registry
    assert "garage_bucket_clear" in deps.registry


def test_garage_disabled_does_not_merge_garage_commands(tmp_path: Path) -> None:
    garage = replace(build_garage_config(tmp_path), enabled=False)
    cfg = build_config(tmp_path, garage=garage)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert not any(name.startswith("garage_") for name in deps.registry)


def test_signoff_sealed_removes_run_verify_block(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    sealed = build_agent_dependencies(
        cfg, signoff_sealed=True, log_position_store=None,
    )
    unsealed = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert "run_verify_block" not in sealed.registry
    assert "run_verify_block" in unsealed.registry


def test_disabled_commands_are_removed(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    cfg = replace(
        cfg,
        agent=replace(cfg.agent, disabled_commands=frozenset({"docker_logs"})),
    )
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert "git_pull" in deps.registry
    assert "docker_logs" not in deps.registry


# ---------------------------------------------------------------------------
# Long-running factory composition
# ---------------------------------------------------------------------------


def test_no_features_yields_empty_long_running_factories(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert deps.long_running_factories == {}


def test_garage_enabled_publishes_seven_long_running_factories(tmp_path: Path) -> None:
    cfg = build_config(tmp_path, garage=build_garage_config(tmp_path))
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert set(deps.long_running_factories.keys()) == {
        "garage_bucket_clear",
        "garage_bucket_set_cors",
        "garage_walk_bucket_stats",
        "garage_provision_customer_bucket",
        "garage_rotate_customer_key",
        "garage_provision_additional_key",
        "garage_delete_provisioned_bucket",
    }


# ---------------------------------------------------------------------------
# Caddy fail-fast verification
# ---------------------------------------------------------------------------


def _caddy_config(tmp_path: Path, *, drop_in_imported: bool) -> CaddyConfig:
    """Build a CaddyConfig pointing at a real on-disk Caddyfile.

    If ``drop_in_imported`` is True, the main Caddyfile contains an
    ``import`` line for the drop-in path; otherwise it's empty so the
    boot-time check fails.
    """
    main = tmp_path / "Caddyfile"
    drop_in = tmp_path / "drop_in.conf"
    main.write_text(f"import {drop_in}\n" if drop_in_imported else "")
    drop_in.parent.mkdir(parents=True, exist_ok=True)
    return CaddyConfig(
        enabled=True,
        admin_url="http://localhost:2019",
        main_caddyfile=main,
        drop_in_path=drop_in,
    )


def test_caddy_missing_drop_in_import_raises_config_error(tmp_path: Path) -> None:
    caddy = _caddy_config(tmp_path, drop_in_imported=False)
    cfg = replace(build_config(tmp_path), caddy=caddy)
    with pytest.raises(ConfigError, match="Caddy configuration invalid"):
        build_agent_dependencies(
            cfg, signoff_sealed=False, log_position_store=None,
        )


def test_caddy_imported_drop_in_succeeds(tmp_path: Path) -> None:
    caddy = _caddy_config(tmp_path, drop_in_imported=True)
    cfg = replace(build_config(tmp_path), caddy=caddy)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert "cellar_custom_domain_caddy_sync" in deps.registry
    assert "cellar_custom_domain_caddy_sync" in deps.long_running_factories


def test_caddy_disabled_does_not_check_drop_in(tmp_path: Path) -> None:
    # Even with a nonexistent main_caddyfile, disabled=False should skip.
    caddy = CaddyConfig(
        enabled=False,
        admin_url="http://localhost:2019",
        main_caddyfile=tmp_path / "missing.caddy",
        drop_in_path=tmp_path / "missing.conf",
    )
    cfg = replace(build_config(tmp_path), caddy=caddy)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert "cellar_custom_domain_caddy_sync" not in deps.registry


# ---------------------------------------------------------------------------
# Log shipper assembly
# ---------------------------------------------------------------------------


def test_no_log_store_yields_empty_shippers(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg, signoff_sealed=False, log_position_store=None,
    )
    assert deps.shippers == {}
    assert deps.streaming_tailers == []


def test_disabled_log_groups_are_skipped(tmp_path: Path) -> None:
    """A disabled group must not produce a shipper even when a store is supplied."""
    from stormpulse.logging import LogPositionStore

    store = LogPositionStore(tmp_path / "pos.db")
    try:
        cfg = build_config(tmp_path)
        cfg = replace(
            cfg,
            log_groups=[
                LogGroupConfig(
                    name="syslog",
                    enabled=False,
                    source_type="file",
                    source_path=Path("/var/log/syslog"),
                    filter_contains="",
                    parser="raw",
                    ship_interval_seconds=5.0,
                    max_lines_per_batch=100,
                    retention_days=7,
                ),
            ],
        )
        deps = build_agent_dependencies(
            cfg, signoff_sealed=False, log_position_store=store,
        )
        assert deps.shippers == {}
    finally:
        store.close()
