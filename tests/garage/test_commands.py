"""Tests for stormpulse.garage.commands."""

from __future__ import annotations

from pathlib import Path

from stormpulse.config import GarageConfig
from stormpulse.garage.commands import build_garage_commands


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )


class TestBuildGarageCommands:
    def test_all_commands_present(self) -> None:
        cmds = build_garage_commands(_make_config())
        expected = {
            "garage_status", "garage_stats",
            "garage_bucket_list", "garage_bucket_info",
            "garage_key_list",
            "garage_bucket_create", "garage_bucket_delete",
            "garage_key_create", "garage_key_delete",
            "garage_bucket_allow", "garage_bucket_allow_rw", "garage_bucket_allow_ro",
            "garage_bucket_deny",
            "garage_bucket_website_allow", "garage_bucket_website_deny",
            "garage_bucket_alias_global_add", "garage_bucket_alias_global_remove",
            "garage_bucket_alias_local_add", "garage_bucket_alias_local_remove",
            "garage_refresh",
            "garage_bucket_clear",
            "garage_provision_customer_bucket",
            "garage_rotate_customer_key",
        }
        assert set(cmds.keys()) == expected

    def test_all_commands_use_absolute_paths(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name, cmd_def in cmds.items():
            if name in {
                "garage_refresh",
                "garage_bucket_clear",
                "garage_provision_customer_bucket",
                "garage_rotate_customer_key",
            }:
                continue  # internal command, not a subprocess
            assert cmd_def.command[0].startswith("/"), (
                f"{name} first arg must be absolute: {cmd_def.command[0]}"
            )

    def test_all_commands_in_garage_group(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name, cmd_def in cmds.items():
            assert cmd_def.group == "garage", f"{name} has wrong group: {cmd_def.group}"

    def test_destructive_commands_require_confirmation(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name in ("garage_bucket_delete", "garage_key_delete", "garage_bucket_deny",
                     "garage_bucket_website_deny",
                     "garage_bucket_alias_global_remove",
                     "garage_bucket_alias_local_remove"):
            assert cmds[name].requires_confirmation is True, (
                f"{name} should require confirmation"
            )

    def test_read_only_commands_no_confirmation(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name in ("garage_status", "garage_stats", "garage_bucket_list",
                      "garage_bucket_info", "garage_key_list", "garage_refresh"):
            assert cmds[name].requires_confirmation is False, (
                f"{name} should not require confirmation"
            )

    def test_key_create_is_sensitive(self) -> None:
        cmds = build_garage_commands(_make_config())
        assert cmds["garage_key_create"].sensitive_output is True

    def test_bucket_clear_is_sensitive(self) -> None:
        # Carries the customer's S3 secret in params; result must be filtered too.
        cmds = build_garage_commands(_make_config())
        assert cmds["garage_bucket_clear"].sensitive_output is True

    def test_bucket_clear_is_long_running(self) -> None:
        cmds = build_garage_commands(_make_config())
        assert cmds["garage_bucket_clear"].long_running is True

    def test_other_commands_not_sensitive(self) -> None:
        cmds = build_garage_commands(_make_config())
        sensitive_allowed = {
            "garage_key_create",
            "garage_bucket_clear",
            "garage_provision_customer_bucket",
            "garage_rotate_customer_key",
        }
        for name, cmd_def in cmds.items():
            if name not in sensitive_allowed:
                assert cmd_def.sensitive_output is False, (
                    f"{name} should not be sensitive"
                )

    def test_custom_container_name(self) -> None:
        cfg = GarageConfig(
            enabled=True,
            container_name="my-garage",
            garage_binary="/opt/bin/garage",
            docker_binary="/usr/local/bin/docker",
            config_path=Path("/etc/garage.toml"),
            state_push_interval_seconds=60,
        )
        cmds = build_garage_commands(cfg)
        status_cmd = cmds["garage_status"].command
        assert status_cmd[0] == "/usr/local/bin/docker"
        assert status_cmd[2] == "my-garage"
        assert status_cmd[3] == "/opt/bin/garage"

    def test_bucket_name_param_pattern(self) -> None:
        cmds = build_garage_commands(_make_config())
        param = cmds["garage_bucket_info"].params["bucket_name"]
        assert param.pattern == r"[a-zA-Z0-9_][a-zA-Z0-9_-]*"
        assert param.default is None

    def test_bucket_name_pattern_rejects_leading_hyphen(self) -> None:
        """Leading hyphen would let a value parse as a CLI flag.

        Forbidding it closes a flag-smuggling state-divergence vector
        for orchestrated commands that pass dashboard-supplied values
        as positional args to the garage CLI.
        """
        import re
        cmds = build_garage_commands(_make_config())
        pattern = cmds["garage_bucket_info"].params["bucket_name"].pattern
        for bad in ("--help", "-c", "-h", "--rpc-host"):
            assert re.fullmatch(pattern, bad) is None, (
                f"pattern should reject {bad!r}"
            )
        for good in ("media", "smattymatty-media", "_provisioning_abc",
                     "a", "0bucket"):
            assert re.fullmatch(pattern, good) is not None, (
                f"pattern should accept {good!r}"
            )

    def test_key_name_pattern_rejects_leading_hyphen(self) -> None:
        import re
        cmds = build_garage_commands(_make_config())
        pattern = cmds["garage_key_create"].params["key_name"].pattern
        for bad in ("--help", "-c"):
            assert re.fullmatch(pattern, bad) is None
        for good in ("usr-1-media-all", "key_admin"):
            assert re.fullmatch(pattern, good) is not None

    def test_bucket_allow_has_all_permission_flags(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_allow"].command
        assert "--read" in cmd
        assert "--write" in cmd
        assert "--owner" in cmd

    def test_bucket_allow_rw_has_correct_flags(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_allow_rw"].command
        assert "--read" in cmd
        assert "--write" in cmd
        assert "--owner" not in cmd

    def test_bucket_allow_ro_has_correct_flags(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_allow_ro"].command
        assert "--read" in cmd
        assert "--write" not in cmd
        assert "--owner" not in cmd

    def test_bucket_allow_variants_no_confirmation(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name in ("garage_bucket_allow", "garage_bucket_allow_rw", "garage_bucket_allow_ro"):
            assert cmds[name].requires_confirmation is False, (
                f"{name} should not require confirmation"
            )

    def test_bucket_deny_has_all_permission_flags(self) -> None:
        cmds = build_garage_commands(_make_config())
        deny_cmd = cmds["garage_bucket_deny"].command
        assert "--read" in deny_cmd
        assert "--write" in deny_cmd
        assert "--owner" in deny_cmd

    def test_alias_global_add_command_shape(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_alias_global_add"].command
        assert cmd[-3:] == ["alias", "{bucket_name}", "{new_alias}"]
        assert "--local" not in cmd
        params = cmds["garage_bucket_alias_global_add"].params
        assert set(params.keys()) == {"bucket_name", "new_alias"}

    def test_alias_global_remove_command_shape(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_alias_global_remove"].command
        assert cmd[-2:] == ["unalias", "{alias_name}"]
        assert "--local" not in cmd
        params = cmds["garage_bucket_alias_global_remove"].params
        assert set(params.keys()) == {"alias_name"}

    def test_alias_local_add_command_shape(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_alias_local_add"].command
        assert "alias" in cmd
        assert "--local" in cmd
        local_idx = cmd.index("--local")
        assert cmd[local_idx + 1] == "{key_id}"
        assert cmd[-2:] == ["{bucket_name}", "{new_alias}"]
        params = cmds["garage_bucket_alias_local_add"].params
        assert set(params.keys()) == {"key_id", "bucket_name", "new_alias"}

    def test_alias_local_remove_command_shape(self) -> None:
        cmds = build_garage_commands(_make_config())
        cmd = cmds["garage_bucket_alias_local_remove"].command
        assert "unalias" in cmd
        assert "--local" in cmd
        local_idx = cmd.index("--local")
        assert cmd[local_idx + 1] == "{key_id}"
        assert cmd[-1] == "{alias_name}"
        params = cmds["garage_bucket_alias_local_remove"].params
        assert set(params.keys()) == {"key_id", "alias_name"}
