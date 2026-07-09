"""Tests for the count-backstop toggle: the handler + guards.

The admin-API ``update_key`` wire shape is covered in
``test_admin_api_writes``; here we pin the handler factory's param guards
and that ``run_*`` maps ``enable`` to ``allow_create_bucket`` and surfaces
admin-API failures.
"""
from __future__ import annotations

from typing import Any

import pytest

from stormpulse.garage import admin_api
from stormpulse.garage.jobs.set_account_key_capability import (
    make_set_account_key_capability_handler,
    run_set_account_key_capability,
)

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}
_KEY_ID = "GK31c2f6a8b9d04e15f7c3a2b1"


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
        *,
        transfer: object | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))


# ---------------------------------------------------------------------------
# Factory guards
# ---------------------------------------------------------------------------


def test_handler_none_when_admin_not_configured() -> None:
    handler = make_set_account_key_capability_handler(
        {"access_key_id": _KEY_ID, "enable": "true"}, admin_url="", admin_token="",
    )
    assert handler is None


def test_handler_none_when_missing_or_bad_params() -> None:
    assert make_set_account_key_capability_handler({"enable": "true"}, **_ADMIN) is None
    assert make_set_account_key_capability_handler(
        {"access_key_id": _KEY_ID}, **_ADMIN,
    ) is None
    assert make_set_account_key_capability_handler(
        {"access_key_id": _KEY_ID, "enable": "maybe"}, **_ADMIN,
    ) is None


def test_handler_built_when_valid() -> None:
    assert make_set_account_key_capability_handler(
        {"access_key_id": _KEY_ID, "enable": "false"}, **_ADMIN,
    ) is not None


# ---------------------------------------------------------------------------
# run_* - enable/disable mapping + failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("enable", [True, False])
async def test_run_maps_enable_to_allow_create_bucket(
    monkeypatch: pytest.MonkeyPatch, enable: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake(*, admin_url: str, admin_token: str, access_key_id: str,
             allow_create_bucket: bool) -> tuple[bool, str]:
        calls.append({"access_key_id": access_key_id, "allow": allow_create_bucket})
        return True, ""

    monkeypatch.setattr(admin_api, "update_key", fake)

    outcome = await run_set_account_key_capability(
        _ProgressRecorder(), **_ADMIN, access_key_id=_KEY_ID, enable=enable,
    )
    assert outcome.success is True
    assert calls == [{"access_key_id": _KEY_ID, "allow": enable}]
    assert outcome.extras["enable"] is enable


@pytest.mark.asyncio
async def test_run_surfaces_admin_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(**kw: Any) -> tuple[bool, str]:
        return False, "HTTP 404: no such key"

    monkeypatch.setattr(admin_api, "update_key", fake)

    outcome = await run_set_account_key_capability(
        _ProgressRecorder(), **_ADMIN, access_key_id=_KEY_ID, enable=False,
    )
    assert outcome.success is False
    assert "404" in outcome.stderr
    assert outcome.failure_reason == "os_error"
