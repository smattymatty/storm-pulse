"""The SDK seam: an adapter's job outcome translated to the internal one."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from stormpulse.agent.external_adapters import (
    _translate_command_spec,
    _translate_handler,
)
from stormpulse.commands.jobs import JobOutcome
from stormpulse.protocol import FailureReason
from stormpulse.sdk import SdkCommandSpec, SdkJobOutcome, SdkProgress


async def _progress(*_args: object, **_kw: object) -> None:
    return None


async def _run(reason: str | None) -> JobOutcome:
    async def job(_progress: SdkProgress) -> SdkJobOutcome:
        return SdkJobOutcome(
            success=False, exit_code=1, stderr="it broke", failure_reason=reason
        )

    handler = _translate_handler(lambda _params: job)({})
    assert handler is not None
    return await handler(_progress)


@pytest.mark.asyncio
async def test_an_adapter_reason_the_enum_knows_passes_through() -> None:
    outcome = await _run("timeout")
    assert outcome.failure_reason is FailureReason.TIMEOUT
    assert outcome.error_code is None and outcome.finer_error() is None


@pytest.mark.asyncio
async def test_an_adapter_reason_the_enum_does_not_know_is_integration_failed() -> None:
    """CORE-011 d9: the wire stays a closed set; the adapter's own word rides
    in ``error.code`` so nothing is lost, only moved."""
    outcome = await _run("reseed_failed")
    assert outcome.failure_reason is FailureReason.INTEGRATION_FAILED
    error = outcome.finer_error()
    assert error is not None
    assert (error.code, error.upstream_code, error.message) == (
        "reseed_failed",
        None,
        "it broke",
    )
    assert (await _run(None)).failure_reason is None


@dataclass(frozen=True, slots=True)
class _Figures:
    count: int


def test_a_declared_result_shape_reaches_the_internal_spec() -> None:
    """CORE-011 d9 at the contributed-command seam: the adapter's ``result``
    is the ``CommandSpec.result`` the wire contract declares."""

    async def job(_progress: SdkProgress) -> SdkJobOutcome:
        return SdkJobOutcome(success=True, extras={"count": 1})

    spec = SdkCommandSpec(
        group="g",
        command=["j"],
        timeout=5,
        mode="job",
        handler=lambda _p: job,
        result=_Figures,
    )
    assert _translate_command_spec(spec).result is _Figures
    assert (
        _translate_command_spec(
            SdkCommandSpec(group="g", command=["/bin/true"], timeout=5)
        ).result
        is None
    )


@pytest.mark.asyncio
async def test_an_adapter_word_on_a_success_is_no_error_and_the_result_still_ships() -> (
    None
):
    """An adapter may set ``failure_reason`` on a success (a warning it had no
    other field for). The word must not become ``error`` on a success: the
    payload refuses that pairing, and a refusal here would crash the job after
    its handler returned, so no command.result would ever reach the socket."""
    from stormpulse.commands.jobs import JobManager
    from stormpulse.protocol import Envelope

    async def job(_progress: SdkProgress) -> SdkJobOutcome:
        return SdkJobOutcome(
            success=True, failure_reason="partial_warning", extras={"n": 1}
        )

    sent: list[Envelope] = []

    async def send(envelope: Envelope) -> None:
        sent.append(envelope)

    handler = _translate_handler(lambda _params: job)({})
    assert handler is not None
    mgr = JobManager("agent-1", send)
    mgr.start("req-1", "ext_job", "ext", handler)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    (result,) = sent
    assert result.type.value == "command.result"
    assert result.payload["success"] is True
    assert result.payload["error"] is None
    assert result.payload["result"] == {"n": 1}
