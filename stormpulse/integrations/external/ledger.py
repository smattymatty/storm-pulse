"""Install receipts and their reader.

A receipt is the durable attestation that a package's exact bytes were installed
and which approved key signed them. It is not an execution grant, a load
authority, or a command authority.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from stormpulse.integrations.external import layout
from stormpulse.integrations.external.model import (
    DIGEST_RE,
    INTEGRATION_ID_RE,
    VERSION_RE,
    CapabilityRequest,
    FailureCode,
    InstallReceiptV1,
    PackageError,
)

_DIR_MODE = 0o700


def receipt_path(state_dir: Path, integration_id: str, package_digest: str) -> Path:
    hex_part = package_digest.split(":", 1)[1]
    return layout.receipts_dir(state_dir) / integration_id / f"{hex_part}.json"


def write_receipt(state_dir: Path, receipt: InstallReceiptV1) -> None:
    path = receipt_path(state_dir, receipt.integration_id, receipt.package_digest)
    path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path.parent, _DIR_MODE)
    layout.atomic_write(path, layout.canonical_json(to_dict(receipt)))


def list_receipts(state_dir: Path) -> list[InstallReceiptV1]:
    """Every readable receipt. A corrupt receipt (F11) is skipped, not fatal, so a
    single bad file cannot brick the listing; doctor reports the corruption."""
    with layout.state_lock(state_dir):  # lock so we never observe a half-commit
        root = layout.receipts_dir(state_dir)
        receipts: list[InstallReceiptV1] = []
        for integration_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for path in sorted(integration_dir.glob("*.json")):
                try:
                    receipts.append(read_receipt(path))
                except PackageError:
                    continue
        return receipts


def read_receipt(path: Path) -> InstallReceiptV1:
    try:
        data = layout.read_json(path)
    except (ValueError, OSError):
        raise PackageError(FailureCode.F11, f"receipt {path.name} is unreadable") from None
    receipt = from_dict(data, path.name)
    # The location must agree with the content: a receipt sitting in
    # receipts/<id>/<hex>.json cannot claim a different id or digest.
    if path.stem != receipt.package_digest.split(":", 1)[1]:
        raise PackageError(FailureCode.F11, f"receipt {path.name} filename disagrees with its digest")
    if path.parent.name != receipt.integration_id:
        raise PackageError(FailureCode.F11, f"receipt {path.name} directory disagrees with its integration id")
    return receipt


def to_dict(receipt: InstallReceiptV1) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": receipt.schema_version,
        "agent_id": receipt.agent_id,
        "integration_id": receipt.integration_id,
        "version": receipt.version,
        "sdk_api": receipt.sdk_api,
        "package_digest": receipt.package_digest,
        "manifest_digest": receipt.manifest_digest,
        "publisher_fingerprint": receipt.publisher_fingerprint,
        "requested_capabilities": [capability.value for capability in receipt.requested_capabilities],
        "installed_relpath": receipt.installed_relpath,
        "installed_at": receipt.installed_at,
    }
    if receipt.command_specs_digest is not None:
        result["command_specs_digest"] = receipt.command_specs_digest
    if receipt.service_manifest_digest is not None:
        result["service_manifest_digest"] = receipt.service_manifest_digest
    return result


def from_dict(data: Any, name: str) -> InstallReceiptV1:
    if not isinstance(data, dict):
        raise PackageError(FailureCode.F11, f"receipt {name} is not an object")
    if data.get("schema_version", 1) != 1:
        raise PackageError(FailureCode.F11, f"receipt {name} has an unsupported schema")

    integration_id = _req_str(data, "integration_id", name)
    if not INTEGRATION_ID_RE.match(integration_id):
        raise PackageError(FailureCode.F11, f"receipt {name} has a malformed integration id")
    version = _req_str(data, "version", name)
    if not VERSION_RE.match(version):
        raise PackageError(FailureCode.F11, f"receipt {name} has a malformed version")
    sdk_api = _req_int(data, "sdk_api", name)
    if sdk_api != 1:
        raise PackageError(FailureCode.F11, f"receipt {name} has an unsupported sdk_api")

    package_digest = _req_digest(data, "package_digest", name)
    manifest_digest = _req_digest(data, "manifest_digest", name)
    publisher_fingerprint = _req_digest(data, "publisher_fingerprint", name)

    # Do not trust installed_relpath: derive it and reject a mismatch, so a
    # tampered receipt cannot walk a consumer out of the state tree.
    expected_relpath = f"packages/sha256/{package_digest.split(':', 1)[1]}"
    if _req_str(data, "installed_relpath", name) != expected_relpath:
        raise PackageError(FailureCode.F11, f"receipt {name} installed_relpath does not match its digest")

    return InstallReceiptV1(
        agent_id=_req_str(data, "agent_id", name),
        integration_id=integration_id,
        version=version,
        sdk_api=sdk_api,
        package_digest=package_digest,
        manifest_digest=manifest_digest,
        publisher_fingerprint=publisher_fingerprint,
        requested_capabilities=_capabilities(data.get("requested_capabilities", []), name),
        installed_relpath=expected_relpath,
        installed_at=_req_str(data, "installed_at", name),
        command_specs_digest=_opt_digest(data, "command_specs_digest", name),
        service_manifest_digest=_opt_digest(data, "service_manifest_digest", name),
    )


def _capabilities(raw: Any, name: str) -> tuple[CapabilityRequest, ...]:
    if not isinstance(raw, list):
        raise PackageError(FailureCode.F11, f"receipt {name} has malformed capabilities")
    result: list[CapabilityRequest] = []
    for item in raw:
        if not isinstance(item, str):
            raise PackageError(FailureCode.F11, f"receipt {name} has a non-string capability")
        try:
            result.append(CapabilityRequest(item))
        except ValueError:
            raise PackageError(FailureCode.F11, f"receipt {name} has an unknown capability") from None
    return tuple(result)


def _req_str(data: dict[str, Any], key: str, name: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise PackageError(FailureCode.F11, f"receipt {name} field '{key}' is invalid")
    return value


def _req_int(data: dict[str, Any], key: str, name: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PackageError(FailureCode.F11, f"receipt {name} field '{key}' is invalid")
    return value


def _opt_str(data: dict[str, Any], key: str, name: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PackageError(FailureCode.F11, f"receipt {name} field '{key}' is invalid")
    return value


def _req_digest(data: dict[str, Any], key: str, name: str) -> str:
    value = _req_str(data, key, name)
    if not DIGEST_RE.match(value):
        raise PackageError(FailureCode.F11, f"receipt {name} field '{key}' is not a digest")
    return value


def _opt_digest(data: dict[str, Any], key: str, name: str) -> str | None:
    value = _opt_str(data, key, name)
    if value is not None and not DIGEST_RE.match(value):
        raise PackageError(FailureCode.F11, f"receipt {name} field '{key}' is not a digest")
    return value
