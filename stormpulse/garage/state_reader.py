"""The cadence-aware periodic Garage read (CORE-005 decision 9).

``GarageStateReader`` owns the process-lifetime caches and the three cadences
(topology, the minute membership diff, the five-minute re-read) and the hint
file's pending ids. Every admin read goes through ``state``'s module-level
functions, so patching ``state.admin_api`` reaches this module too.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from stormpulse import events
from stormpulse.garage import state
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.hint import HintRead, Refusal, read_hint
from stormpulse.garage.state import GarageBucket, GarageState, Topology

logger = logging.getLogger(__name__)

# One summary per admin walk; nested decorated reads fold into it.
_garage_walk = events.walk(source="garage_admin", item="bucket")

# Hinted ids read per push: about 1.3 s of a 15 s push at 40 ms a call.
MAX_HINTED_READS = 32


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


def _merge(
    prior: dict[str, GarageBucket],
    ids: list[str],
    buckets: list[GarageBucket],
    absorbed: dict[str, GarageBucket],
    *,
    every: bool,
) -> tuple[dict[str, GarageBucket], int]:
    """The cache after a list, and how many ids it dropped: the reads over the
    kept cache (nothing kept on a full re-read), absorbed reads over both, and
    only ids listed or absorbed survive. Known ids keep their place."""
    base = {} if every else prior
    merged = base | {b.id: b for b in buckets if b.id} | absorbed
    keep = set(ids) | absorbed.keys()
    cache = {i: b for i, b in merged.items() if i in keep}
    return cache, sum(i not in keep for i in prior)


class GarageStateReader:
    """Cadence-aware periodic read (CORE-005 decision 9). With no hint file every
    call walks every bucket, as before hints existed. With one, each call re-reads
    the buckets it names, a membership diff (one ``ListBuckets``) runs once per
    ``SWEEP_SECONDS``, every bucket once per ``REREAD_SECONDS``, topology every
    ``TOPOLOGY_EVERY`` producing calls. Every targeted read goes through
    ``read_buckets`` and lands in the cache, so a non-sweep call never reverts one."""

    TOPOLOGY_EVERY = 6
    SWEEP_SECONDS = 60.0
    REREAD_SECONDS = 300.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._topology: Topology | None = None
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
        self._reread_due = _Every(self.REREAD_SECONDS, clock)
        # One collect at a time (periodic loop vs refresh). ``_lock`` guards the
        # cache, which ``read_buckets`` touches from the post-mutation hook mid-collect.
        self._collect_lock = threading.Lock()
        self._lock = threading.Lock()

    @_garage_walk
    def collect(
        self, config: GarageConfig, *, fresh: bool = False
    ) -> GarageState | None:
        """One periodic read: a full re-read when due, else a membership diff
        when due, and the hinted buckets on every call that did not re-read.
        ``fresh`` re-reads topology and every bucket regardless of cadence. A
        failed topology read or diff keeps the cache and stays due; a failed
        re-read returns None; only a producing call advances a cadence."""
        if not state.admin_configured(config):
            return None
        with self._collect_lock:
            result = self._collect(config, fresh=fresh)
            if result is not None:
                self._ticks += 1
            return result

    def _collect(self, config: GarageConfig, *, fresh: bool) -> GarageState | None:
        self._take_hint(config)
        topology = self._read_topology(config, fresh=fresh)
        if topology is None:
            logger.warning("No garage topology read yet; skipping state this tick")
            return None
        if fresh or not config.hint_file or self._reread_due.due():
            if not self._refresh(config, every=True):
                logger.warning("Bucket state unavailable this tick; skipping push")
                return None
            # A full re-read satisfies the diff too, so both cadences restart.
            self._reread_due.mark()
            self._sweep_due.mark()
        else:
            if self._sweep_due.due() and self._refresh(config, every=False):
                self._sweep_due.mark()
            self.read_buckets(config, self._drain())
        with self._lock:
            assert self._buckets is not None  # a refresh has produced
            buckets = list(self._buckets.values())
        return state.compose(config, topology, buckets)

    def _read_topology(self, config: GarageConfig, *, fresh: bool) -> Topology | None:
        """The cached topology, re-read when ``fresh`` or due; a failed read
        keeps the cache and stays due."""
        if fresh or self._topology_due.due():
            topology = state.collect_topology(config)
            if topology is not None:
                self._topology = topology
                self._topology_due.mark()
        return self._topology

    def _refresh(self, config: GarageConfig, *, every: bool) -> bool:
        """One ``ListBuckets``, then ``GetBucketInfo`` on every listed id (the full
        re-read, which also clears the pending hints) or only on the ids the
        cache lacks (the minute membership diff). Ids no longer listed drop from
        the cache unless a read absorbed them while the list was in flight, and
        the open walk hears the drop count. False, cache untouched, on a failed list."""
        with self._lock:
            self._absorbed = {}
        ids, buckets = None, []
        try:
            ids = state.list_bucket_ids(config)
            if ids is not None:
                with self._lock:
                    cached = self._buckets or {}
                targets = ids if every else [i for i in ids if i not in cached]
                buckets = state.read_buckets_by_id(config, targets)
        finally:
            with self._lock:
                absorbed, self._absorbed = self._absorbed or {}, None
                if ids is not None:
                    prior = self._buckets or {}
                    self._buckets, gone = _merge(
                        prior, ids, buckets, absorbed, every=every
                    )
                    events.record_dropped(gone)
                    if every:
                        self._pending.clear()
        return ids is not None

    def read_buckets(self, config: GarageConfig, ids: list[str]) -> list[GarageBucket]:
        """Targeted read of *ids*, absorbed into the cache by id (a no-op before
        the first sweep); known ids keep their place, new ids append."""
        buckets = state.read_buckets_by_id(config, ids)
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
            batch = self._pending[:MAX_HINTED_READS]
            del self._pending[:MAX_HINTED_READS]
        return batch
