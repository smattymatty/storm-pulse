"""Tests for immutable content-addressed installation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from stormpulse.integrations.external import digest, install, layout, ledger, trust
from stormpulse.integrations.external.model import FailureCode, PackageError
from tests.integrations.external._helpers import (
    approve as _approve,
    installed_dir as _installed_dir,
    keypair as _keypair,
    make_package as _make_package,
    state_dir as _state,
)


def test_t19_install_valid(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = _make_package(src, private, fingerprint)

    receipt = install.commit_install(src, state_dir=state, agent_id="agent-1")

    assert receipt.package_digest == package_digest
    assert receipt.integration_id == "obs"
    assert receipt.agent_id == "agent-1"
    assert receipt.installed_relpath == f"packages/sha256/{package_digest.split(':')[1]}"

    installed = _installed_dir(state, package_digest)
    assert installed.is_dir()
    assert digest.scan_and_hash(installed).package_digest == package_digest
    assert (installed / digest.MANIFEST_NAME).stat().st_mode & 0o222 == 0  # read-only
    assert [r.package_digest for r in ledger.list_receipts(state)] == [package_digest]


def test_t20_repeated_install_is_idempotent(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    _make_package(src, private, fingerprint)
    first = install.commit_install(src, state_dir=state, agent_id="a")
    second = install.commit_install(src, state_dir=state, agent_id="a")
    assert second.package_digest == first.package_digest
    assert len(ledger.list_receipts(state)) == 1


def test_install_unknown_publisher_is_f7(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    src = tmp_path / "src"
    _make_package(src, private, fingerprint)  # publisher never approved
    with pytest.raises(PackageError) as excinfo:
        install.commit_install(src, state_dir=state, agent_id="a")
    assert excinfo.value.code is FailureCode.F7
    assert ledger.list_receipts(state) == []
    assert list(layout.packages_dir(state).iterdir()) == []


def test_install_revoked_publisher_is_f7(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    trust.revoke_publisher(state, fingerprint)
    src = tmp_path / "src"
    _make_package(src, private, fingerprint)
    with pytest.raises(PackageError) as excinfo:
        install.commit_install(src, state_dir=state, agent_id="a")
    assert excinfo.value.code is FailureCode.F7


def test_install_tampered_package_is_f6(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    _make_package(src, private, fingerprint)
    (src / "code.py").write_bytes(b"tampered\n")  # digest changes; signature no longer matches
    with pytest.raises(PackageError) as excinfo:
        install.commit_install(src, state_dir=state, agent_id="a")
    assert excinfo.value.code is FailureCode.F6
    assert ledger.list_receipts(state) == []


def test_t22_corrupt_target_is_f10(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = _make_package(src, private, fingerprint)
    install.commit_install(src, state_dir=state, agent_id="a")

    target = _installed_dir(state, package_digest)
    os.chmod(target, 0o755)
    (target / "injected.py").write_bytes(b"x")
    with pytest.raises(PackageError) as excinfo:
        install.commit_install(src, state_dir=state, agent_id="a")
    assert excinfo.value.code is FailureCode.F10


def test_crash_before_rename_leaves_no_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    _make_package(src, private, fingerprint)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        install.commit_install(src, state_dir=state, agent_id="a")
    # No blessed package, no receipt (a leftover tmp is allowed; doctor reports it).
    assert list(layout.packages_dir(state).iterdir()) == []
    assert ledger.list_receipts(state) == []


def test_crash_after_rename_before_receipt_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = _make_package(src, private, fingerprint)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash before receipt")

    monkeypatch.setattr(ledger, "write_receipt", boom)
    with pytest.raises(OSError):
        install.commit_install(src, state_dir=state, agent_id="a")
    # Orphan package present, no receipt yet.
    assert _installed_dir(state, package_digest).is_dir()
    assert ledger.list_receipts(state) == []

    monkeypatch.undo()  # retry completes idempotently over the orphan package
    receipt = install.commit_install(src, state_dir=state, agent_id="a")
    assert receipt.package_digest == package_digest
    assert [r.package_digest for r in ledger.list_receipts(state)] == [package_digest]


def test_installed_tree_is_read_only_including_subdirs(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = _make_package(src, private, fingerprint, extra={"sub/nested.py": b"nested\n"})
    install.commit_install(src, state_dir=state, agent_id="a")
    installed = _installed_dir(state, package_digest)
    assert installed.stat().st_mode & 0o777 == 0o555
    assert (installed / "code.py").stat().st_mode & 0o222 == 0
    assert (installed / "sub").stat().st_mode & 0o777 == 0o555
    assert (installed / "sub" / "nested.py").stat().st_mode & 0o222 == 0


def test_idempotent_reinstall_reseals_mode_drift(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = _make_package(src, private, fingerprint)
    install.commit_install(src, state_dir=state, agent_id="a")
    drifted = _installed_dir(state, package_digest) / "code.py"
    os.chmod(drifted, 0o644)  # someone made it writable
    install.commit_install(src, state_dir=state, agent_id="a")  # idempotent reinstall re-seals
    assert drifted.stat().st_mode & 0o222 == 0


_HEX = "ab" * 32
_DIGEST = "sha256:" + _HEX


def _valid_receipt(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "agent_id": "a",
        "integration_id": "obs",
        "version": "1.0.0",
        "sdk_api": 1,
        "package_digest": _DIGEST,
        "manifest_digest": _DIGEST,
        "publisher_fingerprint": _DIGEST,
        "requested_capabilities": ["integration_load"],
        "installed_relpath": f"packages/sha256/{_HEX}",
        "installed_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _write_raw_receipt(state: Path, integration_id: str, filename: str, payload: object) -> Path:
    directory = layout.receipts_dir(state) / integration_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(payload))
    return path


def test_receipt_relpath_traversal_is_f11(tmp_path: Path) -> None:
    state = _state(tmp_path)
    path = _write_raw_receipt(state, "obs", f"{_HEX}.json", _valid_receipt(installed_relpath="../../../../etc"))
    with pytest.raises(PackageError) as excinfo:
        ledger.read_receipt(path)
    assert excinfo.value.code is FailureCode.F11


def test_receipt_bad_digest_is_f11(tmp_path: Path) -> None:
    state = _state(tmp_path)
    path = _write_raw_receipt(state, "obs", f"{_HEX}.json", _valid_receipt(package_digest="sha256:nothex"))
    with pytest.raises(PackageError) as excinfo:
        ledger.read_receipt(path)
    assert excinfo.value.code is FailureCode.F11


def test_receipt_filename_disagrees_with_digest_is_f11(tmp_path: Path) -> None:
    state = _state(tmp_path)
    wrong_name = "deadbeef" + "00" * 28 + ".json"  # 64 hex, but not the receipt's digest
    path = _write_raw_receipt(state, "obs", wrong_name, _valid_receipt())
    with pytest.raises(PackageError) as excinfo:
        ledger.read_receipt(path)
    assert excinfo.value.code is FailureCode.F11


def test_receipt_wrong_parent_dir_is_f11(tmp_path: Path) -> None:
    state = _state(tmp_path)
    path = _write_raw_receipt(state, "bar", f"{_HEX}.json", _valid_receipt(integration_id="obs"))
    with pytest.raises(PackageError) as excinfo:
        ledger.read_receipt(path)
    assert excinfo.value.code is FailureCode.F11


def test_list_receipts_skips_corrupt(tmp_path: Path) -> None:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    good_digest = _make_package(src, private, fingerprint)
    install.commit_install(src, state_dir=state, agent_id="a")
    _write_raw_receipt(state, "obs", "0" * 64 + ".json", {"not": "a receipt"})
    assert [r.package_digest for r in ledger.list_receipts(state)] == [good_digest]
