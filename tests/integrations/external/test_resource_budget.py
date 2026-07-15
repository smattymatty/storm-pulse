"""Bounded resource use over the package tree (T34) and the literal file-count
boundary (T07's count limit at its real value).

Two independent properties are proven here:

* No operation buffers a whole package or a whole file in memory: every read is
  chunked, so the peak single read never exceeds one chunk regardless of file
  size. A 2.5 MiB file therefore takes several reads, not one.
* The read amplification is bounded: inspect and doctor read each included byte
  at most once (a single hash pass); install reads it at most twice (copy, then
  a re-hash of the destination, which is the sole install authority). A fixed,
  small allowance covers the digest-excluded signature file.

Instrumentation note: only ``os.read`` is intercepted. The package hash/copy
paths call it directly, while receipt reads go through ``Path.read_bytes`` (a
C-level read that never enters this wrapper), so the counts reflect package
bytes only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stormpulse.integrations.external import digest as d
from stormpulse.integrations.external import doctor, inspection, install
from stormpulse.integrations.external.model import FailureCode, PackageError
from tests.integrations.external._helpers import (
    approve as _approve,
    keypair as _keypair,
    make_package as _make_package,
    state_dir as _state,
)

# A signature file is always excluded from the digest set but still read once;
# this bounds the fixed allowance added to the per-included-byte budget.
_SIG_ALLOWANCE = d.MAX_SIGNATURE_BYTES


class _ReadSpy:
    """Records every ``os.read`` while installed as the active reader."""

    def __init__(self) -> None:
        self._real = os.read
        self.total_bytes = 0
        self.max_chunk = 0
        self.calls = 0

    def __enter__(self) -> "_ReadSpy":
        def spy(fd: int, length: int) -> bytes:
            data = self._real(fd, length)
            self.calls += 1
            self.total_bytes += len(data)
            self.max_chunk = max(self.max_chunk, len(data))
            return data

        os.read = spy
        return self

    def __exit__(self, *_exc: object) -> None:
        os.read = self._real


def _big_package(tmp_path: Path) -> tuple[Path, Path, int]:
    """A signed package with one multi-chunk file, so chunking is observable."""
    private, fingerprint = _keypair()
    state = _state(tmp_path)
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    _make_package(
        src,
        private,
        fingerprint,
        extra={"big.py": b"x" * (2 * d._CHUNK + 500), "small.py": b"hi\n"},
    )
    included = d.scan_and_hash(src).total_bytes
    return state, src, included


def test_t34_reads_are_chunked_and_bounded(tmp_path: Path) -> None:
    state, src, included = _big_package(tmp_path)

    with _ReadSpy() as inspect_spy:
        inspection.inspect_package(src, state)
    with _ReadSpy() as install_spy:
        install.commit_install(src, state_dir=state, agent_id="a")
    with _ReadSpy() as doctor_spy:
        doctor.doctor_packages(state)

    # A file larger than one chunk must never be read in a single call.
    for spy in (inspect_spy, install_spy, doctor_spy):
        assert spy.max_chunk <= d._CHUNK
        assert spy.calls > 0
    # The 2.5-chunk file forces multiple reads: no whole-file buffer.
    assert inspect_spy.calls >= 3

    assert inspect_spy.total_bytes <= included + _SIG_ALLOWANCE  # <= 1x
    assert doctor_spy.total_bytes <= included + _SIG_ALLOWANCE  # <= 1x
    assert install_spy.total_bytes <= 2 * included + 2 * _SIG_ALLOWANCE  # <= 2x (copy + verify)


def _count_tree(root: Path, included_files: int) -> Path:
    """A tree with ``included_files`` digest-counted files (manifest is one of them)."""
    root.mkdir(parents=True)
    (root / d.MANIFEST_NAME).write_bytes(b'schema_version = 1\n[integration]\nid = "x"\n')
    for i in range(included_files - 1):  # -1: the manifest already counts
        (root / f"f{i}.py").write_bytes(b"x")
    return root


def test_count_boundary_at_limit_passes(tmp_path: Path) -> None:
    tree = _count_tree(tmp_path / "ok", d.MAX_FILES)  # exactly MAX_FILES counted files
    assert d.scan_and_hash(tree).file_count == d.MAX_FILES


def test_count_boundary_over_limit_is_f3(tmp_path: Path) -> None:
    tree = _count_tree(tmp_path / "over", d.MAX_FILES + 1)  # one past the limit
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F3
