"""CSR-based certificate provisioning."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from stormpulse.config import TlsConfig
from stormpulse.init.mode import InstallMode, detect_mode

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

    Returns a dict with required credential keys and, on newer dashboards,
    an optional ``dashboard_url`` used by ``stormpulse init``.
    Raises EnrollError on any failure.
    """
    if endpoint.startswith("http://"):
        logger.warning(
            "Enrollment endpoint uses plain HTTP - credentials will be sent "
            "unencrypted. Use https:// in production."
        )

    body = json.dumps(
        {
            "agent_id": agent_id,
            "token": token,
            "csr_pem": csr_pem.decode("ascii"),
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with (
            urllib.request.urlopen(  # skylos: ignore[SKY-D216] operator-supplied endpoint, https enforced above
                req, timeout=30
            ) as resp
        ):
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
        raise EnrollError(f"Network error connecting to {endpoint}: {exc}") from exc
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

    dashboard_url = data.get("dashboard_url")
    if dashboard_url is not None and (
        not isinstance(dashboard_url, str)
        or not dashboard_url.startswith(("wss://", "ws://"))
    ):
        raise EnrollError(
            "Enrollment response has invalid 'dashboard_url'. "
            "Expected a wss:// or ws:// WebSocket URL."
        )

    return data


def preflight_creds_dir(creds_dir: Path) -> None:
    """Verify ``creds_dir`` is writable before any network calls.

    The enrollment endpoint marks the token used the moment it signs
    the CSR. If the subsequent local write fails (wrong default path,
    permissions), the token is burned and the operator has to issue
    a new one. Catching the local failure here -- before the POST --
    keeps the token reusable on retry.

    Creates the directory at mode 0o700 if missing. If it already
    exists, leaves its permissions alone. Writes and deletes a marker
    file to confirm the dir is actually writable (mkdir alone doesn't
    catch a read-only fs).
    """
    try:
        existed = creds_dir.is_dir()
        creds_dir.mkdir(parents=True, exist_ok=True)
        if not existed:
            os.chmod(creds_dir, 0o700)
    except PermissionError as exc:
        raise EnrollError(
            f"Permission denied creating {creds_dir}. "
            f"Pass --creds-dir to write somewhere your user owns, "
            f"e.g. --creds-dir ~/.config/stormpulse."
        ) from exc
    except OSError as exc:
        raise EnrollError(
            f"Cannot create {creds_dir}: {exc}. "
            f"Pass --creds-dir to choose a writable location."
        ) from exc

    marker = creds_dir / ".stormpulse-write-test"
    try:
        marker.write_bytes(b"")
        marker.unlink()
    except OSError as exc:
        raise EnrollError(
            f"{creds_dir} exists but is not writable: {exc}. "
            f"Pass --creds-dir to choose a writable location."
        ) from exc


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
        existing = [
            p
            for p in (
                paths.client_cert,
                paths.client_key,
                paths.ca_cert,
                paths.hmac_key,
            )
            if p.exists()
        ]
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
            "Dashboard returned an invalid HMAC key (bad base64). "
            "This may indicate a dashboard bug - contact the admin."
        ) from exc

    _write_file(paths.client_key, key_pem, 0o640)
    _write_file(paths.hmac_key, hmac_bytes, 0o640)
    _write_file(paths.client_cert, response["client_cert_pem"].encode("ascii"), 0o644)
    _write_file(paths.ca_cert, response["ca_cert_pem"].encode("ascii"), 0o644)

    # SYSTEM mode: chown to root:stormpulse so the agent (running as the
    # stormpulse system user) can read the cred files. USER mode: the
    # operator owns the files by virtue of having written them; chown would
    # silently fail and confuse anyone reading the comment block above.
    if os.geteuid() == 0:
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
    dashboard_url: str | None = None,
) -> Path:
    """Write enrollment metadata for use by ``stormpulse init``.

    Stores the enrollment endpoint and agent ID. Newer dashboards also return
    an explicit WebSocket URL; older dashboards omit it and ``init`` retains
    the same-host derivation as a compatibility fallback.

    Returns the path to the written file.
    """
    meta = {"endpoint": endpoint, "agent_id": agent_id}
    if dashboard_url:
        meta["dashboard_url"] = dashboard_url
    data = json.dumps(meta, indent=2).encode("utf-8") + b"\n"
    path = creds_dir / "enroll.json"
    _write_file(path, data, 0o644)
    return path


# --- Renewal (CORE-010): the agent replaces its own client cert over mTLS ---

