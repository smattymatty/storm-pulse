"""Strict parsing of the package manifest and its detached signature.

Data-only: this parses declarative bytes and never imports or resolves the entry
point. Every structural surprise fails closed (an unknown field, a wrong type, an
out-of-range value) rather than being tolerated, so a typo can never silently
widen what a package is understood to declare or request.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import tomllib
from typing import Any

from stormpulse.integrations.external.model import (
    CapabilityRequest,
    DetachedSignatureV1,
    FailureCode,
    ManifestV1,
    PackageError,
)

# Reserving stdlib top-levels (and our own root) stops a package id like ``json``
# or ``os`` from later resolving an entry module that shadows the standard library.
_RESERVED_INTEGRATION_IDS = frozenset(sys.stdlib_module_names) | {"stormpulse"}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_SCHEMA_VERSION = 1
_SUPPORTED_SDK_API = 1
_MAX_ENTRY_MODULE_BYTES = 200
_MAX_ENTRY_OBJECT_BYTES = 64
_ED25519_SIGNATURE_BYTES = 64

_MANIFEST_ROOT_KEYS = {"schema_version", "integration", "publisher", "requests"}
_INTEGRATION_KEYS = {"id", "version", "sdk_api", "entry_module", "entry_object"}
_PUBLISHER_KEYS = {"fingerprint"}
_REQUESTS_KEYS = {"capabilities", "command_specs_digest", "service_manifest_digest"}
_SIGNATURE_KEYS = {
    "schema_version",
    "algorithm",
    "publisher_fingerprint",
    "package_digest",
    "signature_b64",
}


def parse_manifest(raw: bytes) -> ManifestV1:
    """Parse and fully validate a manifest, or raise ``PackageError`` (F4)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError(FailureCode.F4, "manifest is not valid utf-8") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PackageError(FailureCode.F4, f"manifest is not valid TOML: {exc}") from None

    _reject_unknown(data, _MANIFEST_ROOT_KEYS, "manifest")
    _require_schema_version(data, "manifest")

    integration = _table(data, "integration", "manifest")
    _reject_unknown(integration, _INTEGRATION_KEYS, "[integration]")
    integration_id = _string(integration, "id", "[integration]")
    if not _ID_RE.match(integration_id):
        raise PackageError(FailureCode.F4, "integration id is malformed")
    if integration_id in _RESERVED_INTEGRATION_IDS:
        raise PackageError(FailureCode.F4, f"integration id '{integration_id}' is reserved")
    version = _string(integration, "version", "[integration]")
    if not _VERSION_RE.match(version):
        raise PackageError(FailureCode.F4, "version is malformed")
    sdk_api = _int_default(integration, "sdk_api", _SUPPORTED_SDK_API, "[integration]")
    if sdk_api != _SUPPORTED_SDK_API:
        raise PackageError(FailureCode.F4, "unsupported sdk_api (this loader requires 1)")
    entry_module = _string(integration, "entry_module", "[integration]")
    _validate_entry_module(entry_module, integration_id)
    entry_object = _string_default(integration, "entry_object", "INTEGRATION", "[integration]")
    _validate_entry_object(entry_object)

    publisher = _table(data, "publisher", "manifest")
    _reject_unknown(publisher, _PUBLISHER_KEYS, "[publisher]")
    fingerprint = _string(publisher, "fingerprint", "[publisher]")
    _validate_digest(fingerprint, "publisher fingerprint")

    capabilities: tuple[CapabilityRequest, ...] = ()
    command_specs_digest: str | None = None
    service_manifest_digest: str | None = None
    if "requests" in data:
        requests = _table(data, "requests", "manifest")
        _reject_unknown(requests, _REQUESTS_KEYS, "[requests]")
        if "capabilities" in requests:
            capabilities = _parse_capabilities(requests["capabilities"])
        if "command_specs_digest" in requests:
            command_specs_digest = _string(requests, "command_specs_digest", "[requests]")
            _validate_digest(command_specs_digest, "command_specs_digest")
        if "service_manifest_digest" in requests:
            service_manifest_digest = _string(requests, "service_manifest_digest", "[requests]")
            _validate_digest(service_manifest_digest, "service_manifest_digest")

    wants_commands = CapabilityRequest.COMMAND_CONTRIBUTOR in capabilities
    if wants_commands and command_specs_digest is None:
        raise PackageError(
            FailureCode.F4, "command_specs_digest is required when command_contributor is requested"
        )
    if command_specs_digest is not None and not wants_commands:
        raise PackageError(
            FailureCode.F4, "command_specs_digest is set without requesting command_contributor"
        )

    return ManifestV1(
        integration_id=integration_id,
        version=version,
        entry_module=entry_module,
        publisher_fingerprint=fingerprint,
        sdk_api=sdk_api,
        entry_object=entry_object,
        requested_capabilities=capabilities,
        command_specs_digest=command_specs_digest,
        service_manifest_digest=service_manifest_digest,
    )


