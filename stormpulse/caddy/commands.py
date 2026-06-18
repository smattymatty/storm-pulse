"""Caddy command registry: one entry for custom-domain Caddyfile sync."""

from __future__ import annotations

from stormpulse.caddy.config import CaddyConfig
from stormpulse.commands.jobs import LongRunningFactory
from stormpulse.config import CommandDef, ParamDef

BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC = "buckets_custom_domain_caddy_sync"

# Region identifier: short lowercase alphanumeric + hyphen. Matches the
# CustomerBucket.Region choices on the Django side (vancouver-1,
# toronto-1, montreal-1).
_REGION_PATTERN = r"[a-z0-9][a-z0-9-]{0,40}[a-z0-9]"

# authorize_bulk rides the wire as a string ('true'/'false') because the
# command param contract is string-valued end to end. Only a deliberate
# operator bulk op sets it true; the automated full-state path leaves it
# false so a suspicious mass-delete trips the agent's delete rail.
_BOOL_PATTERN = r"true|false"

# Manifest-total size cap. The manifest is a JSON object mapping each
# serving bucket's id to its Caddy fragment. This cap bounds the whole
# wire frame (the runaway-memory / oversized-frame guard the single
# fragment cap used to carry). At a few hundred bytes per bucket plus
# JSON overhead, 1MB covers thousands of serving buckets in one region,
# well beyond solo-founder-scale ops.
_MANIFEST_MAX_BYTES = 1_000_000


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
                "Reconcile the per-serving-bucket Caddy drop-in files for "
                "a region against the supplied manifest and reload Caddy "
                "via the admin API."
            ),
            long_running=True,
            params={
                "region": ParamDef(
                    placeholder="region",
                    default=None,
                    pattern=_REGION_PATTERN,
                    description="Region identifier (e.g. vancouver-1)",
                ),
                "tenants": ParamDef(
                    placeholder="tenants",
                    default="{}",
                    pattern=None,
                    max_bytes=_MANIFEST_MAX_BYTES,
                    description=(
                        "JSON object mapping each serving bucket's id to its "
                        "Caddy fragment. The agent writes one site-<id>.caddy "
                        "file per entry and reconciles the set against disk. "
                        "An empty object ({}) means no buckets serve in this "
                        "region; the agent removes the managed files, subject "
                        "to the delete rail."
                    ),
                ),
                "authorize_bulk": ParamDef(
                    placeholder="authorize_bulk",
                    default="false",
                    pattern=_BOOL_PATTERN,
                    description=(
                        "'true' authorizes a bulk delete (more than the inline "
                        "cadence of one file) for a deliberate operator bulk op. "
                        "The automated path leaves it 'false' so a partial "
                        "manifest cannot mass-delete live sites."
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
