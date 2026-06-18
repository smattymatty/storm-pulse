"""Stateful in-process fake of the garage CLI for tests.

Replaces per-test scripted (rc, stdout, stderr) tuples with a fake that
enforces the rules real Garage applies - discovered empirically and from
``garage <subcommand> --help``. Tests assert against fake state (buckets,
keys, aliases, permissions) instead of against their own scripted echoes.

Each ``FakeGarage()`` instance is fresh state. No buckets, no keys.

## Rules encoded

1. ``bucket create <name>`` - S3-strict bucket name validation
   (3-63 chars, lowercase alphanumeric + hyphens, must start/end
   alphanumeric). Atomically creates bucket and attaches ``<name>`` as
   global alias.

2. ``bucket unalias <name>`` - orphan rule: refuse if removal would
   leave the bucket with zero aliases. Locals count.

3. ``bucket unalias --local <key> <name>`` - exactly ONE positional
   after ``--local <key>`` (verified from ``bucket unalias --help``).
   Three-positional form returns USAGE error. Same orphan check.

4. ``bucket alias --local <key> <existing> <new>`` - three positionals
   after ``--local <key>``. Resolves ``<existing>`` per rule 8.

5. ``bucket allow/deny <flags> <ref> --key <key>`` - flags are subset
   of ``--read``, ``--write``, ``--owner``. ``deny`` removes; ``allow``
   adds.

6. ``bucket delete --yes <ref>`` - refuses on non-empty buckets with
   ``BucketNotEmpty``. Otherwise deletes and revokes all key
   permissions referencing the bucket.

7. ``key create <name>`` - generates deterministic ``GK<24-hex>`` ID
   and 64-char secret from a per-instance counter.

8. **Bucket reference resolution.** Accept (a) any global alias, (b)
   the 16-char prefix of ``bucket_id``. Reject the full 64-char form
   with ``NoSuchBucket`` - empirically confirmed against garage-one
   v2.2.0. Local aliases are NOT globally addressable; they only
   resolve in their owning key's namespace.

9. ``bucket info <ref>`` - renders the format
   ``parse_bucket_info`` expects. The "KEYS FOR THIS BUCKET" table
   includes ONLY keys that have permissions on the bucket. Keys with a
   local alias attached but no permissions do not appear (empirically
   observed: ``bucket info`` showed an empty keys table after
   ``bucket alias --local`` succeeded but before ``bucket allow`` ran).

10. ``key create`` stdout - renders the format ``parse_key_create``
    expects: ``Key name: ...\\nKey ID: ...\\nSecret key: ...``.

## What the fake does NOT model

S3 data plane (object PUT/GET/DELETE), cluster state, layout,
replication, workers, scrub, repair, website hosting. None of the
orchestrator code paths touch these.

## Unrecognized call shapes

The dispatcher pattern-matches on the args tuple. Unknown shapes
raise ``NotImplementedError(args)`` - fail loudly rather than silently
lie about the result.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field

from stormpulse.garage.config import GarageConfig

_S3_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_PERM_FLAGS = frozenset({"--read", "--write", "--owner"})


@dataclass
class FailureSpec:
    """Queued failure response for ``fake.fail_next(...)``."""

    rc: int = 1
    stderr: str = ""
    skip_remaining: int = 0  # matching calls to let through before firing


@dataclass
class _Bucket:
    bucket_id: str
    global_aliases: set[str] = field(default_factory=set)
    local_aliases: dict[str, str] = field(default_factory=dict)
    object_count: int = 0
    size_bytes: int = 0


@dataclass
class _Key:
    key_id: str
    name: str
    secret_key: str
    permissions: dict[str, set[str]] = field(default_factory=dict)


class FakeGarage:
    """Stateful semantic fake of the garage CLI.

    Drop-in for ``stormpulse.garage.provision_bucket.run_garage``:
    monkeypatch the module-level binding to ``fake.run_garage`` and
    every ``await run_garage(config, *args)`` call routes through the
    fake's dispatcher.
    """

    def __init__(self) -> None:
        self.buckets: dict[str, _Bucket] = {}
        self.keys: dict[str, _Key] = {}
        self.calls: list[tuple[str, ...]] = []
        self._failure_overrides: dict[str, deque[FailureSpec]] = {}
        self._bucket_counter = 0
        self._key_counter = 0

    # --- Public API for tests ----------------------------------------

    async def run_garage(
        self,
        garage_config: GarageConfig,
        *args: str,
        timeout: float = 30,
    ) -> tuple[int, str, str]:
        """Drop-in replacement for ``provision_bucket.run_garage``."""
        del garage_config, timeout
        self.calls.append(args)
        verb = self._verb_key(args)
        queue = self._failure_overrides.get(verb)
        if queue:
            spec = queue[0]
            if spec.skip_remaining > 0:
                spec.skip_remaining -= 1
            else:
                queue.popleft()
                return (spec.rc, "", spec.stderr)
        return self._dispatch(args)

    def fail_next(
        self,
        verb: str,
        *,
        rc: int = 1,
        stderr: str = "",
        after: int = 0,
    ) -> None:
        """Queue a failure for a future call matching ``verb``.

        With ``after=0`` (default), the very next matching call fails.
        With ``after=N``, the next ``N`` matching calls dispatch
        normally and the (N+1)-th fails. Useful when an orchestrator
        invokes the same verb several times in a row and the test
        targets a specific occurrence (e.g., the second of three
        ``key create`` calls).

        Verb keys: ``bucket_create``, ``bucket_unalias``,
        ``bucket_unalias_local``, ``bucket_alias``,
        ``bucket_alias_local``, ``bucket_allow``, ``bucket_deny``,
        ``bucket_delete``, ``bucket_info``, ``key_create``,
        ``key_delete``.

        Multiple ``fail_next`` calls for the same verb queue in order.
        """
        self._failure_overrides.setdefault(verb, deque()).append(
            FailureSpec(rc=rc, stderr=stderr, skip_remaining=after),
        )

    def add_bucket(self, alias: str) -> _Bucket:
        """Helper: pre-populate state with a bucket. Returns it.

        Equivalent to ``run_garage(cfg, "bucket", "create", alias)``
        but synchronous and returns the ``_Bucket`` so tests can grab
        ``bucket.bucket_id`` for assertions.
        """
        rc, _stdout, stderr = self._bucket_create(alias)
        if rc != 0:
            raise ValueError(f"add_bucket({alias!r}) failed: {stderr}")
        return self._resolve_bucket_strict(alias)

    def add_key(self, name: str) -> _Key:
        """Helper: pre-populate state with a key. Returns it."""
        rc, _stdout, stderr = self._key_create(name)
        if rc != 0:
            raise ValueError(f"add_key({name!r}) failed: {stderr}")
        return list(self.keys.values())[-1]

    # --- Verb keys for failure injection -----------------------------

    @staticmethod
    def _verb_key(args: tuple[str, ...]) -> str:
        match args:
            case ("bucket", "create", *_):
                return "bucket_create"
            case ("bucket", "unalias", "--local", *_):
                return "bucket_unalias_local"
            case ("bucket", "unalias", *_):
                return "bucket_unalias"
            case ("bucket", "alias", "--local", *_):
                return "bucket_alias_local"
            case ("bucket", "alias", *_):
                return "bucket_alias"
            case ("bucket", "allow", *_):
                return "bucket_allow"
            case ("bucket", "deny", *_):
                return "bucket_deny"
            case ("bucket", "delete", *_):
                return "bucket_delete"
            case ("bucket", "info", *_):
                return "bucket_info"
            case ("key", "create", *_):
                return "key_create"
            case ("key", "info", *_):
                return "key_info"
            case ("key", "delete", *_):
                return "key_delete"
            case _:
                return "_unknown"

    # --- Resolution helpers ------------------------------------------

    def _resolve_bucket(self, ref: str) -> _Bucket | None:
        """Rule 8: accept global alias or 16-char prefix; reject 64-char."""
        if _is_64_char_hex(ref):
            return None
        for bucket in self.buckets.values():
            if ref in bucket.global_aliases:
                return bucket
        if _is_16_char_hex(ref):
            for bucket in self.buckets.values():
                if bucket.bucket_id.startswith(ref):
                    return bucket
        return None

    def _resolve_bucket_strict(self, ref: str) -> _Bucket:
        bucket = self._resolve_bucket(ref)
        if bucket is None:
            raise KeyError(f"FakeGarage: no bucket matches ref {ref!r}")
        return bucket

    def _next_bucket_id(self) -> str:
        self._bucket_counter += 1
        return hashlib.sha256(
            f"fake-bucket-{self._bucket_counter}".encode(),
        ).hexdigest()

    def _next_key_credentials(self) -> tuple[str, str]:
        self._key_counter += 1
        seed = f"fake-key-{self._key_counter}"
        key_id = "GK" + hashlib.sha256(seed.encode()).hexdigest()[:24]
        secret = hashlib.sha256(
            (seed + "-secret").encode(),
        ).hexdigest()
        return (key_id, secret)

    @staticmethod
    def _alias_count(bucket: _Bucket) -> int:
        return len(bucket.global_aliases) + len(bucket.local_aliases)

    # --- Stdout rendering --------------------------------------------

    def _render_bucket_info(self, bucket: _Bucket) -> str:
        global_alias = next(iter(sorted(bucket.global_aliases)), "")
        lines = [
            "==== BUCKET INFORMATION ====",
            f"Bucket:          {bucket.bucket_id}",
            "Created:         2026-05-07 12:00:00.000 +00:00",
            "",
            f"Size:            {bucket.size_bytes} B ({bucket.size_bytes} B)",
            f"Objects:         {bucket.object_count}",
            "",
            "Website access:  false",
            "",
        ]
        if global_alias:
            lines.append(f"Global alias:    {global_alias}")
            lines.append("")
        lines.append("==== KEYS FOR THIS BUCKET ====")
        lines.append("Permissions  Access key    Local aliases")
        for key_id, key in self.keys.items():
            perms = key.permissions.get(bucket.bucket_id, set())
            if not perms:
                # Rule 9: a key with only a local alias and no perms
                # does not appear in the keys table.
                continue
            perm_str = _render_perms(perms)
            local = bucket.local_aliases.get(key_id, "")
            lines.append(f"{perm_str}  {key_id}  {local}")
        return "\n".join(lines) + "\n"

    def _render_key_info(self, key: _Key) -> str:
        """Render ``key info <id>`` stdout. Used by orchestrators that
        need to check whether a key has any remaining buckets before
        deciding to delete it.
        """
        lines = [
            "==== KEY INFORMATION ====",
            f"Key name:     {key.name}",
            f"Key ID:       {key.key_id}",
            "Secret key:   *redacted-by-fake*",
            "",
            "==== BUCKETS FOR THIS KEY ====",
            "Permissions  Bucket ID         Global aliases  Local aliases",
        ]
        for bucket in self.buckets.values():
            perms = key.permissions.get(bucket.bucket_id, set())
            local_alias = bucket.local_aliases.get(key.key_id, "")
            if not perms and not local_alias:
                continue
            global_alias = next(iter(sorted(bucket.global_aliases)), "")
            perm_str = _render_perms(perms) if perms else "---"
            line = f"{perm_str}  {bucket.bucket_id[:16]}"
            if global_alias:
                line += f"  {global_alias}"
            if local_alias:
                line += f"  {local_alias}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_key_create(key_id: str, name: str, secret: str) -> str:
        return f"Key name: {name}\nKey ID: {key_id}\nSecret key: {secret}\n"

    # --- Dispatch ----------------------------------------------------

    def _dispatch(self, args: tuple[str, ...]) -> tuple[int, str, str]:
        match args:
            case ("bucket", "create", name):
                return self._bucket_create(name)
            case ("bucket", "info", ref):
                return self._bucket_info(ref)
            case ("bucket", "unalias", "--local", key_id, name):
                return self._bucket_unalias_local(key_id, name)
            case ("bucket", "unalias", "--local", *_):
                return (
                    1,
                    "",
                    f"USAGE: garage bucket unalias --local <key> <name> "
                    f"(got {len(args) - 3} positionals after --local <key>; "
                    f"expected 1). Args: {args}",
                )
            case ("bucket", "unalias", name):
                return self._bucket_unalias_global(name)
            case ("bucket", "alias", "--local", key_id, existing, new):
                return self._bucket_alias_local(key_id, existing, new)
            case ("bucket", "alias", existing, new):
                return self._bucket_alias_global(existing, new)
            case ("bucket", "allow", *rest):
                return self._bucket_allow_or_deny(tuple(rest), deny=False)
            case ("bucket", "deny", *rest):
                return self._bucket_allow_or_deny(tuple(rest), deny=True)
            case ("bucket", "delete", "--yes", ref):
                return self._bucket_delete(ref)
            case ("key", "create", name):
                return self._key_create(name)
            case ("key", "info", key_id):
                return self._key_info(key_id)
            case ("key", "delete", "--yes", key_id):
                return self._key_delete(key_id)
            case _:
                raise NotImplementedError(
                    f"FakeGarage: unhandled call shape {args}. "
                    f"Either add a handler if this is a real garage call, "
                    f"or check whether the orchestrator is calling the "
                    f"wrong verb.",
                )

    # --- Handlers ----------------------------------------------------

    def _bucket_create(self, name: str) -> tuple[int, str, str]:
        if not _S3_BUCKET_NAME_RE.match(name):
            return (
                1,
                "",
                f"InvalidBucketName: '{name}' does not satisfy S3 strict "
                f"naming (3-63 chars, lowercase alphanumeric + hyphens, "
                f"must start and end alphanumeric)",
            )
        for bucket in self.buckets.values():
            if name in bucket.global_aliases:
                return (1, "", f"BucketAlreadyExists: {name}")
        bucket_id = self._next_bucket_id()
        bucket = _Bucket(bucket_id=bucket_id, global_aliases={name})
        self.buckets[bucket_id] = bucket
        return (0, self._render_bucket_info(bucket), "")

    def _bucket_info(self, ref: str) -> tuple[int, str, str]:
        bucket = self._resolve_bucket(ref)
        if bucket is None:
            return (1, "", f"NoSuchBucket: {ref}")
        return (0, self._render_bucket_info(bucket), "")

    def _bucket_unalias_global(self, name: str) -> tuple[int, str, str]:
        for bucket in self.buckets.values():
            if name in bucket.global_aliases:
                if self._alias_count(bucket) <= 1:
                    return (
                        1,
                        "",
                        f"RemoveBucketAlias returned InvalidRequest "
                        f"(400): Bad request: Bucket {name} doesn't have "
                        f"other aliases, please delete it instead of "
                        f"just unaliasing.",
                    )
                bucket.global_aliases.discard(name)
                return (0, "", "")
        return (1, "", f"NoSuchBucketAlias: {name}")

    def _bucket_unalias_local(
        self,
        key_id: str,
        name: str,
    ) -> tuple[int, str, str]:
        if key_id not in self.keys:
            return (1, "", f"NoSuchKey: {key_id}")
        for bucket in self.buckets.values():
            if bucket.local_aliases.get(key_id) == name:
                if self._alias_count(bucket) <= 1:
                    return (
                        1,
                        "",
                        f"RemoveBucketAlias returned InvalidRequest "
                        f"(400): Bad request: Bucket {name} doesn't have "
                        f"other aliases, please delete it instead of "
                        f"just unaliasing.",
                    )
                del bucket.local_aliases[key_id]
                return (0, "", "")
        return (
            1,
            "",
            f"NoSuchBucketAlias: {name} in local namespace of key {key_id}",
        )

    def _bucket_alias_global(
        self,
        existing: str,
        new: str,
    ) -> tuple[int, str, str]:
        bucket = self._resolve_bucket(existing)
        if bucket is None:
            return (1, "", f"NoSuchBucket: {existing}")
        if not _S3_BUCKET_NAME_RE.match(new):
            return (1, "", f"InvalidBucketName: '{new}'")
        for other in self.buckets.values():
            if new in other.global_aliases:
                return (1, "", f"BucketAlreadyExists: {new}")
        bucket.global_aliases.add(new)
        return (0, "", "")

    def _bucket_alias_local(
        self,
        key_id: str,
        existing: str,
        new: str,
    ) -> tuple[int, str, str]:
        if key_id not in self.keys:
            return (1, "", f"NoSuchKey: {key_id}")
        bucket = self._resolve_bucket(existing)
        if bucket is None:
            return (1, "", f"NoSuchBucket: {existing}")
        for other in self.buckets.values():
            if other.local_aliases.get(key_id) == new:
                return (
                    1,
                    "",
                    f"BucketAlreadyExists: {new} in local namespace of key {key_id}",
                )
        bucket.local_aliases[key_id] = new
        return (0, "", "")

    def _bucket_allow_or_deny(
        self,
        rest: tuple[str, ...],
        *,
        deny: bool,
    ) -> tuple[int, str, str]:
        flags: list[str] = []
        ref: str | None = None
        key_id: str | None = None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in _PERM_FLAGS:
                flags.append(tok)
                i += 1
            elif tok == "--key":
                if i + 1 >= len(rest):
                    return (1, "", "USAGE: --key requires a value")
                key_id = rest[i + 1]
                i += 2
            else:
                if ref is not None:
                    return (
                        1,
                        "",
                        f"USAGE: unexpected positional {tok!r}; only one "
                        f"bucket reference allowed",
                    )
                ref = tok
                i += 1
        verb = "deny" if deny else "allow"
        if ref is None or key_id is None:
            return (
                1,
                "",
                f"USAGE: bucket {verb} <flags> <bucket> --key <key>",
            )
        if not flags:
            return (
                1,
                "",
                f"USAGE: bucket {verb} requires at least one of "
                f"--read, --write, --owner",
            )
        bucket = self._resolve_bucket(ref)
        if bucket is None:
            return (1, "", f"NoSuchBucket: {ref}")
        if key_id not in self.keys:
            return (1, "", f"NoSuchKey: {key_id}")
        key = self.keys[key_id]
        perm_set = key.permissions.setdefault(bucket.bucket_id, set())
        for flag in flags:
            perm_name = flag[2:]
            if deny:
                perm_set.discard(perm_name)
            else:
                perm_set.add(perm_name)
        if deny and not perm_set:
            del key.permissions[bucket.bucket_id]
        return (0, "", "")

    def _bucket_delete(self, ref: str) -> tuple[int, str, str]:
        bucket = self._resolve_bucket(ref)
        if bucket is None:
            return (1, "", f"NoSuchBucket: {ref}")
        if bucket.object_count > 0:
            return (
                1,
                "",
                f"BucketNotEmpty: {ref} contains {bucket.object_count} objects",
            )
        # Real Garage v2.2.0 rule: when the bucket is addressed by ID
        # (16-char prefix), ``bucket delete --yes`` rejects if any
        # local aliases remain. When addressed by global alias, the
        # alias being deleted is implicitly removed as part of the
        # bucket teardown - but lingering OTHER locals still trigger
        # the rejection. Empirically observed on garage-one (v2.2.0):
        # ``Bucket X still has other local aliases. Use bucket unalias
        # to delete them one by one.``
        addressed_by_global = ref in bucket.global_aliases
        if bucket.local_aliases:
            # Delete proceeds only if the alias being passed in is the
            # last alias (which it would be only when no locals exist).
            return (
                1,
                "",
                f"Bucket {bucket.bucket_id} still has other local "
                f"aliases. Use `bucket unalias` to delete them one by "
                f"one.",
            )
        # When addressed by global alias and that's the last alias on
        # the bucket, real Garage allows the delete - the implicit
        # alias-removal is part of the teardown. When addressed by ID
        # with zero aliases attached, also allow.
        del addressed_by_global  # documentation-only
        for key in self.keys.values():
            key.permissions.pop(bucket.bucket_id, None)
        del self.buckets[bucket.bucket_id]
        return (0, "", "")

    def _key_info(self, key_id: str) -> tuple[int, str, str]:
        if key_id not in self.keys:
            return (1, "", f"NoSuchKey: {key_id}")
        return (0, self._render_key_info(self.keys[key_id]), "")

    def _key_create(self, name: str) -> tuple[int, str, str]:
        key_id, secret = self._next_key_credentials()
        self.keys[key_id] = _Key(
            key_id=key_id,
            name=name,
            secret_key=secret,
        )
        return (0, self._render_key_create(key_id, name, secret), "")

    def _key_delete(self, key_id: str) -> tuple[int, str, str]:
        if key_id not in self.keys:
            return (1, "", f"NoSuchKey: {key_id}")
        for bucket in self.buckets.values():
            bucket.local_aliases.pop(key_id, None)
        del self.keys[key_id]
        return (0, "", "")


# --- Module-level helpers -------------------------------------------


def _is_64_char_hex(s: str) -> bool:
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def _is_16_char_hex(s: str) -> bool:
    return len(s) == 16 and all(c in "0123456789abcdef" for c in s)


def _render_perms(perms: set[str]) -> str:
    return (
        ("R" if "read" in perms else "-")
        + ("W" if "write" in perms else "-")
        + ("O" if "owner" in perms else "-")
    )
