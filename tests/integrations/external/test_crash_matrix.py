"""Crash-recovery matrix (T23): a failure injected at every commit point in the
install sequence must leave the state tree in one of three allowed recovery states.

The three allowed states, and the points that land in each:

* **No install** (no package tree, no receipt). The crash happened before, or at,
  the package rename, so ``os.replace`` never blessed a tree. Points: temp
  creation, a copied file, destination verification, mode normalization, the
  package rename itself (a failed rename leaves the staged tree, which the
  ``finally`` removes).
* **Orphan package** (a sealed, read-only package tree with no receipt). The
  rename succeeded but the receipt was never committed. ``doctor`` reports one
  ``orphan_package`` warning, and a retry completes idempotently. Points: package
  fsync, receipt-temp fsync, receipt replace (the temp is unlinked, so the
  committed name is never a partial file).
* **Healthy** (package tree plus a complete receipt). The crash happened after
  the receipt's ``os.replace`` but before the parent-dir fsync, so the receipt is
  durable-enough and fully formed. Point: receipt-parent fsync.

Faithfulness: these tests do not stub the commit logic. They run the real code
and make one chosen syscall raise at the target line, which is what a crash at
that instant does. ``monkeypatch.undo()`` restores every patch before ``doctor``
inspects the result.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from stormpulse.integrations.external import digest, install, layout, ledger
from stormpulse.integrations.external.model import Finding, InstallReceiptV1, Severity
from tests.integrations.external._helpers import (
    approve as _approve,
    installed_dir as _installed_dir,
    keypair as _keypair,
    make_package as _make_package,
    state_dir as _state,
)


def _prepared(tmp_path: Path) -> tuple[Path, Path, str]:
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = _make_package(src, private, fingerprint)
    return state, src, package_digest


def _boom(*_args: object, **_kwargs: object) -> None:
    raise OSError("injected crash")


def _findings_after(state: Path) -> list[Finding]:
    from stormpulse.integrations.external import doctor

    return doctor.doctor_packages(state)


# --- States that must leave nothing installed -------------------------------


@pytest.mark.parametrize(
    "target",
    ["mkdtemp", "copy_tree", "scan_and_hash", "seal_contents", "package_rename"],
)
def test_crash_before_commit_leaves_no_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    state, src, _ = _prepared(tmp_path)
    if target == "mkdtemp":
        monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    elif target == "copy_tree":
        monkeypatch.setattr(digest, "copy_tree", _boom)
    elif target == "scan_and_hash":
        monkeypatch.setattr(digest, "scan_and_hash", _boom)  # destination verification
    elif target == "seal_contents":
        monkeypatch.setattr(digest, "seal_contents", _boom)  # mode normalization
    elif target == "package_rename":
        monkeypatch.setattr(os, "replace", _boom)  # first os.replace is the package rename

    with pytest.raises(OSError):
        install.commit_install(src, state_dir=state, agent_id="a")

    monkeypatch.undo()
    assert list(layout.packages_dir(state).iterdir()) == []
    assert ledger.list_receipts(state) == []
    assert _findings_after(state) == []


# --- States that must leave an orphan package (rename done, no receipt) ------


def test_crash_at_package_fsync_leaves_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, src, package_digest = _prepared(tmp_path)
    monkeypatch.setattr(layout, "fsync_dir", _boom)  # first fsync_dir is the package dir

    with pytest.raises(OSError):
        install.commit_install(src, state_dir=state, agent_id="a")

    monkeypatch.undo()
    _assert_orphan(state, package_digest)


@pytest.mark.parametrize("stage", ["temp_fsync", "replace"])
def test_crash_inside_receipt_write_leaves_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    state, src, package_digest = _prepared(tmp_path)
    _arm_receipt_fault(monkeypatch, stage)

    with pytest.raises(OSError):
        install.commit_install(src, state_dir=state, agent_id="a")

    monkeypatch.undo()
    _assert_orphan(state, package_digest)

    # A retry over the orphan package completes idempotently.
    receipt = install.commit_install(src, state_dir=state, agent_id="a")
    assert receipt.package_digest == package_digest
    assert [r.package_digest for r in ledger.list_receipts(state)] == [package_digest]


# --- State that must be healthy (receipt committed, only the fsync lost) ------


def test_crash_at_receipt_parent_fsync_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, src, package_digest = _prepared(tmp_path)
    _arm_receipt_fault(monkeypatch, "parent_fsync")

    # The receipt's os.replace already ran, so the write is committed; only the
    # durability fsync was lost. commit_install still surfaces the error.
    with pytest.raises(OSError):
        install.commit_install(src, state_dir=state, agent_id="a")

    monkeypatch.undo()
    assert _installed_dir(state, package_digest).is_dir()
    assert [r.package_digest for r in ledger.list_receipts(state)] == [package_digest]
    assert _findings_after(state) == []  # fully recovered, no findings


# --- Helpers ----------------------------------------------------------------


def _assert_orphan(state: Path, package_digest: str) -> None:
    assert _installed_dir(state, package_digest).is_dir()
    assert ledger.list_receipts(state) == []
    findings = _findings_after(state)
    assert [f.code for f in findings] == ["orphan_package"]
    assert all(f.severity is Severity.WARNING for f in findings)


def _arm_receipt_fault(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    """Make one syscall inside the atomic receipt write fail.

    Inside ``layout.atomic_write`` the order is: fsync(temp), replace(temp->path),
    fsync(parent). Arming only during ``ledger.write_receipt`` leaves the package
    commit (its own replace/fsync, run earlier) untouched.
    """
    real_write = ledger.write_receipt
    real_fsync = os.fsync
    real_replace = os.replace
    armed = False
    fsync_seen = 0

    def fsync(fd: int) -> None:
        nonlocal fsync_seen
        if armed:
            fsync_seen += 1
            if stage == "temp_fsync" and fsync_seen == 1:
                raise OSError("crash at receipt temp fsync")
            if stage == "parent_fsync" and fsync_seen == 2:
                raise OSError("crash at receipt parent fsync")
        real_fsync(fd)

    def replace(src: str | Path, dst: str | Path) -> None:
        if armed and stage == "replace":
            raise OSError("crash at receipt replace")
        real_replace(src, dst)

    def armed_write(state_dir: Path, receipt: InstallReceiptV1) -> None:
        nonlocal armed
        armed = True
        try:
            real_write(state_dir, receipt)
        finally:
            armed = False

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(ledger, "write_receipt", armed_write)
