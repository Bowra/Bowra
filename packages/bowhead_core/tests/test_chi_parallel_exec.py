"""Sprint χ-2 parallel exec tests — gather concurrency + terminal mid-batch + exception wrap.

Per ship 0.15.0 default ON (chi_flags BOWHEAD_CHI_PARALLEL_EXEC=1), the
gather path is now the **main path** for any LLM response emitting ≥2
tool_calls. This file specifically exercises that path.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.errors import InvocationOutcomeUnknownError

from bowhead_bi_bridge import DatarcClient
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
)


def _router(*rs: LLMResponse) -> Router:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = list(rs)
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "modeler", "analyst", "echo", "judge"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


async def test_direct_llm_unknown_escapes_legacy_loop_without_terminal_projection(
    dc,
) -> None:
    router = _router()
    unknown = InvocationOutcomeUnknownError("direct_invoke_unknown")
    calls = 0

    async def _unknown_invoke(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise unknown

    router.invoke = _unknown_invoke  # type: ignore[method-assign]
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend="legacy",
    )
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    bus = TurnEventBus()

    with pytest.raises(InvocationOutcomeUnknownError) as caught:
        await runner.run(
            profile_name="compute",
            task="q",
            parent_turn_id="t",
            parent_bus=bus,
            parent_ctx=ctx,
        )

    assert caught.value is unknown
    assert calls == 1
    assert not {
        "error",
        "turn_failed",
        "done",
    } & {event.event_type for event in bus.snapshot()}


@pytest.fixture
async def dc():
    import httpx2

    class _M(DatarcClient):
        def __init__(self) -> None:
            self._base_url = "http://bi-mock/"
            self._auth_key = "dev"
            self._timeout = 10
            self._http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=bi_mock_app),
                base_url="http://bi-mock/",
                headers={"Authorization": "Bearer dev"},
                timeout=10,
            )

    c = _M()
    try:
        yield c
    finally:
        await c.aclose()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force PARALLEL_EXEC=1 even if ship-default flips elsewhere."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1")
    # Reset GLOBAL_SEM so test gets fresh semaphore each run.
    from bowhead_core import _chi_runtime

    _chi_runtime._reset_for_test()


# ─────────────────────────────────────────────────────────────
# UT-1: gather truly concurrent (wall time < sum)
# ─────────────────────────────────────────────────────────────
async def test_parallel_gather_concurrent_wall_time(dc) -> None:
    """3 bi_query each sleeping ~0.4s → wall time ≪ 1.2s (sequential sum)
    when parallel exec ON. bi_mock responds <50ms typically, so we patch
    _execute_tool to inject delay.
    """
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="bi_query", arguments={"text": "q1"}),
                ToolCall(id="c2", name="bi_query", arguments={"text": "q2"}),
                ToolCall(id="c3", name="bi_query", arguments={"text": "q3"}),
            ],
        ),
        LLMResponse(content="done"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")

    # Replace _execute_tool with pure sleep stub (bypass bi_mock contention).
    from bowhead_core.subagent import ToolInvocation

    async def _sleep_only(tc, *args, **kwargs):
        await asyncio.sleep(0.4)
        return ToolInvocation(
            tool_name=tc.name,
            arguments=tc.arguments,
            result={"ok": True, "stub": True},
            error=None,
        )

    runner._execute_tool = _sleep_only  # type: ignore[method-assign]

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    bus = TurnEventBus()
    t0 = time.perf_counter()
    result = await runner.run(
        profile_name="compute",
        task="q",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    wall = time.perf_counter() - t0
    assert result.ok
    # Sequential would be ~1.2s; parallel ≈0.4s + overhead. Allow up to 0.9s.
    assert wall < 0.9, f"parallel wall time {wall:.2f}s should be <0.9s for 3×0.4s tasks"


# ─────────────────────────────────────────────────────────────
# UT-2: terminal_idx mid-batch — invocations after terminal skipped from messages
# ─────────────────────────────────────────────────────────────
async def test_parallel_terminal_idx_skips_post_terminal_messages(dc) -> None:
    """Batch [bi_query, subagent_return(__terminal=True), bi_query] — only
    first 2 messages append; the trailing bi_query has __skipped_post_terminal=True
    and is NOT in the message history.

    Use mocked _execute_tool to control inv.result shape precisely
    (real subagent_return handler args/shape mismatch otherwise).
    """
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="bi_query", arguments={"text": "q1"}),
                ToolCall(id="c2", name="subagent_return", arguments={"ok": True, "result": "done"}),
                ToolCall(id="c3", name="bi_query", arguments={"text": "q3"}),
            ],
        ),
        LLMResponse(content="fallback"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")

    # Mock _execute_tool: subagent_return returns __terminal=True/ok=True; others OK.
    from bowhead_core.subagent import ToolInvocation

    async def _mock_exec(tc, *args, **kwargs):
        if tc.name == "subagent_return":
            return ToolInvocation(
                tool_name=tc.name,
                arguments=tc.arguments,
                result={"__terminal": True, "ok": True, "value": "done"},
                error=None,
            )
        return ToolInvocation(
            tool_name=tc.name,
            arguments=tc.arguments,
            result={"ok": True, "stub": True},
            error=None,
        )

    runner._execute_tool = _mock_exec  # type: ignore[method-assign]

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    bus = TurnEventBus()
    result = await runner.run(
        profile_name="compute",
        task="q",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    # Verify post-terminal bi_query (c3) not in invocations (Phase 3 skipped append).
    inv_names = [inv.tool_name for inv in result.tool_invocations]
    assert "subagent_return" in inv_names
    bi_query_count = sum(1 for n in inv_names if n == "bi_query")
    assert bi_query_count == 1, f"expected 1 bi_query (pre-terminal), got {bi_query_count}: {inv_names}"


# ─────────────────────────────────────────────────────────────
# UT-3: exception wrap — one tc raises, batch finishes with failed inv
# ─────────────────────────────────────────────────────────────
async def test_parallel_exception_wrap_does_not_crash_batch(dc) -> None:
    """If _execute_tool raises for one tc, others continue; that tc returns
    ToolInvocation(error=str(e)[:300]) rather than crashing the whole batch."""
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="bi_query", arguments={"text": "ok1"}),
                ToolCall(id="c2", name="bi_query", arguments={"text": "ok2"}),
            ],
        ),
        LLMResponse(content="done"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")

    # Patch _execute_tool to raise for c2 only.
    orig = runner._execute_tool

    async def _selective_raise(tc, *args, **kwargs):
        if tc.id == "c2":
            raise RuntimeError("simulated handler crash")
        return await orig(tc, *args, **kwargs)

    runner._execute_tool = _selective_raise  # type: ignore[method-assign]

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    bus = TurnEventBus()
    result = await runner.run(
        profile_name="compute",
        task="q",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    # Batch did NOT crash — runner returned. result.ok depends on whether
    # crashed tc constituted a fatal path; we mainly verify batch finished.
    assert result.subagent_id.startswith("sub_")
    # 2 invocations recorded (one OK, one with error wrap)
    assert len(result.tool_invocations) == 2
    errors = [inv.error for inv in result.tool_invocations if inv.error]
    assert any("simulated handler crash" in (e or "") for e in errors), f"expected wrapped exception in invocations, got errors={errors}"


@pytest.mark.parametrize("failure_kind", ["unknown", "durability_abort"])
async def test_parallel_durable_failure_waits_for_siblings_then_escapes(
    dc,
    failure_kind: str,
) -> None:
    """Durable failures are not ordinary invocations and gather drains siblings."""

    router = _router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="unknown", name="bi_query", arguments={"text": "a"}),
                ToolCall(id="settled", name="bi_query", arguments={"text": "b"}),
            ],
        ),
        LLMResponse(content="must not dispatch after UNKNOWN"),
    )
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend="legacy",
    )
    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def _controlled_exec(tc, *args, **kwargs):
        del args, kwargs
        if tc.id == "unknown":
            await sibling_started.wait()
            if failure_kind == "unknown":
                raise InvocationOutcomeUnknownError(
                    "tool_handler_outcome_unknown",
                )
            raise TerminalDurabilityAbort("tool_marker_unavailable")
        sibling_started.set()
        await asyncio.sleep(0)
        sibling_finished.set()
        from bowhead_core.subagent import ToolInvocation

        return ToolInvocation(
            tool_name=tc.name,
            arguments=tc.arguments,
            result={"ok": True},
            error=None,
        )

    runner._execute_tool = _controlled_exec  # type: ignore[method-assign]
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )

    expected = InvocationOutcomeUnknownError if failure_kind == "unknown" else TerminalDurabilityAbort
    with pytest.raises(expected) as caught:
        await runner.run(
            profile_name="compute",
            task="q",
            parent_turn_id="t",
            parent_bus=TurnEventBus(),
            parent_ctx=ctx,
        )
    if failure_kind == "unknown":
        assert caught.value.reason_code == "tool_handler_outcome_unknown"
    assert sibling_finished.is_set()