# Under the 20s ping timeout, so a stalled POST reads as a failure, not a hang.
RENEW_TIMEOUT_SECONDS = 15.0
RENEW_WINDOW = timedelta(days=30)
URGENT_WINDOW = timedelta(days=14)
# A cert issued "now" by the control plane must not read as not-yet-valid here.
_CLOCK_SKEW = timedelta(minutes=5)


class RenewError(Exception):
    """Raised when one renewal attempt fails; ``reason`` is a closed enum.

    Events and the CLI branch on ``reason``, never on the message text.
    """

    #: Every value ``reason`` (and a cert event's reason) may take.
    REASONS = frozenset(  # skylos: ignore - documented closed enum; the contract, not a consumer
        {
            "creds_not_writable",
            "unreachable",
            "endpoint_missing",
            "refused",
            "http_error",
            "bad_response",
            "cert_unreadable",
            "unspecified",
        }
    )

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


def renew_endpoint(dashboard_url: str) -> str:
    """Derive ``https://<pulse host>/api/renew/`` from ``[dashboard] url``."""
    parts = urllib.parse.urlsplit(dashboard_url)
    scheme = "http" if parts.scheme == "ws" else "https"
    return urllib.parse.urlunsplit((scheme, parts.netloc, "/api/renew/", "", ""))


def _load_cert(cert_path: Path) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None


def read_cert_not_after(cert_path: Path) -> datetime | None:
    """Return the cert's ``notAfter`` (UTC), or None if it cannot be read."""
    cert = _load_cert(cert_path)
    return cert.not_valid_after_utc if cert else None


def read_cert_serial(cert_path: Path) -> int | None:
    """Return the cert's serial number, or None if it cannot be read."""
    cert = _load_cert(cert_path)
    return cert.serial_number if cert else None


def days_remaining(not_after: datetime, now: datetime) -> int:
    """Whole days left on a cert: the one figure every surface shows (decision 4)."""
    return (not_after - now).days


# The last good pair sits beside the live one; boot falls back to it.
PREV_SUFFIX = ".prev"


def prev_path(live: Path) -> Path:
    """The ``.prev`` sibling of a live credential file."""
    return live.with_name(live.name + PREV_SUFFIX)


def pending_key_path(client_key: Path) -> Path:
    """The pending key sits beside the live one: ``agent-key.pem.new``."""
    return client_key.with_name(client_key.name + ".new")


def _not_writable(message: str) -> RenewError:
    return RenewError(message, reason="creds_not_writable")


def _bad_response(message: str) -> RenewError:
    return RenewError(message, reason="bad_response")


def _load_or_create_pending_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Reuse a stored pending key, else write a fresh one before any request.

    Reuse is what lets a retry after a lost response present the same
    public key (CORE-010 decision 2).
    """
    if path.exists():
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, ValueError):
            logger.warning("Pending key %s is unreadable; replacing it", path)
        else:
            if isinstance(key, ec.EllipticCurvePrivateKey):
                return key
            logger.warning("Pending key %s is not an EC key; replacing it", path)
    private_key, key_pem = generate_keypair()
    try:
        _write_file(path, key_pem, 0o600)
    except EnrollError as exc:
        raise _not_writable(f"Cannot write {path}: {exc.__cause__}") from exc
    return private_key


def _http_reason(code: int) -> str:
    if code == 404:
        return "endpoint_missing"
    if code in (401, 403):
        return "refused"
    return "http_error"


def _post_csr(endpoint: str, csr_pem: bytes, ssl_context: ssl.SSLContext) -> object:
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"csr_pem": csr_pem.decode()}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with (
            urllib.request.urlopen(  # skylos: ignore[SKY-D216] host from the configured transport URL
                req, timeout=RENEW_TIMEOUT_SECONDS, context=ssl_context
            ) as resp
        ):
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RenewError(
            f"Renewal refused by {endpoint} (HTTP {exc.code})",
            reason=_http_reason(exc.code),
        ) from exc
    except OSError as exc:  # URLError, timeouts and TLS failures alike
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise RenewError(
            f"Cannot reach {endpoint}: {cause}", reason="unreachable"
        ) from exc
    except ValueError as exc:
        raise _bad_response(f"{endpoint} returned invalid JSON") from exc


def _issued_by(cert: x509.Certificate, ca_path: Path) -> bool:
    try:
        cas = x509.load_pem_x509_certificates(ca_path.read_bytes())
    except (OSError, ValueError):
        return False
    for ca in cas:
        try:
            cert.verify_directly_issued_by(ca)
        except (ValueError, TypeError, InvalidSignature):
            continue
        return True
    return False


def _issued_cert(
    data: object, private_key: ec.EllipticCurvePrivateKey, tls: TlsConfig, agent_id: str
) -> bytes:
    """The response's cert PEM, refused unless the TLS terminator would take it."""
    cert_pem = data.get("client_cert_pem") if isinstance(data, dict) else None
    if not isinstance(cert_pem, str):
        raise _bad_response("Renewal response has no 'client_cert_pem'")
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
    except ValueError as exc:
        raise _bad_response("Renewal response has an unparseable certificate") from exc
    if cert.public_key() != private_key.public_key():
        raise _bad_response("Renewal response certifies a different key")
    if not _issued_by(cert, tls.ca_cert):
        raise _bad_response(f"Renewal response is not signed by {tls.ca_cert}")
    now = datetime.now(UTC)
    if not cert.not_valid_before_utc - _CLOCK_SKEW <= now < cert.not_valid_after_utc:
        raise _bad_response("Renewal response is outside its validity window")
    cns = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    if [cn.value for cn in cns] != [agent_id]:
        raise _bad_response(f"Renewal response is not issued to {agent_id}")
    return cert_pem.encode()


