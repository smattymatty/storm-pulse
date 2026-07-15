"""Tests for strict manifest and detached-signature parsing."""

from __future__ import annotations

import base64
import json

import pytest

from stormpulse.integrations.external.manifest import parse_manifest, parse_signature
from stormpulse.integrations.external.model import (
    CapabilityRequest,
    FailureCode,
    PackageError,
)

_FP = "sha256:" + "ab" * 32
_PKG = "sha256:" + "cd" * 32
_CSD = "sha256:" + "ef" * 32
_SMD = "sha256:" + "0f" * 32
_SIG64 = base64.b64encode(b"\x01" * 64).decode("ascii")

_DEFAULT_INTEGRATION = 'id = "obs"\nversion = "1.0.0"\nentry_module = "obs.integration"\n'


def _manifest(
    *,
    schema_version: str = "1",
    integration_body: str = _DEFAULT_INTEGRATION,
    publisher_body: str = f'fingerprint = "{_FP}"\n',
    requests_body: str | None = 'capabilities = ["integration_load"]\n',
    extra_root: str = "",
) -> bytes:
    parts = [
        f"schema_version = {schema_version}\n",
        extra_root,
        "\n[integration]\n",
        integration_body,
        "\n[publisher]\n",
        publisher_body,
    ]
    if requests_body is not None:
        parts += ["\n[requests]\n", requests_body]
    return "".join(parts).encode("utf-8")


def _signature(**overrides: object) -> bytes:
    fields: dict[str, object] = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "publisher_fingerprint": _FP,
        "package_digest": _PKG,
        "signature_b64": _SIG64,
    }
    fields.update(overrides)
    return json.dumps(fields).encode("utf-8")


def test_t08_minimal_valid() -> None:
    manifest = parse_manifest(_manifest())
    assert manifest.integration_id == "obs"
    assert manifest.version == "1.0.0"
    assert manifest.sdk_api == 1
    assert manifest.entry_module == "obs.integration"
    assert manifest.entry_object == "INTEGRATION"
    assert manifest.publisher_fingerprint == _FP
    assert manifest.requested_capabilities == (CapabilityRequest.INTEGRATION_LOAD,)
    assert manifest.command_specs_digest is None
    assert manifest.service_manifest_digest is None


def test_no_requests_table_means_no_capabilities() -> None:
    assert parse_manifest(_manifest(requests_body=None)).requested_capabilities == ()


@pytest.mark.parametrize(
    "raw",
    [
        _manifest(extra_root="unexpected = 1\n"),
        _manifest(integration_body=_DEFAULT_INTEGRATION + "bogus = 1\n"),
        _manifest(integration_body='version = "1.0.0"\nentry_module = "obs.integration"\n'),
        _manifest(integration_body='id = "obs"\nversion = 1\nentry_module = "obs.integration"\n'),
        _manifest(schema_version="2"),
        _manifest(integration_body=_DEFAULT_INTEGRATION + "sdk_api = 2\n"),
        _manifest(integration_body='id = "Obs"\nversion = "1.0.0"\nentry_module = "Obs.integration"\n'),
        _manifest(integration_body='id = "obs"\nversion = "1.x.0"\nentry_module = "obs.integration"\n'),
        _manifest(integration_body='id = "obs"\nversion = "1.0.0"\nentry_module = "other.integration"\n'),
        _manifest(publisher_body='fingerprint = "sha256:nothex"\n'),
    ],
)
def test_t09_structural_failures_are_f4(raw: bytes) -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code is FailureCode.F4


def test_non_utf8_manifest_is_f4() -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(b"\xff\xfe not utf-8")
    assert excinfo.value.code is FailureCode.F4


def test_invalid_toml_is_f4() -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(b"this = = not toml")
    assert excinfo.value.code is FailureCode.F4


def test_t10_command_contributor_requires_digest() -> None:
    raw = _manifest(requests_body='capabilities = ["integration_load", "command_contributor"]\n')
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code is FailureCode.F4


def test_t10_digest_without_command_contributor() -> None:
    raw = _manifest(
        requests_body=f'capabilities = ["integration_load"]\ncommand_specs_digest = "{_CSD}"\n'
    )
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code is FailureCode.F4


def test_t10_command_contributor_with_digest_ok() -> None:
    raw = _manifest(requests_body=f'capabilities = ["command_contributor"]\ncommand_specs_digest = "{_CSD}"\n')
    manifest = parse_manifest(raw)
    assert CapabilityRequest.COMMAND_CONTRIBUTOR in manifest.requested_capabilities
    assert manifest.command_specs_digest == _CSD


def test_t10_duplicate_capability_is_f4() -> None:
    raw = _manifest(requests_body='capabilities = ["integration_load", "integration_load"]\n')
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code is FailureCode.F4


def test_t10_dropped_capability_is_unknown() -> None:
    # manage_service was removed from the capability set; it must not parse.
    raw = _manifest(requests_body='capabilities = ["manage_service"]\n')
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code is FailureCode.F4


def test_service_manifest_digest_is_structural() -> None:
    raw = _manifest(
        requests_body=f'capabilities = ["integration_load"]\nservice_manifest_digest = "{_SMD}"\n'
    )
    assert parse_manifest(raw).service_manifest_digest == _SMD


def test_signature_valid() -> None:
    signature = parse_signature(_signature())
    assert signature.publisher_fingerprint == _FP
    assert signature.package_digest == _PKG
    assert signature.signature_b64 == _SIG64


def test_signature_unknown_field_is_f4() -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_signature(_signature(extra="x"))
    assert excinfo.value.code is FailureCode.F4


def test_signature_bad_base64_is_f4() -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_signature(_signature(signature_b64="not!valid!base64!"))
    assert excinfo.value.code is FailureCode.F4


def test_signature_wrong_length_is_f4() -> None:
    short = base64.b64encode(b"\x01" * 32).decode("ascii")
    with pytest.raises(PackageError) as excinfo:
        parse_signature(_signature(signature_b64=short))
    assert excinfo.value.code is FailureCode.F4


def test_signature_bad_algorithm_is_f5() -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_signature(_signature(algorithm="rsa"))
    assert excinfo.value.code is FailureCode.F5


def test_signature_bad_digest_is_f4() -> None:
    with pytest.raises(PackageError) as excinfo:
        parse_signature(_signature(package_digest="sha256:nothex"))
    assert excinfo.value.code is FailureCode.F4


def test_reserved_integration_id_is_f4() -> None:
    raw = _manifest(
        integration_body='id = "json"\nversion = "1.0.0"\nentry_module = "json.integration"\n'
    )
    with pytest.raises(PackageError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code is FailureCode.F4
