"""Tests for the XML parsing helpers in stormpulse.garage.s3.

These exercise the response parsers in isolation, so they don't require a
live Garage. Real Garage responses use the standard S3 namespace; we
verify that the parser handles namespaced and bare tags identically.
"""

from __future__ import annotations

import pytest

from xml.etree import ElementTree

from stormpulse.garage.s3 import (
    CorsRule,
    DeleteResult,
    GarageS3Client,
    S3AuthError,
    S3Error,
    _build_cors_xml,
    _build_delete_xml,
    _parse_delete_response,
    _parse_error_response,
    _parse_list_response,
)


# ---------------------------------------------------------------------------
# ListObjectsV2 response parsing
# ---------------------------------------------------------------------------


_LIST_RESPONSE_NS = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>test-bucket</Name>
  <KeyCount>2</KeyCount>
  <IsTruncated>false</IsTruncated>
  <Contents>
    <Key>file-a.txt</Key>
    <Size>123</Size>
    <LastModified>2026-01-01T00:00:00.000Z</LastModified>
  </Contents>
  <Contents>
    <Key>folder/file-b.txt</Key>
    <Size>456</Size>
  </Contents>
</ListBucketResult>"""


def test_parse_list_response_with_namespace() -> None:
    result = _parse_list_response(_LIST_RESPONSE_NS)
    assert len(result.contents) == 2
    assert result.contents[0].key == "file-a.txt"
    assert result.contents[0].size == 123
    assert result.contents[1].key == "folder/file-b.txt"
    assert result.contents[1].size == 456
    assert result.is_truncated is False
    assert result.next_continuation_token is None
    assert result.key_count == 2


def test_parse_list_response_truncated() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <KeyCount>1000</KeyCount>
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>opaque-token-here</NextContinuationToken>
  <Contents><Key>x</Key><Size>1</Size></Contents>
</ListBucketResult>"""
    result = _parse_list_response(body)
    assert result.is_truncated is True
    assert result.next_continuation_token == "opaque-token-here"
    assert result.key_count == 1000


def test_parse_list_response_empty_bucket() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <KeyCount>0</KeyCount>
  <IsTruncated>false</IsTruncated>
</ListBucketResult>"""
    result = _parse_list_response(body)
    assert result.contents == []
    assert result.is_truncated is False
    assert result.key_count == 0


def test_parse_list_response_empty_body() -> None:
    result = _parse_list_response(b"")
    assert result.contents == []
    assert result.key_count == 0


# ---------------------------------------------------------------------------
# DeleteObjects response parsing
# ---------------------------------------------------------------------------


def test_parse_delete_response_all_succeeded() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Deleted><Key>a</Key></Deleted>
  <Deleted><Key>b</Key></Deleted>
</DeleteResult>"""
    result = _parse_delete_response(body)
    assert result.deleted == ["a", "b"]
    assert result.errors == []


def test_parse_delete_response_with_errors() -> None:
    """The load-bearing case: HTTP 200 but per-object failures."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Deleted><Key>good-key</Key></Deleted>
  <Error>
    <Key>bad-key</Key>
    <Code>AccessDenied</Code>
    <Message>Permission denied for object</Message>
  </Error>
</DeleteResult>"""
    result = _parse_delete_response(body)
    assert result.deleted == ["good-key"]
    assert len(result.errors) == 1
    assert result.errors[0].key == "bad-key"
    assert result.errors[0].code == "AccessDenied"
    assert "Permission denied" in result.errors[0].message


def test_parse_delete_response_empty_body() -> None:
    result = _parse_delete_response(b"")
    assert result == DeleteResult(deleted=[], errors=[])


# ---------------------------------------------------------------------------
# Error envelope parsing
# ---------------------------------------------------------------------------


def test_parse_error_response_standard_envelope() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>InvalidAccessKeyId</Code>
  <Message>The AWS Access Key Id you provided does not exist</Message>
  <RequestId>abc-123</RequestId>
