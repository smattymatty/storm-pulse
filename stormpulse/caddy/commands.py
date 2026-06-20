"""Caddy command registry: custom-domain Caddyfile sync + cert status.

Both entries are ``mode="job"`` and carry their own lazy handler thunk, so there
is no separate name->factory map. caddy declares no ``collect_state``, so it
gets no synthesized ``caddy_refresh`` command (nothing to refresh).
"""

from __future__ import annotations

from stormpulse.caddy.config import CaddyConfig
from stormpulse.config import CommandSpec, ParamDef

BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC = "buckets_custom_domain_caddy_sync"
CADDY_CERT_STATUS = "caddy_cert_status"

# Region identifier: short lowercase alphanumeric + hyphen. Matches the
# CustomerBucket.Region choices on the Django side (vancouver-1,
# toronto-1, montreal-1).
_REGION_PATTERN = r"[a-z0-9][a-z0-9-]{0,40}[a-z0-9]"

# A custom-domain FQDN: dotted labels, no scheme/path/port. Used only in
# Python (an SNI string for a loopback handshake), never a shell, but
# validated for hygiene like every other param.
_DOMAIN_PATTERN = r"[a-zA-Z0-9.-]{1,253}"

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


def build_caddy_specs(config: CaddyConfig) -> dict[str, CommandSpec]:
    """Build the Caddy command registry, binding each job's handler to ``config``.

    Today this is two commands. Future Caddy-side operations would land here.
    """
    # Lazy handler imports: loaded only when a live caddy integration builds its
    # specs. Each thunk fires at dispatch.
    from stormpulse.caddy.cert_status import make_caddy_cert_status_handler
    from stormpulse.caddy.sync import make_caddy_sync_handler

    return {
        BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC: CommandSpec(
            group="caddy",
            command=[BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC],  # internal - JobManager
            timeout=30,
            description=(
                "Reconcile the per-serving-bucket Caddy drop-in files for "
                "a region against the supplied manifest and reload Caddy "
                "via the admin API."
            ),
            mode="job",
            handler=lambda params: make_caddy_sync_handler(config, params),
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
        CADDY_CERT_STATUS: CommandSpec(
            group="caddy",
            command=[CADDY_CERT_STATUS],  # internal - handled by JobManager
            timeout=15,
            description=(
                "Probe Caddy's localhost HTTPS listener for a live, publicly-"
                "trusted TLS cert for a domain. The custom-domain "
                "CERT_PENDING -> ACTIVE reconcile backstop (BUCKETS-008): a "
                "clean handshake under the system trust store confirms the "
                "cert is real, in date, and covers the domain. Read-only, one "
                "outbound loopback handshake."
            ),
            mode="job",
            read_only=True,
            handler=lambda params: make_caddy_cert_status_handler(config, params),
            params={
                "domain": ParamDef(
                    placeholder="domain",
                    default=None,
                    pattern=_DOMAIN_PATTERN,
                    description="FQDN to check (e.g. example.com)",
                ),
            },
        ),
    }