def parse_signature(raw: bytes) -> DetachedSignatureV1:
    """Parse and validate the detached signature, or raise (F4/F5)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PackageError(FailureCode.F4, f"signature is not valid JSON: {exc}") from None

    _reject_unknown(data, _SIGNATURE_KEYS, "signature")
    _require_schema_version(data, "signature")
    algorithm = _string_default(data, "algorithm", "ed25519", "signature")
    if algorithm != "ed25519":
        raise PackageError(FailureCode.F5, f"unsupported signature algorithm '{algorithm}'")
    publisher_fingerprint = _string(data, "publisher_fingerprint", "signature")
    _validate_digest(publisher_fingerprint, "publisher_fingerprint")
    package_digest = _string(data, "package_digest", "signature")
    _validate_digest(package_digest, "package_digest")
    signature_b64 = _string(data, "signature_b64", "signature")
    _validate_signature_bytes(signature_b64)

    return DetachedSignatureV1(
        publisher_fingerprint=publisher_fingerprint,
        package_digest=package_digest,
        signature_b64=signature_b64,
        algorithm="ed25519",
    )


def _reject_unknown(mapping: Any, allowed: set[str], context: str) -> None:
    if not isinstance(mapping, dict):
        raise PackageError(FailureCode.F4, f"{context} must be a table/object")
    extra = {str(key) for key in mapping} - allowed
    if extra:
        raise PackageError(FailureCode.F4, f"unknown field(s) {sorted(extra)} in {context}")


def _require_schema_version(mapping: Any, context: str) -> None:
    value = _get(mapping, "schema_version", context)
    if isinstance(value, bool) or not isinstance(value, int) or value != _SCHEMA_VERSION:
        raise PackageError(FailureCode.F4, f"{context} schema_version must be {_SCHEMA_VERSION}")


def _table(mapping: Any, key: str, context: str) -> dict[str, Any]:
    value = _get(mapping, key, context)
    if not isinstance(value, dict):
        raise PackageError(FailureCode.F4, f"'{key}' in {context} must be a table")
    return {str(k): v for k, v in value.items()}


def _get(mapping: Any, key: str, context: str) -> Any:
    if not isinstance(mapping, dict):
        raise PackageError(FailureCode.F4, f"{context} must be a table/object")
    if key not in mapping:
        raise PackageError(FailureCode.F4, f"missing '{key}' in {context}")
    return mapping[key]


def _string(mapping: Any, key: str, context: str) -> str:
    value = _get(mapping, key, context)
    if not isinstance(value, str):
        raise PackageError(FailureCode.F4, f"'{key}' in {context} must be a string")
    return value


def _string_default(mapping: Any, key: str, default: str, context: str) -> str:
    if not isinstance(mapping, dict) or key not in mapping:
        return default
    return _string(mapping, key, context)


def _int_default(mapping: Any, key: str, default: int, context: str) -> int:
    if not isinstance(mapping, dict) or key not in mapping:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PackageError(FailureCode.F4, f"'{key}' in {context} must be an integer")
    return value


def _parse_capabilities(raw: Any) -> tuple[CapabilityRequest, ...]:
    if not isinstance(raw, list):
        raise PackageError(FailureCode.F4, "'capabilities' must be an array")
    seen: set[str] = set()
    result: list[CapabilityRequest] = []
    for item in raw:
        if not isinstance(item, str):
            raise PackageError(FailureCode.F4, "each capability must be a string")
        if item in seen:
            raise PackageError(FailureCode.F4, f"duplicate capability '{item}'")
        seen.add(item)
        try:
            result.append(CapabilityRequest(item))
        except ValueError:
            raise PackageError(FailureCode.F4, f"unknown capability '{item}'") from None
    return tuple(result)


def _validate_entry_module(entry_module: str, integration_id: str) -> None:
    if len(entry_module.encode("utf-8")) > _MAX_ENTRY_MODULE_BYTES:
        raise PackageError(FailureCode.F4, "entry_module exceeds length limit")
    segments = entry_module.split(".")
    if not all(_IDENTIFIER_RE.match(segment) for segment in segments):
        raise PackageError(FailureCode.F4, "entry_module is not a dotted identifier")
    if segments[0] != integration_id:
        raise PackageError(FailureCode.F4, "entry_module first segment must equal the integration id")


def _validate_entry_object(entry_object: str) -> None:
    if len(entry_object.encode("utf-8")) > _MAX_ENTRY_OBJECT_BYTES:
        raise PackageError(FailureCode.F4, "entry_object exceeds length limit")
    if not _IDENTIFIER_RE.match(entry_object):
        raise PackageError(FailureCode.F4, "entry_object is not an identifier")


def _validate_digest(value: str, label: str) -> None:
    if not _DIGEST_RE.match(value):
        raise PackageError(FailureCode.F4, f"{label} is not a sha256 digest")


def _validate_signature_bytes(signature_b64: str) -> None:
    try:
        decoded = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        raise PackageError(FailureCode.F4, "signature_b64 is not valid base64") from None
    if len(decoded) != _ED25519_SIGNATURE_BYTES:
        raise PackageError(
            FailureCode.F4, f"signature must decode to {_ED25519_SIGNATURE_BYTES} bytes"
        )
