"""Caddy sync handler + boot-time drop-in import verification.

Sync writes the fragment atomically (disk-authoritative), then POSTs the
absolutised main Caddyfile to ``/load`` so Caddy re-adapts. Persist failure
leaves Caddy untouched; reload failure leaves disk newer than live,
eventually consistent on the next successful sync.

The verifier catches the silent failure where the main Caddyfile doesn't
import the drop-in path - fragments would land on disk but never serve.
"""

from __future__ import annotations

import asyncio
import fnmatch
import http.client
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from stormpulse.caddy.config import CaddyConfig
from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback

logger = logging.getLogger(__name__)


_LOAD_TIMEOUT_SECONDS = 20

# Managed per-bucket drop-in files are named site-<id>.caddy and globbed
# as a set. The prefix/suffix are the agent's contract: only files matching
# this shape are reconciled, so an operator's hand-written drop-in in the
# same directory is never touched.
_MANAGED_PREFIX = "site-"
_MANAGED_SUFFIX = ".caddy"
_MANAGED_GLOB = f"{_MANAGED_PREFIX}*{_MANAGED_SUFFIX}"

# A tenant id keys a filename, so it must not carry a path separator or a
# dot run. Garage ids are hex hashes; this charset (hex plus the safe id
# punctuation) blocks traversal while staying forgiving of the exact slice
# Storm sends.
_TENANT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A single bucket's fragment: its assigned-subdomain block plus any
# custom-domain blocks. 16KB is far above a real bucket (a few hundred
# bytes per block) and exists to catch a pathological Storm-side render
# before it lands on disk.
_PER_TENANT_MAX_BYTES = 16_384

# Inline delete cadence. A normal state change disables at most one site,
# so at most one managed file (plus, once, the legacy single-file drop-in)
# should disappear per sync. A delete count above this without
# authorize_bulk is the suspicious-mass-delete signal the rail guards.
_INLINE_DELETE_CADENCE = 1


def verify_drop_in_imported(
    main_caddyfile: Path,
    drop_in_path: Path,
) -> str | None:
    """Check that the main Caddyfile imports the drop-in path.

    Returns ``None`` if a matching import directive is found. Returns
    a human-readable error message if not. Called at agent boot - a
    non-None result must hard-fail the agent so the operator fixes
    Caddy before the agent is ever asked to sync.

    Matches both exact paths and globs. Imports are resolved relative
    to the main Caddyfile's directory (per Caddy's documented behavior).
    Glob matching uses ``fnmatch`` against the drop-in filename - we
    deliberately don't require the drop-in file to exist on disk, so
    the boot check works before the first cert lifecycle event fires.
    """
    if not main_caddyfile.is_file():
        return f"Main Caddyfile not found: {main_caddyfile}"

    try:
        content = main_caddyfile.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read main Caddyfile {main_caddyfile}: {exc}"

    base_dir = main_caddyfile.parent
    drop_in_abs = drop_in_path.resolve()
    drop_in_parent = drop_in_abs.parent

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comments (Caddyfile syntax).
        line = line.split("#", 1)[0].strip()
        if not line.startswith("import "):
            continue
        target = line[len("import ") :].strip()
        if not target:
            continue

        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = base_dir / target_path

        # Glob pattern in the filename component? Use fnmatch.
        if any(ch in target_path.name for ch in "*?["):
            if target_path.parent.resolve() == drop_in_parent and fnmatch.fnmatch(
                drop_in_abs.name, target_path.name
            ):
                return None
        else:
            # Exact import path.
            if target_path.resolve() == drop_in_abs:
                return None

    return (
        f"Main Caddyfile {main_caddyfile} does not import drop-in path "
        f"{drop_in_path}. Add an 'import' directive (e.g. "
        f"'import {drop_in_path}' or 'import {drop_in_path.parent}/*.caddy') "
        f"and reload Caddy before starting the agent."
    )


@dataclass(frozen=True)
class ReconcilePlan:
    """The disk mutations a manifest implies, with the delete direction
    already rail-checked.

    ``writes`` maps managed filename -> fragment and is always applied: an
    add or update that briefly proxies to nothing self-heals on the next
    sync, never an outage. ``deletes`` are the managed filenames to remove;
    it is empty when the rail tripped. ``skipped_deletes`` records what the
    rail refused so the failure can name them. ``rail_tripped`` is true when
    a delete-beyond-cadence was refused for lack of ``authorize_bulk``.
    """

    writes: dict[str, str] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    skipped_deletes: list[str] = field(default_factory=list)
    rail_tripped: bool = False