def renew_certificate(
    tls: TlsConfig, agent_id: str, dashboard_url: str, ssl_context: ssl.SSLContext
) -> bytes:
    """Make one renewal attempt, install the pair, and return the issued cert PEM.

    ``ssl_context`` presents the current cert (CORE-010 decision 5). Blocks
    on the network: agent callers run it off the event loop. Any failure
    raises ``RenewError``; ``ca_cert_pem`` in the response is ignored.
    """
    creds_dir = tls.client_key.parent
    if detect_mode() is InstallMode.SYSTEM or not os.access(
        creds_dir, os.W_OK | os.X_OK
    ):  # root would leave a key the stormpulse user cannot read (decision 1)
        raise _not_writable(
            f"{creds_dir} is not writable by the agent; renewal needs user mode. "
            f"Re-enroll this node by hand."
        )
    endpoint = renew_endpoint(dashboard_url)
    private_key = _load_or_create_pending_key(pending_key_path(tls.client_key))
    data = _post_csr(endpoint, build_csr(private_key, agent_id), ssl_context)
    cert_pem = _issued_cert(data, private_key, tls, agent_id)
    install_renewed_pair(tls, cert_pem)
    return cert_pem


def _is_whole_pair(cert_path: Path, key_path: Path) -> bool:
    cert = _load_cert(cert_path)
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return False
    return cert is not None and cert.public_key() == key.public_key()


def complete_pending_swap(tls: TlsConfig) -> bool:
    """Finish a swap cut off between its two live renames; True if it did.

    A live cert that certifies the pending key means the cert went live and
    the key did not (CORE-010 decision 3): roll forward, never back.
    """
    pending = pending_key_path(tls.client_key)
    if not pending.exists() or not _is_whole_pair(tls.client_cert, pending):
        return False
    try:
        os.replace(pending, tls.client_key)
    except OSError as exc:
        logger.error("Cannot finish the cert swap into %s: %s", tls.client_key, exc)
        return False
    logger.warning("Finished an interrupted cert swap into %s", tls.client_key)
    return True


def presented_cert(tls: TlsConfig) -> Path:
    """The cert a booting agent presents: live if it can be made whole, else ``.prev``."""
    live, key = tls.client_cert, tls.client_key
    if _is_whole_pair(live, key) or _is_whole_pair(live, pending_key_path(key)):
        return live
    if _is_whole_pair(prev_path(live), prev_path(key)):
        return prev_path(live)
    return live


def install_renewed_pair(tls: TlsConfig, cert_pem: bytes) -> None:
    """Swap the pending pair live, keeping the old one whole as ``.prev``.

    ``.prev`` is snapshotted only from a live pair that matches, so a crash
    mid-swap never overwrites the last good pair (CORE-010 decision 3).
    The cert goes live before the key: ``complete_pending_swap`` finishes a cut.
    """
    live_cert, live_key = tls.client_cert, tls.client_key
    new_cert = live_cert.with_name(live_cert.name + ".new")
    new_key = pending_key_path(live_key)
    try:
        _write_file(new_cert, cert_pem, 0o644)
        if _is_whole_pair(live_cert, live_key):
            for live in (live_cert, live_key):  # hard link: live never goes missing
                tmp = live.with_name(live.name + PREV_SUFFIX + ".tmp")
                tmp.unlink(missing_ok=True)
                os.link(live, tmp)
                os.replace(tmp, prev_path(live))
        else:
            logger.warning("Live pair %s is not whole; keeping .prev", live_cert)
        os.replace(new_cert, live_cert)
        os.replace(new_key, live_key)
    except EnrollError as exc:
        raise _not_writable(f"Cannot install {new_cert}: {exc.__cause__}") from exc
    except OSError as exc:
        raise _not_writable(f"Cannot install the renewed pair: {exc}") from exc
