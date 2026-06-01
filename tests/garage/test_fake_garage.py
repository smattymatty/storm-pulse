"""Unit tests for FakeGarage itself.

Without these the fake becomes an unverified single point of trust -
all the orchestrator tests would rely on its rules being correct, but
no test would pin those rules. Every rule the fake encodes has a
positive case (rule satisfied → success) and a negative case (rule
violated → expected error code/stderr).

The four "floor" reproductions at the bottom are the load-bearing
test of *what this whole exercise is for*: the fake must catch the
bugs the monkey-patched suite missed this morning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.config import GarageConfig
from stormpulse.garage.parse import (
    GarageBucketInfo,
    parse_bucket_info,
    parse_key_create,
)
from tests.garage._fake_garage import FakeGarage


def _config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )


# ---------------------------------------------------------------------------
# Rule 1: bucket create - S3-strict name validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_create_accepts_valid_name() -> None:
    fake = FakeGarage()
    rc, stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "my-bucket-001",
    )
    assert rc == 0
    assert stderr == ""
    info = parse_bucket_info(stdout)
    assert isinstance(info, GarageBucketInfo)
    assert info.global_alias == "my-bucket-001"
    assert len(info.bucket_id) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "_provisioning_abc",  # leading underscore (the bug from this morning)
        "Bucket-Caps",  # uppercase
        "ab",  # too short
        "a" * 64,  # too long (max 63)
        "-leading-hyphen",
        "trailing-hyphen-",
        "has_underscore",
        "has spaces",
    ],
)
async def test_bucket_create_rejects_invalid_names(name: str) -> None:
    fake = FakeGarage()
    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        name,
    )
    assert rc == 1
    assert "InvalidBucketName" in stderr


@pytest.mark.asyncio
async def test_bucket_create_rejects_duplicate_global_alias() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "twin")
    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "twin",
    )
    assert rc == 1
    assert "BucketAlreadyExists" in stderr


# ---------------------------------------------------------------------------
# Rule 2: bucket unalias <name> - orphan rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_unalias_global_succeeds_when_other_aliases_exist() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "primary")
    # Add a second global alias so removing "primary" leaves one behind.
    bucket = next(iter(fake.buckets.values()))
    bucket.global_aliases.add("secondary")

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "unalias",
        "primary",
    )
    assert rc == 0
    assert "primary" not in bucket.global_aliases
    assert "secondary" in bucket.global_aliases


@pytest.mark.asyncio
async def test_bucket_unalias_global_succeeds_when_local_aliases_exist() -> None:
    """Locals count toward the orphan rule.

    Empirically confirmed against garage-one: a bucket with one global
    alias and one local alias can have the global removed.
    """
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "with-local")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    bucket.local_aliases[key_id] = "local-alias"

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "unalias",
        "with-local",
    )
    assert rc == 0
    assert "with-local" not in bucket.global_aliases


@pytest.mark.asyncio
async def test_bucket_unalias_global_rejects_when_only_alias() -> None:
    """Floor reproduction #3: orphan rule on global unalias."""
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "lonely")

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "unalias",
        "lonely",
    )
    assert rc == 1
    assert "doesn't have other aliases" in stderr


# ---------------------------------------------------------------------------
# Rule 3: bucket unalias --local - one positional, orphan check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_unalias_local_one_positional_succeeds() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    # Two aliases so removing one doesn't trip orphan rule.
    bucket.local_aliases[key_id] = "media"

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "unalias",
        "--local",
        key_id,
        "media",
    )
    assert rc == 0
    assert key_id not in bucket.local_aliases


@pytest.mark.asyncio
async def test_bucket_unalias_local_three_positional_form_rejected() -> None:
    """Floor reproduction #4: 3-positional form (the rotate_key bug)."""
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "unalias",
        "--local",
        key_id,
        bucket.bucket_id[:16],
        "media",
    )
    assert rc == 1
    assert "USAGE" in stderr


# ---------------------------------------------------------------------------
# Rule 4: bucket alias --local - three positionals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_alias_local_three_positional_succeeds() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "alias",
        "--local",
        key_id,
        bucket.bucket_id[:16],
        "my-name",
    )
    assert rc == 0
    assert bucket.local_aliases[key_id] == "my-name"