def _decode_manifest(raw: str) -> tuple[dict[str, str] | None, str | None]:
    """Parse and validate the tenants manifest JSON.

    Returns ``(manifest, None)`` on success or ``(None, error)`` on any
    problem: malformed JSON, a non-object, a non-string key or value, a key
    that could escape the drop-in directory (a filename is built from it),
    or a fragment over the per-bucket cap. A bad manifest is a Storm-side
    render bug, so the whole sync is rejected rather than a partial set
    written.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"tenants manifest is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, (
            f"tenants manifest must be a JSON object, got {type(parsed).__name__}"
        )
    for key, frag in parsed.items():
        if not isinstance(key, str) or not isinstance(frag, str):
            return None, "tenants manifest keys and values must be strings"
        if not _TENANT_KEY_RE.fullmatch(key):
            return None, (
                f"tenant key {key!r} is not a safe filename component "
                "(allowed: letters, digits, '_', '-'; 1-64 chars)"
            )
        frag_bytes = len(frag.encode("utf-8"))
        if frag_bytes > _PER_TENANT_MAX_BYTES:
            return None, (
                f"tenant {key!r} fragment is {frag_bytes} bytes, exceeds "
                f"per-bucket cap {_PER_TENANT_MAX_BYTES}"
            )
    return parsed, None


def _plan_reconcile(
    *,
    tenants: dict[str, str],
    on_disk: set[str],
    legacy_name: str | None,
    legacy_exists: bool,
    authorize_bulk: bool,
    cadence: int = _INLINE_DELETE_CADENCE,
) -> ReconcilePlan:
    """Turn a manifest plus the on-disk managed file set into the writes and
    deletes to apply, guarding the delete direction. Pure: no I/O.

    The reference is the agent's OWN on-disk set, never a count Storm sends.
    Storm's query cannot audit Storm's query, so only the agent, blind to
    what the query returned, is an independent witness to an under-return.

    Writes always flow. Deletes are the managed files no longer named in the
    manifest, plus, on first cutover, the legacy single-file drop-in: it
    does not match the managed glob, so it would otherwise linger and
    collide on /adapt with the new per-bucket files declaring the same
    sites. If the delete count exceeds ``cadence`` and ``authorize_bulk`` is
    false, ALL deletes are skipped, not an arbitrary subset: picking which
    of a suspicious batch to delete would still risk darkening a live site,
    which is the exact failure the rail exists to prevent. The old files
    keep serving and the plan is marked tripped.
    """
    writes = {
        f"{_MANAGED_PREFIX}{tid}{_MANAGED_SUFFIX}": frag
        for tid, frag in tenants.items()
    }
    desired = set(writes)
    delete_set = on_disk - desired
    if legacy_name and legacy_exists and legacy_name not in desired:
        delete_set.add(legacy_name)
    delete_names = sorted(delete_set)

    if len(delete_names) > cadence and not authorize_bulk:
        return ReconcilePlan(
            writes=writes,
            deletes=[],
            skipped_deletes=delete_names,
            rail_tripped=True,
        )
    return ReconcilePlan(writes=writes, deletes=delete_names)


# One reconcile per region at a time, per agent process. Each sync is a
# full read-modify-write of the region's drop-in set (scan -> plan ->
# apply -> reload), so two running concurrently race on the shared file
# set: the 2026-07-04 persist_failed in the events plane's first live
# minutes was two same-second syncs sharing site-<id>.caddy.tmp, the
# loser's os.replace hitting Errno 2. Bursts of same-region dispatches
# are legitimate website behavior (per-bucket closure sweeps), so the
# serialization lives here, at the invariant's home.
_REGION_LOCKS: dict[str, asyncio.Lock] = {}


def _region_lock(region: str) -> asyncio.Lock:
    lock = _REGION_LOCKS.get(region)
    if lock is None:
        lock = _REGION_LOCKS[region] = asyncio.Lock()
    return lock


class _SyncFailure(Exception):
    """Raised by a sync step to abort the workflow with a failed JobOutcome."""

    def __init__(self, outcome: JobOutcome) -> None:
        super().__init__(outcome.stderr)
        self.outcome = outcome


def make_caddy_sync_handler(
    caddy: CaddyConfig,
    params: dict[str, str],
) -> JobHandler:
    """Build a long-running handler for buckets_custom_domain_caddy_sync.

    Workflow, one ``_*`` step function per numbered phase below; a step
    aborts the sync by raising ``_SyncFailure`` with its failed outcome:

    1. Decode + validate the tenants manifest (a Storm-side render guard:
       bad JSON, an unsafe key, or an oversized fragment rejects the sync
       before anything touches disk).
    2. Plan the reconcile against the agent's own on-disk ``site-*.caddy``
       set, rail-guarding the delete direction.
    3. Apply all disk mutations (writes then deletes, write-to-tmp +
       ``os.replace``) BEFORE the preflight, so the config Caddy adapts is
       the final composed state, never a transient superset. Disk is now
       authoritative.
    4. POST the main Caddyfile (relative ``import`` paths absolutised) to
       ``{admin_url}/adapt`` as a dry run, then ``/load`` to apply. Caddy
       re-adapts the composed config from source, picking up the per-bucket
       files alongside everything else the operator declared.

    A reload-after-persist failure surfaces as a failed sync with the disk
    in the new state and the running Caddy in the old state. The next
    successful sync (or operator-initiated reload) restores consistency.
    """

    async def handler(progress: ProgressCallback) -> JobOutcome:
        async with _region_lock(params.get("region", "")):
            return await _sync_once(progress)

    async def _sync_once(progress: ProgressCallback) -> JobOutcome:
        region = params.get("region", "")
        tenants_raw = params.get("tenants", "{}")
        authorize_bulk = params.get("authorize_bulk", "false") == "true"

        try:
            await progress(
                "starting",
                0,
                4,
                f"syncing Caddy for region {region}",
            )
            tenants = _validate_manifest(region, tenants_raw)

            await progress(
                "running",
                1,
                4,
                "reconciling drop-in file set",
            )
            plan = _scan_and_plan(caddy, region, tenants, authorize_bulk)

            await progress(
                "running",
                2,
                4,
                "persisting drop-in files to disk",
            )
            await _persist_plan(caddy, region, plan)

            await progress(
                "running",
                3,
                5,
                "preflighting composed config via admin /adapt",
            )
            load_body = await _preflight_composed(caddy, region)

            await progress(
                "running",
                4,
                5,
                "reloading Caddy via admin /load",
            )
            await _reload_caddy(caddy, region, load_body)
        except _SyncFailure as failed:
            return failed.outcome

        await progress(
            "finalizing",
            5,
            5,
            "sync applied, delete rail tripped" if plan.rail_tripped else "sync complete",
        )
        return _terminal_outcome(region, plan)

    return handler


def _validate_manifest(region: str, tenants_raw: str) -> dict[str, str]:
    """Step 1: decode + validate the tenants manifest before anything touches disk."""
    tenants, manifest_err = _decode_manifest(tenants_raw)
    if tenants is None:
        logger.warning(
            "caddy_sync: rejected manifest for region=%s: %s",
            region,
            manifest_err,
        )
        raise _SyncFailure(
            JobOutcome(
                success=False,
                exit_code=-1,
                stderr=(
                    f"Rejected Caddy sync for region {region}: {manifest_err}. "
                    "The running Caddy is untouched."
                ),
                failure_reason="config_invalid",
            )
        )
    return tenants


def _scan_and_plan(
    caddy: CaddyConfig,
    region: str,
    tenants: dict[str, str],
    authorize_bulk: bool,
) -> ReconcilePlan:
    """Step 2: plan the reconcile against the agent's own on-disk managed set."""
    drop_in_dir = caddy.drop_in_path.parent
    legacy_name = caddy.drop_in_path.name
    try:
        on_disk = {p.name for p in drop_in_dir.glob(_MANAGED_GLOB)}
        legacy_exists = caddy.drop_in_path.exists()
    except OSError as exc:
        logger.error(
            "caddy_sync: could not scan drop-in dir %s for region=%s: %s",
            drop_in_dir,
            region,
            exc,
        )
        raise _SyncFailure(
            JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Failed to scan drop-in directory {drop_in_dir}: {exc}",
                failure_reason="persist_failed",
            )
        ) from exc

    return _plan_reconcile(
        tenants=tenants,
        on_disk=on_disk,
        legacy_name=legacy_name,
        legacy_exists=legacy_exists,
        authorize_bulk=authorize_bulk,
    )


