"""Caddy command registry: one entry for custom-domain Caddyfile sync."""

from __future__ import annotations

from stormpulse.commands.jobs import LongRunningFactory
from stormpulse.config import CaddyConfig, CommandDef, ParamDef

BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC = "buckets_custom_domain_caddy_sync"

# Region identifier: short lowercase alphanumeric + hyphen. Matches the
# CustomerBucket.Region choices on the Django side (vancouver-1,
# toronto-1, montreal-1).
_REGION_PATTERN = r"[a-z0-9][a-z0-9-]{0,40}[a-z0-9]"

# Fragment size cap. At ~150 bytes per server block (one domain), 150KB
# headroom covers ~1000 active custom domains in a single region - well
# beyond solo-founder-scale ops, with margin to spare. Hard cap exists
# to prevent runaway memory / wire frame blowups from a misbehaving
# Storm-side renderer.
_FRAGMENT_MAX_BYTES = 150_000


def build_caddy_commands(_config: CaddyConfig) -> dict[str, CommandDef]:
    """Build the Caddy command registry.

    Today this is exactly one command. Future Caddy-side operations
    (e.g. raw fragment removal for region migration) would land here.

    The config parameter is currently unused - the handler reads paths
    and admin URL from config at dispatch time, not from the registry
    metadata. Kept in the signature so the call site mirrors
    ``build_garage_commands(config.garage)`` and future config-derived
    metadata (e.g. command timeouts tuned per-deployment) has a natural
    home.
    """
    return {
        BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC: CommandDef(
            group="caddy",
            command=[BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC],  # internal - JobManager
            timeout=30,
            description=(
                "Write the per-region Caddyfile fragment for "
                "custom-domain serving and reload Caddy via the admin API."
            ),
            long_running=True,
            params={
                "region": ParamDef(
                    placeholder="region",
                    default=None,
                    pattern=_REGION_PATTERN,
                    description="Region identifier (e.g. vancouver-1)",
                ),
                "fragment": ParamDef(
                    placeholder="fragment",
                    default="",
                    pattern=None,
                    max_bytes=_FRAGMENT_MAX_BYTES,
                    description=(
                        "Full Caddyfile fragment for serving-eligible "
                        "custom domains in this region. Empty fragment "
                        "removes the drop-in file (no domains active)."
                    ),
                ),
            },
        ),
    }


def long_running_factories(config: CaddyConfig) -> dict[str, LongRunningFactory]:
    """Return the Caddy long-running command name → handler-factory map."""
    from stormpulse.caddy.sync import make_caddy_sync_handler

    return {
        BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC: (
            lambda params: make_caddy_sync_handler(config, params)
        ),
    }
