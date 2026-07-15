#!/usr/bin/env python3
"""End-to-end smoke test for the P1 external integration loader.

Drives the public CLI through the full lifecycle against a throwaway state dir:
approve a publisher, inspect (before/after trust), install, doctor, tamper, and
revoke. Every step asserts a sentinel file is absent (the package's code would
create it if it were ever imported) and that installed files are genuinely
read-only (an actual append is attempted, not just a mode check). Exits non-zero
on the first failed assertion; prints PRIVATE_LOADER_P1_SMOKE_OK on success.

Run:  .venv/bin/python scripts/private_loader_p1_smoke.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import socket
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stormpulse.integrations import registered_integrations
from stormpulse.integrations.external import digest, trust


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run(state_dir: Path, **fields: Any) -> tuple[int, dict[str, Any]]:
    from stormpulse.cli import integration as cli

    defaults: dict[str, Any] = {
        "json": True,
        "config": "unused",
        "integration_command": None,
        "publisher_command": None,
        "source": None,
        "integration_id": None,
        "key_file": None,
        "label": None,
        "fingerprint": None,
        "confirm_hostname": None,
    }
    defaults.update(fields)
    args = argparse.Namespace(**defaults)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.run(args, state_dir=state_dir, agent_id="smoke-agent")
    text = buffer.getvalue().strip()
    return code, (json.loads(text) if text else {})


def _write_package(pkg: Path, private: Ed25519PrivateKey, fingerprint: str, sentinel: Path) -> str:
    pkg.mkdir(parents=True, exist_ok=True)
    manifest = (
        "schema_version = 1\n\n[integration]\nid = \"obs\"\nversion = \"1.0.0\"\n"
        "entry_module = \"obs.integration\"\n\n[publisher]\n"
        f'fingerprint = "{fingerprint}"\n\n[requests]\ncapabilities = ["integration_load"]\n'
    )
    (pkg / digest.MANIFEST_NAME).write_bytes(manifest.encode())
    # If this module were ever imported, it would create the sentinel.
    (pkg / "code.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n")
    package_digest = digest.scan_and_hash(pkg).package_digest
    payload = trust.signed_payload(package_digest, "obs", "1.0.0")
    signature = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "publisher_fingerprint": fingerprint,
        "package_digest": package_digest,
        "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii"),
    }
    (pkg / digest.SIGNATURE_NAME).write_text(json.dumps(signature))
    return package_digest


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        state = root / "state"
        state.mkdir()
        source = root / "src"
        sentinel = root / "sentinel"
        ids_before = sorted(i.id for i in registered_integrations())

        private = Ed25519PrivateKey.generate()
        raw_pub = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        fingerprint = "sha256:" + hashlib.sha256(raw_pub).hexdigest()
        key_file = root / "key.raw"
        key_file.write_bytes(raw_pub)

        package_digest = _write_package(source, private, fingerprint, sentinel)

        # 1. inspect before approval
        code, payload = _run(state, integration_command="inspect", source=str(source))
        _check(code == 0, "inspect(unknown) exit")
        _check(payload["result"]["trust_status"] == "unknown", "trust unknown before approval")
        _check(payload["result"]["signature_status"] == "unverifiable", "unverifiable before approval")
        _check(not sentinel.exists(), "sentinel after inspect(unknown)")

        # 2. approve publisher
        code, payload = _run(
            state,
            integration_command="publisher",
            publisher_command="add",
            key_file=str(key_file),
            label="smoke key",
            confirm_hostname=socket.gethostname(),
        )
        _check(code == 0, "publisher add exit")
        _check(payload["result"]["fingerprint"] == fingerprint, "publisher fingerprint")

        # 3. inspect after approval
        code, payload = _run(state, integration_command="inspect", source=str(source))
        _check(payload["result"]["trust_status"] == "trusted", "trusted after approval")
        _check(payload["result"]["signature_status"] == "valid", "valid after approval")
        _check(not sentinel.exists(), "sentinel after inspect(trusted)")

        # 4. install
        code, payload = _run(state, integration_command="install", source=str(source))
        _check(code == 0, "install exit")
        _check(payload["result"]["package_digest"] == package_digest, "receipt digest == inspect digest")
        installed = state / "integrations" / payload["result"]["installed_relpath"]
        _check(digest.scan_and_hash(installed).package_digest == package_digest, "installed tree re-hashes")
        try:
            with open(installed / "code.py", "a"):
                pass
            wrote = True
        except OSError:
            wrote = False
        _check(not wrote, "installed file is genuinely read-only (append rejected)")
        _check(not sentinel.exists(), "sentinel after install")

        # 5. doctor: healthy
        code, payload = _run(state, integration_command="doctor")
        _check(code == 0, "doctor(healthy) exit")

        # 6. tamper the source, re-inspect and re-install
        (source / "code.py").write_text("print('tampered')\n")
        code, payload = _run(state, integration_command="inspect", source=str(source))
        _check(payload["result"]["signature_status"] == "invalid", "signature invalid after tamper")
        code, _ = _run(state, integration_command="install", source=str(source))
        _check(code == 4, "tampered install refused (exit 4)")
        _check(digest.scan_and_hash(installed).package_digest == package_digest, "original tree unchanged")

        # 7. revoke publisher -> doctor warns
        _run(
            state,
            integration_command="publisher",
            publisher_command="revoke",
            fingerprint=fingerprint,
            confirm_hostname=socket.gethostname(),
        )
        code, payload = _run(state, integration_command="doctor")
        _check(any(f["code"] == "publisher_revoked" for f in payload["findings"]), "doctor warns revoked")
        _check(not sentinel.exists(), "sentinel after revoke")

        # 8. the integration registry is untouched
        _check(sorted(i.id for i in registered_integrations()) == ids_before, "registry unchanged")

    print("PRIVATE_LOADER_P1_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
