"""Tests for the caddy command registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.caddy.commands import (
    CELLAR_CUSTOM_DOMAIN_CADDY_SYNC,
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
        drop_in_path=Path("/etc/caddy/conf.d/cellar-custom-domains.caddy"),
    )


class TestBuildCaddyCommands:
    def test_registers_sync_command(self) -> None:
        commands = build_caddy_commands(_make_caddy_config())
        assert CELLAR_CUSTOM_DOMAIN_CADDY_SYNC in commands

    def test_sync_is_long_running(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        assert cmd.long_running is True
        assert cmd.group == "caddy"

    def test_region_param_uses_regex(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        region = cmd.params["region"]
        assert region.pattern is not None
        assert region.max_bytes is None

    def test_fragment_param_uses_byte_cap(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        fragment = cmd.params["fragment"]
        assert fragment.pattern is None
        assert fragment.max_bytes is not None
        assert fragment.max_bytes >= 100_000


class TestValidateParams:
    """Verifies the opaque-content param type works end-to-end."""

    def test_valid_region_and_fragment(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        # Multi-line content with braces (would break a regex) is fine
        # because the fragment param has no pattern, only a byte cap.
        fragment = (
            "example.com {\n"
            "    reverse_proxy localhost:3902 {\n"
            "        header_up Host bucket.web.cellar.example\n"
            "    }\n"
            "}\n"
        )
        validated = validate_params(
            cmd, {"region": "vancouver-1", "fragment": fragment},
        )
        assert validated["region"] == "vancouver-1"
        assert validated["fragment"] == fragment

    def test_empty_fragment_uses_default(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        validated = validate_params(cmd, {"region": "vancouver-1"})
        # Default is empty string - meaning "remove the drop-in file."
        assert validated["fragment"] == ""

    def test_bad_region_rejected(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        with pytest.raises(ParamValidationError):
            validate_params(
                cmd, {"region": "../etc/passwd", "fragment": ""},
            )

    def test_oversize_fragment_rejected(self) -> None:
        cmd = build_caddy_commands(_make_caddy_config())[
            CELLAR_CUSTOM_DOMAIN_CADDY_SYNC
        ]
        oversize = "a" * 200_000  # over the 150_000 cap
        with pytest.raises(ParamValidationError) as exc_info:
            validate_params(
                cmd, {"region": "vancouver-1", "fragment": oversize},
            )
        assert "exceeds max_bytes" in str(exc_info.value)
