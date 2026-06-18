"""Resolve a Garage S3 access-log line to its globally-unique bucket id.

ADR BUCKETS-015. A Garage S3 access-log line carries only ``(key_id, name)``,
where ``name`` is the first path segment: the bucket's local alias scoped to
the requesting key, or its global alias. Neither is globally unique on its own
(an account's two buckets may share a display name), so the website cannot
attribute an account-key write to the right bucket from the name alone.

Garage resolves ``(key, name)`` to exactly one bucket id, and the agent already
holds that mapping in :class:`~stormpulse.garage.state.GarageState`. This turns
that state into a frozen ``(key_id, name) -> bucket_id`` lookup. It lives in the
garage layer because it is pure GarageState-derived domain logic; the log-ship
path (the logging layer) only ever sees the plain ``(key_id, name) -> str``
callable, and the agent layer composes the two (the four-layer topology forbids
logging and garage, sibling features, from importing each other directly).

The resolver is immutable once built. The log loop rebuilds one per tick from
the latest ``GarageState``.
"""

from __future__ import annotations

from stormpulse.garage.state import GarageState


class BucketIdResolver:
    """Frozen ``(key_id, name) -> bucket_id`` lookup built from one GarageState.

    Two maps, checked in order of specificity:

    - ``_key_scoped`` keys on ``(key_id, local_alias)``. A key's local alias is
      the name namespace private to that key, so this is the exact, unambiguous
      handle for an S3-created (BUCKETS-012) bucket addressed by its owning key.
    - ``_global_alias`` keys on the bucket's global alias alone. Dashboard-
      provisioned buckets carry a global alias and their owning key holds no
      local alias, so a global-alias hit is the fallback for those rows. Global
      aliases are unique across the cluster, so this never misfiles.
    """

    __slots__ = ("_key_scoped", "_global_alias")

    def __init__(
        self,
        key_scoped: dict[tuple[str, str], str],
        global_alias: dict[str, str],
    ) -> None:
        self._key_scoped = key_scoped
        self._global_alias = global_alias

    @classmethod
    def from_state(cls, state: GarageState | None) -> BucketIdResolver:
        """Build a resolver from a GarageState snapshot (or an empty one).

        ``None`` (Garage not live, or no state collected yet) yields an empty
        resolver: every lookup returns ``''`` and the website falls back to
        key-anchoring.
        """
        key_scoped: dict[tuple[str, str], str] = {}
        global_alias: dict[str, str] = {}
        if state is not None:
            for bucket in state.buckets:
                if not bucket.id:
                    continue
                if bucket.alias:
                    global_alias[bucket.alias] = bucket.id
                for key in bucket.keys:
                    if not key.key_id:
                        continue
                    for local_alias in key.bucket_local_aliases:
                        if local_alias:
                            key_scoped[(key.key_id, local_alias)] = bucket.id
        return cls(key_scoped, global_alias)

    def resolve(self, key_id: str, name: str) -> str:
        """Return the bucket id for ``(key_id, name)``, or ``''`` when unresolved.

        Key-scoped local aliases win over global aliases: they are the exact
        namespace the requesting key addressed. ``''`` means "not in the last
        state snapshot" (a brand-new bucket, or a non-bucket line such as an
        admin operation) and signals the website to fall back to key-anchoring.
        """
        if not name:
            return ""
        scoped = self._key_scoped.get((key_id, name))
        if scoped:
            return scoped
        return self._global_alias.get(name, "")

    def __call__(self, key_id: str, name: str) -> str:
        return self.resolve(key_id, name)
