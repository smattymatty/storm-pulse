"""A result's commitment and a task's lifetime are separate."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.agent.dispatch import post_success_hook
from stormpulse.commands.jobs import JobManager, JobManagerClosing, JobOutcome
from stormpulse.config import CommandSpec
from stormpulse.garage.state import GarageState
from stormpulse.protocol import CancelReason
from stormpulse.signoff import SignoffState
from tests.helpers import (
    FAKE_METRICS,
    SECRET,
    build_config,
    build_garage_config,
    get_garage_state,
    make_fake_garage_state,
    make_garage_bucket,
    matched_test_session,
    set_garage_state,
)


async def succeeds(_progress):
    return JobOutcome(success=True)


@pytest.mark.asyncio
async def test_cleanup_stays_owned_and_duplicate_request_is_rejected():
    entered = asyncio.Event()

    async def cleanup():
        entered.set()
        await asyncio.Event().wait()

    send = AsyncMock()
    manager = JobManager('a', send)
    manager.start('r', manager.QUOTA_COMMAND, 'demo', succeeds, on_success=cleanup)
    await entered.wait()
    assert manager.load()['pending'] == manager.load()['running'] == 1
    assert manager.load()['quota_pending'] == manager.load()['quota_running'] == 1
    with pytest.raises(ValueError):
        manager.start('r', 'demo', 'demo', succeeds)
    task = manager._jobs['r']
    await manager.shutdown_all(CancelReason.SHUTDOWN)
    assert task.done() and manager.active_count() == 0
    assert [call.args[0].type.value for call in send.call_args_list] == [
        'command.result'
    ]
    with pytest.raises(JobManagerClosing, match='Retry after'):
        manager.start('new', 'demo', 'demo', succeeds)


@pytest.mark.asyncio
@pytest.mark.parametrize('written', [False, True])
async def test_cancelled_result_send_never_produces_contradictory_cancellation(written):
    entered = asyncio.Event()
    sent = []

    async def send(frame):
        if written:
            sent.append(frame.type.value)
        entered.set()
        await asyncio.Event().wait()

    manager = JobManager('a', send)
    manager.start('r', 'demo', 'demo', succeeds)
    await entered.wait()
    task = manager._jobs['r']
    await manager.shutdown_all(CancelReason.SHUTDOWN)
    assert task.done()
    assert sent == (['command.result'] if written else [])
    assert manager.load()['pending'] == manager.load()['running'] == 0


@pytest.mark.asyncio
async def test_concurrent_shutdowns_settle_cleanup_and_cancel_only_once():
    entered = asyncio.Event()
    cleaned = []

    async def handler(_progress):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    send = AsyncMock()
    manager = JobManager('a', send)
    manager.start('r', 'demo', 'demo', handler)
    await entered.wait()
    await asyncio.gather(
        manager.shutdown_all(CancelReason.SHUTDOWN),
        manager.shutdown_all(CancelReason.SHUTDOWN),
    )
    assert cleaned == [True]
    assert send.await_count == 1
    assert send.call_args.args[0].type.value == 'command.cancelled'


@pytest.mark.asyncio
async def test_cancellation_before_first_instruction_is_reaped():
    manager = JobManager('a', AsyncMock())
    manager.start('r', 'demo', 'demo', succeeds)
    task = manager._jobs['r']
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert manager._jobs == {} and manager._job_command == {}


@pytest.mark.asyncio
async def test_handler_returning_after_cancellation_cannot_send_success():
    entered = asyncio.Event()

    async def handler(progress):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await progress('running', 1, 1, 'late progress')
            return JobOutcome(success=True)

    send = AsyncMock()
    manager = JobManager('a', send)
    manager.start('r', 'demo', 'demo', handler)
    await entered.wait()
    await manager.shutdown_all(CancelReason.SHUTDOWN)
    assert [call.args[0].type.value for call in send.call_args_list] == [
        'command.cancelled'
    ]


# ---------------------------------------------------------------------------
# A refresh is bound to the session that started its job (CORE-011 d9)
# ---------------------------------------------------------------------------

_BUCKET = 'b' * 64
# How many thread hops the hook makes before each trip point stops it.
_HOPS = {'result': 0, 'read': 1, 'metrics': 2}


@pytest.fixture
def garage_agent(tmp_path, nonce_store, ssl_ctx, shutdown) -> Agent:
    """An agent with a live garage runtime holding one bucket at size 0."""
    cfg = build_config(tmp_path, garage=build_garage_config(tmp_path))
    signoff = SignoffState(cfg.storage.db_path.parent)
    agent = Agent(cfg, SECRET, nonce_store, ssl_ctx, shutdown, signoff_state=signoff)
    bucket = make_garage_bucket(_BUCKET, size_bytes=0)
    set_garage_state(agent, make_fake_garage_state().with_items([bucket]))
    return matched_test_session(agent)


def _bucket_size(agent) -> int:
    state = get_garage_state(agent)
    assert isinstance(state, GarageState)
    return state.buckets[0].size_bytes


async def _run_refresh(agent, at, trip):
    """Run one mutating garage job under a fresh JobManager; its hook re-reads and
    pushes. ``trip(manager)`` fires at ``at``: inside the result send, the read hop
    or the metrics hop. Returns the frame types sent and the hops the hook made."""
    sent, hops = [], 0

    async def send(frame):
        sent.append(frame.type.value)
        if at == 'result' and frame.type.value == 'command.result':
            trip(manager)

    async def to_thread(_fn, *_args):
        nonlocal hops
        hops += 1
        if _HOPS[at] == hops:
            trip(manager)
        bucket = make_garage_bucket(_BUCKET, size_bytes=42)
        return [bucket] if hops == 1 else FAKE_METRICS

    async def handler(_progress):
        return JobOutcome(success=True)

    manager = JobManager('a', send)
    agent.job_manager = manager
    spec = CommandSpec(
        group='garage', command=['/garage'], timeout=60, mode='job', handler=handler
    )
    params = {'bucket_id': _BUCKET}
    hook = post_success_hook(agent, spec, 'garage_bucket_clear', params)
    assert hook is not None
    with patch('asyncio.to_thread', side_effect=to_thread):
        manager.start('r', 'garage_bucket_clear', 'garage', handler, hook, params)
        await manager._jobs['r']
    return sent, hops


@pytest.mark.asyncio
@pytest.mark.parametrize('at', ['result', 'read', 'metrics'])
async def test_refresh_never_publishes_through_a_replacement_session(garage_agent, at):
    """Whichever await the swap lands behind, the old session's refresh stops at the
    next check: nothing reaches either socket, a push already built is discarded."""
    new_send = AsyncMock()
    new = JobManager('a', new_send)

    def swap(_old):
        garage_agent.job_manager = new

    sent, hops = await _run_refresh(garage_agent, at, swap)
    assert hops == _HOPS[at]
    assert sent == ['command.result']
    new_send.assert_not_called()
    assert garage_agent.integrations['garage'].pending_pushes == {}
    assert _bucket_size(garage_agent) == (42 if at == 'metrics' else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('at', ['result', 'read', 'metrics'])
async def test_refresh_never_publishes_through_a_closing_session(garage_agent, at):
    """A closing session's socket still answers; the refresh must stop on the flag
    alone, at the next check, and discard a push it already built."""

    def close(manager):
        # shutdown_all writes this flag first and cancels the task next; the flag
        # alone reaches the checks that cancellation would otherwise pre-empt.
        manager._closing = True

    sent, hops = await _run_refresh(garage_agent, at, close)
    assert hops == _HOPS[at]
    assert sent == ['command.result']
    assert garage_agent.integrations['garage'].pending_pushes == {}
    assert _bucket_size(garage_agent) == (42 if at == 'metrics' else 0)
