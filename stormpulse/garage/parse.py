"""Pure parsers for Garage CLI stdout - no subprocess calls, fully unit testable."""

from __future__ import annotations

import re
from dataclasses import dataclass


class GarageParseError(Exception):
    """Raised when Garage CLI output cannot be parsed."""


@dataclass(frozen=True, slots=True)
class GaragePeer:
    """A single node row from ``garage status`` output."""

    node_id: str
    hostname: str
    address: str
    zone: str
    capacity_gb: float
    data_avail_gb: float
    data_avail_percent: float
    version: str
    healthy: bool


def parse_status(stdout: str) -> list[GaragePeer]:
    """Parse ``garage status`` stdout into a list of nodes.

    Expects the ``==== HEALTHY NODES ====`` table format.
    Returns an empty list if no nodes are found.
    """
    nodes: list[GaragePeer] = []
    in_healthy = False
    in_sick = False

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if "HEALTHY NODES" in stripped:
            in_healthy = True
            in_sick = False
            continue
        if "SICK NODES" in stripped or "DRAINING NODES" in stripped:
            in_healthy = False
            in_sick = "SICK" in stripped
            continue

        # Skip header line
        if stripped.startswith("ID"):
            continue

        # Parse node rows: ID  Hostname  Address  Tags  Zone  Capacity  DataAvail  Version
        parts = stripped.split()
        if len(parts) < 8:
            continue

        node_id = parts[0]
        hostname = parts[1]
        address = parts[2]

        # Tags field is like [] or [tag1,tag2] - find it and skip
        tags_idx = -1
        for i, p in enumerate(parts):
            if p.startswith("["):
                tags_idx = i
                break
        if tags_idx < 0:
            continue

        # After tags: zone, capacity (with unit), data_avail (with unit and percent), version
        rest = parts[tags_idx + 1 :]
        if len(rest) < 5:
            continue

        zone = rest[0]
        capacity_gb = _parse_size_gb(rest[1], rest[2])
        # DataAvail is like "16.3" "GB" "(83.0%)"
        data_avail_gb = _parse_size_gb(rest[3], rest[4])
        data_avail_percent = 0.0
        version_idx = 5
        if len(rest) > 5 and rest[5].startswith("("):
            pct_match = re.search(r"([\d.]+)%", rest[5])
            if pct_match:
                data_avail_percent = float(pct_match.group(1))
            version_idx = 6

        version = rest[version_idx] if len(rest) > version_idx else "unknown"

        nodes.append(
            GaragePeer(
                node_id=node_id,
                hostname=hostname,
                address=address,
                zone=zone,
                capacity_gb=capacity_gb,
                data_avail_gb=data_avail_gb,
                data_avail_percent=data_avail_percent,
                version=version,
                healthy=in_healthy and not in_sick,
            )
        )

    return nodes


_SIZE_MULTIPLIERS: dict[str, int] = {
    # SI (decimal). Garage uses these in the parenthesised
    # human-readable side of size lines, e.g. ``Size: 5.7 kiB (5.8 KB)``.
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    # IEC (binary). Garage uses these for the authoritative size in
    # ``bucket info`` and ``garage status``.
    "KIB": 1_024,
    "MIB": 1_048_576,
    "GIB": 1_073_741_824,
    "TIB": 1_099_511_627_776,
}


def _parse_size_gb(value: str, unit: str) -> float:
    """Convert a size value + unit to GB (decimal, 10^9 bytes).

    Uses the same SI/IEC convention as ``_size_to_bytes`` - KB/MB/GB/TB
    are decimal, KiB/MiB/GiB/TiB are binary. Without this alignment,
    ``5 GB`` parsed by ``_parse_size_gb`` and ``_size_to_bytes`` would
    disagree on byte count.
    """
    try:
        num = float(value)
    except ValueError:
        return 0.0
    multiplier = _SIZE_MULTIPLIERS.get(unit.upper().rstrip(")"))
    if multiplier is None:
        return 0.0
    return num * multiplier / 1_000_000_000


@dataclass(frozen=True, slots=True)
class GarageStats:
    """Parsed output from ``garage stats``."""

    db_engine: str
    object_count: int
    block_count: int


