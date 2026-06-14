"""Tests for the caddy command registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.caddy.commands import (
    BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC,
    build_caddy_commands,
)
from stormpulse.commands.registry import (
    ParamValidationError,
    validate_params,
)
from stormpulse.config import CaddyConfig


def _make_caddy_config() -> CaddyConfig:
    return CaddyConfig(
        enabled=True,
        admin_url="http://localhost:2019",
        main_caddyfile=Path("/etc/caddy/Caddyfile"),
        drop_in_path=Path("/etc/caddy/conf.d/buckets-custom-domains.caddy"),
    )


class TestBuildCaddyCommands:
    def test_registers_sync_command(self) -> None:
        commands = build_caddy_commands(_make_caddy_config())
        assert BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC in commands

    def test_sync_is_long_running(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        assert cmd.long_running is True
        assert cmd.group == "caddy"

    def test_region_param_uses_regex(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        region = cmd.params["region"]
        assert region.pattern is not None
        assert region.max_bytes is None

    def test_tenants_param_uses_byte_cap(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        tenants = cmd.params["tenants"]
        # The manifest is opaque JSON content: capped by bytes, not regex.
        assert tenants.pattern is None
        assert tenants.max_bytes is not None
        assert tenants.max_bytes >= 100_000
        # Empty object default means "no buckets serve in this region."
        assert tenants.default == "{}"

    def test_authorize_bulk_param_uses_regex(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        authorize_bulk = cmd.params["authorize_bulk"]
        assert authorize_bulk.pattern is not None
        assert authorize_bulk.default == "false"


class TestValidateParams:
    """Verifies the manifest + flag params validate end-to-end.

    The manifest is a JSON string (opaque content, byte-capped); the agent
    handler decodes and per-tenant-validates it. authorize_bulk is a
    'true'/'false' string. The wire is string-valued end to end, which is
    what keeps these on the existing ParamDef machinery.
    """

    def test_valid_region_tenants_and_flag(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        # Multi-line fragments with braces (would break a regex) are fine
        # inside the JSON because the tenants param has no pattern, only a
        # byte cap.
        tenants = (
            '{"abcdef0123456789": "mathew.stormsites.ca {\\n'
            "    reverse_proxy localhost:3902\\n"
            '}\\n"}'
        )
        validated = validate_params(
            cmd,
            {
                "region": "vancouver-1",
                "tenants": tenants,
                "authorize_bulk": "false",
            },
        )
        assert validated["region"] == "vancouver-1"
        assert validated["tenants"] == tenants
        assert validated["authorize_bulk"] == "false"

    def test_omitted_params_use_defaults(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        validated = validate_params(cmd, {"region": "vancouver-1"})
        # Empty object = "remove the managed files"; flag defaults off.
        assert validated["tenants"] == "{}"
        assert validated["authorize_bulk"] == "false"

    def test_bad_region_rejected(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        with pytest.raises(ParamValidationError):
            validate_params(
                cmd,
                {"region": "../etc/passwd", "tenants": "{}"},
            )

    def test_bad_authorize_bulk_rejected(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        # Only the literals 'true'/'false' cross the wire; anything else is
        # a malformed flag and must be refused.
        with pytest.raises(ParamValidationError):
            validate_params(
                cmd,
                {"region": "vancouver-1", "authorize_bulk": "yes"},
            )

    def test_oversize_manifest_rejected(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        oversize = '{"x": "' + "a" * 1_100_000 + '"}'  # over the 1MB cap
        with pytest.raises(ParamValidationError) as exc_info:
            validate_params(
                cmd,
                {"region": "vancouver-1", "tenants": oversize},
            )
        assert "exceeds max_bytes" in str(exc_info.value)
