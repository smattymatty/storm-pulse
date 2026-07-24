"""The file-browser walk against a real Garage: pagination, prefixes, scale.

``walk_bucket_stats`` is the file browser's backend. Every folder click in the
dashboard is one walk under a prefix, run agent-side and signed on loopback so
the customer's home IP never lands in Garage's access log on a folder click
(the privacy reason in the handler's own docstring).

Two things here only a real Garage can prove, and both are the file browser
working or silently lying:

- **Pagination past 1000 objects.** ListObjectsV2 caps a page at 1000, so every
  real customer bucket is multi-page. Page two rides Garage's own continuation
  token back as a signed query parameter, and that token is opaque bytes: the
  one this cluster returns literally starts with ``]`` (``]b2JqLTEwMDAuYmlu``),
  a character that must be percent-encoded in the SigV4 canonical query or the
  signature fails. A fake generates trivial tokens and can never exercise that,
  so a broken re-sign would pass every fake test and truncate every real
  bucket's count at 1000.

- **Prefix encoding.** A folder named ``2024 vacation`` or ``a+b`` walks only if
  the space / plus survive both the URL and the signature. Verified against
  v2.3.0: they do, once the objects are genuinely seeded (a naive seed that
  mangles the same characters is the trap that makes the LIST look broken).

The default suite's fake proves the accumulation loop; only this proves the
loop survives a real Garage's paging and signing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from stormpulse.garage.jobs.walk_bucket_stats import run_walk_bucket_stats
from stormpulse.garage.s3 import GarageS3Client
from tests.wire.garage.conftest import (
    WireBucket,
    WireEnv,
    drain_bucket,
    garage_cli,
    seed_objects,
)

# Three full pages plus a remainder: proves the continuation token is re-signed
# more than once, each time a different opaque value.
_LARGE_N = 2050
_BODY = b"xx"  # 2 bytes per object, so the byte total is a clean multiple


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        transfer: object | None = None,
        bytes_freed: object | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))


@pytest.fixture(scope="session")
def large_bucket(wire: WireEnv) -> Iterator[str]:
    """A bucket holding _LARGE_N objects, seeded once, shared read-only.

    Session-scoped: the walk is read-only, so every pagination test can share
    one seed and none of them mutate it. Seeding this many objects per test
    would dominate the run.
    """
    name = "wire-walk-large"
    garage_cli("bucket", "create", name)
    garage_cli(
        "bucket", "allow", "--read", "--write", "--owner", name,
        "--key", wire.access_key,
    )
    seed_objects(wire, name, [f"obj-{i:05d}.bin" for i in range(_LARGE_N)], _BODY)
    try:
        yield name
    finally:
        drain_bucket(wire, name)
        garage_cli("bucket", "delete", "--yes", name)


def _walk(
    wire: WireEnv, bucket: str, prefix: str = "", max_objects: int = 100_000
):
    client = GarageS3Client(
        endpoint=wire.s3_endpoint, region=wire.region,
        access_key=wire.access_key, secret_key=wire.secret_key,
    )
    import asyncio

    return asyncio.run(
        run_walk_bucket_stats(_ProgressRecorder(), client, bucket, prefix, max_objects)
    )


# ---------------------------------------------------------------------------
# Pagination past 1000: the headline
# ---------------------------------------------------------------------------


def test_walk_counts_every_object_across_multiple_pages(
    wire: WireEnv, large_bucket: str
) -> None:
    """The whole bucket is counted, not just the first page.

    If the continuation token were dropped or mis-signed on page two, the count
    would stop at 1000 (or 2000) and every large bucket would under-report. The
    exact count is the proof all three-plus pages were walked.
    """
    out = _walk(wire, large_bucket)
    assert out.success, out.stderr
    assert out.extras["count"] == _LARGE_N, (
        f"walked {out.extras['count']} of {_LARGE_N}; pagination lost objects"
    )
    assert out.extras["truncated"] is False


def test_walk_sums_exact_bytes_across_pages(
    wire: WireEnv, large_bucket: str
) -> None:
    """Bytes accumulate correctly across every page, not just page one.

    The number the file browser shows as folder size. A per-page reset or a
    lost page would under-report it.
    """
    out = _walk(wire, large_bucket)
    assert out.success, out.stderr
    assert out.extras["bytes"] == _LARGE_N * len(_BODY), out.extras


# ---------------------------------------------------------------------------
# The truncation cap
# ---------------------------------------------------------------------------


def test_walk_truncates_at_max_objects_mid_walk(
    wire: WireEnv, large_bucket: str
) -> None:
    """The cap bites and is reported, so a huge bucket cannot run unbounded.

    max_objects below the bucket size must stop the walk and flag it truncated,
    the backstop that keeps a folder-size query on a million-object bucket from
    paging forever.
    """
    out = _walk(wire, large_bucket, max_objects=500)
    assert out.success, out.stderr
    assert out.extras["count"] == 500, out.extras
    assert out.extras["truncated"] is True
    assert out.extras["bytes"] == 500 * len(_BODY), out.extras


def test_walk_at_exactly_the_page_boundary_is_not_falsely_truncated(
    wire: WireEnv, large_bucket: str
) -> None:
    """A cap of exactly 1000 (one full page) reports truncated because more remain.

    The off-by-one seam: at the page boundary the loop must know the bucket
    holds more, and say so.
    """
    out = _walk(wire, large_bucket, max_objects=1000)
    assert out.extras["count"] == 1000, out.extras
    assert out.extras["truncated"] is True, out.extras


# ---------------------------------------------------------------------------
# Prefix walks: the actual folder navigation
# ---------------------------------------------------------------------------


def test_walk_a_folder_whose_name_has_a_space(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A prefix with a space walks the folder, proving SigV4 query encoding.

    "2024 vacation" is an ordinary folder name a customer will make. The space
    has to survive both the URL and the signature; if the canonical query
    encoding were wrong the walk would return nothing and the folder would look
    empty in the browser.
    """
    seed_objects(
        wire, bucket.name,
        [
            "photos/2024 vacation/beach.jpg",
            "photos/2024 vacation/sunset.jpg",
            "photos/cover.jpg",
        ],
    )
    out = _walk(wire, bucket.name, prefix="photos/2024 vacation/")
    assert out.success, out.stderr
    assert out.extras["count"] == 2, out.extras


