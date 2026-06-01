"""Read-only integration smoke tests against a real local Garage.

These tests run only when ``STORM_PULSE_GARAGE_TEST_ENDPOINT`` is set in
the environment. Otherwise they're skipped - most CI runs won't have a
Garage available, and the SigV4 + parsing unit tests already cover the
cryptographic and response-shape correctness in isolation.

The destructive end-to-end test (list + delete + verify empty) lives in
the Issue 4 clear-bucket test suite, where it has appropriate scaffolding.
This file is the lighter "the wire format actually works" smoke test.

To run locally::

    STORM_PULSE_GARAGE_TEST_ENDPOINT=http://localhost:3900 \\
    STORM_PULSE_GARAGE_TEST_REGION=garage \\
    STORM_PULSE_GARAGE_TEST_ACCESS_KEY=GK... \\
    STORM_PULSE_GARAGE_TEST_SECRET_KEY=... \\
    STORM_PULSE_GARAGE_TEST_BUCKET=test-bucket \\
        pytest tests/garage/test_s3_integration.py
"""

from __future__ import annotations

import os

import pytest

from stormpulse.garage.s3 import GarageS3Client, S3AuthError

_ENDPOINT = os.environ.get("STORM_PULSE_GARAGE_TEST_ENDPOINT")
_REGION = os.environ.get("STORM_PULSE_GARAGE_TEST_REGION", "garage")
_ACCESS_KEY = os.environ.get("STORM_PULSE_GARAGE_TEST_ACCESS_KEY")
_SECRET_KEY = os.environ.get("STORM_PULSE_GARAGE_TEST_SECRET_KEY")
_BUCKET = os.environ.get("STORM_PULSE_GARAGE_TEST_BUCKET")


_skip_unless_garage = pytest.mark.skipif(
    not all([_ENDPOINT, _ACCESS_KEY, _SECRET_KEY, _BUCKET]),
    reason="Set STORM_PULSE_GARAGE_TEST_* env vars to run integration tests",
)


@pytest.fixture
def client() -> GarageS3Client:
    assert _ENDPOINT and _ACCESS_KEY and _SECRET_KEY  # for the type checker
    return GarageS3Client(
        endpoint=_ENDPOINT,
        region=_REGION,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
    )


@_skip_unless_garage
def test_head_bucket_succeeds_with_valid_creds(client: GarageS3Client) -> None:
    assert _BUCKET is not None
    # No exception means success.
    client.head_bucket(_BUCKET)


@_skip_unless_garage
def test_list_objects_v2_reaches_garage(client: GarageS3Client) -> None:
    """Smoke: list returns a parseable response with the expected fields.

    Doesn't assume the bucket has any specific contents. Just verifies
    the round-trip shape is correct.
    """
    assert _BUCKET is not None
    result = client.list_objects_v2(_BUCKET, max_keys=10)
    # Field types are correct
    assert isinstance(result.contents, list)
    assert isinstance(result.is_truncated, bool)
    assert isinstance(result.key_count, int)


@_skip_unless_garage
def test_head_bucket_rejects_bad_credentials() -> None:
    """A bogus secret should produce a clean S3AuthError, not a generic crash."""
    assert _ENDPOINT and _ACCESS_KEY and _BUCKET
    bad_client = GarageS3Client(
        endpoint=_ENDPOINT,
        region=_REGION,
        access_key=_ACCESS_KEY,
        secret_key="not-the-real-secret-key-deliberately-wrong",
    )
    with pytest.raises(S3AuthError):
        bad_client.head_bucket(_BUCKET)