async def _persist_plan(
    caddy: CaddyConfig, region: str, plan: ReconcilePlan
) -> None:
    """Step 3: apply disk mutations, writes then deletes, atomically per file.

    All mutations happen before the /adapt preflight so the composed
    state Caddy adapts is the final set, never a transient superset.
    A leftover legacy file colliding with a new per-bucket file
    declaring the same site would otherwise fail /adapt on a duplicate
    site address.
    """
    drop_in_dir = caddy.drop_in_path.parent
    try:
        for name, frag in plan.writes.items():
            await asyncio.to_thread(
                _atomic_write_or_remove,
                drop_in_dir / name,
                frag,
            )
        for name in plan.deletes:
            await asyncio.to_thread(
                _atomic_write_or_remove,
                drop_in_dir / name,
                "",
            )
    except OSError as exc:
        logger.error(
            "caddy_sync: persist failed for region=%s dir=%s: %s",
            region,
            drop_in_dir,
            exc,
        )
        raise _SyncFailure(
            JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Failed to persist drop-in files to {drop_in_dir}: {exc}",
                failure_reason="persist_failed",
            )
        ) from exc


async def _preflight_composed(caddy: CaddyConfig, region: str) -> str:
    """Step 4: preflight the composed config via admin /adapt; returns the load body.

    /adapt runs the Caddyfile adapter WITHOUT loading, so a
    broken composed config (missing import target, two files
    declaring the same site) surfaces as a named failure here
    while the running Caddy keeps serving untouched. Both bugs
    of the 2026-06-11 incident were adapter errors that a /load
    400 reported into a log nobody read; this step is what turns
    that into a self-diagnosing command result.
    """
    try:
        load_body = await asyncio.to_thread(
            _read_and_absolutize_imports,
            caddy.main_caddyfile,
        )
    except OSError as exc:
        logger.error(
            "caddy_sync: drop-in persisted but could not read main "
            "Caddyfile %s for reload: %s",
            caddy.main_caddyfile,
            exc,
        )
        raise _SyncFailure(
            JobOutcome(
                success=False,
                exit_code=-1,
                stderr=(f"Drop-in persisted but main Caddyfile read failed: {exc}"),
                failure_reason="reload_failed",
            )
        ) from exc
    ok, err = await asyncio.to_thread(
        _post_caddy_adapt,
        caddy.admin_url,
        load_body,
    )
    if not ok:
        logger.warning(
            "caddy_sync: composed config failed /adapt preflight "
            "for region=%s: %s",
            region,
            err,
        )
        raise _SyncFailure(
            JobOutcome(
                success=False,
                exit_code=-1,
                stderr=(
                    f"Composed Caddy config failed preflight (/adapt): {err}. "
                    "The running Caddy is untouched and still serves the old "
                    "config. Common causes: an import target missing on disk, "
                    "or two drop-ins declaring the same site address - check "
                    f"the files next to {caddy.drop_in_path}."
                ),
                failure_reason="config_invalid",
            )
        )
    return load_body


