"""Garage wire harness: a real Garage, the real admin API, the real S3 endpoint.

Mocks nothing. The tier's contract lives in ``tests/wire/conftest.py``; this
file is the Garage half of it.

Self-provisioning, like the storm-buckets-guard harness: on first use it mints
its own key and bucket inside the running test Garage via ``docker exec``, and
feeds them to every test. No env vars to set, no secrets to source, nothing
committed.

Provisioning runs **out of band** (the Garage CLI, over ``docker exec``) on
purpose. The admin API is the thing under test; bootstrapping the tests with
it would make a broken admin API look like a broken harness.

Run it::

    make garage-up && make test-garage-wire
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stormpulse.garage.config import GarageConfig
from stormpulse.garage.s3 import GarageS3Client

CONTAINER = "storm-pulse-test-garage"
ADMIN_URL = "http://127.0.0.1:3913"
ADMIN_TOKEN = "storm-pulse-wire-test-token"  # matches docker/garage.toml
S3_ENDPOINT = "http://127.0.0.1:3910"
REGION = "garage"
KEY_NAME = "wire-it-key"

_UP_HINT = (
    f"the test Garage container {CONTAINER!r} is not running.\n"
    "Start it with:  make garage-up\n"
    "(or: docker compose -f docker/garage.test.yml up -d)"
)


# ---------------------------------------------------------------------------
# Out-of-band truth: the Garage CLI, over docker exec
# ---------------------------------------------------------------------------


def garage_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the test Garage's own CLI. The out-of-band truth channel.

    Used to provision, and to read state the admin API also reports, so a test
    can assert the two agree. Never used to assert on behalf of the admin API.
    """
    try:
        return subprocess.run(
            ["docker", "exec", CONTAINER, "/garage", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:  # docker itself missing
        raise RuntimeError(f"`docker` not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`garage {' '.join(args)}` timed out: {exc}") from exc


@dataclass(frozen=True)
class WireEnv:
    """Everything a wire test needs to reach the real Garage."""

    admin_url: str
    admin_token: str
    s3_endpoint: str
    region: str
    access_key: str
    secret_key: str

    @property
    def admin_kwargs(self) -> Any:
        """Splat into any ``stormpulse.garage.admin_api`` call.

        Typed ``Any`` on purpose. Every admin_api function is keyword-only on
        ``admin_url``/``admin_token``, and mypy matches a ``**dict[str, str]``
        splat against EVERY parameter of the target, so a precise type here
        produces a false error on each of ~40 call sites. One documented
        widening in the harness beats repeating two arguments everywhere or
        loosening mypy for the whole test tree.
        """
        return {"admin_url": self.admin_url, "admin_token": self.admin_token}

    def garage_config(self) -> GarageConfig:
        """A GarageConfig pointed at the test cluster.

        ``container_name``/``garage_binary`` match the real container so the
        precondition and CLI-spec paths would resolve, though wire tests drive
        the admin API and S3 directly.
        """
        return GarageConfig(
            enabled=True,
            container_name=CONTAINER,
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=Path("/etc/garage.toml"),
            admin_url=self.admin_url,
            admin_token=self.admin_token,
        )


def _require_container() -> None:
    status = garage_cli("status")
    if status.returncode != 0:
        raise RuntimeError(f"{_UP_HINT}\n\ngarage status said:\n{status.stderr.strip()}")


def _apply_layout_if_unassigned() -> None:
    """Give the single node a role so it can serve S3. Idempotent.

    A fresh container boots with NO ROLE ASSIGNED and every S3 call fails in a
    way that looks like an agent bug. Assign once; on an already-applied
    cluster both calls no-op with a non-zero exit we deliberately ignore.
    """
    status = garage_cli("status")
    if "NO ROLE ASSIGNED" not in status.stdout:
        return
    node = next(
        (
            line.split()[0]
            for line in status.stdout.splitlines()
            if line[:16].strip() and len(line.split()[0]) == 16
        ),
        None,
    )
    if node is None:
        raise RuntimeError(f"could not find the node id in:\n{status.stdout}")
    garage_cli("layout", "assign", "-z", "dc1", "-c", "10G", node)
    garage_cli("layout", "apply", "--version", "1")


def _mint_key() -> tuple[str, str]:
    """Create the harness key if absent, return ``(access_key, secret_key)``.

    Resolved by name through ``key list`` then ``key info --show-secret``,
    because Garage permits duplicate key NAMES: the id is the identity, the
    name never is (the same rule that makes local aliases unusable as ids,
    core/buckets-customer-truth.md).
    """
    listing = garage_cli("key", "list")
    existing = next(
        (
            line.split()[0]
            for line in listing.stdout.splitlines()
            if KEY_NAME in line and line.split() and line.split()[0].startswith("GK")
        ),
        None,
    )
    if existing is None:
        created = garage_cli("key", "create", KEY_NAME)
        if created.returncode != 0:
            raise RuntimeError(f"key create failed:\n{created.stderr}")

    info = garage_cli("key", "info", KEY_NAME, "--show-secret")
    if info.returncode != 0:
        raise RuntimeError(f"key info failed:\n{info.stderr}")
    access_key = secret_key = ""
    for line in info.stdout.splitlines():
        if line.startswith("Key ID:"):
            access_key = line.split(":", 1)[1].strip()
        elif line.startswith("Secret key:"):
            secret_key = line.split(":", 1)[1].strip()
    if not (access_key and secret_key):
        raise RuntimeError(f"could not parse key info:\n{info.stdout}")
    return access_key, secret_key


@pytest.fixture(scope="session")
def wire() -> WireEnv:
    """The provisioned wire environment. Session-scoped: minted once.

    Raises (never skips) when the container is down, naming the command that
    fixes it.
    """
    _require_container()
    _apply_layout_if_unassigned()
    access_key, secret_key = _mint_key()
    return WireEnv(
        admin_url=ADMIN_URL,
        admin_token=ADMIN_TOKEN,
        s3_endpoint=S3_ENDPOINT,
        region=REGION,
        access_key=access_key,
        secret_key=secret_key,
    )


@pytest.fixture
def s3(wire: WireEnv) -> GarageS3Client:
    """The agent's own S3 client, pointed at the real endpoint."""
    return GarageS3Client(
        endpoint=wire.s3_endpoint,
        region=wire.region,
        access_key=wire.access_key,
        secret_key=wire.secret_key,
    )


@dataclass(frozen=True)
class WireBucket:
    """A test bucket's two identities, which are not interchangeable.

    ``name`` is the global alias, the only thing S3 addresses. ``id`` is
    Garage's full 64-char bucket id, the only thing the admin API's mutating
    endpoints accept (Storm's 16-char id is its prefix). Conflating them is
    the alias-as-id bug; keeping both on one object makes each call site say
    which one it means.
    """

    name: str
    id: str


@pytest.fixture
def bucket(wire: WireEnv) -> Iterator[WireBucket]:
    """A fresh bucket the harness key owns, torn down after the test.

    Per-test and uniquely named: wire tests mutate real state, so sharing one
    bucket would make them order-dependent. Teardown drains before deleting,
    since Garage refuses a non-empty DeleteBucket.
    """
    name = f"wire-{uuid.uuid4().hex[:12]}"
    created = garage_cli("bucket", "create", name)
    if created.returncode != 0:
        raise RuntimeError(f"bucket create failed:\n{created.stderr}")
    garage_cli(
        "bucket", "allow", "--read", "--write", "--owner", name,
        "--key", wire.access_key,
    )
    full_id = bucket_info_cli(name).get("Bucket", "")
    if len(full_id) != 64:
        raise RuntimeError(f"could not read the bucket id for {name}: {full_id!r}")
    try:
        yield WireBucket(name=name, id=full_id)
    finally:
        client = GarageS3Client(
            endpoint=wire.s3_endpoint, region=wire.region,
            access_key=wire.access_key, secret_key=wire.secret_key,
        )
        try:
            page = client.list_objects_v2(name, max_keys=1000)
            if page.contents:
                client.delete_objects(name, [o.key for o in page.contents])
            # In-flight uploads survive an object delete and are invisible to
            # the list, so a test that leaves one would leak bytes into every
            # later run's cluster.
            for upload in client.list_multipart_uploads(name).uploads:
                client.abort_multipart_upload(name, upload.key, upload.upload_id)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        garage_cli("bucket", "delete", "--yes", name)


def put_object(wire: WireEnv, bucket: str, key: str, body: bytes) -> None:
    """Upload one object with curl's native SigV4.

    The agent's S3 client is read-and-delete only (it never uploads), so
    seeding real objects needs an outside tool. curl speaks SigV4 natively;
    no aws-cli dependency, matching the guard's ad-hoc probe convention.
    """
    proc = subprocess.run(
        [
            "curl", "-sS", "--fail-with-body",
            "--aws-sigv4", f"aws:amz:{wire.region}:s3",
            "--user", f"{wire.access_key}:{wire.secret_key}",
            "-X", "PUT", "--data-binary", "@-",
            f"{wire.s3_endpoint}/{bucket}/{key}",
        ],
        input=body,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"seeding {bucket}/{key} failed: {proc.stderr.decode(errors='replace')}"
        )


def put_object_with_declared_hash(
    wire: WireEnv, bucket: str, key: str, body: bytes, declared_sha256: str
) -> int:
    """PUT an object declaring ``declared_sha256`` as the payload hash.

    Returns the HTTP status. Used to pin whether Garage cross-checks the
    declared ``x-amz-content-sha256`` against the bytes it actually received.
    """
    proc = subprocess.run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "--aws-sigv4", f"aws:amz:{wire.region}:s3",
            "--user", f"{wire.access_key}:{wire.secret_key}",
            "-H", f"x-amz-content-sha256: {declared_sha256}",
            "-X", "PUT", "--data-binary", "@-",
            f"{wire.s3_endpoint}/{bucket}/{key}",
        ],
        input=body,
        capture_output=True,
        timeout=30,
    )
    return int(proc.stdout.decode().strip() or 0)


