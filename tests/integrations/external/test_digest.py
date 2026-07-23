"""Tests for the deterministic package digest.

The reference-vector test recomputes the digest formula independently, so it
proves the implementation matches the intended definition, not merely itself.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from stormpulse.integrations.external import digest as d
from stormpulse.integrations.external.model import FailureCode, PackageError

_MANIFEST = b'schema_version = 1\n[integration]\nid = "x"\n'


def _write_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _reference_digest(included: dict[str, bytes]) -> str:
    """Independent re-implementation of the digest formula (sig already excluded)."""
    hasher = hashlib.sha256()
    hasher.update(b"stormpulse-package-v1\x00")
    for rel in sorted(included, key=lambda r: r.encode("utf-8")):
        content = included[rel]
        rel_bytes = rel.encode("utf-8")
        hasher.update(len(rel_bytes).to_bytes(4, "big"))
        hasher.update(rel_bytes)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(hashlib.sha256(content).digest())
    return "sha256:" + hasher.hexdigest()


def test_reference_vector(tmp_path: Path) -> None:
    tree = _write_tree(
        tmp_path / "pkg",
        {
            d.MANIFEST_NAME: b"m",
            "sub/f.txt": b"hello",
            d.SIGNATURE_NAME: b"SIGNATURE-BYTES",
        },
    )
    result = d.scan_and_hash(tree)
    assert result.package_digest == _reference_digest({d.MANIFEST_NAME: b"m", "sub/f.txt": b"hello"})
    assert result.file_count == 2  # signature excluded from the digest set
    assert result.manifest_bytes == b"m"
    assert result.signature_bytes == b"SIGNATURE-BYTES"


def test_t01_order_independent(tmp_path: Path) -> None:
    files = {d.MANIFEST_NAME: _MANIFEST, "z.txt": b"z", "a/b.txt": b"bb"}
    left = _write_tree(tmp_path / "left", files)
    right = _write_tree(tmp_path / "right", dict(reversed(list(files.items()))))
    assert d.scan_and_hash(left).package_digest == d.scan_and_hash(right).package_digest


def test_t02_metadata_independent(tmp_path: Path) -> None:
    files = {d.MANIFEST_NAME: _MANIFEST, "f.txt": b"data"}
    left = _write_tree(tmp_path / "left", files)
    right = _write_tree(tmp_path / "right", files)
    os.utime(left / "f.txt", (1, 1))
    os.utime(right / "f.txt", (10_000_000, 10_000_000))
    os.chmod(left / "f.txt", 0o600)
    os.chmod(right / "f.txt", 0o644)
    assert d.scan_and_hash(left).package_digest == d.scan_and_hash(right).package_digest


def test_t03_content_changes_digest(tmp_path: Path) -> None:
    left = _write_tree(tmp_path / "left", {d.MANIFEST_NAME: _MANIFEST, "f.txt": b"data"})
    right = _write_tree(tmp_path / "right", {d.MANIFEST_NAME: _MANIFEST, "f.txt": b"datb"})
    assert d.scan_and_hash(left).package_digest != d.scan_and_hash(right).package_digest


def test_t04_structure_changes_digest(tmp_path: Path) -> None:
    base = _write_tree(tmp_path / "base", {d.MANIFEST_NAME: _MANIFEST, "f.txt": b"x"})
    added = _write_tree(tmp_path / "added", {d.MANIFEST_NAME: _MANIFEST, "f.txt": b"x", "g.txt": b"x"})
    renamed = _write_tree(tmp_path / "renamed", {d.MANIFEST_NAME: _MANIFEST, "renamed.txt": b"x"})
    base_digest = d.scan_and_hash(base).package_digest
    assert d.scan_and_hash(added).package_digest != base_digest
    assert d.scan_and_hash(renamed).package_digest != base_digest


def test_signature_excluded_from_digest(tmp_path: Path) -> None:
    left = _write_tree(tmp_path / "left", {d.MANIFEST_NAME: _MANIFEST, d.SIGNATURE_NAME: b"A"})
    right = _write_tree(tmp_path / "right", {d.MANIFEST_NAME: _MANIFEST, d.SIGNATURE_NAME: b"BBBB"})
    assert d.scan_and_hash(left).package_digest == d.scan_and_hash(right).package_digest


def test_t05_symlink_file_rejected(tmp_path: Path) -> None:
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: _MANIFEST})
    (tree / "link.txt").symlink_to(tree / d.MANIFEST_NAME)
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F2


def test_t05_symlink_dir_rejected(tmp_path: Path) -> None:
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: _MANIFEST})
    (tmp_path / "outside").mkdir()
    (tree / "linkdir").symlink_to(tmp_path / "outside")
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F2


def test_t06_fifo_rejected(tmp_path: Path) -> None:
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: _MANIFEST})
    os.mkfifo(tree / "pipe")
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F2


def test_t07_file_size_boundary(tmp_path: Path) -> None:
    at_limit = _write_tree(
        tmp_path / "ok", {d.MANIFEST_NAME: _MANIFEST, "big": b"x" * d.MAX_FILE_BYTES}
    )
    d.scan_and_hash(at_limit)  # exact limit passes
    over = _write_tree(
        tmp_path / "over", {d.MANIFEST_NAME: _MANIFEST, "big": b"x" * (d.MAX_FILE_BYTES + 1)}
    )
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(over)
    assert excinfo.value.code is FailureCode.F3


def test_t07_manifest_size_boundary(tmp_path: Path) -> None:
    at_limit = _write_tree(tmp_path / "ok", {d.MANIFEST_NAME: b"m" * d.MAX_MANIFEST_BYTES})
    d.scan_and_hash(at_limit)
    over = _write_tree(tmp_path / "over", {d.MANIFEST_NAME: b"m" * (d.MAX_MANIFEST_BYTES + 1)})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(over)
    assert excinfo.value.code is FailureCode.F3


def test_t07_path_byte_boundary(tmp_path: Path) -> None:
    ok_name = "p" * d.MAX_PATH_BYTES
    at_limit = _write_tree(tmp_path / "ok", {d.MANIFEST_NAME: _MANIFEST, ok_name: b"x"})
    d.scan_and_hash(at_limit)
    over_name = "p" * (d.MAX_PATH_BYTES + 1)
    over = _write_tree(tmp_path / "over", {d.MANIFEST_NAME: _MANIFEST, over_name: b"x"})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(over)
    assert excinfo.value.code is FailureCode.F2


def test_t07_depth_boundary(tmp_path: Path) -> None:
    ok_rel = "/".join(["d"] * d.MAX_DEPTH) + "/f.txt"
    at_limit = _write_tree(tmp_path / "ok", {d.MANIFEST_NAME: _MANIFEST, ok_rel: b"x"})
    d.scan_and_hash(at_limit)
    over_rel = "/".join(["d"] * (d.MAX_DEPTH + 1)) + "/f.txt"
    over = _write_tree(tmp_path / "over", {d.MANIFEST_NAME: _MANIFEST, over_rel: b"x"})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(over)
    assert excinfo.value.code is FailureCode.F2


def test_t07_signature_size_boundary(tmp_path: Path) -> None:
    # The signature is excluded from the digest but still size-checked on read.
    at_limit = _write_tree(
        tmp_path / "ok",
        {d.MANIFEST_NAME: _MANIFEST, d.SIGNATURE_NAME: b"s" * d.MAX_SIGNATURE_BYTES},
    )
    assert d.scan_and_hash(at_limit).signature_bytes == b"s" * d.MAX_SIGNATURE_BYTES
    over = _write_tree(
        tmp_path / "over",
        {d.MANIFEST_NAME: _MANIFEST, d.SIGNATURE_NAME: b"s" * (d.MAX_SIGNATURE_BYTES + 1)},
    )
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(over)
    assert excinfo.value.code is FailureCode.F3


def test_missing_source_is_f1(tmp_path: Path) -> None:
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tmp_path / "does-not-exist")
    assert excinfo.value.code is FailureCode.F1


def test_file_count_limit_fails_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d, "MAX_FILES", 2)
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: _MANIFEST, "a": b"1", "b": b"2"})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F3


def test_total_byte_budget_aborts_mid_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d, "MAX_TOTAL_BYTES", 5)
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: b"m", "big.txt": b"x" * 8})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F3


def test_leading_dot_name_rejected(tmp_path: Path) -> None:
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: _MANIFEST, ".DS_Store": b"junk"})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F2


def test_bytecode_cache_dir_rejected(tmp_path: Path) -> None:
    tree = _write_tree(
        tmp_path / "pkg",
        {d.MANIFEST_NAME: _MANIFEST, "__pycache__/mod.cpython-312.pyc": b"\x00bytecode"},
    )
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F2


def test_loose_bytecode_file_rejected(tmp_path: Path) -> None:
    tree = _write_tree(tmp_path / "pkg", {d.MANIFEST_NAME: _MANIFEST, "mod.pyc": b"\x00bytecode"})
    with pytest.raises(PackageError) as excinfo:
        d.scan_and_hash(tree)
    assert excinfo.value.code is FailureCode.F2


def test_copy_tree_rejects_bytecode(tmp_path: Path) -> None:
    src = _write_tree(
        tmp_path / "src",
        {d.MANIFEST_NAME: _MANIFEST, "__pycache__/mod.cpython-312.pyc": b"\x00bytecode"},
    )
    with pytest.raises(PackageError) as excinfo:
        d.copy_tree(src, tmp_path / "dest")
    assert excinfo.value.code is FailureCode.F2


def test_copy_tree_rejects_symlink(tmp_path: Path) -> None:
    src = _write_tree(tmp_path / "src", {d.MANIFEST_NAME: _MANIFEST, "f.txt": b"x"})
    (src / "link.txt").symlink_to(src / "f.txt")
    with pytest.raises(PackageError) as excinfo:
        d.copy_tree(src, tmp_path / "dest")
    assert excinfo.value.code is FailureCode.F2


def test_copy_tree_roundtrip_matches_source_digest(tmp_path: Path) -> None:
    src = _write_tree(
        tmp_path / "src",
        {d.MANIFEST_NAME: _MANIFEST, "a/b.txt": b"hello", "c.txt": b"world", d.SIGNATURE_NAME: b"sig"},
    )
    dest = tmp_path / "dest"
    d.copy_tree(src, dest)
    assert d.scan_and_hash(dest).package_digest == d.scan_and_hash(src).package_digest
    assert (dest / "a" / "b.txt").read_bytes() == b"hello"
    assert (dest / d.SIGNATURE_NAME).read_bytes() == b"sig"  # copied even though digest-excluded
