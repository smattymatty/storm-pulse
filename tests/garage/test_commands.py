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
            "garage_status",
            "garage_stats",
            "garage_bucket_list",
            "garage_bucket_info",
            "garage_key_list",
            "garage_bucket_create",
            "garage_bucket_delete",
            "garage_bucket_set_quota",
            "garage_set_account_key_create_bucket",
            "garage_key_create",
            "garage_key_delete",
            "garage_bucket_allow",
            "garage_bucket_allow_rw",
            "garage_bucket_allow_ro",
            "garage_bucket_deny",
            "garage_bucket_website_allow",
            "garage_bucket_website_deny",
            "garage_bucket_alias_global_add",
            "garage_bucket_alias_global_remove",
            "garage_bucket_alias_local_add",
            "garage_bucket_alias_local_remove",
            "garage_refresh",
            "garage_bucket_clear",
            "garage_provision_customer_bucket",
            "garage_provision_additional_key",
            "garage_provision_account_key",
            "garage_delete_provisioned_bucket",
            "garage_delete_key",
            "garage_detach_account_key",
            "garage_converge_account_key_rotation",
            "garage_snapshot_and_reap_account_key",
            "garage_get_key_buckets",
            "garage_get_bucket_owners",
            "garage_rotate_customer_key",
            "garage_walk_bucket_stats",
        }
        assert set(cmds.keys()) == expected

    def test_all_commands_use_absolute_paths(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name, cmd_def in cmds.items():
            if name in {
                "garage_refresh",
                "garage_bucket_clear",
                "garage_bucket_set_quota",
                "garage_set_account_key_create_bucket",
                "garage_provision_customer_bucket",
                "garage_provision_additional_key",
                "garage_provision_account_key",
                "garage_delete_provisioned_bucket",
                "garage_delete_key",
                "garage_detach_account_key",
                "garage_converge_account_key_rotation",
                "garage_snapshot_and_reap_account_key",
                "garage_get_key_buckets",
                "garage_get_bucket_owners",
                "garage_rotate_customer_key",
                "garage_walk_bucket_stats",
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
        for name in (
            "garage_bucket_delete",
            "garage_key_delete",
            "garage_delete_key",
            "garage_detach_account_key",
            "garage_snapshot_and_reap_account_key",
            "garage_bucket_deny",
            "garage_bucket_website_deny",
            "garage_bucket_alias_global_remove",
            "garage_bucket_alias_local_remove",
        ):
            assert cmds[name].requires_confirmation is True, (
                f"{name} should require confirmation"
            )

    def test_read_only_commands_no_confirmation(self) -> None:
        cmds = build_garage_commands(_make_config())
        for name in (
            "garage_status",
            "garage_stats",
            "garage_bucket_list",
            "garage_bucket_info",
            "garage_key_list",
            "garage_refresh",
        ):
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

    def test_set_quota_is_long_running_admin_api(self) -> None:
        # Applied via the Garage admin HTTP API (UpdateBucket), not a CLI
        # subprocess, so it rides the long-running JobManager path.
        cmds = build_garage_commands(_make_config())
        assert cmds["garage_bucket_set_quota"].long_running is True
        assert cmds["garage_bucket_set_quota"].command == ["garage_bucket_set_quota"]

    def test_other_commands_not_sensitive(self) -> None:
        cmds = build_garage_commands(_make_config())
        sensitive_allowed = {
            "garage_key_create",
            "garage_bucket_clear",
            "garage_provision_customer_bucket",
            "garage_provision_additional_key",
            "garage_provision_account_key",
            "garage_rotate_customer_key",
            "garage_walk_bucket_stats",
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
        assert param.pattern == r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
        assert param.default is None

    def test_bucket_name_pattern_is_s3_strict(self) -> None:
        """The pattern enforces S3-strict bucket naming: 3-63 chars,
        lowercase alphanumeric + hyphens, must start/end alphanumeric.
        Garage's bucket-create validator enforces the same; we close
        a defense-in-depth gap so dashboard-supplied names that Garage
        will reject get caught before dispatch instead of crashing
        mid-orchestration.

        Also forbids leading hyphens (CLI-flag smuggling) and
        underscores/uppercase (S3 rejects), which the previous
        permissive pattern allowed.
        """
        import re

        cmds = build_garage_commands(_make_config())
        pattern = cmds["garage_bucket_info"].params["bucket_name"].pattern
        assert pattern is not None
        # Flag-smuggling and S3-illegal names rejected.
        for bad in (
            "--help",
            "-c",
            "-h",
            "--rpc-host",  # flag smuggling
            "_provisioning_abc",  # leading underscore (S3 rejects)
            "with_underscore",  # any underscore (S3 rejects)
            "UpperCase",  # uppercase (S3 rejects)
            "a",  # too short (min 3)
            "ab",  # too short (min 3)
            "-leading",  # leading hyphen
            "trailing-",  # trailing hyphen
        ):
            assert re.fullmatch(pattern, bad) is None, f"pattern should reject {bad!r}"
        # Valid S3-strict bucket names accepted (display names, alias
        # references, and 16-char hex UUID prefixes).
        for good in (
            "media",
            "smattymatty-media",
            "obsidian",
            "0bucket",
            "provisioning-abc123",
            "5c8d6c0bb73f0770",  # 16-char UUID prefix
        ):
            assert re.fullmatch(pattern, good) is not None, (
                f"pattern should accept {good!r}"
            )

    def test_bucket_id_pattern_accepts_full_and_truncated_garage_uuids(self) -> None:
        """``bucket_id`` is a Garage internal UUID, distinct from a
        display-name alias. The full form is 64 lowercase hex chars; the
        CLI displays a 16-char unique prefix and accepts either as a
        reference. The ``garage_state`` snapshot pushed to Storm carries
        the full form, so anywhere bucket_id rides as a parameter from
        the dashboard, it arrives at full length. Match both.
        """
        import re

        cmds = build_garage_commands(_make_config())
        for cmd_name in (
            "garage_delete_provisioned_bucket",
            "garage_provision_additional_key",
            "garage_rotate_customer_key",
            "garage_bucket_set_quota",
        ):
            pattern = cmds[cmd_name].params["bucket_id"].pattern
            assert pattern is not None
            # Both Garage UUID forms accepted.
            for good in (
                "d05213985bdf79da",  # 16-char CLI prefix
                "d05213985bdf79da9fa8faed05f01e44c5344d2600736d8402b46755e7fb3980",  # 64-char internal
            ):
                assert re.fullmatch(pattern, good) is not None, (
                    f"{cmd_name}.bucket_id should accept {good!r}"
                )
            # Non-hex, uppercase, and out-of-range lengths rejected.
            for bad in (
                "",  # empty
                "abc",  # too short (min 16)
                "G" * 32,  # non-hex (uppercase/non-hex)
                "d05213985bdf79DA",  # uppercase hex
                "d05213985bdf79da-bad",  # contains hyphen
                "x" * 65,  # too long (max 64)
                "--help",  # flag smuggling
            ):
                assert re.fullmatch(pattern, bad) is None, (
                    f"{cmd_name}.bucket_id should reject {bad!r}"
                )

    def test_key_name_pattern_rejects_leading_hyphen(self) -> None:
        import re

        cmds = build_garage_commands(_make_config())
        pattern = cmds["garage_key_create"].params["key_name"].pattern
        assert pattern is not None
        for bad in ("--help", "-c"):
            assert re.fullmatch(pattern, bad) is None
        for good in ("usr-1-media-all", "key_admin"):
            assert re.fullmatch(pattern, good) is not None

    def test_walk_bucket_stats_prefix_accepts_real_s3_keys(self) -> None:
        """The stats-walk ``prefix`` is the customer's real S3 prefix, not
        an identifier. It reaches Garage as a URL-encoded ListObjectsV2
        query param (no shell), so the old ``[A-Za-z0-9_\\-./]`` charset
        wrongly rejected legal folder names (spaces, ``+``, parens,
        unicode) and left per-folder stats stuck on "Calculating...".

        This pattern is the *only* charset gate (the website validates
        structure, not charset), so it must own the structural invariants
        itself: empty = root, ends with '/', never starts with '/', and
        no control bytes (C0 ``\\x00-\\x1f``, DEL, and C1 ``\\x7f-\\x9f``).
        """
        import re

        cmds = build_garage_commands(_make_config())
        pattern = cmds["garage_walk_bucket_stats"].params["prefix"].pattern
        assert pattern is not None
        # Real S3 prefixes the old pattern wrongly rejected.
        for good in (
            "",  # bucket root
            "Software Architecture/",  # space
            "Comptia Security+/",  # plus
            "Réseau (prod)/",  # parens + unicode
            "日本語/",  # non-ASCII
            "photos/2026/q2/",  # nested
        ):
            assert re.fullmatch(pattern, good) is not None, (
                f"prefix pattern should accept {good!r}"
            )
        # Structural invariants the agent enforces on its own.
        for bad in (
            "/leading",  # starts with '/'
            "no-trailing-slash",  # missing trailing '/'
            "/",  # bare slash (no first char before it)
            "bad\nname/",  # C0 control (newline)
            "\x00/",  # NUL
            "bad\x7fname/",  # DEL (pattern B excludes \x7f-\x9f)
        ):
            assert re.fullmatch(pattern, bad) is None, (
                f"prefix pattern should reject {bad!r}"
            )

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
        for name in (
            "garage_bucket_allow",
            "garage_bucket_allow_rw",
            "garage_bucket_allow_ro",
        ):
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