@pytest.mark.asyncio
async def test_bucket_alias_local_unknown_key_rejected() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "alias",
        "--local",
        "GKnonexistent",
        "host",
        "my-name",
    )
    assert rc == 1
    assert "NoSuchKey" in stderr


# ---------------------------------------------------------------------------
# Rule 5: bucket allow / deny - flag handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_allow_grants_permissions() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "allow",
        "--read",
        "--write",
        "--owner",
        bucket.bucket_id[:16],
        "--key",
        key_id,
    )
    assert rc == 0
    assert fake.keys[key_id].permissions[bucket.bucket_id] == {
        "read",
        "write",
        "owner",
    }


@pytest.mark.asyncio
async def test_bucket_deny_revokes_permissions() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    fake.keys[key_id].permissions[bucket.bucket_id] = {
        "read",
        "write",
        "owner",
    }

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "deny",
        "--write",
        "--owner",
        bucket.bucket_id[:16],
        "--key",
        key_id,
    )
    assert rc == 0
    assert fake.keys[key_id].permissions[bucket.bucket_id] == {"read"}


@pytest.mark.asyncio
async def test_bucket_allow_with_no_flags_rejected() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "allow",
        "host",
        "--key",
        key_id,
    )
    assert rc == 1
    assert "USAGE" in stderr


# ---------------------------------------------------------------------------
# Rule 6: bucket delete --yes - empty check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_delete_empty_succeeds() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "doomed")

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "delete",
        "--yes",
        "doomed",
    )
    assert rc == 0
    assert not fake.buckets


@pytest.mark.asyncio
async def test_bucket_delete_non_empty_rejected() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "occupied")
    bucket = next(iter(fake.buckets.values()))
    bucket.object_count = 5

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "delete",
        "--yes",
        "occupied",
    )
    assert rc == 1
    assert "BucketNotEmpty" in stderr
    assert "occupied" in fake.buckets[bucket.bucket_id].global_aliases


@pytest.mark.asyncio
async def test_bucket_delete_revokes_referencing_permissions() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "shared")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    fake.keys[key_id].permissions[bucket.bucket_id] = {"read"}

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "delete",
        "--yes",
        "shared",
    )
    assert rc == 0
    assert bucket.bucket_id not in fake.keys[key_id].permissions


# ---------------------------------------------------------------------------
# Rule 7: key create - deterministic output, parseable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_create_returns_parseable_stdout() -> None:
    fake = FakeGarage()
    rc, stdout, _stderr = await fake.run_garage(
        _config(),
        "key",
        "create",
        "my-key",
    )
    assert rc == 0
    result = parse_key_create(stdout)
    assert result.name == "my-key"
    assert result.key_id.startswith("GK")
    assert len(result.key_id) == 26
    assert len(result.secret_key) == 64


@pytest.mark.asyncio
async def test_key_create_produces_distinct_ids() -> None:
    fake = FakeGarage()
    rc1, stdout1, _ = await fake.run_garage(_config(), "key", "create", "k1")
    rc2, stdout2, _ = await fake.run_garage(_config(), "key", "create", "k2")
    assert rc1 == 0 and rc2 == 0
    r1 = parse_key_create(stdout1)
    r2 = parse_key_create(stdout2)
    assert r1.key_id != r2.key_id
    assert r1.secret_key != r2.secret_key


# ---------------------------------------------------------------------------
# Rule 8: bucket reference resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_global_alias() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "by-name")
    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "info",
        "by-name",
    )
    assert rc == 0


@pytest.mark.asyncio
async def test_resolve_by_16_char_prefix() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "by-prefix")
    bucket = next(iter(fake.buckets.values()))

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "info",
        bucket.bucket_id[:16],
    )
    assert rc == 0


@pytest.mark.asyncio
async def test_resolve_by_64_char_rejected() -> None:
    """Floor reproduction #2: full hex rejected with NoSuchBucket.

    Empirically confirmed against garage-one v2.2.0 - `bucket info`
    with the full 64-char hash returns NoSuchBucket. The conservative
    fake matches that behavior.
    """
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "by-full-hash")
    bucket = next(iter(fake.buckets.values()))

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "info",
        bucket.bucket_id,
    )
    assert rc == 1
    assert "NoSuchBucket" in stderr


