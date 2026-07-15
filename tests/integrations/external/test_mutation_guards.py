"""Mutation evidence: each security guard is load-bearing.

A guard is only proven when removing it makes a protected behavior fail. For
guards that a test can cleanly neutralize in isolation, this module removes the
guard and asserts the property breaks. For guards that resist clean in-test
neutralization (they are structural, ordering, or defense-in-depth, so no single
call carries them), the kill already lives in a dedicated test; those are mapped
here rather than contorted into a symmetrical shape:

* T05 (symlink rejection) - proven by ``test_digest.test_t05_symlink_*``; the
  guard is a pair (an explicit ``is_symlink`` reject plus ``O_NOFOLLOW`` on every
  open), so neutralizing one still leaves the other. Defense in depth, by design.
* T15 (never execute) - proven by ``test_no_execution`` (an import sentinel that
  must stay absent) and by the AST fence's own detection test below's sibling in
  ``fitness/external_loader_p1.py``.
* T21 (source race) - proven by ``test_install.test_t21_raced_source_yields_no_committed_receipt``:
  the destination re-hash is the authority, so a raced tree is rejected.
* T23 (crash recovery) - proven by ``test_crash_matrix``: a failure injected at
  every commit point lands in an allowed recovery state.
* T29 (startup isolation) - proven by ``test_startup_isolation``: malicious P1
  state leaves the agent registry unchanged.
* T32 (fence catches mutation) - proven by
  ``test_no_execution.test_fence_catches_each_forbidden_primitive``, which is
  itself a parametrized mutation probe over every forbidden primitive.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stormpulse.integrations.external import digest as d
from stormpulse.integrations.external import install, trust
from stormpulse.integrations.external.model import FailureCode, PackageError
from tests.integrations.external._helpers import (
    approve as _approve,
    keypair as _keypair,
    make_package as _make_package,
    state_dir as _state,
)


def test_t03_content_fold_is_load_bearing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two trees identical except one file's bytes (same length). With the content
    # fold intact they must differ; neutralize the per-file content hash and they
    # collide, proving the fold is what makes content changes visible.
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root, body in ((left, b"AAAA"), (right, b"BBBB")):
        root.mkdir()
        (root / d.MANIFEST_NAME).write_bytes(b'schema_version = 1\n[integration]\nid = "x"\n')
        (root / "f.txt").write_bytes(body)

    assert d.scan_and_hash(left).package_digest != d.scan_and_hash(right).package_digest

    def blind(fd: int, rel: str, already_total: int) -> tuple[bytes, int]:
        data = os.read(fd, d.MAX_FILE_BYTES)  # consume and keep the real size
        return b"\x00" * 32, len(data)

    monkeypatch.setattr(d, "_hash_capped", blind)
    assert d.scan_and_hash(left).package_digest == d.scan_and_hash(right).package_digest


def test_t12_signature_verify_is_load_bearing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A package whose signature carries the approved fingerprint and the correct
    # digest, but was signed by a DIFFERENT key: only the cryptographic check
    # rejects it. Baseline install is F6; neutralize verify_signature and the
    # forged package (wrongly) installs, proving the check is load-bearing.
    approved_private, approved_fingerprint = _keypair()
    attacker_private, _ = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, approved_private)
    src = tmp_path / "src"
    package_digest = _make_package(src, attacker_private, approved_fingerprint)  # forged signer

    with pytest.raises(PackageError) as excinfo:
        install.commit_install(src, state_dir=state, agent_id="a")
    assert excinfo.value.code is FailureCode.F6

    monkeypatch.setattr(trust, "verify_signature", lambda *a, **k: True)
    receipt = install.commit_install(src, state_dir=state, agent_id="a")
    assert receipt.package_digest == package_digest