</Error>"""
    code, message = _parse_error_response(body)
    assert code == "InvalidAccessKeyId"
    assert "does not exist" in (message or "")


def test_parse_error_response_unparseable_returns_truncated_body() -> None:
    body = b"not actually xml"
    code, message = _parse_error_response(body)
    assert code is None
    assert message is not None and "not actually xml" in message


def test_parse_error_response_empty() -> None:
    code, message = _parse_error_response(b"")
    assert code is None
    assert message is None


# ---------------------------------------------------------------------------
# DeleteObjects request body
# ---------------------------------------------------------------------------


def test_build_delete_xml_includes_each_key() -> None:
    body = _build_delete_xml(["a", "b/c", "d e"])
    assert b"<Key>a</Key>" in body
    assert b"<Key>b/c</Key>" in body
    assert b"<Key>d e</Key>" in body
    assert b"<Quiet>false</Quiet>" in body


def test_build_delete_xml_escapes_special_chars() -> None:
    body = _build_delete_xml(["key&with<special>"])
    # ElementTree escapes & < > automatically
    assert b"&amp;" in body
    assert b"&lt;" in body
    assert b"&gt;" in body


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_client_rejects_unsupported_scheme() -> None:
    with pytest.raises(ValueError, match="http or https"):
        GarageS3Client(
            endpoint="ftp://example.com",
            region="us-east-1",
            access_key="x",
            secret_key="y",
        )


def test_client_rejects_missing_host() -> None:
    with pytest.raises(ValueError, match="missing host"):
        GarageS3Client(
            endpoint="http://",
            region="us-east-1",
            access_key="x",
            secret_key="y",
        )


def test_client_accepts_http_localhost() -> None:
    # No exception
    GarageS3Client(
        endpoint="http://localhost:3900",
        region="garage",
        access_key="GK123",
        secret_key="abc",
    )


# ---------------------------------------------------------------------------
# Exception class hierarchy
# ---------------------------------------------------------------------------


def test_auth_error_is_an_s3_error() -> None:
    """S3AuthError catches code can also catch via S3Error base."""
    err = S3AuthError("nope", status=403, code="SignatureDoesNotMatch")
    assert isinstance(err, S3Error)
    assert err.status == 403
    assert err.code == "SignatureDoesNotMatch"


# ---------------------------------------------------------------------------
# PutBucketCors XML construction
# ---------------------------------------------------------------------------


def _parse_cors_xml(body: bytes) -> dict[str, list[str] | int]:
    """Round-trip the bytes through ElementTree to verify shape."""
    root = ElementTree.fromstring(body)
    assert root.tag == "CORSConfiguration"
    rules = list(root)
    assert len(rules) == 1, "single-rule v1 contract"
    rule = rules[0]
    assert rule.tag == "CORSRule"
    out: dict[str, list[str] | int] = {
        "AllowedOrigin": [],
        "AllowedMethod": [],
        "AllowedHeader": [],
        "ExposeHeader": [],
    }
    for child in rule:
        if child.tag in out:
            assert isinstance(child.text, str)
            out[child.tag].append(child.text)  # type: ignore[union-attr]
        elif child.tag == "MaxAgeSeconds":
            assert child.text is not None
            out["MaxAgeSeconds"] = int(child.text)
    return out


def test_build_cors_xml_emits_one_rule_with_all_fields() -> None:
    rule = CorsRule(
        allowed_origins=["https://stormdevelopments.ca"],
        allowed_methods=["GET", "PUT", "HEAD", "POST"],
        allowed_headers=[
            "authorization",
            "x-amz-date",
            "x-amz-content-sha256",
            "content-type",
            "content-length",
        ],
        expose_headers=["ETag"],
        max_age_seconds=3000,
    )
    body = _build_cors_xml(rule)
    assert body.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')

    parsed = _parse_cors_xml(body)
    assert parsed["AllowedOrigin"] == ["https://stormdevelopments.ca"]
    assert parsed["AllowedMethod"] == ["GET", "PUT", "HEAD", "POST"]
    assert parsed["AllowedHeader"] == [
        "authorization",
        "x-amz-date",
        "x-amz-content-sha256",
        "content-type",
        "content-length",
    ]
    assert parsed["ExposeHeader"] == ["ETag"]
    assert parsed["MaxAgeSeconds"] == 3000


def test_build_cors_xml_handles_multiple_origins() -> None:
    rule = CorsRule(
        allowed_origins=["https://a.example", "https://b.example"],
        allowed_methods=["GET"],
        allowed_headers=[],
        expose_headers=[],
        max_age_seconds=60,
    )
    body = _build_cors_xml(rule)
    parsed = _parse_cors_xml(body)
    assert parsed["AllowedOrigin"] == ["https://a.example", "https://b.example"]
    assert parsed["AllowedHeader"] == []
    assert parsed["ExposeHeader"] == []
    assert parsed["MaxAgeSeconds"] == 60


def test_build_cors_xml_escapes_special_chars_in_origin() -> None:
    """Defensive: ElementTree escaping handles & < > in origin values."""
    rule = CorsRule(
        allowed_origins=["https://x.example/?a=1&b=2"],
        allowed_methods=["GET"],
        allowed_headers=[],
        expose_headers=[],
        max_age_seconds=60,
    )
    body = _build_cors_xml(rule)
    # Raw ampersand must not appear unescaped
    assert b"a=1&b=2" not in body
    assert b"a=1&amp;b=2" in body
    # Round-trip parses back to the original
    parsed = _parse_cors_xml(body)
    assert parsed["AllowedOrigin"] == ["https://x.example/?a=1&b=2"]
