"""Local stalls, active operation waits and explicit cancellation stay distinct."""

import asyncio

import pytest
from bowhead_core.agent_run_limits import AgentRunLimits, AgentRuntimeControls
from bowhead_core.subagent import _controlled_execution, _RuntimeExecution

pytestmark = pytest.mark.unit


async def test_owned_wait_survives_stall_interval_then_unowned_wait_stops_once():
    now = [0.0]
    control = _RuntimeExecution(AgentRuntimeControls(stall_timeout_s=30).budget(AgentRunLimits()), clock=lambda: now[0])
    stopped = []

    async def stop(reason):
        stopped.append(reason)
        return reason

    control.stop = stop
    entered, release, local = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        with control.owned():
            entered.set()
            await release.wait()
        local.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_controlled_execution(work(), control))
    await entered.wait()
    now[0] = 500
    control.changed.set()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await local.wait()
    assert control.progress_at == 500
    now[0] = 531
    control.changed.set()
    assert await asyncio.wait_for(task, timeout=1) == "agent_stalled"
    assert stopped == ["agent_stalled"]


async def test_user_cancellation_is_not_projected_as_a_stall():
    control = _RuntimeExecution(AgentRuntimeControls().budget(AgentRunLimits()))
    entered = asyncio.Event()
    stopped = []

    async def stop(reason):
        stopped.append(reason)

    control.stop = stop

    async def work():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_controlled_execution(work(), control))
    await entered.wait()
    task.cancel("user_cancelled")
    with pytest.raises(asyncio.CancelledError, match="user_cancelled"):
        await task
    assert stopped == []


async def test_failed_owned_operation_releases_its_wait_without_hiding_the_error():
    control = _RuntimeExecution(AgentRuntimeControls().budget(AgentRunLimits()))

    async def work():
        with control.owned():
            raise ValueError("operation failed")

    with pytest.raises(ValueError, match="operation failed"):
        await _controlled_execution(work(), control)
    assert control.owners == 0
