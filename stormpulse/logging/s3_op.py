"""Name the S3 operation a request line performs (ADR CORE-011 decision 10).

Method, sub-resource and the presence of a key decide. The query string stops
here; the key is split off and rides as ``object_key`` on Garage lines only.
"""

from __future__ import annotations

from urllib.parse import parse_qs

UNKNOWN = "Unknown"

# First sub-resource found wins; partNumber before uploadId so a part upload
# is never read as a completion.
_SUB_RESOURCES = (
    "delete",
    "uploads",
    "partNumber",
    "uploadId",
    "cors",
    "location",
    "list-type",
)

_BUCKET_OPS: dict[tuple[str, str | None], str] = {
    ("GET", None): "ListObjects",
    ("GET", "list-type"): "ListObjectsV2",
    ("GET", "uploads"): "ListMultipartUploads",
    ("GET", "cors"): "GetBucketCors",
    ("PUT", "cors"): "PutBucketCors",
    ("DELETE", "cors"): "DeleteBucketCors",
    ("GET", "location"): "GetBucketLocation",
    ("POST", "delete"): "DeleteObjects",
    ("HEAD", None): "HeadBucket",
    ("PUT", None): "CreateBucket",
    ("DELETE", None): "DeleteBucket",
}

_OBJECT_OPS: dict[tuple[str, str | None], str] = {
    ("GET", None): "GetObject",
    ("PUT", None): "PutObject",
    ("DELETE", None): "DeleteObject",
    ("HEAD", None): "HeadObject",
    ("POST", "uploads"): "CreateMultipartUpload",
    ("PUT", "partNumber"): "UploadPart",
    ("POST", "uploadId"): "CompleteMultipartUpload",
    ("DELETE", "uploadId"): "AbortMultipartUpload",
}

S3_OPS: frozenset[str] = frozenset(
    {"ListBuckets", UNKNOWN, *_BUCKET_OPS.values(), *_OBJECT_OPS.values()}
)


def split_bucket_key(path: str) -> tuple[str, str]:
    """Split a path-style request path into ``(bucket, object_key)``.

    The bucket is the first segment, the key the rest; the query string
    is dropped. Anything not rooted at ``/`` yields two empty strings.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return "", ""
    parts = path.split("?", 1)[0][1:].split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _sub_resource(path: str) -> str | None:
    query = path.split("?", 1)[1] if "?" in path else ""
    keys = parse_qs(query, keep_blank_values=True).keys()
    return next((s for s in _SUB_RESOURCES if s in keys), None)


def classify_s3_op(method: str, path: str) -> str:
    """The full S3 operation name for ``method path``, or ``Unknown``."""
    if not isinstance(path, str) or not path.startswith("/"):
        return UNKNOWN
    bucket, key = split_bucket_key(path)
    if not bucket:
        return "ListBuckets" if method == "GET" else UNKNOWN
    table = _OBJECT_OPS if key else _BUCKET_OPS
    return table.get((method, _sub_resource(path)), UNKNOWN)
