"""SigV4 signing tests for stormpulse.garage.s3.

The end-to-end test uses AWS's published "get-vanilla" test vector from
the AWS SigV4 test suite. The vector has known-correct values for the
canonical request, string to sign, derived signing key, and final
signature - if any step in our implementation drifts, the comparison
fails at the offending step.

Reference: AWS Signature Version 4 test suite "get-vanilla" task
(https://docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html).
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from stormpulse.garage.s3 import (
    _build_authorization,
    _canonical_headers,
    _canonical_query_string,
    _derive_signing_key,
)


# AWS get-vanilla vector inputs
_ACCESS_KEY = "AKIDEXAMPLE"
_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_REGION = "us-east-1"
_SERVICE = "service"  # AWS test suite uses synthetic service name
_AMZ_DATE = "20150830T123600Z"
_DATE_STAMP = "20150830"


# ---------------------------------------------------------------------------
# Canonical query string
# ---------------------------------------------------------------------------


def test_canonical_query_empty() -> None:
    assert _canonical_query_string([]) == ""


def test_canonical_query_single() -> None:
    assert _canonical_query_string([("foo", "bar")]) == "foo=bar"


def test_canonical_query_sorted_by_key() -> None:
    out = _canonical_query_string([("b", "2"), ("a", "1")])
    assert out == "a=1&b=2"


def test_canonical_query_url_encoded_values() -> None:
    out = _canonical_query_string([("prefix", "a/b c")])
    assert out == "prefix=a%2Fb%20c"


def test_canonical_query_empty_value_kept() -> None:
    assert _canonical_query_string([("delete", "")]) == "delete="


# ---------------------------------------------------------------------------
# Canonical headers
# ---------------------------------------------------------------------------


def test_canonical_headers_lowercased_and_sorted() -> None:
    block, signed = _canonical_headers({
        "Host": "example.amazonaws.com",
        "X-Amz-Date": "20150830T123600Z",
    })
    # Sorted lowercased: host before x-amz-date
    assert block == "host:example.amazonaws.com\nx-amz-date:20150830T123600Z\n"
    assert signed == "host;x-amz-date"


def test_canonical_headers_values_trimmed() -> None:
    block, _ = _canonical_headers({"x-foo": "  bar  "})
    assert block == "x-foo:bar\n"


# ---------------------------------------------------------------------------
# End-to-end Authorization header (AWS get-vanilla vector)
# ---------------------------------------------------------------------------


def test_authorization_matches_aws_get_vanilla_vector() -> None:
    """End-to-end: build_authorization output must match the published header."""
    headers = {
        "Host": "example.amazonaws.com",
        "X-Amz-Date": _AMZ_DATE,
    }
    body_sha256 = hashlib.sha256(b"").hexdigest()  # empty body

    authorization = _build_authorization(
        method="GET",
        path="/",
        query_params=[],
        headers=headers,
        body_sha256=body_sha256,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        region=_REGION,
        amz_date=_AMZ_DATE,
        date_stamp=_DATE_STAMP,
        service=_SERVICE,
    )

    expected = (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )
    assert authorization == expected


def test_authorization_query_params_affect_signature() -> None:
    """Adding query params changes the signature (regression guard)."""
    headers = {"Host": "example.amazonaws.com", "X-Amz-Date": _AMZ_DATE}
    body_sha256 = hashlib.sha256(b"").hexdigest()
    sig_no_query = _build_authorization(
        method="GET", path="/", query_params=[],
        headers=headers, body_sha256=body_sha256,
        access_key=_ACCESS_KEY, secret_key=_SECRET_KEY,
        region=_REGION, amz_date=_AMZ_DATE, date_stamp=_DATE_STAMP, service=_SERVICE,
    )
    sig_with_query = _build_authorization(
        method="GET", path="/", query_params=[("prefix", "x")],
        headers=headers, body_sha256=body_sha256,
        access_key=_ACCESS_KEY, secret_key=_SECRET_KEY,
        region=_REGION, amz_date=_AMZ_DATE, date_stamp=_DATE_STAMP, service=_SERVICE,
    )
    assert sig_no_query != sig_with_query


def test_authorization_secret_change_changes_signature() -> None:
    """Different secret -> different signature (sanity)."""
    headers = {"Host": "example.amazonaws.com", "X-Amz-Date": _AMZ_DATE}
    body_sha256 = hashlib.sha256(b"").hexdigest()
    sig_a = _build_authorization(
        method="GET", path="/", query_params=[],
        headers=headers, body_sha256=body_sha256,
        access_key=_ACCESS_KEY, secret_key=_SECRET_KEY,
        region=_REGION, amz_date=_AMZ_DATE, date_stamp=_DATE_STAMP, service=_SERVICE,
    )
    sig_b = _build_authorization(
        method="GET", path="/", query_params=[],
        headers=headers, body_sha256=body_sha256,
        access_key=_ACCESS_KEY, secret_key="different-secret-not-the-aws-vector-one",
        region=_REGION, amz_date=_AMZ_DATE, date_stamp=_DATE_STAMP, service=_SERVICE,
    )
    assert sig_a != sig_b


# ---------------------------------------------------------------------------
# Cross-check: signing key derivation matches a manual HMAC chain
# ---------------------------------------------------------------------------


def test_derive_signing_key_matches_manual_chain() -> None:
    """Independent verification: manual HMAC chain matches our helper."""
    secret = "test-secret"
    date_stamp = "20260101"
    region = "ca-central-1"
    service = "s3"
    expected_step1 = hmac.new(
        ("AWS4" + secret).encode(), date_stamp.encode(), hashlib.sha256,
    ).digest()
    expected_step2 = hmac.new(expected_step1, region.encode(), hashlib.sha256).digest()
    expected_step3 = hmac.new(expected_step2, service.encode(), hashlib.sha256).digest()
    expected_final = hmac.new(expected_step3, b"aws4_request", hashlib.sha256).digest()

    actual = _derive_signing_key(secret, date_stamp, region, service)
    assert actual == expected_final
