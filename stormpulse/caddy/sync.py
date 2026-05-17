"""Caddy sync handler + boot-time drop-in import verification.

The handler atomically reloads Caddy via the admin API and persists the
fragment to disk. The verifier guards against the silent failure mode
where the main Caddyfile doesn't import the drop-in path — without it,
fragments get written but never served.

Single POST to ``/load`` with ``Content-Type: text/caddyfile``: Caddy
validates and commits atomically. Non-2xx fails the sync; the on-disk
fragment stays unchanged (eventually consistent on next successful
sync). On 2xx, the fragment is written to the drop-in path via an
atomic rename (write-tmp-then-replace).
"""

from __future__ import annotations

import asyncio
import fnmatch
import http.client
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import CaddyConfig

logger = logging.getLogger(__name__)


_LOAD_TIMEOUT_SECONDS = 20


# ---------------------------------------------------------------------------
# Boot-time import verification
# ---------------------------------------------------------------------------


def verify_drop_in_imported(
    main_caddyfile: Path,
    drop_in_path: Path,
) -> str | None:
    """Check that the main Caddyfile imports the drop-in path.

    Returns ``None`` if a matching import directive is found. Returns
    a human-readable error message if not. Called at agent boot — a
    non-None result must hard-fail the agent so the operator fixes
    Caddy before the agent is ever asked to sync.

    Matches both exact paths and globs. Imports are resolved relative
    to the main Caddyfile's directory (per Caddy's documented behavior).
    Glob matching uses ``fnmatch`` against the drop-in filename — we
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
        target = line[len("import "):].strip()
        if not target:
            continue

        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = (base_dir / target_path)

        # Glob pattern in the filename component? Use fnmatch.
        if any(ch in target_path.name for ch in "*?["):
            if (
                target_path.parent.resolve() == drop_in_parent
                and fnmatch.fnmatch(drop_in_abs.name, target_path.name)
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


# ---------------------------------------------------------------------------
# Sync handler
# ---------------------------------------------------------------------------


def make_caddy_sync_handler(
    caddy: CaddyConfig,
    params: dict[str, str],
) -> JobHandler:
    """Build a long-running handler for cellar_custom_domain_caddy_sync.

    Workflow:

    1. POST the fragment body to ``{admin_url}/load`` with
       ``Content-Type: text/caddyfile``. Caddy adapts internally and
       atomically rejects on syntax error. Non-2xx fails the sync;
       the on-disk drop-in stays unchanged (eventually consistent).
    2. On 2xx: atomically write the fragment to the drop-in path
       (write-to-tmp then ``os.replace``). Empty fragment removes the
       file instead.

    A persist-after-reload failure surfaces as a failed sync even
    though the running Caddy is correct — operators need to know the
    on-disk state diverged from the live config (a Caddy restart
    would lose the new fragment).
    """

    async def handler(progress: ProgressCallback) -> JobOutcome:
        region = params.get("region", "")
        fragment = params.get("fragment", "")

        await progress(
            "starting", 0, 3, f"syncing Caddy for region {region}",
        )

        # ----- Step 1: POST to admin /load -----
        await progress(
            "running", 1, 3, "POSTing fragment to admin /load",
        )
        ok, err = await asyncio.to_thread(
            _post_caddy_load, caddy.admin_url, fragment,
        )
        if not ok:
            logger.warning(
                "caddy_sync: /load rejected fragment for region=%s: %s",
                region, err,
            )
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Caddy admin /load rejected fragment: {err}",
                failure_reason="reload_failed",
            )

        # ----- Step 2: atomic write to drop-in path -----
        await progress(
            "running", 2, 3, "persisting drop-in fragment to disk",
        )
        try:
            await asyncio.to_thread(
                _atomic_write_or_remove, caddy.drop_in_path, fragment,
            )
        except OSError as exc:
            logger.error(
                "caddy_sync: reload succeeded but persist failed for "
                "region=%s path=%s: %s",
                region, caddy.drop_in_path, exc,
            )
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=(
                    f"Caddy reload succeeded but failed to persist "
                    f"fragment to {caddy.drop_in_path}: {exc}"
                ),
                failure_reason="persist_failed",
            )

        await progress("finalizing", 3, 3, "sync complete")
        return JobOutcome(
            success=True,
            stdout=f"Synced {len(fragment)} bytes for region {region}",
            extras={
                "region": region,
                "fragment_bytes": len(fragment),
                "removed": not fragment,
            },
        )

    return handler


def _post_caddy_load(admin_url: str, fragment: str) -> tuple[bool, str]:
    """POST a Caddyfile fragment to Caddy's admin /load endpoint.

    Returns ``(success, error_message)``. The error message on failure
    rides through to the operator via the JobOutcome.stderr field;
    it is not customer-facing.
    """
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
        http.client.HTTPSConnection if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        conn = conn_class(
            parsed.hostname, port, timeout=_LOAD_TIMEOUT_SECONDS,
        )
        conn.request("POST", "/load", body=body, headers=headers)
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
    """Write fragment atomically, or remove file if fragment is empty.

    Empty fragment is the "no domains active in this region" case —
    the drop-in file should not linger as a stale empty import target.
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
