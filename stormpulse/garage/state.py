"""Garage state collection — structured dataclasses and subprocess runner."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass
from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")

from stormpulse.config import GarageConfig
from stormpulse.garage.parse import (
    GaragePeer,
    GarageParseError,
    parse_bucket_info,
    parse_bucket_list,
    parse_key_list,
    parse_stats,
    parse_status,
)

logger = logging.getLogger(__name__)

_GARAGE_TIMEOUT = 15


# ---------------------------------------------------------------------------
# State dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GarageKeyRef:
    """Key reference within a bucket — ID and permissions only, never the secret."""

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
    """Full Garage node state, included in metrics.push and register payloads."""

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

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dict for inclusion in protocol payloads."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_garage(config: GarageConfig, *args: str) -> str | None:
    """Run a garage CLI command via docker exec. Returns stdout or None on failure."""
    cmd = [
        config.docker_binary, "exec", config.container_name,
        config.garage_binary, *args,
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
            "Garage command failed (exit %d): %s — %s",
            proc.returncode, " ".join(args), proc.stderr.strip(),
        )
        return None

    return proc.stdout


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------


def _try_parse(config: GarageConfig, parser: Callable[[str], _T], *args: str, what: str) -> _T | None:
    out = _run_garage(config, *args)
    if out is None:
        return None
    try:
        return parser(out)
    except GarageParseError:
        logger.warning("Failed to parse garage %s output", what)
        return None


def collect_garage_state(config: GarageConfig) -> GarageState | None:
    """Collect full Garage state by running status, stats, and bucket info commands.

    Returns None if the node is unreachable or the output can't be parsed.
    """
    nodes = _try_parse(config, parse_status, "status", what="status")
    if not nodes:
        logger.warning("No nodes found in garage status output")
        return None
    node = nodes[0]

    stats = _try_parse(config, parse_stats, "stats", what="stats")
    key_entries = _try_parse(config, parse_key_list, "key", "list", what="key list") or []
    key_name_map = {k.key_id: k.name for k in key_entries}

    buckets: list[GarageBucket] = []
    for entry in _try_parse(config, parse_bucket_list, "bucket", "list", what="bucket list") or []:
        # Address bucket info by global alias when present, otherwise by UUID.
        # The Garage CLI accepts a bucket UUID anywhere it accepts a global alias.
        # Post-bucket-naming-refactor most customer buckets won't have a global
        # alias — only website-hosted ones do — so dropping alias-less buckets
        # here would silently hide them from the dashboard.
        bucket_ref = entry.global_alias or entry.bucket_id
        if not bucket_ref:
            continue
        info = _try_parse(
            config, parse_bucket_info, "bucket", "info", bucket_ref,
            what=f"bucket info for {bucket_ref}",
        )
        if info is None:
            continue
        buckets.append(GarageBucket(
            id=info.bucket_id,
            alias=entry.global_alias,
            size_bytes=info.size_bytes,
            object_count=info.object_count,
            keys=[
                GarageKeyRef(k.access_key_id, key_name_map.get(k.access_key_id, ""), k.permissions)
                for k in info.keys
            ],
            website_access=info.website_access,
            website_index_document=info.website_index_document,
            website_error_document=info.website_error_document,
            quota_max_size_bytes=info.quota_max_size_bytes,
            quota_max_objects=info.quota_max_objects,
        ))

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
