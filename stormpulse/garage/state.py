"""Garage state collection — structured dataclasses and subprocess runner."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

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


def collect_garage_state(config: GarageConfig) -> GarageState | None:
    """Collect full Garage state by running status, stats, and bucket info commands.

    Returns None if the node is unreachable or the output can't be parsed.
    """
    status_out = _run_garage(config, "status")
    if status_out is None:
        return None

    nodes = parse_status(status_out)
    if not nodes:
        logger.warning("No nodes found in garage status output")
        return None

    # Use the first node (single-node deployment)
    node = nodes[0]

    # Collect stats
    db_engine = "unknown"
    object_count = 0
    block_count = 0
    stats_out = _run_garage(config, "stats")
    if stats_out is not None:
        try:
            stats = parse_stats(stats_out)
            db_engine = stats.db_engine
            object_count = stats.object_count
            block_count = stats.block_count
        except GarageParseError:
            logger.warning("Failed to parse garage stats output")

    # Build key ID -> name map from key list (real key names)
    key_name_map: dict[str, str] = {}
    key_list_out = _run_garage(config, "key", "list")
    if key_list_out is not None:
        try:
            for key_entry in parse_key_list(key_list_out):
                key_name_map[key_entry.key_id] = key_entry.name
        except GarageParseError:
            logger.warning("Failed to parse garage key list output")

    # Collect bucket list + info
    buckets: list[GarageBucket] = []
    bucket_list_out = _run_garage(config, "bucket", "list")
    if bucket_list_out is not None:
        try:
            bucket_entries = parse_bucket_list(bucket_list_out)
            for entry in bucket_entries:
                alias = entry.global_alias
                if not alias:
                    continue
                info_out = _run_garage(config, "bucket", "info", alias)
                if info_out is None:
                    continue
                try:
                    info = parse_bucket_info(info_out)
                    keys = [
                        GarageKeyRef(
                            key_id=k.access_key_id,
                            key_name=key_name_map.get(k.access_key_id, ""),
                            permissions=k.permissions,
                        )
                        for k in info.keys
                    ]
                    buckets.append(GarageBucket(
                        id=info.bucket_id,
                        alias=alias,
                        size_bytes=info.size_bytes,
                        object_count=info.object_count,
                        keys=keys,
                    ))
                except GarageParseError:
                    logger.warning("Failed to parse bucket info for %s", alias)
        except GarageParseError:
            logger.warning("Failed to parse garage bucket list output")

    # All keys (including unlinked ones) for the dashboard
    all_keys = [
        GarageKeyRef(key_id=kid, key_name=kname, permissions="")
        for kid, kname in key_name_map.items()
    ]

    return GarageState(
        node_id=node.node_id,
        hostname=node.hostname,
        zone=node.zone,
        capacity_gb=node.capacity_gb,
        data_avail_gb=node.data_avail_gb,
        version=node.version,
        healthy=node.healthy,
        db_engine=db_engine,
        object_count=object_count,
        block_count=block_count,
        buckets=buckets,
        keys=all_keys,
        peers=nodes,
    )
