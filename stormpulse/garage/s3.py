"""Purpose-built S3 client for Garage's localhost data plane.

Storm Pulse intentionally avoids boto3 — a 30MB vendor library is the wrong
shape for an agent that runs alongside Garage on the same host. The agent
needs exactly two S3 operations: list every object in a bucket, and bulk-
delete a batch of keys. Plus one helper, ``head_bucket``, for credential
pre-flight.

Scope and non-features:

- Path-style addressing only (``http://host:port/bucket/key``). Virtual-
  host style is not supported and not needed against a local Garage.
- HTTP, not HTTPS, by default. The agent talks to Garage on ``localhost``
  on the same node — TLS is unnecessary overhead. HTTPS works if the
  endpoint URL says ``https``.
- No retries, no backoff, no pagination beyond what the caller asks for.
  Caller drives the loop.
- No bucket creation, no object upload, no copy, no multipart, no
  versioning, no lifecycle, no presigning. Add only what the agent's
  command handlers need.

SigV4 signing is implemented from the AWS-published spec using the stdlib
plus ``cryptography`` (already a runtime dependency for HMAC). Verified
against AWS's published "GET-vanilla" canonical-request and signing-key
test vectors in ``tests/garage/test_s3_sigv4.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


_S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class S3ObjectEntry:
    """A single object as returned by ListObjectsV2."""

    key: str
    size: int


@dataclass(frozen=True, slots=True)
class ListResult:
    """Result of one ListObjectsV2 page."""

    contents: list[S3ObjectEntry]
    is_truncated: bool
    next_continuation_token: str | None
    key_count: int


@dataclass(frozen=True, slots=True)
class S3ErrorEntry:
    """One per-object failure from a DeleteObjects response."""

    key: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Result of one DeleteObjects request.

    ``errors`` is the load-bearing field — DeleteObjects returns HTTP 200
    even when individual objects failed. Callers MUST inspect ``errors``
    before treating the operation as successful.
    """

    deleted: list[str]
    errors: list[S3ErrorEntry]


@dataclass(frozen=True, slots=True)
class CorsRule:
    """One CORSRule for PutBucketCors.

    The agent currently sets a single rule per bucket. The S3 spec allows
    a list of rules; if that ever becomes a need, plumb a list through
    ``put_bucket_cors`` instead of widening this dataclass.
    """

    allowed_origins: list[str]
    allowed_methods: list[str]
    allowed_headers: list[str]
    expose_headers: list[str]
    max_age_seconds: int


class S3Error(Exception):
    """Raised when Garage returns an HTTP error for an S3 operation."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class S3AuthError(S3Error):
    """Raised specifically for 403 / SignatureDoesNotMatch / InvalidAccessKeyId."""


# ---------------------------------------------------------------------------
# SigV4 signing primitives (pure, testable)
# ---------------------------------------------------------------------------


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _derive_signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key from the secret access key."""
    k_date = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def _canonical_query_string(query_params: list[tuple[str, str]]) -> str:
    """Build the canonical query string per SigV4 rules.

    Sort by key (then value), URL-encode both, join with ``&``. Empty
    values are kept as ``key=`` per spec.
    """
    encoded = sorted(
        (quote(k, safe=""), quote(v, safe=""))
        for k, v in query_params
    )
    return "&".join(f"{k}={v}" for k, v in encoded)


def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    """Return (canonical_headers_block, signed_headers_list)."""
    items = sorted((k.lower(), v.strip()) for k, v in headers.items())
    canonical = "".join(f"{k}:{v}\n" for k, v in items)
    signed = ";".join(k for k, _ in items)
    return canonical, signed


