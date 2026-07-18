"""Read-only Garage CLI diagnostics: subprocess passthroughs that mutate
nothing. The agent's zero-risk surface."""

from __future__ import annotations

from typing import Callable

from stormpulse.config import CommandSpec
from stormpulse.garage.commands.params import bucket_name_param


def build_readonly_specs(
    garage_cli: Callable[..., list[str]],
) -> dict[str, CommandSpec]:
    """Build the read-only diagnostic specs on the shared ``garage_cli`` prefix."""
    return {
        "garage_status": CommandSpec(
            group="garage",
            command=garage_cli("status"),
            timeout=15,
            description="Show Garage node status",
        ),
        "garage_stats": CommandSpec(
            group="garage",
            command=garage_cli("stats"),
            timeout=15,
            description="Show Garage cluster statistics",
        ),
        "garage_bucket_list": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "list"),
            timeout=15,
            description="List all Garage buckets",
        ),
        "garage_bucket_info": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "info", "{bucket_name}"),
            timeout=15,
            description="Show bucket details",
            params={"bucket_name": bucket_name_param("Bucket name or alias")},
        ),
        "garage_key_list": CommandSpec(
            group="garage",
            command=garage_cli("key", "list"),
            timeout=15,
            description="List all Garage API keys",
        ),
    }
