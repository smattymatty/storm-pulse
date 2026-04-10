"""Pure parsers for Garage CLI stdout — no subprocess calls, fully unit testable."""

from __future__ import annotations

import re
from dataclasses import dataclass


class GarageParseError(Exception):
    """Raised when Garage CLI output cannot be parsed."""


# ---------------------------------------------------------------------------
# garage status
# ---------------------------------------------------------------------------


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

        # Tags field is like [] or [tag1,tag2] — find it and skip
        tags_idx = -1
        for i, p in enumerate(parts):
            if p.startswith("["):
                tags_idx = i
                break
        if tags_idx < 0:
            continue

        # After tags: zone, capacity (with unit), data_avail (with unit and percent), version
        rest = parts[tags_idx + 1:]
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

        nodes.append(GaragePeer(
            node_id=node_id,
            hostname=hostname,
            address=address,
            zone=zone,
            capacity_gb=capacity_gb,
            data_avail_gb=data_avail_gb,
            data_avail_percent=data_avail_percent,
            version=version,
            healthy=in_healthy and not in_sick,
        ))

    return nodes


def _parse_size_gb(value: str, unit: str) -> float:
    """Convert a size value + unit to GB."""
    try:
        num = float(value)
    except ValueError:
        return 0.0
    unit_upper = unit.upper().rstrip(")")
    if unit_upper == "TB":
        return num * 1024
    if unit_upper == "GB":
        return num
    if unit_upper == "MB":
        return num / 1024
    if unit_upper == "KB" or unit_upper == "KIB":
        return num / (1024 * 1024)
    return num


# ---------------------------------------------------------------------------
# garage stats
# ---------------------------------------------------------------------------


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

        # Table stats section — object count is the Items column
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


# ---------------------------------------------------------------------------
# garage bucket list
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GarageBucketListEntry:
    """A single row from ``garage bucket list``."""

    bucket_id: str
    global_alias: str


def parse_bucket_list(stdout: str) -> list[GarageBucketListEntry]:
    """Parse ``garage bucket list`` stdout.

    Expected format:
        ID                Created     Global aliases  Local aliases
        f1dc32249aa1d80a  2026-04-07  obsidian-vault
    """
    buckets: list[GarageBucketListEntry] = []
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
        if len(parts) < 2:
            continue

        bucket_id = parts[0]
        # parts[1] is the created date, parts[2] is global alias (if present)
        global_alias = parts[2] if len(parts) >= 3 else ""

        buckets.append(GarageBucketListEntry(
            bucket_id=bucket_id,
            global_alias=global_alias,
        ))

    return buckets


# ---------------------------------------------------------------------------
# garage bucket info <name>
# ---------------------------------------------------------------------------


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
                local_alias = parts[2] if len(parts) >= 3 else ""
                keys.append(GarageBucketKeyEntry(
                    permissions=permissions,
                    access_key_id=access_key_id,
                    local_alias=local_alias,
                ))
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
    """Convert a numeric value + unit to bytes."""
    multipliers = {"B": 1, "KB": 1000, "KIB": 1024, "MB": 1_000_000, "MIB": 1_048_576,
                   "GB": 1_000_000_000, "GIB": 1_073_741_824, "TB": 1_000_000_000_000}
    return int(num * multipliers.get(unit, 1))


# ---------------------------------------------------------------------------
# garage key list
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# garage key create
# ---------------------------------------------------------------------------


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
    to validate the output structure — the raw stdout is returned to the
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
        raise GarageParseError("Could not parse key ID or secret from key create output")

    return GarageKeyCreateResult(key_id=key_id, name=name, secret_key=secret_key)
