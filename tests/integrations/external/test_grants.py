"""Sealed execution grants (CORE-007 D3): seal binds an installed digest, revoke
fences capability-specifically, rollback re-activates a prior sealed digest."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stormpulse.integrations.external import digest, grants, install, trust
from stormpulse.integrations.external.model import CapabilityRequest, FailureCode, PackageError

from ._helpers import approve, keypair, state_dir

_AGENT = "agent-1"
_CSD = "sha256:" + "0" * 64


def _active_digest(state: Path, integration_id: str = "obs") -> str:
    grant = grants.active_grant(state, integration_id)
    assert grant is not None
    return grant.package_digest


def _install(
    tmp_path: Path,
    state: Path,
    private: Ed25519PrivateKey,
    fingerprint: str,
    *,
    integration_id: str = "obs",
    version: str = "1.0.0",
    capabilities: tuple[str, ...] = ("integration_load",),
) -> str:
    """Build, sign, and install a package; return its package_digest."""
    pkg = tmp_path / f"src-{integration_id}-{version}"
    pkg.mkdir(parents=True, exist_ok=True)
    caps = ", ".join(f'"{c}"' for c in capabilities)
    lines = [
        "schema_version = 1",
        "",
        "[integration]",
        f'id = "{integration_id}"',
        f'version = "{version}"',
        f'entry_module = "{integration_id}.integration"',
        "",
        "[publisher]",
        f'fingerprint = "{fingerprint}"',
        "",
        "[requests]",
        f"capabilities = [{caps}]",
    ]
    if "command_contributor" in capabilities:
        lines.append(f'command_specs_digest = "{_CSD}"')
    (pkg / digest.MANIFEST_NAME).write_bytes(("\n".join(lines) + "\n").encode())
    (pkg / "code.py").write_bytes(b"code\n")

    package_digest = digest.scan_and_hash(pkg).package_digest
    payload = trust.signed_payload(package_digest, integration_id, version)
    signature = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "publisher_fingerprint": fingerprint,
        "package_digest": package_digest,
        "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii"),
    }
    (pkg / digest.SIGNATURE_NAME).write_bytes(json.dumps(signature).encode())
    return install.commit_install(pkg, state_dir=state, agent_id=_AGENT).package_digest


# ---------------------------------------------------------------------------
# seal
# ---------------------------------------------------------------------------


def test_seal_grants_all_requested_and_sets_active(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    d = _install(tmp_path, state, private, fp, capabilities=("integration_load", "command_contributor"))

    grant = grants.seal(state, package_digest=d)
    assert set(grant.granted_capabilities) == {
        CapabilityRequest.INTEGRATION_LOAD,
        CapabilityRequest.COMMAND_CONTRIBUTOR,
    }
    assert grant.command_specs_digest == _CSD
    assert _active_digest(state) == d


def test_seal_refuses_revoked_publisher(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    d = _install(tmp_path, state, private, fp)
    trust.revoke_publisher(state, fp)  # publisher revoked AFTER install
    try:
        grants.seal(state, package_digest=d)
        raise AssertionError("seal should refuse a revoked publisher")
    except PackageError as exc:
        assert exc.code is FailureCode.F7


def test_seal_unknown_digest_is_f11(tmp_path: Path) -> None:
    state = state_dir(tmp_path)
    try:
        grants.seal(state, package_digest="sha256:" + "a" * 64)
        raise AssertionError("seal should refuse an uninstalled digest")
    except PackageError as exc:
        assert exc.code is FailureCode.F11


# ---------------------------------------------------------------------------
# revoke (capability-specific)
# ---------------------------------------------------------------------------


def test_revoke_command_contributor_keeps_load(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    d = _install(tmp_path, state, private, fp, capabilities=("integration_load", "command_contributor"))
    grants.seal(state, package_digest=d)

    grant = grants.revoke(state, package_digest=d, capability=CapabilityRequest.COMMAND_CONTRIBUTOR)
    eff = grants.effective_capabilities(grant)
    assert CapabilityRequest.COMMAND_CONTRIBUTOR not in eff
    assert CapabilityRequest.INTEGRATION_LOAD in eff  # adapter still loads for state/health
    # still the active grant, just fenced
    assert _active_digest(state) == d


def test_revoke_ungranted_capability_is_f5(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    d = _install(tmp_path, state, private, fp, capabilities=("integration_load",))
    grants.seal(state, package_digest=d)
    try:
        grants.revoke(state, package_digest=d, capability=CapabilityRequest.COMMAND_CONTRIBUTOR)
        raise AssertionError("cannot revoke a capability that was never granted")
    except PackageError as exc:
        assert exc.code is FailureCode.F5


def test_revoke_is_idempotent(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    d = _install(tmp_path, state, private, fp, capabilities=("integration_load", "command_contributor"))
    grants.seal(state, package_digest=d)
    once = grants.revoke(state, package_digest=d, capability=CapabilityRequest.COMMAND_CONTRIBUTOR)
    twice = grants.revoke(state, package_digest=d, capability=CapabilityRequest.COMMAND_CONTRIBUTOR)
    assert once.revoked_capabilities == twice.revoked_capabilities


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_reactivates_prior_digest(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    v1 = _install(tmp_path, state, private, fp, version="1.0.0")
    v2 = _install(tmp_path, state, private, fp, version="2.0.0")

    grants.seal(state, package_digest=v1)
    grants.seal(state, package_digest=v2)
    assert _active_digest(state) == v2  # newest sealed is active

    rolled = grants.rollback(state, integration_id="obs", package_digest=v1)
    assert rolled.package_digest == v1
    assert _active_digest(state) == v1  # prior version restored


def test_rollback_refuses_load_revoked_grant(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    v1 = _install(tmp_path, state, private, fp, version="1.0.0")
    v2 = _install(tmp_path, state, private, fp, version="2.0.0")
    grants.seal(state, package_digest=v1)
    grants.seal(state, package_digest=v2)  # active = v2
    grants.revoke(state, package_digest=v1, capability=CapabilityRequest.INTEGRATION_LOAD)
    try:
        grants.rollback(state, integration_id="obs", package_digest=v1)
        raise AssertionError("cannot roll back to a load-revoked grant")
    except PackageError as exc:
        assert exc.code is FailureCode.F7


def test_active_grant_is_none_before_sealing(tmp_path: Path) -> None:
    state = state_dir(tmp_path)
    assert grants.active_grant(state, "obs") is None
