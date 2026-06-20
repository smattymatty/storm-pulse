"""Garage state collection over the admin HTTP API (ADR garage/001).

Node telemetry (cluster status, statistics, key list) and per-bucket state
(sizes, object counts, quotas, keys) are all read via the admin HTTP API, never
the Garage CLI. ``GaragePeer`` is the one type still imported from ``parse`` (a
dataclass, not a scraper); it relocates when ``parse.py`` is finally deleted.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GaragePeer:
    """A single Garage cluster node, mapped from a ``GetClusterStatus`` row."""

    node_id: str
    hostname: str
    address: str
    zone: str
    capacity_gb: float
    data_avail_gb: float
    data_avail_percent: float
    version: str
    healthy: bool


@dataclass(frozen=True, slots=True)
class GarageKeyRef:
    """Key reference within a bucket - ID, permissions, and the key's local
    aliases for this bucket, never the secret.

    ``bucket_local_aliases`` is the bucket-name namespace private to this key.
    An S3-created (BUCKETS-012) bucket has no global alias, so its name lives
    here under the owning key; the website's adopt branch reads it to name the
    bucket. Empty for the top-level key inventory and for dashboard-provisioned
    buckets that carry a global alias.
    """

    key_id: str
    key_name: str
    permissions: str
    bucket_local_aliases: tuple[str, ...] = ()


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
    """Full Garage node state, the ``state`` blob of garage's Integration report.

    CORE-005 relocated the self-disabled cause to the Integration envelope
    (``status: disabled_error`` + ``disabled_reason``), so this state object is
    built only when Garage is live and never carries a disabled sentinel. The
    blob is byte-identical to the pre-CORE-005 ``to_dict()`` minus that field.
    """

    node_id: str
    hostname: str
    zone: str
    capacity_gb: float
    data_avail_gb: float
    version: str
    healthy: bool
    object_count: int
    buckets: list[GarageBucket]
    keys: list[GarageKeyRef]
    peers: list[GaragePeer]

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dict for inclusion in protocol payloads."""
        return asdict(self)

    def summary(self) -> str:
        """One-line summary for the on-demand refresh command result.

        The optional ``summary()`` capability the generic agent refresh routine
        uses (falling back to a default for a state type that doesn't define
        one). Preserves the pre-single-source ``Refreshed: N buckets`` line.
        """
        return f"{len(self.buckets)} buckets"


def _perm_flags(permissions: dict[str, Any] | None) -> str:
    """Render Garage's structured key permissions as the legacy ``RWO`` string.

    The admin API returns ``{"read","write","owner"}`` booleans
    (``ApiBucketKeyPerm``). We emit the same ``R``/``W``/``O`` string the CLI
    ``bucket info`` keys table printed, so nothing downstream of the state push
    changes shape.
    """
    p = permissions or {}
    return (
        ("R" if p.get("read") else "")
        + ("W" if p.get("write") else "")
        + ("O" if p.get("owner") else "")
    )


def _peer_from_node(node: dict[str, Any]) -> GaragePeer:
    """Map a ``NodeResp`` (``GetClusterStatus`` v2 JSON) to a GaragePeer.

    Sizes are exact bytes from the API (``role.capacity`` /
    ``dataPartition.available``/``total``), converted to decimal GB. Every read
    is defensive: gateway nodes have ``role.capacity`` null, unassigned nodes
    have ``role`` null, so a missing field degrades to a zero, never a KeyError.
    """
    role = node.get("role") or {}
    data_part = node.get("dataPartition") or {}
    avail = data_part.get("available")
    total = data_part.get("total")
    capacity = role.get("capacity")
    pct = round(avail / total * 100, 1) if avail is not None and total else 0.0
    return GaragePeer(
        node_id=node.get("id", "") or "",
        hostname=node.get("hostname", "") or "",
        address=node.get("addr", "") or "",
        zone=role.get("zone", "") or "",
        capacity_gb=(capacity or 0) / 1_000_000_000,
        data_avail_gb=(avail or 0) / 1_000_000_000,
        data_avail_percent=pct,
        version=node.get("garageVersion", "") or "unknown",
        healthy=bool(node.get("isUp")),
    )


def _bucket_from_admin_info(info: dict[str, Any]) -> GarageBucket:
    """Map a ``GetBucketInfoResponse`` (admin API v2 JSON) to a GarageBucket.

    Field names are the exact v2 schema: ``bytes``/``objects`` are int64,
    ``quotas.maxSize``/``maxObjects`` are int-or-null, ``websiteConfig`` is an
    object-or-null, and each key carries ``accessKeyId``/``name``/``permissions``/
    ``bucketLocalAliases`` inline. Every read is defensive: a missing or null
    field degrades to a zero-value, never a KeyError that would crash the tick.
    """
    quotas = info.get("quotas") or {}
    website = info.get("websiteConfig") or {}
    global_aliases = info.get("globalAliases") or []
    keys: list[GarageKeyRef] = []
    for k in info.get("keys") or []:
        local_aliases = k.get("bucketLocalAliases") or []
        keys.append(
            GarageKeyRef(
                key_id=k.get("accessKeyId", "") or "",
                key_name=k.get("name", "") or "",
                permissions=_perm_flags(k.get("permissions")),
                bucket_local_aliases=tuple(a for a in local_aliases if a),
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
    (``ListBuckets`` unreachable). The caller treats None as "skip this tick":
    pushing an empty set would read downstream as "every bucket vanished"
    (BUCKETS-006 invariant 4 - no fresh read, no action). A single bucket whose
    ``GetBucketInfo`` fails is skipped and logged, never crashing the tick.
    """
    admin_url, admin_token = config.admin_url, config.admin_token
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
    """Collect full Garage node state for the state push, all via the admin API.

    Returns None when the admin API is unconfigured or unreachable, no node is
    found, or the bucket set can't be enumerated this tick - the caller skips
    the push rather than reporting a degraded snapshot. Cluster statistics and
    the key list are best-effort: their failure degrades a field, not the tick.
    """
    admin_url, admin_token = config.admin_url, config.admin_token
    if not (admin_url and admin_token):
        logger.error(
            "Garage admin API not configured (admin_url + admin_token); cannot "
            "collect state. Set [garage] admin_url and admin_token_file."
        )
        return None

    status, err = admin_api.get_cluster_status(
        admin_url=admin_url, admin_token=admin_token,
    )
    if status is None:
        logger.warning("GetClusterStatus failed; skipping state push: %s", err)
        return None
    peers = [_peer_from_node(n) for n in status.get("nodes") or []]
    if not peers:
        logger.warning("No nodes in GetClusterStatus; skipping state push")
        return None
    node = peers[0]

    # Best-effort: a stats failure degrades object_count to 0, not the tick.
    stats, _err = admin_api.get_cluster_statistics(
        admin_url=admin_url, admin_token=admin_token,
    )
    object_count = int((stats or {}).get("totalObjectCount") or 0)

    # Best-effort: a key-list failure empties the top-level inventory only.
    keys_raw, _err = admin_api.list_keys(admin_url=admin_url, admin_token=admin_token)
    keys = [
        GarageKeyRef(k.get("id", "") or "", k.get("name", "") or "", "")
        for k in (keys_raw or [])
    ]

    buckets = _collect_buckets_via_admin(config)
    if buckets is None:
        # Admin API unreachable or ListBuckets failed. Skip the whole tick rather
        # than push an empty bucket set, which reads downstream as "every bucket
        # vanished". The next tick retries (BUCKETS-006 invariant 4).
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
        object_count=object_count,
        buckets=buckets,
        keys=keys,
        peers=peers,
    )
