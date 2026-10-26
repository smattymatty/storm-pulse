"""Compatibility fences commands while preserving observer traffic."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, dispatch
from stormpulse.agent.external_adapters import _translate
from stormpulse.agent.session import SessionCompatibility
from stormpulse.agent.wire_contract import contract_digest
from stormpulse.integrations import Integration, registry
from stormpulse.integrations.external.model import SealedGrantV1
from stormpulse.protocol import MessageType
from stormpulse.sdk import SdkIntegration
from tests.helpers import (
    CONTRACT_DIGEST,
    envelope,
    sign_command_request,
    signed_command_sequence,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('sequence', [False, True])
@pytest.mark.parametrize('state', ['mismatch', 'pending'])
async def test_incompatible_commands_resolve_without_side_effects(
    agent: Agent, *, sequence: bool, state: str
):
    agent.session_compatibility = (
        SessionCompatibility(CONTRACT_DIGEST, 'sha256:' + 'ab' * 32)
        if state == 'mismatch'
        else None
    )
    request = (
        signed_command_sequence(['git_pull'], stop_on_failure=False)
        if sequence
        else None
    )
    raw = request.to_json() if request else sign_command_request()
    frame = json.loads(raw)
    ws = AsyncMock()
    with (
        patch.object(dispatch, 'execute_command') as execute,
        patch.object(dispatch, 'dispatch_long_running', new_callable=AsyncMock) as job,
        patch.object(dispatch, 'handle_refresh', new_callable=AsyncMock) as refresh,
    ):
        await dispatch.dispatch_message(agent, ws, raw)
    execute.assert_not_called()
    job.assert_not_called()
    refresh.assert_not_called()
    result = json.loads(ws.send.call_args.args[0])['payload']
    assert result['request_id'] == frame['id']
    assert result['failure_reason'] == 'forbidden'
    assert result['error']['code'] == (
        'contract_mismatch' if state == 'mismatch' else 'registration_pending'
    )
    if sequence:
        assert request is not None
        assert result['sequence_id'] == request.payload['sequence_id']
        assert result['step_index'] == 0


@pytest.mark.asyncio
async def test_an_empty_sequence_is_refused_as_the_sequence_itself(agent: Agent):
    """With no step to name, the refusal names ``command.sequence`` and carries
    the sequence id with no step index: there is no step 0 to point at."""
    agent.session_compatibility = None
    request = signed_command_sequence(steps=[])
    ws = AsyncMock()
    with patch.object(dispatch, 'execute_command') as execute:
        await dispatch.dispatch_message(agent, ws, request.to_json())
    execute.assert_not_called()
    result = json.loads(ws.send.call_args.args[0])['payload']
    assert result['error']['code'] == 'registration_pending'
    assert result['command'] == 'command.sequence'
    assert result['sequence_id'] == request.payload['sequence_id']
    assert result['step_index'] is None


@pytest.mark.asyncio
async def test_fenced_session_keeps_acknowledgements_and_rejects_forgery(agent: Agent):
    agent.session_compatibility = SessionCompatibility(
        CONTRACT_DIGEST, 'sha256:' + 'ab' * 32
    )
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent, ws, envelope(MessageType.HEARTBEAT_ACK, {}).to_json()
    )
    await dispatch.dispatch_message(
        agent,
        ws,
        envelope(
            MessageType.COMMAND_REQUEST,
            {'command': 'git_pull', 'params': {}, 'hmac': 'bad', 'nonce': 'n'},
        ).to_json(),
    )
    ws.send.assert_not_called()


@pytest.mark.parametrize('with_commands', [False, True])
def test_loaded_adapter_does_not_change_release_catalog(*, with_commands: bool):
    before = contract_digest()
    specs = (
        MagicMock(side_effect=AssertionError('release catalog must not call adapter'))
        if with_commands
        else None
    )
    sdk = SdkIntegration(
        id='local_adapter',
        parse_config=lambda c: c,
        enabled=lambda c: True,
        specs=specs,
    )
    grant = SealedGrantV1(
        agent_id='a',
        integration_id=sdk.id,
        publisher_fingerprint='sha256:' + 'ab' * 32,
        package_digest='sha256:' + 'cd' * 32,
        manifest_digest='sha256:' + 'ef' * 32,
        granted_capabilities=(),
        sealed_at='2026-10-09T00:00:00Z',
    )
    adapter = _translate(
        sdk, grant, Path('/nonexistent')
    )  # never asked: nothing is called
    assert not adapter.in_release_catalog
    with patch.object(
        registry, '_integrations', [*registry.registered_integrations(), adapter]
    ):
        assert contract_digest() == before
    if specs is not None:
        specs.assert_not_called()


def test_bad_release_declaration_still_fails_loudly():
    broken = Integration(
        'broken_release', lambda c: c, lambda c: True, specs=lambda c: {}
    )
    with patch.object(
        registry, '_integrations', [*registry.registered_integrations(), broken]
    ):
        with pytest.raises(LookupError, match='declared_config'):
            contract_digest()


def test_minimal_release_integration_needs_no_fake_declaration_config():
    before = contract_digest()
    minimal = Integration('minimal_release', lambda c: c, lambda c: True)
    with patch.object(
        registry, '_integrations', [*registry.registered_integrations(), minimal]
    ):
        assert contract_digest() == before