def _build_authorization(
    method: str,
    path: str,
    query_params: list[tuple[str, str]],
    headers: dict[str, str],
    body_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
    date_stamp: str,
    service: str = _SERVICE,
) -> str:
    """Build the ``Authorization`` header value for one request.

    All inputs are pure data; no I/O. This is the function exercised by
    test vectors. ``service`` defaults to ``"s3"`` but is overridable so
    AWS's published test vectors (which use ``"service"``) can drive it.
    """
    canonical_query = _canonical_query_string(query_params)
    canonical_headers_block, signed_headers = _canonical_headers(headers)
    canonical_request = (
        f"{method}\n"
        f"{path}\n"
        f"{canonical_query}\n"
        f"{canonical_headers_block}\n"
        f"{signed_headers}\n"
        f"{body_sha256}"
    )
    canonical_request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{_ALGORITHM}\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{canonical_request_hash}"
    )

    signing_key = _derive_signing_key(secret_key, date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()

    return (
        f"{_ALGORITHM} "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GarageS3Client:
    """Synchronous S3 client narrow-scoped to the agent's needs.

    Synchronous because http.client is. Async callers wrap method calls
    in ``loop.run_in_executor`` — the methods are CPU-light and I/O-bound
    against localhost, so executor wrapping is appropriate.
    """

    def __init__(
        self,
        endpoint: str,
        region: str,
        access_key: str,
        secret_key: str,
        timeout: float = 30.0,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"endpoint must use http or https, got {endpoint!r}")
        if not parsed.hostname:
            raise ValueError(f"endpoint missing host: {endpoint!r}")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._timeout = timeout

    # -- public S3 operations -------------------------------------------

    def head_bucket(self, bucket: str) -> None:
        """Validate credentials and bucket existence. Raises on failure."""
        self._signed_request("HEAD", f"/{bucket}", [], b"")

    def list_objects_v2(
        self,
        bucket: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
    ) -> ListResult:
        """One page of ListObjectsV2. Caller paginates via ``next_continuation_token``."""
        query: list[tuple[str, str]] = [("list-type", "2"), ("max-keys", str(max_keys))]
        if continuation_token:
            query.append(("continuation-token", continuation_token))
        body = self._signed_request("GET", f"/{bucket}", query, b"")
        return _parse_list_response(body)

    def delete_objects(self, bucket: str, keys: list[str]) -> DeleteResult:
        """DeleteObjects (multi-object delete). Up to 1000 keys per call.

        Returns a ``DeleteResult`` with ``errors`` populated for any per-
        object failures. HTTP 200 with non-empty errors is *not* success;
        callers must inspect both fields.
        """
        if not keys:
            return DeleteResult(deleted=[], errors=[])
        if len(keys) > 1000:
            raise ValueError(f"DeleteObjects accepts at most 1000 keys, got {len(keys)}")
        xml_body = _build_delete_xml(keys)
        body = self._signed_request(
            "POST", f"/{bucket}", [("delete", "")], xml_body,
            content_type="application/xml",
        )
        return _parse_delete_response(body)

    def put_bucket_cors(self, bucket: str, rule: CorsRule) -> None:
        """PutBucketCors. Apply ``rule`` to ``bucket``.

        Garage accepts this without ``Content-MD5`` (verified 2026-05-10
        against ``s3.vancouver-1.stormdevelopments.ca``); the S3 spec
        mandates the header but Garage is lenient on this op.

        Raises ``S3AuthError`` on 401/403, ``S3Error`` on other failures.
        """
        xml_body = _build_cors_xml(rule)
        self._signed_request(
            "PUT", f"/{bucket}", [("cors", "")], xml_body,
            content_type="application/xml",
        )

    # -- internals ------------------------------------------------------

    def _signed_request(
        self,
        method: str,
        path: str,
        query_params: list[tuple[str, str]],
        body: bytes,
        content_type: str | None = None,
    ) -> bytes:
        """Sign and execute a single S3 request. Returns response body bytes."""
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        body_sha256 = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256

        host_header = self._host
        if (self._scheme == "http" and self._port != 80) or (self._scheme == "https" and self._port != 443):
            host_header = f"{self._host}:{self._port}"

        headers: dict[str, str] = {
            "Host": host_header,
            "X-Amz-Date": amz_date,
            "X-Amz-Content-SHA256": body_sha256,
        }
        if content_type:
            headers["Content-Type"] = content_type
        if body:
            headers["Content-Length"] = str(len(body))

        authorization = _build_authorization(
            method=method,
            path=path,
            query_params=query_params,
            headers=headers,
            body_sha256=body_sha256,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self._region,
            amz_date=amz_date,
            date_stamp=date_stamp,
        )
        headers["Authorization"] = authorization

        canonical_query = _canonical_query_string(query_params)
        url_path = f"{path}?{canonical_query}" if canonical_query else path

        conn: http.client.HTTPConnection
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(method, url_path, body=body or None, headers=headers)
            response = conn.getresponse()
            response_body = response.read()
            status = response.status
        finally:
            conn.close()

        if 200 <= status < 300:
            return response_body
        # Try to parse the S3 error envelope
        code, message = _parse_error_response(response_body)
        full_message = f"{method} {path} -> HTTP {status}: {code or 'Unknown'}: {message or 'no message'}"
        if status in (401, 403):
            raise S3AuthError(full_message, status=status, code=code)
        raise S3Error(full_message, status=status, code=code)


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _build_delete_xml(keys: list[str]) -> bytes:
    """Build the DeleteObjects request body."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Delete>"]
    for k in keys:
        # Minimal escaping — keys must not contain raw < > & in S3 anyway,
        # but we use ElementTree's text-escaping to be defensive.
        elem = ElementTree.Element("Key")
        elem.text = k
        escaped = ElementTree.tostring(elem, encoding="unicode")
        parts.append(f"<Object>{escaped}</Object>")
    parts.append("<Quiet>false</Quiet></Delete>")
    return "".join(parts).encode("utf-8")


def _build_cors_xml(rule: CorsRule) -> bytes:
    """Build the PutBucketCors request body for one CORSRule."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<CORSConfiguration><CORSRule>",
    ]
    for tag, values in (
        ("AllowedOrigin", rule.allowed_origins),
        ("AllowedMethod", rule.allowed_methods),
        ("AllowedHeader", rule.allowed_headers),
        ("ExposeHeader", rule.expose_headers),
    ):
        for v in values:
            elem = ElementTree.Element(tag)
            elem.text = v
            parts.append(ElementTree.tostring(elem, encoding="unicode"))
    parts.append(f"<MaxAgeSeconds>{int(rule.max_age_seconds)}</MaxAgeSeconds>")
    parts.append("</CORSRule></CORSConfiguration>")
    return "".join(parts).encode("utf-8")


def _strip_ns(tag: str) -> str:
    """Strip the S3 namespace prefix from an ElementTree tag."""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _parse_list_response(body: bytes) -> ListResult:
    if not body:
        return ListResult(contents=[], is_truncated=False, next_continuation_token=None, key_count=0)
    root = ElementTree.fromstring(body)
    contents: list[S3ObjectEntry] = []
    is_truncated = False
    next_token: str | None = None
    key_count = 0
    for child in root:
        tag = _strip_ns(child.tag)
        if tag == "Contents":
            key = ""
            size = 0
            for sub in child:
                stag = _strip_ns(sub.tag)
                if stag == "Key" and sub.text:
                    key = sub.text
                elif stag == "Size" and sub.text:
                    size = int(sub.text)
            if key:
                contents.append(S3ObjectEntry(key=key, size=size))
        elif tag == "IsTruncated" and child.text:
            is_truncated = child.text.strip().lower() == "true"
        elif tag == "NextContinuationToken" and child.text:
            next_token = child.text
        elif tag == "KeyCount" and child.text:
            key_count = int(child.text)
    return ListResult(
        contents=contents, is_truncated=is_truncated,
        next_continuation_token=next_token, key_count=key_count or len(contents),
    )


def _parse_delete_response(body: bytes) -> DeleteResult:
    if not body:
        return DeleteResult(deleted=[], errors=[])
    root = ElementTree.fromstring(body)
    deleted: list[str] = []
    errors: list[S3ErrorEntry] = []
    for child in root:
        tag = _strip_ns(child.tag)
        if tag == "Deleted":
            for sub in child:
                if _strip_ns(sub.tag) == "Key" and sub.text:
                    deleted.append(sub.text)
        elif tag == "Error":
            key, code, message = "", "", ""
            for sub in child:
                stag = _strip_ns(sub.tag)
                if stag == "Key" and sub.text:
                    key = sub.text
                elif stag == "Code" and sub.text:
                    code = sub.text
                elif stag == "Message" and sub.text:
                    message = sub.text
            errors.append(S3ErrorEntry(key=key, code=code, message=message))
    return DeleteResult(deleted=deleted, errors=errors)


def _parse_error_response(body: bytes) -> tuple[str | None, str | None]:
    """Best-effort parse of the S3 error XML envelope. Returns (code, message)."""
    if not body:
        return None, None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None, body[:200].decode("utf-8", errors="replace")
    code: str | None = None
    message: str | None = None
    for child in root:
        tag = _strip_ns(child.tag)
        if tag == "Code" and child.text:
            code = child.text
        elif tag == "Message" and child.text:
            message = child.text
    return code, message
