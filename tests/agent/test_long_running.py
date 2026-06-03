"""Tests for the long-running command dispatch path.

These verify the param-validation contract (regex pattern enforcement
before the handler factory runs) and the factory plumbing (validated
params reach the factory with defaults merged in).

Tests inject fake factories into ``agent.long_running_factories``
directly — the same seam production uses — rather than monkey-patching
``_make_long_running_handler``. That keeps test setup symmetrical with
how a real Feature publishes its handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.agent import Agent, dispatch
from stormpulse.commands.jobs import JobManager
from stormpulse.config import CommandDef, GarageConfig, ParamDef
from stormpulse.garage.commands import build_garage_commands
from stormpulse.protocol import CommandRequestPayload, Envelope, MessageType

# ---------------------------------------------------------------------------
# Synthetic CommandDef param-validation contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_running_dispatch_rejects_params_failing_regex(
    agent: Agent,
) -> None:
    """A bucket_name that doesn't match the registry regex must produce
    a structured failure result without ever invoking the handler factory.
    """
    test_cmd = CommandDef(
        group="test",
        command=["test_long_running"],
        timeout=60,
        long_running=True,
        params={
            "bucket_name": ParamDef(
                placeholder="bucket_name",
                default=None,
                pattern=r"[a-z0-9_-]+",
                description="bucket",
            ),
        },
    )
    agent.registry["test_long_running"] = test_cmd

    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    agent.job_manager = JobManager(agent.config.agent.id, fake_send)

    factory_invocations: list[dict[str, str]] = []

    def fake_factory(params: dict[str, str]) -> None:
        factory_invocations.append(params)
        return None

    agent.long_running_factories["test_long_running"] = fake_factory

    payload = CommandRequestPayload(
        command="test_long_running",
        params={"bucket_name": "../etc/passwd"},
        hmac="x",
        nonce="x",
    )
    await dispatch.dispatch_long_running(agent, "req-bad", payload, test_cmd)

    assert factory_invocations == []
    assert len(sent) == 1
    failure = sent[0]
    assert failure.type == MessageType.COMMAND_RESULT
    assert failure.payload["request_id"] == "req-bad"
    assert failure.payload["success"] is False
    assert failure.payload["failure_reason"] == "os_error"
    assert "does not match pattern" in failure.payload["stderr"]

    await agent.job_manager.shutdown_all()


@pytest.mark.asyncio
async def test_long_running_dispatch_passes_validated_params_to_factory(
    agent: Agent,
) -> None:
    """When params validate, they reach the handler factory (with
    defaults merged in by ``validate_params``)."""
    test_cmd = CommandDef(
        group="test",
        command=["test_long_running"],
        timeout=60,
        long_running=True,
        params={
            "bucket_name": ParamDef(
                placeholder="bucket_name",
                default=None,
                pattern=r"[a-z0-9_-]+",
                description="bucket",
            ),
            "mode": ParamDef(
                placeholder="mode",
                default="fast",
                pattern=r"fast|slow",
                description="mode",
            ),
        },
    )
    agent.registry["test_long_running"] = test_cmd

    async def fake_send(env: Envelope) -> None:
        pass

    agent.job_manager = JobManager(agent.config.agent.id, fake_send)

    captured: list[dict[str, str]] = []

    def fake_factory(params: dict[str, str]) -> None:
        captured.append(dict(params))
        return None

    agent.long_running_factories["test_long_running"] = fake_factory

    payload = CommandRequestPayload(
        command="test_long_running",
        params={"bucket_name": "valid-bucket"},  # mode omitted → default
        hmac="x",
        nonce="x",
    )
    await dispatch.dispatch_long_running(agent, "req-ok", payload, test_cmd)

    assert captured == [{"bucket_name": "valid-bucket", "mode": "fast"}]

    await agent.job_manager.shutdown_all()


@pytest.mark.asyncio
async def test_no_registered_factory_emits_structured_failure(
    agent: Agent,
) -> None:
    """A CommandDef marked long_running but with no registered factory
    must surface as a structured failure, not a silent drop."""
    test_cmd = CommandDef(
        group="test",
        command=["unregistered"],
        timeout=60,
        long_running=True,
    )
    agent.registry["unregistered"] = test_cmd

    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    agent.job_manager = JobManager(agent.config.agent.id, fake_send)

    payload = CommandRequestPayload(
        command="unregistered",
        params={},
        hmac="x",
        nonce="x",
    )
    await dispatch.dispatch_long_running(agent, "req-no-handler", payload, test_cmd)

    assert len(sent) == 1
    failure = sent[0]
    assert failure.payload["request_id"] == "req-no-handler"
    assert failure.payload["success"] is False
    assert "No long-running handler" in failure.payload["stderr"]

    await agent.job_manager.shutdown_all()


# ---------------------------------------------------------------------------
# garage_bucket_set_cors dispatch integration
# ---------------------------------------------------------------------------


_SET_CORS_VALID_PARAMS = {
    "bucket_name": "media",
    "s3_endpoint": "http://localhost:3900",
    "region": "garage",
    "access_key_id": "GK1",
    "secret_access_key": "secret",
    "origins": '["https://stormdevelopments.ca"]',
}


def _set_cors_cmd_def() -> CommandDef:
    """Pull the real ``garage_bucket_set_cors`` CommandDef from the builder.

    Using the production builder surfaces any drift between test
    expectations and the real registry shape.
    """
    cfg = GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )
    return build_garage_commands(cfg)["garage_bucket_set_cors"]


@pytest.mark.asyncio
async def test_garage_bucket_set_cors_dispatch_validates_origins_pattern(
    agent: Agent,
) -> None:
    """A non-bracketed ``origins`` value violates the registry pattern;
    the dispatcher must emit a structured failure before the factory
    runs."""
    cmd_def = _set_cors_cmd_def()
    agent.registry["garage_bucket_set_cors"] = cmd_def

    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    agent.job_manager = JobManager(agent.config.agent.id, fake_send)

    factory_invocations: list[dict[str, str]] = []

    def fake_factory(params: dict[str, str]) -> None:
        factory_invocations.append(params)
        return None

    agent.long_running_factories["garage_bucket_set_cors"] = fake_factory

    bad_params = dict(_SET_CORS_VALID_PARAMS)
    bad_params["origins"] = "not-bracketed"
    payload = CommandRequestPayload(
        command="garage_bucket_set_cors",
        params=bad_params,
        hmac="x",
        nonce="x",
    )
    await dispatch.dispatch_long_running(agent, "req-bad-origins", payload, cmd_def)

    assert factory_invocations == []
    assert len(sent) == 1
    failure = sent[0]
    assert failure.type == MessageType.COMMAND_RESULT
    assert failure.payload["request_id"] == "req-bad-origins"
    assert failure.payload["success"] is False
    assert failure.payload["failure_reason"] == "os_error"
    assert "does not match pattern" in failure.payload["stderr"]

    await agent.job_manager.shutdown_all()


@pytest.mark.asyncio
async def test_garage_bucket_set_cors_dispatch_passes_json_origins_to_factory(
    agent: Agent,
) -> None:
    """Valid params reach the factory with origins still as a JSON
    string — the factory decodes; the dispatcher just shuttles strings.
    """
    cmd_def = _set_cors_cmd_def()
    agent.registry["garage_bucket_set_cors"] = cmd_def

    async def fake_send(env: Envelope) -> None:
        pass

    agent.job_manager = JobManager(agent.config.agent.id, fake_send)

    captured: list[dict[str, str]] = []

    def fake_factory(params: dict[str, str]) -> None:
        captured.append(dict(params))
        return None

    agent.long_running_factories["garage_bucket_set_cors"] = fake_factory

    payload = CommandRequestPayload(
        command="garage_bucket_set_cors",
        params=dict(_SET_CORS_VALID_PARAMS),
        hmac="x",
        nonce="x",
    )
    await dispatch.dispatch_long_running(agent, "req-ok", payload, cmd_def)

    assert len(captured) == 1
    seen = captured[0]
    assert seen == _SET_CORS_VALID_PARAMS
    # origins is still a JSON string at the dispatcher boundary
    assert isinstance(seen["origins"], str)
    assert seen["origins"].startswith("[") and seen["origins"].endswith("]")

    await agent.job_manager.shutdown_all()
