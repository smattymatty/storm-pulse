"""Reconcile Caddy drop-ins and verify their main Caddyfile import at boot.

Persist files before posting the composed config to /adapt, then /load.
Persistence failure leaves live Caddy untouched. Reload failure leaves disk
newer than live until a successful sync or operator reload."""

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

# Reconcile only site-<id>.caddy files; leave other operator drop-ins alone.
_MANAGED_PREFIX = "site-"
_MANAGED_SUFFIX = ".caddy"
_MANAGED_GLOB = f"{_MANAGED_PREFIX}*{_MANAGED_SUFFIX}"

# Tenant IDs become filenames; restrict characters and length to block traversal.
_TENANT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Cap each bucket fragment at 16 KiB to reject pathological renders.
_PER_TENANT_MAX_BYTES = 16_384

# More than one deletion per sync requires authorize_bulk.
# Count the legacy drop-in removal too.
_INLINE_DELETE_CADENCE = 1


def verify_drop_in_imported(
    main_caddyfile: Path,
    drop_in_path: Path,
) -> str | None:
    """Return None for a matching drop-in import, otherwise a boot-blocking error.

    Resolve relative imports against the main Caddyfile's directory.
    Accept exact paths or filename globs without requiring the drop-in to exist."""
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
    """Planned writes and deletes after enforcing the bulk-delete guard.

    Writes always apply. If unauthorized deletes exceed cadence, deletes is empty,
    skipped_deletes lists the refused files, and rail_tripped is True."""

    writes: dict[str, str] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    skipped_deletes: list[str] = field(default_factory=list)
    rail_tripped: bool = False


def _decode_manifest(raw: str) -> tuple[dict[str, str] | None, str | None]:
    """Validate tenant JSON; return (manifest, None) or (None, error).

    Reject the whole manifest for invalid JSON, non-string entries, unsafe filename
    keys, or oversized fragments. Never persist a partially valid manifest."""
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
    """Plan writes and guarded deletes from the manifest and disk; no I/O.

    Use the agent's on-disk set as an independent check on Storm's manifest.
    Always plan writes; delete obsolete managed files and the legacy drop-in.
    If deletes exceed cadence without authorize_bulk, skip all deletes and mark
    the guard tripped so existing sites keep serving."""
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


# Serialize scan, plan, persist, and reload per directory within this process.
# Region names cannot isolate shared files; concurrent syncs race on temp paths.
_DIR_LOCKS: dict[str, asyncio.Lock] = {}


def _dir_lock(drop_in_dir: str) -> asyncio.Lock:
    lock = _DIR_LOCKS.get(drop_in_dir)
    if lock is None:
        lock = _DIR_LOCKS[drop_in_dir] = asyncio.Lock()
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
    """Build a sync handler serialized per drop-in directory.

    Validate the manifest, plan guarded reconciliation, then persist writes and
    deletes before /adapt preflight and /load. Send the main Caddyfile with
    absolute imports so Caddy loads the complete configuration.
    Steps raise _SyncFailure with a failed JobOutcome. Reload failure leaves disk
    newer than live until a successful sync or operator reload."""

    async def handler(progress: ProgressCallback) -> JobOutcome:
        async with _dir_lock(str(caddy.drop_in_path.parent)):
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
            "sync applied, delete rail tripped"
            if plan.rail_tripped
            else "sync complete",
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


async def _persist_plan(caddy: CaddyConfig, region: str, plan: ReconcilePlan) -> None:
    """Apply writes then deletes, atomically per file, before /adapt.

    Preflight must see the final file set; leftover legacy files can duplicate
    sites declared by new per-bucket files."""
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
    """Dry-run the composed Caddyfile through /adapt; return the load body.

    Report missing imports or duplicate sites without changing live Caddy."""
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
            "caddy_sync: composed config failed /adapt preflight for region=%s: %s",
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
    """Reload the complete main Caddyfile through /load.

    /load replaces the entire live config; sending only a fragment loses other sites."""
    ok, err = await asyncio.to_thread(
        _post_caddy_load,
        caddy.admin_url,
        load_body,
    )
    if not ok:
        logger.warning(
            "caddy_sync: drop-ins persisted but Caddy reload failed for region=%s: %s",
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
    """Return the sync result after writes are live.

    Report a named failure if the delete guard refused removals, even though
    writes succeeded; the skipped files remain on disk."""
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
    """POST the composed Caddyfile to /adapt without changing live config.

    Return (success, error_message), using the same transport as /load."""
    return _post_caddyfile(admin_url, "/adapt", fragment)


def _post_caddy_load(admin_url: str, fragment: str) -> tuple[bool, str]:
    """POST Caddyfile text to /load; return (success, error_message).

    Failures reach the operator through JobOutcome.stderr."""
    return _post_caddyfile(admin_url, "/load", fragment)


def _post_caddyfile(
    admin_url: str,
    endpoint: str,
    fragment: str,
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
    """Atomically write a nonempty fragment; delete the file for an empty one."""
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
    """Collect (name) snippet definitions that must remain named imports.

    Caddy resolves snippets before treating import targets as file paths."""
    names: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("(") and ")" in line:
            name = line[1 : line.index(")")].strip()
            if name:
                names.add(name)
    return names


def _read_and_absolutize_imports(main_caddyfile: Path) -> str:
    """Resolve relative import paths against the main Caddyfile's directory.

    Posted Caddyfiles otherwise resolve paths against Caddy's working directory.
    Preserve snippet names: Caddy resolves them before file imports."""
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
        if not target or target in snippets or Path(target).is_absolute():
            out.append(line)
            continue
        abs_target = (base_dir / target).as_posix()
        out.append(line.replace(target, abs_target, 1))
    return "".join(out)