def parse_stats(stdout: str) -> GarageStats:
    """Parse ``garage stats`` stdout for db engine, object count, block count.

    Real output format (v2.2.0):
    - ``Database engine:  sqlite3 v3.50.2 (using rusqlite crate)``
    - Object count from table stats row: ``object  5  6  0  0  3``
      (Items column = first number after the table name)
    - Block count from: ``number of RC entries:  1 (~= number of blocks)``
    """
    db_engine = "unknown"
    object_count = 0
    block_count = 0
    in_table_stats = False

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("Database engine:"):
            db_engine = stripped.split(":", 1)[1].strip()

        # Table stats section - object count is the Items column
        elif stripped == "Table stats:":
            in_table_stats = True
        elif in_table_stats:
            if stripped.startswith("Table"):
                # Header row, skip
                continue
            if not stripped[0].isspace() and not stripped.startswith("object"):
                # Only match rows starting with a table name at indented level
                pass
            # Match: "  object  5  6  0  0  3"
            m = re.match(r"^object\s+(\d+)", stripped)
            if m:
                object_count = int(m.group(1))
            # End of table stats section
            if stripped.startswith("Block manager") or stripped.startswith("===="):
                in_table_stats = False

        # Block count from block manager stats
        elif stripped.startswith("number of RC entries:"):
            # "number of RC entries:  1 (~= number of blocks)"
            m = re.search(r":\s*(\d+)", stripped)
            if m:
                block_count = int(m.group(1))

    return GarageStats(
        db_engine=db_engine,
        object_count=object_count,
        block_count=block_count,
    )


def _parse_int(s: str) -> int:
    """Parse an integer from a string, returning 0 on failure."""
    try:
        return int(s.split()[0])
    except (ValueError, IndexError):
        return 0


@dataclass(frozen=True, slots=True)
class GarageBucketKeyEntry:
    """A key's permissions for a specific bucket."""

    permissions: str
    access_key_id: str
    local_alias: str


@dataclass(frozen=True, slots=True)
class GarageBucketInfo:
    """Parsed output from ``garage bucket info <name>``."""

    bucket_id: str
    size_bytes: int
    object_count: int
    website_access: bool
    website_index_document: str
    website_error_document: str | None
    global_alias: str
    keys: list[GarageBucketKeyEntry]
    quota_max_size_bytes: int | None
    quota_max_objects: int | None


