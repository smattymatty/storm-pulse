"""Garage state collection over the admin HTTP API, never the Garage CLI.

Node telemetry (cluster status, statistics, key list) and per-bucket state
(sizes, object counts, quotas, keys) are all read via the admin HTTP API, never
the Garage CLI. ``GaragePeer`` (a dataclass, not a scraper) lives here; it
moved in when the CLI-scraping ``parse.py`` was deleted.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from stormpulse import events
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.hint import HintRead, Refusal, read_hint

logger = logging.getLogger(__name__)

# One summary per admin walk; each decorated call opens its own scope.
_garage_walk = events.walk(source="garage_admin", item="bucket")


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
    An S3-created bucket has no global alias, so its name lives
    here under the owning key; the control plane's adopt branch reads it to name the
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
    # Bytes held by multipart uploads that were started and never completed.
    # Reported SEPARATELY from size_bytes, never folded into it: Garage does
    # not count these against the bucket's quota (an ordinary PUT past the cap
    # is refused, an MPU part past the same cap is accepted), so folding them
    # in would silently redefine what "usage" means to the capacity model.
    # Surfaced so the control plane can see storage the cap does not govern.
    unfinished_upload_bytes: int = 0
    unfinished_uploads: int = 0


@dataclass(frozen=True, slots=True)
class GarageAdminMetric:
    """Admin-API call telemetry for one target node over the agent's trailing
    meter window (``admin_api._AdminCallMeter``): the graph the 2026-06-27
    saturation incident lacked. Keyed by target node so multi-node dispatch
    attributes load to the endpoint, not the agent. It rides the state blob but is
    NOT durable state; the control plane keeps it in ``GarageNodeMetric``.
    """

    target_node_id: str
    calls_per_sec: float
    p95_latency_ms: float
    sample_count: int


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
    # Per-target-node admin-API call telemetry over the agent's meter window.
    # Empty by default so test/fixture states and targeted merges (which carry
    # the last periodic value, see ``with_items``) stay valid.
    admin_metrics: tuple[GarageAdminMetric, ...] = ()

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

    def with_items(self, buckets: Iterable[GarageBucket]) -> GarageState:
        """Upsert *buckets* by id into a new, order-stable state - the shared merge
        primitive (CORE-005 decision 11). Always the FULL set, never a partial
        (manifest alarms, never acts); falsy ids ignored."""
        incoming = {b.id: b for b in buckets if b.id}
        if not incoming:
            return self
        # Pop replaces known ids in place; whatever remains in incoming is new,
        # appended in order.
        merged = [incoming.pop(existing.id, existing) for existing in self.buckets]
        merged.extend(incoming.values())
        return replace(self, buckets=merged)


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
        unfinished_upload_bytes=int(info.get("unfinishedMultipartUploadBytes") or 0),
        unfinished_uploads=int(info.get("unfinishedMultipartUploads") or 0),
    )


@_garage_walk
def read_buckets_by_id(
    config: GarageConfig, bucket_ids: Iterable[str]
) -> list[GarageBucket]:
    """Targeted ``GetBucketInfo`` per id; a failed read (incl. a delete's 404) is
    skipped and logged, never fabricated. Callers merge upsert-only, so a removal
    rides the full walk + reconcile, never a partial-manifest deletion."""
    admin_url, admin_token = config.admin_url, config.admin_token
    if not (admin_url and admin_token):
        return []
    buckets: list[GarageBucket] = []
    for bucket_id in bucket_ids:
        info, err = admin_api.get_bucket_info(
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_ref=bucket_id,
        )
        if info is None:
            logger.warning(
                "GetBucketInfo failed for %s; skipping this bucket: %s",
                bucket_id,
                err,
            )
            continue
        buckets.append(_bucket_from_admin_info(info))
    return buckets


# Param names a garage command uses to name the resource it touched. Explicit
# allowlists, not substring magic, and co-located with the command param source
# (``commands.py``, this package): a param renamed there silently misses here and
# its change rides the periodic walk instead, never a wrong re-read.
_BUCKET_ID_PARAMS = ("bucket_id",)
# A bucket named only by alias (global or key-local) is not cheaply resolvable to
# an id, so it defers to the walk - but it still marks the command as bucket-scoped,
# suppressing the key path below (the key is just the grantee, not the target).
_BUCKET_ALIAS_PARAMS = ("bucket_name", "alias_name", "local_alias")
# NOTE: ``new_key_id`` is intentionally absent. The only command that takes it is
# ``garage_converge_account_key_rotation``, which is ``self_reconciling`` and so
# never fires the hook (the resolver is never called for it). Safe today, but by
# coincidence not design: if a HOOKED command ever takes ``new_key_id``, add it
# here, or its change will only ride the periodic walk (safe, just not instant).
_KEY_ID_PARAMS = ("key_id", "account_key_id", "access_key_id", "old_key_id")


def affected_bucket_ids(params: Mapping[str, str], state: GarageState) -> list[str]:
    """Bucket ids a just-succeeded garage mutation could have changed. Precedence:

    1. A ``bucket_id`` param: exactly that bucket; any key param is the grantee.
    2. A bucket named only by alias: deferred to the walk (alias to id is not
       cheap here, and re-reading the grantee key's buckets would miss it).
    3. Only a key (delete_key, reap): the buckets that key touches, from the
       snapshot's recorded grants, never a live read. Creates name no existing
       id and resolve to nothing: the hint or the next walk brings them in.
    """
    direct = [params[name] for name in _BUCKET_ID_PARAMS if params.get(name)]
    if direct:
        return direct
    if any(params.get(name) for name in _BUCKET_ALIAS_PARAMS):
        return []
    key_ids = {params[name] for name in _KEY_ID_PARAMS if params.get(name)}
    if not key_ids:
        return []
    return [
        b.id for b in state.buckets if b.id and any(k.key_id in key_ids for k in b.keys)
    ]


def _collect_buckets_via_admin(config: GarageConfig) -> list[GarageBucket] | None:
    """List buckets and fetch each one's info over the admin HTTP API.

    Returns the bucket list, or **None** when the cluster can't be enumerated
    (``ListBuckets`` unreachable). The caller treats None as "skip this tick":
    pushing an empty set would read downstream as "every bucket vanished"
    (no fresh read, no action). A single bucket whose
    ``GetBucketInfo`` fails is skipped and logged, never crashing the tick.
    """
    admin_url, admin_token = config.admin_url, config.admin_token
    items, err = admin_api.list_buckets(admin_url=admin_url, admin_token=admin_token)
    if items is None:
        logger.warning("ListBuckets failed; skipping bucket state this tick: %s", err)
        return None
    return read_buckets_by_id(
        config, (item.get("id", "") for item in items if item.get("id"))
    )


@dataclass(frozen=True, slots=True)
class _Topology:
    """The slowly-changing slice of garage state: cluster nodes, totals, key inventory.

    Read together by ``_collect_topology`` and cached by ``GarageStateReader``
    between its slow-multiple refreshes. Internal to this module: never
    transmitted on its own, ``_compose_state`` folds it together with the
    per-bucket walk into the wire ``GarageState``.
    """

    object_count: int
    keys: list[GarageKeyRef]
    peers: list[GaragePeer]


def _admin_configured(config: GarageConfig) -> bool:
    """True iff the admin HTTP API is wired (admin_url + admin_token), else logs why."""
    if config.admin_url and config.admin_token:
        return True
    logger.error(
        "Garage admin API not configured (admin_url + admin_token); cannot "
        "collect state. Set [garage] admin_url and admin_token_file."
    )
    return False


def _collect_topology(config: GarageConfig) -> _Topology | None:
    """Read cluster status (required) plus statistics and key list (best-effort).

    Returns None when ``GetClusterStatus`` is unreachable or reports no nodes -
    the caller skips rather than compose a node-less snapshot. A stats or
    key-list failure only degrades its own field (object_count to 0, empty
    inventory), never the whole read.
    """
    admin_url, admin_token = config.admin_url, config.admin_token
    status, err = admin_api.get_cluster_status(
        admin_url=admin_url,
        admin_token=admin_token,
    )
    if status is None:
        logger.warning("GetClusterStatus failed; skipping topology read: %s", err)
        return None
    peers = [_peer_from_node(n) for n in status.get("nodes") or []]
    if not peers:
        logger.warning("No nodes in GetClusterStatus; skipping topology read")
        return None

    # Best-effort: a stats failure degrades object_count to 0, not the read.
    stats, _err = admin_api.get_cluster_statistics(
        admin_url=admin_url,
        admin_token=admin_token,
    )
    object_count = int((stats or {}).get("totalObjectCount") or 0)

    # Best-effort: a key-list failure empties the top-level inventory only.
    keys_raw, _err = admin_api.list_keys(admin_url=admin_url, admin_token=admin_token)
    keys = [
        GarageKeyRef(k.get("id", "") or "", k.get("name", "") or "", "")
        for k in (keys_raw or [])
    ]
    return _Topology(object_count=object_count, keys=keys, peers=peers)


def _walk_and_compose(config: GarageConfig, topology: _Topology) -> GarageState | None:
    """Walk every bucket and fold with *topology* into one wire ``GarageState``; None
    when enumeration fails (never push an empty set: invariant 4)."""
    buckets = _collect_buckets_via_admin(config)
    if buckets is None:
        logger.warning("Bucket state unavailable this tick; skipping state push")
        return None
    return _compose(config, topology, buckets)


def _compose(
    config: GarageConfig, topology: _Topology, buckets: list[GarageBucket]
) -> GarageState:
    """Fold *topology* and *buckets* into one wire ``GarageState``."""
    node = topology.peers[0]
    return GarageState(
        node_id=node.node_id,
        hostname=node.hostname,
        zone=node.zone,
        capacity_gb=node.capacity_gb,
        data_avail_gb=node.data_avail_gb,
        version=node.version,
        healthy=node.healthy,
        object_count=topology.object_count,
        buckets=buckets,
        keys=topology.keys,
        peers=topology.peers,
        admin_metrics=_read_admin_metrics(config, node),
    )


def _read_admin_metrics(
    config: GarageConfig, node: GaragePeer
) -> tuple[GarageAdminMetric, ...]:
    """Fold the admin-API meter snapshot into per-node telemetry for the push.

    The meter keys by admin endpoint (``admin_url``). Today every admin call
    targets the dispatch node's endpoint, so the one endpoint maps to ``node``,
    the node this read composed. A future multi-endpoint dispatch keys each by
    its own endpoint string rather than collapsing to the dispatch node - the
    saturation lives at the target, so we never hardcode the dispatch identity.
    """
    return tuple(
        GarageAdminMetric(
            target_node_id=node.node_id if endpoint == config.admin_url else endpoint,
            calls_per_sec=stats.calls_per_sec,
            p95_latency_ms=stats.p95_latency_ms,
            sample_count=stats.sample_count,
        )
        for endpoint, stats in admin_api.admin_call_stats().items()
    )


@_garage_walk
def collect_garage_state(config: GarageConfig) -> GarageState | None:
    """Collect the FULL Garage node state (topology + every bucket) via the admin API.

    The full, every-field compose: used by startup discovery and any force-full
    refresh. The periodic loop instead uses ``GarageStateReader``, which reads
    the per-bucket walk every tick but topology only on a slow multiple
    (capacity-model 2026-06-27 amendment). Returns None when the admin API is
    unconfigured/unreachable, no node is found, or the bucket set can't be
    enumerated - the caller skips rather than report a degraded snapshot.
    """
    if not _admin_configured(config):
        return None
    topology = _collect_topology(config)
    if topology is None:
        return None
    return _walk_and_compose(config, topology)


class _Every:
    """Due on first use, then once ``period`` has passed on ``clock`` since the
    last ``mark``. Only the caller marks, so a failed attempt stays due."""

    def __init__(self, period: float, clock: Callable[[], float]) -> None:
        self._period = period
        self._clock = clock
        self._last: float | None = None

    def due(self) -> bool:
        return self._last is None or self._clock() - self._last >= self._period

    def mark(self) -> None:
        self._last = self._clock()


class GarageStateReader:
    """Cadence-aware periodic read (CORE-005 decision 9). With no hint file every
    call walks every bucket, as before hints existed. With one, each call re-reads
    the buckets it names and every bucket is walked once per ``SWEEP_SECONDS``;
    topology every ``TOPOLOGY_EVERY`` producing calls. Every targeted read goes
    through ``read_buckets`` and lands in the cache, so a non-sweep call never reverts
    one. Process-lifetime: the caches deliberately survive reconnects."""

    TOPOLOGY_EVERY = 6
    SWEEP_SECONDS = 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._topology: _Topology | None = None
        # Last sweep's buckets by id, upserted by every targeted read since.
        # Not runtime state: the agent's one merge primitive stays the only
        # writer there (Fitness Function 6).
        self._buckets: dict[str, GarageBucket] | None = None
        # Reads absorbed while a sweep is in flight, laid over its result.
        self._absorbed: dict[str, GarageBucket] | None = None
        # Hinted ids not yet read, most recent first: the latest file leads.
        self._pending: list[str] = []
        self._last_refusal: Refusal | None = None
        self._ticks = 0
        self._topology_due = _Every(self.TOPOLOGY_EVERY, lambda: self._ticks)
        self._sweep_due = _Every(self.SWEEP_SECONDS, clock)
        # One collect at a time (periodic loop vs refresh). ``_lock`` guards the
        # cache, which ``read_buckets`` touches from the post-mutation hook mid-collect.
        self._collect_lock = threading.Lock()
        self._lock = threading.Lock()

    @_garage_walk
    def collect(
        self, config: GarageConfig, *, fresh: bool = False
    ) -> GarageState | None:
        """One periodic read: a full sweep when due, else the hinted buckets.

        ``fresh`` is the on-demand refresh: "the operator just changed
        something", so it re-reads topology and sweeps regardless of cadence.
        A failed topology read keeps the cache and stays due; a failed sweep
        returns None and stays due; only a producing call advances cadence.
        """
        if not _admin_configured(config):
            return None
        with self._collect_lock:
            state = self._collect(config, fresh=fresh)
            if state is not None:
                self._ticks += 1
            return state

    def _collect(self, config: GarageConfig, *, fresh: bool) -> GarageState | None:
        self._take_hint(config)
        if fresh or self._topology_due.due():
            topology = _collect_topology(config)
            if topology is not None:
                self._topology = topology
                self._topology_due.mark()
        topology = self._topology
        if topology is None:
            logger.warning("No garage topology read yet; skipping state this tick")
            return None
        if fresh or not config.hint_file or self._sweep_due.due():
            if not self._sweep(config):
                return None
            self._sweep_due.mark()
        else:
            self.read_buckets(config, self._drain())
        with self._lock:
            assert self._buckets is not None  # a sweep has produced
            buckets = list(self._buckets.values())
        return _compose(config, topology, buckets)

    def _sweep(self, config: GarageConfig) -> bool:
        """Walk every bucket into the cache, keeping reads absorbed meanwhile."""
        with self._lock:
            self._absorbed = {}
        buckets = None
        try:
            buckets = _collect_buckets_via_admin(config)
        finally:
            with self._lock:
                absorbed, self._absorbed = self._absorbed or {}, None
                if buckets is not None:
                    self._buckets = {b.id: b for b in buckets} | absorbed
                    self._pending.clear()
        if buckets is None:
            logger.warning("Bucket state unavailable this tick; skipping state push")
        return buckets is not None

    def read_buckets(self, config: GarageConfig, ids: list[str]) -> list[GarageBucket]:
        """Targeted read of *ids*, absorbed into the cache by id (a no-op before
        the first sweep); known ids keep their place, new ids append."""
        buckets = read_buckets_by_id(config, ids)
        with self._lock:
            upserts = [(b.id, b) for b in buckets if b.id]
            if self._buckets is not None:
                self._buckets.update(upserts)
            if self._absorbed is not None:
                self._absorbed.update(upserts)
        return buckets

    def _take_hint(self, config: GarageConfig) -> None:
        if not config.hint_file:
            return
        read = read_hint(config.hint_file)
        if read.refusal is None:
            self._last_refusal = None
            with self._lock:
                fresh = read.bucket_ids
                self._pending = [*fresh, *(i for i in self._pending if i not in fresh)]
            return
        self._log_refusal(config.hint_file, read)

    def _log_refusal(self, path: str, read: HintRead) -> None:
        # Once per distinct reason until the next good read; a stale file
        # only means the writer is down, so it never warns.
        if read.refusal == self._last_refusal:
            return
        self._last_refusal = read.refusal
        level = logging.DEBUG if read.refusal is Refusal.STALE else logging.WARNING
        logger.log(
            level, "Ignoring hint file %s (%s): %s", path, read.refusal, read.detail
        )

    def _drain(self) -> list[str]:
        with self._lock:
            batch = self._pending[:MAX_TARGETED_BUCKET_READS]
            del self._pending[:MAX_TARGETED_BUCKET_READS]
        return batch


# Shared fan-out bound for every per-id GetBucketInfo burst (one push's hinted
# batch, read_affected key->buckets): sized to admin-API tolerance. Hardcoded.
MAX_TARGETED_BUCKET_READS = 8


def cap_targeted_reads(ids: list[str], *, context: str) -> list[str]:
    """Cap a targeted-read fan-out at ``MAX_TARGETED_BUCKET_READS``, logging any deferral.

    Overflow is deferred loudly, never truncated silently; the periodic sweep
    catches it. ``context`` names the fan-out that deferred.
    """
    capped = ids[:MAX_TARGETED_BUCKET_READS]
    deferred = len(ids) - len(capped)
    if deferred:
        logger.info(
            "%s: %d targeted reads requested; reading %d this pass, deferring %d "
            "to the periodic walk",
            context,
            len(ids),
            len(capped),
            deferred,
        )
    return capped
