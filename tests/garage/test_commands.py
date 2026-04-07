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
            "garage_bucket_allow", "garage_bucket_deny",
        }
        assert set(cmds.keys()) == expected

    def test_all_commands_use_absolute_paths(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name, cmd_def in cmds.items():
            assert cmd_def.command[0].startswith("/"), (
                f"{name} first arg must be absolute: {cmd_def.command[0]}"
            )

    def test_all_commands_in_garage_group(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name, cmd_def in cmds.items():
            assert cmd_def.group == "garage", f"{name} has wrong group: {cmd_def.group}"

    def test_destructive_commands_require_confirmation(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name in ("garage_bucket_delete", "garage_key_delete", "garage_bucket_deny"):
            assert cmds[name].requires_confirmation is True, (
                f"{name} should require confirmation"
            )

    def test_read_only_commands_no_confirmation(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name in ("garage_status", "garage_stats", "garage_bucket_list",
                      "garage_bucket_info", "garage_key_list"):
            assert cmds[name].requires_confirmation is False, (
                f"{name} should not require confirmation"
            )

    def test_key_create_is_sensitive(self) -> None:
        cmds = build_garage_commands(_make_config())
        assert cmds["garage_key_create"].sensitive_output is True

    def test_other_commands_not_sensitive(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name, cmd_def in cmds.items():
            if name != "garage_key_create":
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
        assert param.pattern == r"[a-zA-Z0-9_-]+"
        assert param.default is None

    def test_bucket_deny_has_all_permission_flags(self) -> None:
        cmds = build_garage_commands(_make_config())
        deny_cmd = cmds["garage_bucket_deny"].command
        assert "--read" in deny_cmd
        assert "--write" in deny_cmd
        assert "--owner" in deny_cmd