def start_upload(wire: WireEnv, bucket: str, key: str) -> str:
    """Initiate a multipart upload and return its UploadId.

    The agent never uploads, so seeding an in-flight upload needs an outside
    tool. curl speaks SigV4 natively.
    """
    proc = subprocess.run(
        [
            "curl", "-sS", "--fail-with-body",
            "--aws-sigv4", f"aws:amz:{wire.region}:s3",
            "--user", f"{wire.access_key}:{wire.secret_key}",
            "-X", "POST", f"{wire.s3_endpoint}/{bucket}/{key}?uploads",
        ],
        capture_output=True,
        timeout=30,
    )
    body = proc.stdout.decode(errors="replace")
    match = re.search(r"<UploadId>([^<]+)</UploadId>", body)
    if match is None:
        raise RuntimeError(f"no UploadId in CreateMultipartUpload response:\n{body}")
    return match.group(1)


def upload_part(
    wire: WireEnv, bucket: str, key: str, upload_id: str, body: bytes
) -> int:
    """Upload one part of an in-flight multipart upload. Returns the status."""
    proc = subprocess.run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "--aws-sigv4", f"aws:amz:{wire.region}:s3",
            "--user", f"{wire.access_key}:{wire.secret_key}",
            "-X", "PUT", "--data-binary", "@-",
            f"{wire.s3_endpoint}/{bucket}/{key}"
            f"?partNumber=1&uploadId={upload_id}",
        ],
        input=body,
        capture_output=True,
        timeout=60,
    )
    return int(proc.stdout.decode().strip() or 0)


def abort_upload(wire: WireEnv, bucket: str, key: str, upload_id: str) -> None:
    """Abort an in-flight upload. Used by tests to clean up after themselves."""
    GarageS3Client(
        endpoint=wire.s3_endpoint, region=wire.region,
        access_key=wire.access_key, secret_key=wire.secret_key,
    ).abort_multipart_upload(bucket, key, upload_id)


def bucket_info_cli(name: str) -> dict[str, str]:
    """Parse ``garage bucket info`` into a field map. Out-of-band truth."""
    out = garage_cli("bucket", "info", name)
    fields: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


def unique_alias(prefix: str = "alias") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def pretty(obj: object) -> str:
    """Render a response for an assertion message, so a shape change reads."""
    return json.dumps(obj, indent=2, sort_keys=True, default=str)[:2000]
