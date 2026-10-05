"""Function 14 (no publisher signing key in the agent) catches each drift it exists for."""

from __future__ import annotations

from pathlib import Path

from fitness.no_signing_key import check_no_signing_key


def _tree(tmp_path: Path, source: str) -> Path:
    pkg = tmp_path / "stormpulse"
    pkg.mkdir()
    (pkg / "mod.py").write_text(source, encoding="utf-8")
    return pkg


def test_live_agent_is_clean() -> None:
    assert check_no_signing_key() == []


def test_importing_the_signer_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, "import os\nfrom authoring.signer import sign_tree\n")
    assert check_no_signing_key(root) == [
        "stormpulse/mod.py:2 imports the release-side signer"
    ]


def test_naming_an_ed25519_private_key_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, "from x import ed25519\nk = ed25519.Ed25519PrivateKey\n")
    assert check_no_signing_key(root) == [
        "stormpulse/mod.py:2 names an Ed25519 private key"
    ]


def test_the_agents_own_transport_key_passes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        "from cryptography.hazmat.primitives import serialization\n"
        "key = serialization.load_pem_private_key(b'', password=None)\n"
        "import authoring_notes\n",
    )
    assert check_no_signing_key(root) == []
