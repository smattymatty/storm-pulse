"""Garage state collection - structured dataclasses and subprocess runner."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from stormpulse.config import GarageConfig
from stormpulse.garage import admin_api
from stormpulse.garage.parse import (
    GarageParseError,
    GaragePeer,
    parse_key_list,
    parse_stats,
    parse_status,
)

logger = logging.getLogger(__name__)

_GARAGE_TIMEOUT = 15


@dataclass(frozen=True, slots=True)
class GarageKeyRef:
    """Key reference within a bucket - ID and permissions only, never the secret."""

    key_id: str
    key_name: str
    permissions: str


@dataclass(frozen=True, slots=True)
class GarageBucket:
    """Bucket summary for state pushes."""

    id: str
    alias: str
    size_bytes: int
    object_count: int
    keys: list[GarageKeyRef]
    website_access: bool
    website_index_document: str
    website_error_document: str | None
    quota_max_size_bytes: int | None
    quota_max_objects: int | None


@dataclass(frozen=True, slots=True)
class GarageState:
    """Full Garage node state, included in metrics.push and register payloads.

    ``disabled_reason`` is set only when the agent's Garage feature has
    self-disabled at start because of a precondition failure (see
    GARAGE-000). When non-None, every other field is a zero-value
    sentinel and ``healthy`` is False; the dashboard reads
    ``disabled_reason`` and renders a named cause rather than the
    healthy/unhealthy distinction.
    """

    node_id: str
    hostname: str
    zone: str
    capacity_gb: float
    data_avail_gb: float
    version: str
    healthy: bool
    db_engine: str
    object_count: int
    block_count: int
    buckets: list[GarageBucket]
    keys: list[GarageKeyRef]
    peers: list[GaragePeer]
    disabled_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dict for inclusion in protocol payloads."""
        return asdict(self)

    @classmethod
    def disabled(cls, reason: str) -> GarageState:
        """Build the sentinel state for a self-disabled Garage feature."""
        return cls(
            node_id="",
            hostname="",
            zone="",
            capacity_gb=0.0,
            data_avail_gb=0.0,
            version="",
            healthy=False,
            db_engine="",
            object_count=0,
            block_count=0,
            buckets=[],
            keys=[],
            peers=[],
            disabled_reason=reason,
        )


def run_garage(config: GarageConfig, *args: str) -> str | None:
    """Run a garage CLI command via docker exec. Returns stdout or None on failure."""
    cmd = [
        config.docker_binary,
        "exec",
        config.container_name,
        config.garage_binary,
        *args,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GARAGE_TIMEOUT,
            shell=False,
        )
    except FileNotFoundError:
        logger.warning("Docker binary not found: %s", config.docker_binary)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Garage command timed out: %s", " ".join(args))
        return None

    if proc.returncode != 0:
        logger.warning(
            "Garage command failed (exit %d): %s - %s",
            proc.returncode,
            " ".join(args),
            proc.stderr.strip(),
        )
        return None

    return proc.stdout


def _try_parse[T](
    config: GarageConfig, parser: Callable[[str], T], *args: str, what: str
) -> T | None:
    out = run_garage(config, *args)
    if out is None:
        return None
    try:
        return parser(out)
    except GarageParseError:
        logger.warning("Failed to parse garage %s output", what)
        return None


def _perm_flags(permissions: dict[str, Any] | None) -> str:
    """Render Garage's structured key permissions as the legacy ``RWO`` string.

    The CLI ``bucket info`` keys table printed read/write/owner as the flags
    ``R``/``W``/``O``; the admin API returns ``{"read","write","owner"}`` booleans
    (``ApiBucketKeyPerm``). We keep emitting the same string so nothing
    downstream of the state push changes shape.
    """
    p = permissions or {}
    return (
        ("R" if p.get("read") else "")
        + ("W" if p.get("write") else "")
        + ("O" if p.get("owner") else "")
    )


