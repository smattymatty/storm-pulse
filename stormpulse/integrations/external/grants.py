"""Sealed execution grants and their store (CORE-007 D3).

A grant is the operator's per-agent authority to RUN an installed package's code
in-process. It is a separate act from install (which only attests bytes +
signer): the loader reads grants, never receipts. Sealing binds every digest, so
any change invalidates the grant - authority never carries across versions.

Layout under ``grants/<integration_id>/``:
- ``<package-hex>.json`` - one immutable `SealedGrantV1` per sealed digest (the
  history rollback draws from), carrying the capability-specific revocation
  overlay.
- ``active`` - a pointer to the digest currently loaded for this integration id.
  The loader loads exactly one grant per id (the active one), so two sealed
  versions never collide.

No package code is imported or executed here (Fn7): this module reads receipts,
re-hashes an already-installed immutable tree, checks trust, and writes grant
records. The one module that executes sealed code is the loader.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from stormpulse.integrations.external import digest, layout, ledger, trust
from stormpulse.integrations.external.model import (
    DIGEST_RE,
    INTEGRATION_ID_RE,
    CapabilityRequest,
    FailureCode,
    InstallReceiptV1,
    PackageError,
    SealedGrantV1,
)

_DIR_MODE = 0o700


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def grant_path(state_dir: Path, integration_id: str, package_digest: str) -> Path:
    hex_part = package_digest.split(":", 1)[1]
    return layout.grants_dir(state_dir) / integration_id / f"{hex_part}.json"


def _active_path(state_dir: Path, integration_id: str) -> Path:
    return layout.grants_dir(state_dir) / integration_id / "active"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def to_dict(grant: SealedGrantV1) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": grant.schema_version,
        "seal_format_version": grant.seal_format_version,
        "agent_id": grant.agent_id,
        "integration_id": grant.integration_id,
        "publisher_fingerprint": grant.publisher_fingerprint,
        "package_digest": grant.package_digest,
        "manifest_digest": grant.manifest_digest,
        "granted_capabilities": [c.value for c in grant.granted_capabilities],
        "revoked_capabilities": [c.value for c in grant.revoked_capabilities],
        "sealed_at": grant.sealed_at,
    }
    if grant.command_specs_digest is not None:
        result["command_specs_digest"] = grant.command_specs_digest
    if grant.service_manifest_digest is not None:
        result["service_manifest_digest"] = grant.service_manifest_digest
    if grant.revoked_at is not None:
        result["revoked_at"] = grant.revoked_at
    return result


def from_dict(data: Any, name: str) -> SealedGrantV1:
    if not isinstance(data, dict):
        raise PackageError(FailureCode.F9, f"grant {name} is not an object")
    if data.get("schema_version", 1) != 1 or data.get("seal_format_version", 1) != 1:
        raise PackageError(FailureCode.F9, f"grant {name} has an unsupported schema")

    integration_id = _req_str(data, "integration_id", name)
    if not INTEGRATION_ID_RE.match(integration_id):
        raise PackageError(FailureCode.F9, f"grant {name} has a malformed integration id")

    return SealedGrantV1(
        agent_id=_req_str(data, "agent_id", name),
        integration_id=integration_id,
        publisher_fingerprint=_req_digest(data, "publisher_fingerprint", name),
        package_digest=_req_digest(data, "package_digest", name),
        manifest_digest=_req_digest(data, "manifest_digest", name),
        granted_capabilities=_capabilities(data.get("granted_capabilities", []), name),
        revoked_capabilities=_capabilities(data.get("revoked_capabilities", []), name),
        sealed_at=_req_str(data, "sealed_at", name),
        command_specs_digest=_opt_digest(data, "command_specs_digest", name),
        service_manifest_digest=_opt_digest(data, "service_manifest_digest", name),
        revoked_at=_opt_str(data, "revoked_at", name),
    )


# ---------------------------------------------------------------------------
# Reads (lock-free; callers that mutate hold the state lock around these)
# ---------------------------------------------------------------------------


def read_grant(path: Path) -> SealedGrantV1:
    try:
        data = layout.read_json(path)
    except (ValueError, OSError):
        raise PackageError(FailureCode.F9, f"grant {path.name} is unreadable") from None
    grant = from_dict(data, path.name)
    # Location must agree with content: a grant in grants/<id>/<hex>.json cannot
    # claim a different id or digest.
    if path.stem != grant.package_digest.split(":", 1)[1]:
        raise PackageError(FailureCode.F9, f"grant {path.name} filename disagrees with its digest")
    if path.parent.name != grant.integration_id:
        raise PackageError(FailureCode.F9, f"grant {path.name} directory disagrees with its id")
    return grant


def lookup(state_dir: Path, integration_id: str, package_digest: str) -> SealedGrantV1 | None:
    path = grant_path(state_dir, integration_id, package_digest)
    return read_grant(path) if path.exists() else None


def list_grants(state_dir: Path) -> list[SealedGrantV1]:
    """Every readable grant. A corrupt grant is skipped, not fatal."""
    with layout.state_lock(state_dir):
        return _list_grants_unlocked(state_dir)


def active_integration_ids(state_dir: Path) -> list[str]:
    """Integration ids that have an active grant (a loadable adapter). Lock-free."""
    root = layout.grants_dir(state_dir)
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "active").exists())


def active_grant(state_dir: Path, integration_id: str) -> SealedGrantV1 | None:
    """The grant currently loaded for an id (via its active pointer), or None."""
    active_digest = _read_active(state_dir, integration_id)
    if active_digest is None:
        return None
    path = grant_path(state_dir, integration_id, active_digest)
    return read_grant(path) if path.exists() else None


def effective_capabilities(grant: SealedGrantV1) -> frozenset[CapabilityRequest]:
    """Granted minus revoked. `integration_load` absent here means the whole
    adapter is fenced; `command_contributor` absent means its commands are."""
    return frozenset(grant.granted_capabilities) - frozenset(grant.revoked_capabilities)


# ---------------------------------------------------------------------------
# Mutations (each takes the state lock once; nested locking would deadlock)
# ---------------------------------------------------------------------------


def seal(state_dir: Path, *, package_digest: str) -> SealedGrantV1:
    """Grant execution authority for an installed digest. Re-hashes the installed
    tree (F10 on drift) and re-checks the publisher is still active (F7), then
    writes the grant (all requested capabilities) and points active at it."""
    if not DIGEST_RE.match(package_digest):
        raise PackageError(FailureCode.F4, "package_digest is not a sha256 digest")
    with layout.state_lock(state_dir):
        receipt = _find_receipt_unlocked(state_dir, package_digest)

        installed = layout.packages_dir(state_dir) / package_digest.split(":", 1)[1]
        if not installed.is_dir():
            raise PackageError(FailureCode.F11, "installed package tree is missing")
        if digest.scan_and_hash(installed).package_digest != package_digest:
            raise PackageError(FailureCode.F10, "installed digest path is corrupt")

        record = trust.lookup(state_dir, receipt.publisher_fingerprint)
        if record is None or not trust.is_active(record):
            raise PackageError(FailureCode.F7, "publisher is unknown or revoked; cannot seal")

        grant = SealedGrantV1(
            agent_id=receipt.agent_id,
            integration_id=receipt.integration_id,
            publisher_fingerprint=receipt.publisher_fingerprint,
            package_digest=receipt.package_digest,
            manifest_digest=receipt.manifest_digest,
            granted_capabilities=receipt.requested_capabilities,
            command_specs_digest=receipt.command_specs_digest,
            service_manifest_digest=receipt.service_manifest_digest,
            sealed_at=layout.now_rfc3339(),
        )
        _write_grant(state_dir, grant)
        _write_active(state_dir, grant.integration_id, grant.package_digest)
        return grant


def revoke(state_dir: Path, *, package_digest: str, capability: CapabilityRequest) -> SealedGrantV1:
    """Fence one capability on a grant (D3: revocation fences, never unloads).
    Idempotent for an already-revoked capability."""
    with layout.state_lock(state_dir):
        grant = _find_grant_unlocked(state_dir, package_digest)
        if grant is None:
            raise PackageError(FailureCode.F11, "no grant for that digest; seal it first")
        if capability not in grant.granted_capabilities:
            raise PackageError(FailureCode.F5, f"capability {capability.value} was never granted")
        if capability in grant.revoked_capabilities:
            return grant
        revoked = dataclasses.replace(
            grant,
            revoked_capabilities=(*grant.revoked_capabilities, capability),
            revoked_at=layout.now_rfc3339(),
        )
        _write_grant(state_dir, revoked)
        return revoked


def rollback(state_dir: Path, *, integration_id: str, package_digest: str) -> SealedGrantV1:
    """Re-activate a previously sealed, non-load-revoked digest (D3)."""
    with layout.state_lock(state_dir):
        path = grant_path(state_dir, integration_id, package_digest)
        if not path.exists():
            raise PackageError(FailureCode.F11, "no such sealed grant to roll back to")
        grant = read_grant(path)
        if CapabilityRequest.INTEGRATION_LOAD in grant.revoked_capabilities:
            raise PackageError(FailureCode.F7, "cannot roll back to a load-revoked grant")
        _write_active(state_dir, integration_id, package_digest)
        return grant


# ---------------------------------------------------------------------------
# Internals (lock-free; only called with the state lock already held, or from a
# read that took it)
# ---------------------------------------------------------------------------


def _write_grant(state_dir: Path, grant: SealedGrantV1) -> None:
    path = grant_path(state_dir, grant.integration_id, grant.package_digest)
    path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    layout.atomic_write(path, layout.canonical_json(to_dict(grant)))


def _write_active(state_dir: Path, integration_id: str, package_digest: str) -> None:
    path = _active_path(state_dir, integration_id)
    path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    layout.atomic_write(path, layout.canonical_json({"schema_version": 1, "package_digest": package_digest}))


def _read_active(state_dir: Path, integration_id: str) -> str | None:
    path = _active_path(state_dir, integration_id)
    if not path.exists():
        return None
    try:
        data = layout.read_json(path)
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("package_digest")
    if not isinstance(value, str) or not DIGEST_RE.match(value):
        return None
    return value


def _list_grants_unlocked(state_dir: Path) -> list[SealedGrantV1]:
    root = layout.grants_dir(state_dir)
    if not root.exists():
        return []
    grants: list[SealedGrantV1] = []
    for integration_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in sorted(integration_dir.glob("*.json")):  # skips the extensionless `active` pointer
            try:
                grants.append(read_grant(path))
            except PackageError:
                continue
    return grants


def _find_grant_unlocked(state_dir: Path, package_digest: str) -> SealedGrantV1 | None:
    if not DIGEST_RE.match(package_digest):
        raise PackageError(FailureCode.F4, "package_digest is not a sha256 digest")
    hex_part = package_digest.split(":", 1)[1]
    root = layout.grants_dir(state_dir)
    if not root.exists():
        return None
    for integration_dir in root.iterdir():
        if not integration_dir.is_dir():
            continue
        candidate = integration_dir / f"{hex_part}.json"
        if candidate.exists():
            return read_grant(candidate)
    return None


def _find_receipt_unlocked(state_dir: Path, package_digest: str) -> InstallReceiptV1:
    hex_part = package_digest.split(":", 1)[1]
    root = layout.receipts_dir(state_dir)
    if root.exists():
        for integration_dir in root.iterdir():
            if not integration_dir.is_dir():
                continue
            candidate = integration_dir / f"{hex_part}.json"
            if candidate.exists():
                return ledger.read_receipt(candidate)
    raise PackageError(FailureCode.F11, f"no installed package with digest {package_digest}")


def _capabilities(raw: Any, name: str) -> tuple[CapabilityRequest, ...]:
    if not isinstance(raw, list):
        raise PackageError(FailureCode.F9, f"grant {name} has malformed capabilities")
    result: list[CapabilityRequest] = []
    for item in raw:
        if not isinstance(item, str):
            raise PackageError(FailureCode.F9, f"grant {name} has a non-string capability")
        try:
            result.append(CapabilityRequest(item))
        except ValueError:
            raise PackageError(FailureCode.F9, f"grant {name} has an unknown capability") from None
    return tuple(result)


def _req_str(data: dict[str, Any], key: str, name: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise PackageError(FailureCode.F9, f"grant {name} field '{key}' is invalid")
    return value


def _opt_str(data: dict[str, Any], key: str, name: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PackageError(FailureCode.F9, f"grant {name} field '{key}' is invalid")
    return value


def _req_digest(data: dict[str, Any], key: str, name: str) -> str:
    value = _req_str(data, key, name)
    if not DIGEST_RE.match(value):
        raise PackageError(FailureCode.F9, f"grant {name} field '{key}' is not a digest")
    return value


def _opt_digest(data: dict[str, Any], key: str, name: str) -> str | None:
    value = _opt_str(data, key, name)
    if value is not None and not DIGEST_RE.match(value):
        raise PackageError(FailureCode.F9, f"grant {name} field '{key}' is not a digest")
    return value