async def _reload_caddy(caddy: CaddyConfig, region: str, load_body: str) -> None:
    """Step 5: reload Caddy via admin /load.

    POST the main Caddyfile so Caddy re-adapts the composed
    config from disk. Posting just a fragment would replace
    the entire running config (Caddy /load is a full-config
    endpoint), wiping every other site the main Caddyfile
    declares until the next operator-initiated restart.
    """
    ok, err = await asyncio.to_thread(
        _post_caddy_load,
        caddy.admin_url,
        load_body,
    )
    if not ok:
        logger.warning(
            "caddy_sync: drop-ins persisted but Caddy reload "
            "failed for region=%s: %s",
            region,
            err,
        )
        raise _SyncFailure(
            JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Caddy admin /load rejected reload: {err}",
                failure_reason="reload_failed",
            )
        )


def _terminal_outcome(region: str, plan: ReconcilePlan) -> JobOutcome:
    """Build the terminal outcome once the writes are live.

    If the rail tripped, the writes still applied (adds/updates are safe)
    but the suspicious deletes were refused and those files keep serving;
    return a named failure so the operator sees it, rather than a silent
    partial apply.
    """
    extras = {
        "region": region,
        "tenants": len(plan.writes),
        "deleted": len(plan.deletes),
        "rail_tripped": plan.rail_tripped,
    }
    if plan.rail_tripped:
        skipped = ", ".join(plan.skipped_deletes)
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=(
                f"Delete rail tripped for region {region}: the manifest "
                f"would remove {len(plan.skipped_deletes)} drop-in files "
                f"({skipped}), above the inline cadence of "
                f"{_INLINE_DELETE_CADENCE}. Writes were applied and those "
                "files keep serving; no delete was performed. If this is a "
                "deliberate bulk op (e.g. region decommission), re-dispatch "
                "with authorize_bulk set. Otherwise Storm's manifest is "
                "under-returning and should be investigated before the "
                "files are removed."
            ),
            failure_reason="delete_rail_tripped",
            extras=extras,
        )

    return JobOutcome(
        success=True,
        stdout=(
            f"Synced {len(plan.writes)} drop-in file(s), removed "
            f"{len(plan.deletes)} for region {region}"
        ),
        extras=extras,
    )