def _bucket_from_admin_info(info: dict[str, Any]) -> GarageBucket:
    """Map a ``GetBucketInfoResponse`` (admin API v2 JSON) to a GarageBucket.

    Field names are the exact v2 schema: ``bytes``/``objects`` are int64,
    ``quotas.maxSize``/``maxObjects`` are int-or-null, ``websiteConfig`` is an
    object-or-null, and each key carries ``accessKeyId``/``name``/``permissions``
    inline (so the separate ``key list`` lookup is no longer needed for buckets).
    Every read is defensive: a missing or null field degrades to a zero-value,
    never a KeyError that would crash the tick.
    """
    quotas = info.get("quotas") or {}
    website = info.get("websiteConfig") or {}
    global_aliases = info.get("globalAliases") or []
    keys: list[GarageKeyRef] = []
    for k in info.get("keys") or []:
        # bucketLocalAliases is per-key in the v2 schema but GarageKeyRef carries
        # only id/name/permissions, matching the old CLI state push, so it's dropped.
        keys.append(
            GarageKeyRef(
                key_id=k.get("accessKeyId", "") or "",
                key_name=k.get("name", "") or "",
                permissions=_perm_flags(k.get("permissions")),
            )
        )
    return GarageBucket(
        id=info.get("id", "") or "",
        alias=global_aliases[0] if global_aliases else "",
        size_bytes=int(info.get("bytes") or 0),
        object_count=int(info.get("objects") or 0),
        keys=keys,
        website_access=bool(info.get("websiteAccess", False)),
        website_index_document=website.get("indexDocument") or "index.html",
        website_error_document=website.get("errorDocument"),
        quota_max_size_bytes=quotas.get("maxSize"),
        quota_max_objects=quotas.get("maxObjects"),
    )


def _collect_buckets_via_admin(config: GarageConfig) -> list[GarageBucket] | None:
    """List buckets and fetch each one's info over the admin HTTP API.

    Returns the bucket list, or **None** when the cluster can't be enumerated
    (admin API unconfigured, or ``ListBuckets`` unreachable). The caller treats
    None as "skip this tick": pushing an empty set would read downstream as
    "every bucket vanished" (BUCKETS-006 invariant 4 - no fresh read, no action).
    A single bucket whose ``GetBucketInfo`` fails is skipped and logged, never
    crashing the tick - stale-and-missing-one beats acting on a bad read.

    This is the migration of ADR garage/001 follow-up #1: it replaces the
    per-bucket CLI ``bucket info`` spawn (and its lossy text size/quota parse)
    with exact-integer JSON, which is what lets the website anchor BUCKETS-006's
    quota_bytes on this read.
    """
    admin_url, admin_token = config.admin_url, config.admin_token
    if not (admin_url and admin_token):
        logger.error(
            "Garage admin API not configured (admin_url + admin_token); cannot "
            "collect bucket state. Set [garage] admin_url and admin_token_file."
        )
        return None
    items, err = admin_api.list_buckets(admin_url=admin_url, admin_token=admin_token)
    if items is None:
        logger.warning("ListBuckets failed; skipping bucket state this tick: %s", err)
        return None
    buckets: list[GarageBucket] = []
    for item in items:
        bucket_id = item.get("id", "")
        if not bucket_id:
            continue
        info, err = admin_api.get_bucket_info(
            admin_url=admin_url, admin_token=admin_token, bucket_ref=bucket_id,
        )
        if info is None:
            logger.warning(
                "GetBucketInfo failed for %s; skipping this bucket: %s",
                bucket_id, err,
            )
            continue
        buckets.append(_bucket_from_admin_info(info))
    return buckets


def collect_garage_state(config: GarageConfig) -> GarageState | None:
    """Collect full Garage node state for the state push.

    Node telemetry (status, stats, key list) is read via the Garage CLI; the
    per-bucket state (sizes, object counts, quotas, keys) is read via the admin
    HTTP API (ADR garage/001 follow-up #1) so the website can anchor
    BUCKETS-006's quota_bytes on exact JSON instead of scraped text.

    Returns None if the node is unreachable, the CLI output can't be parsed, or
    the bucket set can't be enumerated this tick - the caller skips the push.
    """
    nodes = _try_parse(config, parse_status, "status", what="status")
    if not nodes:
        logger.warning("No nodes found in garage status output")
        return None
    node = nodes[0]

    stats = _try_parse(config, parse_stats, "stats", what="stats")
    key_entries = (
        _try_parse(config, parse_key_list, "key", "list", what="key list") or []
    )
    key_name_map = {k.key_id: k.name for k in key_entries}

    buckets = _collect_buckets_via_admin(config)
    if buckets is None:
        # Admin API unconfigured/unreachable or ListBuckets failed. Skip the whole
        # tick rather than push an empty bucket set, which reads downstream as
        # "every bucket vanished". The dashboard sees no fresh push and shows
        # degraded; the next tick retries (BUCKETS-006 invariant 4).
        logger.warning("Bucket state unavailable this tick; skipping state push")
        return None

    return GarageState(
        node_id=node.node_id,
        hostname=node.hostname,
        zone=node.zone,
        capacity_gb=node.capacity_gb,
        data_avail_gb=node.data_avail_gb,
        version=node.version,
        healthy=node.healthy,
        db_engine=stats.db_engine if stats else "unknown",
        object_count=stats.object_count if stats else 0,
        block_count=stats.block_count if stats else 0,
        buckets=buckets,
        keys=[GarageKeyRef(kid, kname, "") for kid, kname in key_name_map.items()],
        peers=nodes,
    )