def parse_bucket_info(stdout: str) -> GarageBucketInfo:
    """Parse ``garage bucket info`` stdout."""
    bucket_id = ""
    size_bytes = 0
    object_count = 0
    website_access = False
    website_index_document = "index.html"
    website_error_document: str | None = None
    global_alias = ""
    keys: list[GarageBucketKeyEntry] = []
    quota_max_size_bytes: int | None = None
    quota_max_objects: int | None = None

    in_keys_section = False

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if "KEYS FOR THIS BUCKET" in stripped:
            in_keys_section = True
            continue

        if in_keys_section:
            # Skip header
            if stripped.startswith("Permissions"):
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                permissions = parts[0]
                access_key_id = parts[1]
                # Garage's ``bucket info`` keys table has evolved:
                #   v1.x (3 cols): Permissions | Access key | Local aliases
                #   v2.x (4 cols): Permissions | Access key | Key name | Local aliases
                # Detect by part count. The local alias is always the
                # LAST column. If only 2 parts, the bucket has no local
                # alias attached for this key.
                if len(parts) >= 4:
                    local_alias = parts[3]
                elif len(parts) == 3:
                    local_alias = parts[2]
                else:
                    local_alias = ""
                keys.append(
                    GarageBucketKeyEntry(
                        permissions=permissions,
                        access_key_id=access_key_id,
                        local_alias=local_alias,
                    )
                )
            continue

        if stripped.startswith("Bucket:"):
            bucket_id = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Size:"):
            size_bytes = _parse_size_bytes(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Objects:"):
            object_count = _parse_int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Website access:"):
            website_access = stripped.split(":", 1)[1].strip().lower() == "true"
        elif stripped.startswith("index document:"):
            website_index_document = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("error document:"):
            val = stripped.split(":", 1)[1].strip()
            website_error_document = None if val == "(not defined)" else val
        elif stripped.startswith("Global alias:"):
            global_alias = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("maximum size:"):
            quota_max_size_bytes = _parse_size_bytes(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("maximum number of objects:"):
            quota_max_objects = _parse_int(stripped.split(":", 1)[1].strip())

    if not bucket_id:
        raise GarageParseError("Could not parse bucket ID from bucket info output")

    return GarageBucketInfo(
        bucket_id=bucket_id,
        size_bytes=size_bytes,
        object_count=object_count,
        website_access=website_access,
        website_index_document=website_index_document,
        website_error_document=website_error_document,
        global_alias=global_alias,
        keys=keys,
        quota_max_size_bytes=quota_max_size_bytes,
        quota_max_objects=quota_max_objects,
    )


def _parse_size_bytes(s: str) -> int:
    """Parse size string like '5.7 kiB (5.8 KB)' into bytes.

    Uses the parenthesized KB/MB/GB value if present, otherwise the first value.
    """
    # Try parenthesized value first: (5.8 KB)
    paren_match = re.search(r"\(([\d.]+)\s*(B|KB|MB|GB|TB)\)", s, re.IGNORECASE)
    if paren_match:
        num = float(paren_match.group(1))
        unit = paren_match.group(2).upper()
        return _size_to_bytes(num, unit)

    # Fall back to first value
    parts = s.split()
    if len(parts) >= 2:
        try:
            num = float(parts[0])
            unit = parts[1].upper()
            return _size_to_bytes(num, unit)
        except ValueError:
            pass

    return 0


def _size_to_bytes(num: float, unit: str) -> int:
    """Convert a numeric value + unit to bytes.

    SI (KB/MB/GB/TB) are decimal (10^N); IEC (KiB/MiB/GiB/TiB) are
    binary (2^N). Garage CLI output uses both, e.g.
    ``Size: 5.7 kiB (5.8 KB)``. Falls back to a single byte multiplier
    for unrecognised units so a malformed size string yields the
    numeric portion as bytes rather than panicking.
    """
    return int(num * _SIZE_MULTIPLIERS.get(unit.upper(), 1))


@dataclass(frozen=True, slots=True)
class GarageKeyListEntry:
    """A single row from ``garage key list``."""

    key_id: str
    name: str


def parse_key_list(stdout: str) -> list[GarageKeyListEntry]:
    """Parse ``garage key list`` stdout.

    Expected format:
        ID                          Created     Name          Expiration
        GK5e6fb0b4fa406ace8126a7db  2026-04-07  obsidian-key  never
    """
    keys: list[GarageKeyListEntry] = []
    past_header = False

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ID"):
            past_header = True
            continue
        if not past_header:
            continue

        parts = stripped.split()
        if len(parts) < 3:
            continue

        key_id = parts[0]
        # parts[1] is created date, parts[2] is name
        name = parts[2]

        keys.append(GarageKeyListEntry(key_id=key_id, name=name))

    return keys


@dataclass(frozen=True, slots=True)
class GarageKeyInfoBucketRef:
    """A bucket entry from ``garage key info`` - what bucket(s) the key
    has access to, with permissions and any local alias.
    """

    bucket_id: str
    permissions: str
    local_alias: str


@dataclass(frozen=True, slots=True)
class GarageKeyInfo:
    """Parsed output from ``garage key info <id>``.

    The key field of interest for the bucket-delete orchestrator is
    ``buckets`` - empty list means the key is unmoored and safe to
    delete after a bucket teardown.
    """

    key_id: str
    name: str
    buckets: list[GarageKeyInfoBucketRef]


def parse_key_info(stdout: str) -> GarageKeyInfo:
    """Parse ``garage key info`` stdout.

    The output structure mirrors ``bucket info``: a header section with
    key metadata, then a ``BUCKETS FOR THIS KEY`` table. An empty
    table (just the header line) means the key has no associated
    buckets - a safety signal for orchestrators that need to decide
    whether to delete a key after its only bucket is gone.
    """
    key_id = ""
    name = ""
    buckets: list[GarageKeyInfoBucketRef] = []
    in_buckets_section = False

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if "BUCKETS FOR THIS KEY" in stripped:
            in_buckets_section = True
            continue

        if in_buckets_section:
            # Skip header
            if stripped.startswith("Permissions"):
                continue
            parts = stripped.split()
            # Bucket rows: Permissions  Bucket ID  [Global aliases]  [Local aliases]
            if len(parts) >= 2:
                permissions = parts[0]
                bucket_id = parts[1]
                # Last column is local alias if present
                local_alias = parts[-1] if len(parts) >= 3 else ""
                buckets.append(
                    GarageKeyInfoBucketRef(
                        bucket_id=bucket_id,
                        permissions=permissions,
                        local_alias=local_alias,
                    )
                )
            continue

        if stripped.startswith("Key name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Key ID:"):
            key_id = stripped.split(":", 1)[1].strip()

    if not key_id:
        raise GarageParseError("Could not parse key ID from key info output")

    return GarageKeyInfo(key_id=key_id, name=name, buckets=buckets)


@dataclass(frozen=True, slots=True)
class GarageKeyCreateResult:
    """Parsed output from ``garage key create``.

    WARNING: ``secret_key`` contains the secret access key. This value
    must NEVER be logged at any level (DEBUG, INFO, WARNING, ERROR, or TRACE).
    It is returned in the command result stdout exactly once. The dashboard
    is responsible for displaying it once and never storing it.
    """

    key_id: str
    name: str
    secret_key: str


def parse_key_create(stdout: str) -> GarageKeyCreateResult:
    """Parse ``garage key create`` stdout.

    WARNING: The returned ``secret_key`` must NEVER be logged at any level.
    The dashboard displays it once and discards it. This parser exists only
    to validate the output structure - the raw stdout is returned to the
    dashboard via command.result as-is.
    """
    key_id = ""
    name = ""
    secret_key = ""

    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Key ID:"):
            key_id = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Key name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Secret key:"):
            secret_key = stripped.split(":", 1)[1].strip()

    if not key_id or not secret_key:
        raise GarageParseError(
            "Could not parse key ID or secret from key create output"
        )

    return GarageKeyCreateResult(key_id=key_id, name=name, secret_key=secret_key)