@pytest.mark.parametrize("folder", ["a+b/", "100%/"])
def test_walk_a_folder_whose_name_needs_percent_encoding(
    wire: WireEnv, bucket: WireBucket, folder: str
) -> None:
    """Plus and percent in a folder name survive the signed request.

    The two characters percent-encoding treats specially. Each must reach
    Garage as the literal folder, or the walk silently misses the folder's
    contents.
    """
    seed_objects(wire, bucket.name, [f"{folder}file.txt", "other.txt"])
    out = _walk(wire, bucket.name, prefix=folder)
    assert out.extras["count"] == 1, out.extras


def test_walk_prefix_is_recursive_across_subfolders(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A folder walk counts every object beneath it, at any depth.

    No delimiter: the file browser's folder size is the recursive total, so a
    nested object counts toward its ancestor folders.
    """
    seed_objects(
        wire, bucket.name,
        [
            "docs/readme.txt",
            "docs/deep/nested/here.txt",
            "docs/deep/more.txt",
            "unrelated.txt",
        ],
    )
    out = _walk(wire, bucket.name, prefix="docs/")
    assert out.extras["count"] == 3, out.extras


def test_walk_prefix_is_a_literal_string_not_a_path_component(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """S3 prefixes match literally, so ``photo`` also matches ``photos``.

    Not a bug, a semantic worth pinning: the file browser sends a
    slash-terminated prefix precisely because a bare one would over-count. If
    Garage ever changed prefix semantics, the browser's folder scoping would
    silently shift.
    """
    seed_objects(wire, bucket.name, ["photo.txt", "photos/inside.txt"])
    # The bare "photo" prefix matches BOTH the file and the folder.
    out = _walk(wire, bucket.name, prefix="photo")
    assert out.extras["count"] == 2, out.extras
    # The slash-terminated prefix scopes to the folder alone.
    scoped = _walk(wire, bucket.name, prefix="photos/")
    assert scoped.extras["count"] == 1, scoped.extras


# ---------------------------------------------------------------------------
# The clean-error paths the browser shows
# ---------------------------------------------------------------------------


def test_walk_with_a_bad_secret_reports_auth_failed(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A wrong secret is a clean auth_failed, not an opaque list error.

    The pre-flight head_bucket exists so the browser shows "check your key"
    rather than a raw 403 from the first page. The reason string is what the
    dashboard branches on.
    """
    import asyncio

    bad = GarageS3Client(
        endpoint=wire.s3_endpoint, region=wire.region,
        access_key=wire.access_key, secret_key="wrong-secret-deliberately",
    )
    out = asyncio.run(
        run_walk_bucket_stats(_ProgressRecorder(), bad, bucket.name, "", 100_000)
    )
    assert not out.success
    assert out.failure_reason == "auth_failed", out.failure_reason


def test_walk_an_empty_bucket_is_zero_not_an_error(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """An empty folder reads as zero, the browser's empty state."""
    out = _walk(wire, bucket.name)
    assert out.success, out.stderr
    assert out.extras["count"] == 0
    assert out.extras["bytes"] == 0
    assert out.extras["truncated"] is False