def _post_caddy_adapt(admin_url: str, fragment: str) -> tuple[bool, str]:
    """POST the composed Caddyfile to admin /adapt as a dry run.

    /adapt runs the caddyfile adapter and returns the JSON config
    without applying it, so adapter-level errors (missing import
    targets, ambiguous site definitions) are caught while the running
    Caddy stays untouched. Same transport contract as
    ``_post_caddy_load``.
    """
    return _post_caddyfile(admin_url, "/adapt", fragment)


def _post_caddy_load(admin_url: str, fragment: str) -> tuple[bool, str]:
    """POST a Caddyfile fragment to Caddy's admin /load endpoint.

    Returns ``(success, error_message)``. The error message on failure
    rides through to the operator via the JobOutcome.stderr field;
    it is not customer-facing.
    """
    return _post_caddyfile(admin_url, "/load", fragment)


def _post_caddyfile(
    admin_url: str, endpoint: str, fragment: str,
) -> tuple[bool, str]:
    """Shared transport: POST text/caddyfile to a Caddy admin endpoint."""
    parsed = urlparse(admin_url)
    if parsed.scheme not in ("http", "https"):
        return False, f"Invalid admin URL scheme: {parsed.scheme!r}"
    if not parsed.hostname:
        return False, f"Admin URL missing hostname: {admin_url!r}"

    body = fragment.encode("utf-8")
    headers = {
        "Content-Type": "text/caddyfile",
        "Content-Length": str(len(body)),
    }

    conn_class = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        conn = conn_class(
            parsed.hostname,
            port,
            timeout=_LOAD_TIMEOUT_SECONDS,
        )
        conn.request("POST", endpoint, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        return False, f"Could not reach Caddy admin at {admin_url}: {exc}"

    if 200 <= status < 300:
        return True, ""
    return False, f"HTTP {status}: {resp_body.strip()[:500]}"


def _atomic_write_or_remove(drop_in_path: Path, fragment: str) -> None:
    """Write fragment atomically, or remove the file if fragment is empty.

    The reconcile uses both directions: a non-empty fragment writes (or
    updates) a per-bucket file; an empty fragment is how a delete is
    expressed, so a removed bucket's file does not linger as a stale
    import target.
    """
    if not fragment:
        try:
            drop_in_path.unlink()
        except FileNotFoundError:
            pass
        return

    tmp_path = drop_in_path.with_suffix(drop_in_path.suffix + ".tmp")
    tmp_path.write_text(fragment, encoding="utf-8")
    os.replace(tmp_path, drop_in_path)


def _snippet_names(content: str) -> set[str]:
    """Collect Caddyfile snippet names: top-level ``(name) { ... }`` blocks.

    ``import`` resolves a snippet name before it is ever treated as a
    file path, so any import target matching one of these names must be
    left untouched by absolutisation.
    """
    names: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("(") and ")" in line:
            name = line[1 : line.index(")")].strip()
            if name:
                names.add(name)
    return names


def _read_and_absolutize_imports(main_caddyfile: Path) -> str:
    """Read the main Caddyfile and absolutise any relative ``import`` paths.

    Caddy's /load endpoint resolves relative imports against Caddy's
    current working directory, not the source file's location. When
    we POST a Caddyfile that was authored to be read from disk
    (where imports resolve relative to the file), relative targets
    may resolve to the wrong place. Absolutising them in place
    sidesteps this - the running Caddy sees the same import targets
    it would resolve on a disk-based load.

    Snippet imports are exempt: ``import security-headers`` referencing
    a ``(security-headers)`` block is a name, not a path. Rewriting it
    to ``/etc/caddy/security-headers`` makes Caddy reject the whole
    /load with "File to import not found" - which silently broke every
    sync against a hardened Caddyfile until 2026-06-11. Targets are
    checked against the file's own snippet definitions, mirroring
    Caddy's snippet-before-file resolution order.
    """
    content = main_caddyfile.read_text(encoding="utf-8")
    base_dir = main_caddyfile.parent
    snippets = _snippet_names(content)
    out: list[str] = []
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append(line)
            continue
        code_part = stripped.split("#", 1)[0].strip()
        if not code_part.startswith("import "):
            out.append(line)
            continue
        target = code_part[len("import ") :].strip()
        if (
            not target
            or target in snippets
            or Path(target).is_absolute()
        ):
            out.append(line)
            continue
        abs_target = (base_dir / target).as_posix()
        out.append(line.replace(target, abs_target, 1))
    return "".join(out)