@pytest.mark.asyncio
async def test_local_alias_not_globally_resolvable() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k1")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    bucket.local_aliases[key_id] = "private-name"

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "info",
        "private-name",
    )
    assert rc == 1
    assert "NoSuchBucket" in stderr


# ---------------------------------------------------------------------------
# Rule 9: bucket info renders only keys with permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_info_excludes_keys_without_permissions() -> None:
    """Empirically observed: a key with a local alias attached but no
    permissions does NOT appear in the keys table of `bucket info`.
    """
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k-no-perms")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    bucket.local_aliases[key_id] = "lonely-alias"

    rc, stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "info",
        "host",
    )
    assert rc == 0
    info = parse_bucket_info(stdout)
    assert info.keys == []


@pytest.mark.asyncio
async def test_bucket_info_includes_keys_with_permissions() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("k-with-perms")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    fake.keys[key_id].permissions[bucket.bucket_id] = {"read"}

    rc, stdout, _stderr = await fake.run_garage(
        _config(),
        "bucket",
        "info",
        "host",
    )
    assert rc == 0
    info = parse_bucket_info(stdout)
    assert len(info.keys) == 1
    assert info.keys[0].access_key_id == key_id


# ---------------------------------------------------------------------------
# key delete - strips local aliases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_delete_strips_local_aliases() -> None:
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "host")
    fake.add_key("doomed-key")
    key_id = next(iter(fake.keys))
    bucket = next(iter(fake.buckets.values()))
    bucket.local_aliases[key_id] = "via-doomed-key"

    rc, _stdout, _stderr = await fake.run_garage(
        _config(),
        "key",
        "delete",
        "--yes",
        key_id,
    )
    assert rc == 0
    assert key_id not in fake.keys
    assert key_id not in bucket.local_aliases


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_next_overrides_dispatch() -> None:
    fake = FakeGarage()
    fake.fail_next("bucket_create", rc=1, stderr="injected failure")

    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "valid-name",
    )
    assert rc == 1
    assert "injected" in stderr
    # Bucket was not actually created since the override fired first.
    assert not fake.buckets


@pytest.mark.asyncio
async def test_fail_next_queues_per_verb() -> None:
    fake = FakeGarage()
    fake.fail_next("bucket_create", stderr="first")
    fake.fail_next("bucket_create", stderr="second")

    _, _, stderr1 = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "first-bucket",
    )
    _, _, stderr2 = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "second-bucket",
    )
    rc3, _, _ = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "third-bucket",
    )

    assert "first" in stderr1
    assert "second" in stderr2
    # Third call is no longer overridden - dispatches normally.
    assert rc3 == 0
    assert "third-bucket" in {
        a for b in fake.buckets.values() for a in b.global_aliases
    }


# ---------------------------------------------------------------------------
# Floor reproduction #1: S3-strict on the original throwaway alias bug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_floor_repro_underscore_throwaway_rejected() -> None:
    """The original `_provisioning_<hex>` form gets rejected.

    This was the first bug discovered this morning. It's already fixed
    in `provision_bucket.py:176` (`provisioning-<hex>` form), but the
    fake codifies the rule so a future regression would surface
    immediately.
    """
    fake = FakeGarage()
    rc, _stdout, stderr = await fake.run_garage(
        _config(),
        "bucket",
        "create",
        "_provisioning_abc123",
    )
    assert rc == 1
    assert "InvalidBucketName" in stderr


# ---------------------------------------------------------------------------
# Unrecognized call shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrecognized_shape_raises() -> None:
    fake = FakeGarage()
    with pytest.raises(NotImplementedError):
        await fake.run_garage(_config(), "node", "status")


@pytest.mark.asyncio
async def test_calls_recorded_for_assertions() -> None:
    """Tests use ``fake.calls`` for call-sequence assertions, identical
    to the prior ``runner.calls`` shape.
    """
    fake = FakeGarage()
    await fake.run_garage(_config(), "bucket", "create", "a")
    await fake.run_garage(_config(), "key", "create", "k")

    assert fake.calls == [
        ("bucket", "create", "a"),
        ("key", "create", "k"),
    ]
