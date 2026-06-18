"""Tests for stormpulse.garage.provision_account_key.

Mints a BUCKETS-012 account key: one CreateKey with the key-level
``allow_create_bucket`` capability set, returning the one-time secret. One
forward step, no rollback. As in ``test_provision_additional_key``, we patch
``admin_api.create_key`` and assert on the recorded call and the
injected-failure handling, never the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.provision_account_key import (
    make_provision_account_key_handler,
    run_provision_account_key,
)


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
    )


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


class _FakeAdmin:
    """Records ``create_key`` calls and returns a canned success.

    ``fail`` makes ``create_key`` return its failure shape; ``no_id`` makes it
    succeed transport-wise but omit ``accessKeyId``. The fake's signature
    carries ``allow_create_bucket`` because the account-key handler always
    passes it.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail: str | None = None
        self.no_id: bool = False

    def create_key(
        self, *, admin_url: str, admin_token: str, name: str,
        allow_create_bucket: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append({"name": name, "allow_create_bucket": allow_create_bucket})
        if self.fail is not None:
            return None, self.fail
        if self.no_id:
            return {"secretAccessKey": "s" * 40, "name": name}, ""
        return {
            "accessKeyId": "GK" + "0" * 24,
            "secretAccessKey": "s" * 40,
            "name": name,
        }, ""


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    monkeypatch.setattr(
        "stormpulse.garage.provision_account_key.admin_api.create_key",
        fake.create_key,
    )
    return fake


async def _run(
    fake: _FakeAdmin,
    *,
    new_key_name: str = "acct-key",
    config: GarageConfig | None = None,
) -> JobOutcome:
    return await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        new_key_name=new_key_name,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_mints_key_with_create_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)

    outcome = await _run(fake, new_key_name="acct-key")

    assert outcome.success is True
    assert outcome.failure_reason is None
    assert outcome.extras["new_key_id"] == "GK" + "0" * 24
    assert outcome.extras["new_secret"] == "s" * 40
    assert outcome.extras["new_key_name"] == "acct-key"
    assert outcome.extras["can_create_bucket"] is True
    assert outcome.extras["step_completed"] == "account_key_create"
    assert outcome.extras["rollback_status"] == "not_required"
    # The single call carried the createBucket capability; no bucket touched.
    assert fake.calls == [{"name": "acct-key", "allow_create_bucket": True}]


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_failure_no_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.fail = "key create error"

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "account_key_create_failed"
    assert outcome.extras["step_failed"] == "account_key_create"
    assert outcome.extras["new_key_id"] is None
    assert outcome.extras["rollback_status"] == "not_required"


@pytest.mark.asyncio
async def test_missing_access_key_id_flags_manual_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.no_id = True

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "account_key_create_failed"
    cleanup = outcome.extras["manual_cleanup_required"]
    assert {"type": "key_unknown_id", "name": "acct-key"} in cleanup


@pytest.mark.asyncio
async def test_admin_api_unconfigured_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    config = _make_config()
    object.__setattr__(config, "admin_url", "")
    object.__setattr__(config, "admin_token", "")

    outcome = await _run(fake, config=config)

    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    # No Garage calls were attempted.
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_name() -> None:
    handler = make_provision_account_key_handler(_make_config(), params={})
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_provision_account_key_handler(
        _make_config(), params={"new_key_name": "acct-key"},
    )
    assert handler is not None
    assert callable(handler)
