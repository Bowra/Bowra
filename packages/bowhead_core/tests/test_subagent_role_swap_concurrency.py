"""F6 regression — _execute_tool role swap must be concurrency-safe.

Pre-existing defect (introduced c346aed 2026-05-01 swap + 6449a52 2026-05-13
parallel exec, surfaced by the cca block ③ review): _execute_tool used to mutate
the SHARED parent_ctx (`ctx.role = "analyst"`) and restore it in a finally across
the `await execute`. Under the parallel batch path (_bounded_exec / asyncio.gather)
all siblings share one parent_ctx, so a sibling could snapshot the already-swapped
"analyst" as its "saved" role and restore THAT — corrupting ctx.role to "analyst"
after the batch.

Fix: dispatch against a shallow per-call copy with role overridden; the shared
parent_ctx is never mutated. The swap must still be VISIBLE to the handler (e.g.
kb/permission.py reads ctx.role), which the copy preserves.

These tests assert: (1) the handler sees the swapped "analyst" role; (2) the shared
ctx.role is unchanged after dispatch; (3) under forced concurrency the shared role
is NOT corrupted; (4) running_profile=="lead" is not swapped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSchema,
    ToolSpec,
)

from bowhead_router import ToolCall


def _stub_router() -> Any:
    class _R:
        async def invoke(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

        def stream(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

        async def close(self) -> None:
            pass

    return _R()


def _runner_with_probe(seen: list, barrier: asyncio.Barrier | None = None) -> SubagentRunner:
    async def _probe(args: dict, ctx: Any) -> Any:
        seen.append(ctx.role)  # what the handler actually sees
        if barrier is not None:
            await barrier.wait()  # force concurrent siblings to be in-flight together
        return {"seen_role": ctx.role}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="probe", description="", input_schema={}),
            handler=_probe,
        )
    )
    return SubagentRunner(router=_stub_router(), tool_registry=reg)  # type: ignore[arg-type]


def _ctx(role: str | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        role=role,
    )


@pytest.mark.asyncio
async def test_peer_swap_visible_to_handler_but_shared_ctx_unmutated() -> None:
    seen: list = []
    runner = _runner_with_probe(seen)
    ctx = _ctx("team_admin")

    inv = await runner._execute_tool(
        ToolCall(id="c1", name="probe", arguments={}),
        ctx,
        TurnEventBus(),
        parent_turn_id="t",
        subagent_id="s",
        running_profile="analyst",
    )

    assert seen == ["analyst"]  # swap IS visible to the handler
    assert inv.result == {"seen_role": "analyst"}
    assert ctx.role == "team_admin"  # shared parent_ctx NOT mutated


@pytest.mark.asyncio
async def test_lead_profile_is_not_swapped() -> None:
    seen: list = []
    runner = _runner_with_probe(seen)
    ctx = _ctx("lead")

    await runner._execute_tool(
        ToolCall(id="c1", name="probe", arguments={}),
        ctx,
        TurnEventBus(),
        parent_turn_id="t",
        subagent_id="s",
        running_profile="lead",  # Lead keeps its role (subagent_spawn is lead-only)
    )
    assert seen == ["lead"]
    assert ctx.role == "lead"


@pytest.mark.asyncio
async def test_role_none_is_not_swapped() -> None:
    seen: list = []
    runner = _runner_with_probe(seen)
    ctx = _ctx(None)  # paradigm-v2 off → no enforcement, no swap

    await runner._execute_tool(
        ToolCall(id="c1", name="probe", arguments={}),
        ctx,
        TurnEventBus(),
        parent_turn_id="t",
        subagent_id="s",
        running_profile="analyst",
    )
    assert seen == [None]
    assert ctx.role is None


@pytest.mark.asyncio
async def test_parallel_batch_shared_ctx_role_not_corrupted() -> None:
    """Two _execute_tool concurrently on the SAME ctx (the prod parallel-batch
    shape). A Barrier(2) forces both handlers in-flight simultaneously so the
    OLD shared-mutation code would deterministically corrupt ctx.role to
    'analyst'. With the fix the shared role stays 'team_admin'."""
    seen: list = []
    barrier = asyncio.Barrier(2)
    runner = _runner_with_probe(seen, barrier)
    ctx = _ctx("team_admin")  # one shared parent_ctx, like _legacy_run's batch path
    bus = TurnEventBus()

    invs = await asyncio.gather(
        runner._execute_tool(
            ToolCall(id="a", name="probe", arguments={}),
            ctx,
            bus,
            parent_turn_id="t",
            subagent_id="s",
            running_profile="analyst",
            concurrency_group_id="g",
        ),
        runner._execute_tool(
            ToolCall(id="b", name="probe", arguments={}),
            ctx,
            bus,
            parent_turn_id="t",
            subagent_id="s",
            running_profile="analyst",
            concurrency_group_id="g",
        ),
    )

    assert seen == ["analyst", "analyst"]  # both siblings saw the swapped role
    assert all(i.result == {"seen_role": "analyst"} for i in invs)
    assert ctx.role == "team_admin"  # shared role NOT corrupted by the race
