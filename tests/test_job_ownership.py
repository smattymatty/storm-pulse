"""A result's commitment and a task's lifetime are separate."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from stormpulse.commands.jobs import JobManager, JobManagerClosing, JobOutcome
from stormpulse.protocol import CancelReason


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
    assert manager.in_flight() == []
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
