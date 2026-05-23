"""CSR-based certificate provisioning."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)


class EnrollError(Exception):
    """Raised when enrollment fails."""


@dataclass(frozen=True, slots=True)
class Credentials:
    """Paths to the written credential files."""

    client_cert: Path
    client_key: Path
    ca_cert: Path
    hmac_key: Path


def generate_keypair() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    """Generate an EC P-256 keypair for mTLS client authentication.

    Returns the private key object and its PEM-encoded bytes.
    The private key must never leave this machine.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_key, key_pem


def build_csr(private_key: ec.EllipticCurvePrivateKey, agent_id: str) -> bytes:
    """Build a PEM-encoded CSR with CN=agent_id.

    The CSR is signed with the private key to prove possession.
    """
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, agent_id)])
        )
        .sign(private_key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


def _friendly_http_error(exc: urllib.error.HTTPError) -> str:
    """Turn an HTTP error into an actionable message."""
    detail = ""
    if exc.fp:
        try:
            raw = exc.fp.read(1024).decode("utf-8", errors="replace")
            body = json.loads(raw)
            detail = body.get("error", raw)
        except (json.JSONDecodeError, ValueError):
            detail = raw
        except Exception:  # noqa: BLE001
            pass

    hints: dict[int, str] = {
        400: "Check that agent_id matches what the dashboard expects.",
        401: "Is the enrollment token correct? Tokens are single-use.",
        403: "Token may have already been used or expired. "
             "Generate a new one in the dashboard admin.",
        404: "Enrollment endpoint not found. "
             "Is the dashboard updated to support enrollment?",
        409: "This agent_id is already enrolled. "
             "Revoke the existing enrollment in the dashboard first.",
        429: "Too many enrollment attempts. Wait and try again.",
    }
    hint = hints.get(exc.code, "")

    parts = [f"Enrollment failed (HTTP {exc.code})"]
    if detail:
        parts.append(detail)
    if hint:
        parts.append(hint)
    return ". ".join(parts) + "."


def request_certificate(
    endpoint: str,
    agent_id: str,
    token: str,
    csr_pem: bytes,
) -> dict[str, str]:
    """POST the CSR to the enrollment endpoint and return credentials.

    Returns a dict with keys: client_cert_pem, ca_cert_pem, hmac_key.
    Raises EnrollError on any failure.
    """
    if endpoint.startswith("http://"):
        logger.warning(
            "Enrollment endpoint uses plain HTTP - credentials will be sent "
            "unencrypted. Use https:// in production."
        )

    body = json.dumps({
        "agent_id": agent_id,
        "token": token,
        "csr_pem": csr_pem.decode("ascii"),
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data: dict[str, str] = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise EnrollError(_friendly_http_error(exc)) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if exc.reason else str(exc)
        raise EnrollError(
            f"Cannot reach {endpoint} - {reason}. "
            f"Is the dashboard running? Is the URL correct?"
        ) from exc
    except OSError as exc:
        raise EnrollError(
            f"Network error connecting to {endpoint}: {exc}"
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise EnrollError(
            f"Dashboard returned invalid JSON. "
            f"Is {endpoint} the correct enrollment URL?"
        ) from exc

    for key in ("client_cert_pem", "ca_cert_pem", "hmac_key"):
        if key not in data:
            raise EnrollError(
                f"Enrollment response missing '{key}'. "
                f"The dashboard may be running an older version."
            )

    return data


def _write_file(path: Path, data: bytes, mode: int) -> None:
    """Write data and set permissions atomically.

    Writes to a .tmp file, sets permissions, then renames - so the
    target path never exists with wrong permissions.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        tmp.rename(path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise EnrollError(
            f"Permission denied writing {path}. "
            f"Run enrollment with sudo: sudo stormpulse enroll ..."
        ) from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise EnrollError(f"Failed to write {path}: {exc}") from exc


def write_credentials(
    creds_dir: Path,
    key_pem: bytes,
    response: dict[str, str],
    *,
    force: bool = False,
) -> Credentials:
    """Write credential files with appropriate permissions.

    Private key and HMAC key: 0o640 root:stormpulse (group-readable).
    Certificates: 0o644 root:stormpulse (world-readable, not secret).
    Creates creds_dir if it does not exist (mode 0o700).
    If the directory already exists, its permissions are left unchanged.

    Ownership is set to root:stormpulse so the agent can read at runtime.
    Falls back silently if the stormpulse group does not exist (e.g. in tests).

    Raises EnrollError if credential files already exist and force is False.
    """
    existed = creds_dir.is_dir()
    creds_dir.mkdir(parents=True, exist_ok=True)
    if not existed:
        os.chmod(creds_dir, 0o700)

    paths = Credentials(
        client_cert=creds_dir / "agent.pem",
        client_key=creds_dir / "agent-key.pem",
        ca_cert=creds_dir / "ca.pem",
        hmac_key=creds_dir / "hmac.key",
    )

    if not force:
        existing = [p for p in (paths.client_cert, paths.client_key, paths.ca_cert, paths.hmac_key) if p.exists()]
        if existing:
            names = ", ".join(p.name for p in existing)
            raise EnrollError(
                f"Credential files already exist: {names}. "
                f"Use --force to overwrite, or revoke the old enrollment first."
            )

    try:
        hmac_bytes = base64.b64decode(response["hmac_key"])
    except (binascii.Error, ValueError) as exc:
        raise EnrollError(
            f"Dashboard returned an invalid HMAC key (bad base64). "
            f"This may indicate a dashboard bug - contact the admin."
        ) from exc

    _write_file(paths.client_key, key_pem, 0o640)
    _write_file(paths.hmac_key, hmac_bytes, 0o640)
    _write_file(paths.client_cert, response["client_cert_pem"].encode("ascii"), 0o644)
    _write_file(paths.ca_cert, response["ca_cert_pem"].encode("ascii"), 0o644)

    for p in (paths.client_key, paths.hmac_key, paths.client_cert, paths.ca_cert):
        try:
            shutil.chown(p, "root", "stormpulse")
        except (LookupError, PermissionError):
            pass  # stormpulse group may not exist (e.g. tests, dev machines)

    return paths


def write_enroll_metadata(
    creds_dir: Path,
    endpoint: str,
    agent_id: str,
) -> Path:
    """Write enrollment metadata for use by ``stormpulse init``.

    Stores the enrollment endpoint and agent ID so that ``init`` can derive
    a default dashboard WebSocket URL without prompting blindly.

    Returns the path to the written file.
    """
    meta = {"endpoint": endpoint, "agent_id": agent_id}
    data = json.dumps(meta, indent=2).encode("utf-8") + b"\n"
    path = creds_dir / "enroll.json"
    _write_file(path, data, 0o644)
    return path
