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

from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.caddy.config import CaddyConfig
from stormpulse.config import Config, LogGroupConfig
from tests.helpers import build_config, build_garage_config, caddy_raw_table

# Garage preconditions are patched to pass by the autouse fixture in
# tests/conftest.py.


# ---------------------------------------------------------------------------
# Registry assembly
# ---------------------------------------------------------------------------


def test_no_features_yields_built_in_commands_only(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    # Built-in commands include git_pull and docker_logs at minimum.
    assert "git_pull" in deps.registry
    assert "docker_logs" in deps.registry
    # No feature commands when no feature is configured.
    assert not any(name.startswith("garage_") for name in deps.registry)
    assert not any(name.startswith("buckets_") for name in deps.registry)


def test_garage_enabled_merges_garage_commands(tmp_path: Path) -> None:
    garage = build_garage_config(tmp_path)
    cfg = build_config(tmp_path, garage=garage)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert "garage_refresh" in deps.registry
    assert "garage_bucket_clear" in deps.registry


def test_garage_disabled_does_not_merge_garage_commands(tmp_path: Path) -> None:
    garage = replace(build_garage_config(tmp_path), enabled=False)
    cfg = build_config(tmp_path, garage=garage)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert not any(name.startswith("garage_") for name in deps.registry)


def test_signoff_sealed_removes_run_verify_block(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    sealed = build_agent_dependencies(
        cfg,
        signoff_sealed=True,
        log_position_store=None,
    )
    unsealed = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert "run_verify_block" not in sealed.registry
    assert "run_verify_block" in unsealed.registry


def test_signoff_sealed_removes_run_apply_block(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    sealed = build_agent_dependencies(
        cfg,
        signoff_sealed=True,
        log_position_store=None,
    )
    unsealed = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert "run_apply_block" not in sealed.registry
    assert "run_apply_block" in unsealed.registry


def test_disabled_commands_are_removed(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    cfg = replace(
        cfg,
        agent=replace(cfg.agent, disabled_commands=frozenset({"docker_logs"})),
    )
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert "git_pull" in deps.registry
    assert "docker_logs" not in deps.registry


# ---------------------------------------------------------------------------
# Single-source command surface: handlers ride on the spec, refresh synthesized
# ---------------------------------------------------------------------------


def test_garage_job_handlers_ride_on_the_spec(tmp_path: Path) -> None:
    # Single source: a job command carries its own handler thunk; there is no
    # parallel name->factory map (deleted by this redesign).
    cfg = build_config(tmp_path, garage=build_garage_config(tmp_path))
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    clear = deps.registry["garage_bucket_clear"]
    assert clear.mode == "job"
    assert clear.handler is not None


def test_garage_enabled_synthesizes_refresh_command(tmp_path: Path) -> None:
    # "Refresh now" is a generic agent-owned capability: garage declares
    # collect_state, so bootstrap synthesizes garage_refresh (mode="refresh",
    # no handler) the same way a third-party state integration would get one.
    cfg = build_config(tmp_path, garage=build_garage_config(tmp_path))
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert "garage_refresh" in deps.registry
    assert deps.registry["garage_refresh"].mode == "refresh"
    assert deps.registry["garage_refresh"].handler is None


def test_no_features_yields_no_refresh_command(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert "garage_refresh" not in deps.registry


# ---------------------------------------------------------------------------
# Caddy soft-disable (CORE-005 decision 5: the bug the ADR names)
# ---------------------------------------------------------------------------


def _caddy_cfg(tmp_path: Path, *, drop_in_imported: bool) -> CaddyConfig:
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


def _config_with_caddy(tmp_path: Path, caddy: CaddyConfig) -> Config:
    return build_config(tmp_path, integrations={"caddy": caddy_raw_table(caddy)})


def test_caddy_missing_drop_in_import_soft_disables(tmp_path: Path) -> None:
    # CORE-005: a missing import directive disables caddy alone, it does NOT
    # raise/abort boot. The reason rides as disabled_error and no command merges.
    caddy = _caddy_cfg(tmp_path, drop_in_imported=False)
    cfg = _config_with_caddy(tmp_path, caddy)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    rt = deps.integrations["caddy"]
    assert rt.status == "disabled_error"
    assert rt.disabled_reason is not None
    assert "buckets_custom_domain_caddy_sync" not in deps.registry


def test_caddy_imported_drop_in_succeeds(tmp_path: Path) -> None:
    caddy = _caddy_cfg(tmp_path, drop_in_imported=True)
    cfg = _config_with_caddy(tmp_path, caddy)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert deps.integrations["caddy"].status == "live"
    assert "buckets_custom_domain_caddy_sync" in deps.registry
    assert deps.registry["buckets_custom_domain_caddy_sync"].mode == "job"
    assert deps.registry["buckets_custom_domain_caddy_sync"].handler is not None


def test_caddy_disabled_does_not_check_drop_in(tmp_path: Path) -> None:
    # Even with a nonexistent main_caddyfile, enabled=False should skip the check.
    caddy = CaddyConfig(
        enabled=False,
        admin_url="http://localhost:2019",
        main_caddyfile=tmp_path / "missing.caddy",
        drop_in_path=tmp_path / "missing.conf",
    )
    cfg = _config_with_caddy(tmp_path, caddy)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
    )
    assert deps.integrations["caddy"].status == "disabled_choice"
    assert "buckets_custom_domain_caddy_sync" not in deps.registry


# ---------------------------------------------------------------------------
# Log shipper assembly
# ---------------------------------------------------------------------------


def test_no_log_store_yields_empty_shippers(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    deps = build_agent_dependencies(
        cfg,
        signoff_sealed=False,
        log_position_store=None,
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
                ),
            ],
        )
        deps = build_agent_dependencies(
            cfg,
            signoff_sealed=False,
            log_position_store=store,
        )
        assert deps.shippers == {}
    finally:
        store.close()
