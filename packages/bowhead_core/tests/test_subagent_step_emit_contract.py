"""Sprint chat-redesign-2026-05-21 Phase 1.1 — emit-contract for _execute_tool.

Per memory `feedback_ut_must_assert_emit_contract`: when backend changes emit
behavior, UT must assert the bus event sequence + payload shape, not just
return value. Earlier `test_subagent_step_emit.py` only checked helpers /
signature. This file fills the gap by driving `_execute_tool` end-to-end
against a fake registry + capturing bus.

Asserts:
1. Happy path: tool_call → step_started → tool_result → step_done in order
   on the same turn bus; step_done.state == "success"; concurrency_group_id
   propagates from caller kwarg.
2. Error path: when tool_registry.execute raises, step_done STILL fires
   with state="failure" + error.type="tool_exception" (orphan prevention).
3. step_done payload mirrors tool_result's back-compat fields during the
   Phase 1.1 dual-emit window (FE replay can use either source).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language
from bowhead_core.events import (
    OrchestratorEvent,
    TerminalDurabilityAbort,
    TurnEventBus,
)
from bowhead_core.subagent import SubagentRunner, _admission_block_guidance, _extract_step_code_input, _serialize
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSchema,
    ToolSpec,
)

from bowhead_router import ToolCall


def _make_runner(tool_handlers: dict[str, Any]) -> SubagentRunner:
    """Build a SubagentRunner with a stub registry + null router."""
    reg = ToolRegistry()
    for name, handler in tool_handlers.items():
        spec = ToolSpec(
            schema=ToolSchema(name=name, description="", input_schema={}),
            handler=handler,
        )
        reg.register(spec)

    # router not used in _execute_tool path — pass a stub
    class _StubRouter:
        async def invoke(self, *a, **kw):
            raise NotImplementedError

        def stream(self, *a, **kw):
            raise NotImplementedError

        async def close(self):
            pass

    return SubagentRunner(router=_StubRouter(), tool_registry=reg)  # type: ignore[arg-type]


def _make_ctx(turn_id: str = "t_test") -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,
        datarc=None,  # type: ignore[arg-type]  # not exercised by handlers in this test
        role="lead",
    )


def test_extract_step_code_input_bounds_utf8_and_keeps_legacy_direct_script() -> None:
    direct = _extract_step_code_input("python_sandbox", {"script": "print(1)"})
    assert direct == {"language": "python", "code": "print(1)", "truncated": False}

    bounded = _extract_step_code_input(
        "python_sandbox",
        {"input": {"code": "数" * 30_000}},
    )
    assert bounded is not None
    assert bounded["truncated"] is True
    assert len(bounded["code"].encode("utf-8")) <= 64 * 1024
    assert _extract_step_code_input("bi_query", {"code": "secret"}) is None


async def _drain_bus(bus: TurnEventBus) -> list[OrchestratorEvent]:
    """Subscribe + collect everything until done sentinel."""
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    asyncio.create_task(_consume())
    # Yield control so subscriber drains queue
    await asyncio.sleep(0)
    return captured  # caller must close bus + await task for full drain


@pytest.mark.asyncio
async def test_execute_tool_happy_path_emits_full_sequence() -> None:
    """tool_call → step_started → tool_result → step_done with success state."""

    async def _ok_handler(args: dict, ctx) -> Any:
        return {"answer": 42}

    runner = _make_runner({"my_tool": _ok_handler})
    ctx = _make_ctx()
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(id="call_1", name="my_tool", arguments={"q": "x"})
    inv = await runner._execute_tool(
        tc,
        ctx,
        bus,
        parent_turn_id="t_test",
        subagent_id="lead_root",
        running_profile="lead",
        concurrency_group_id="grp_abc",
    )
    assert inv.error is None
    assert inv.result == {"answer": 42}

    # close bus
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer

    types = [ev.event_type for ev in captured]
    # Sprint chat-redesign p0-p1-followup B2 — legacy `tool_call` emit DELETED
    # (dual-emit cleanup). Step List Card driven by step_started/step_done.
    # tool_result kept (FE legacy translator + Inspector + result_inline use it).
    assert "tool_call" not in types
    assert "step_started" in types
    assert "tool_result" in types and "step_done" in types
    assert types.index("tool_result") < types.index("step_done")
    assert types.index("step_started") < types.index("step_done")
    # Both step_started + step_done fire
    started = [e for e in captured if e.event_type == "step_started"]
    done = [e for e in captured if e.event_type == "step_done"]
    assert len(started) == 1
    assert len(done) == 1
    # step_started payload carries the concurrency_group_id from caller kwarg
    assert started[0].payload["concurrency_group_id"] == "grp_abc"
    assert started[0].payload["step_type"] == "tool_run"
    assert started[0].payload["step_id"] == "step_call_1"
    assert started[0].payload["header_zh"] == "my_tool"  # unmapped → raw name
    # step_done payload mirrors tool_result back-compat fields
    assert done[0].payload["state"] == "success"
    assert done[0].payload["tool_name"] == "my_tool"
    assert done[0].payload["call_id"] == "call_1"
    assert done[0].payload["ok"] is True
    assert "duration_ms" in done[0].payload


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["agent_not_runnable", "agent_candidate_stale"])
@pytest.mark.parametrize("ui_language", ["zh-CN", "en-US"])
async def test_admission_denial_keeps_model_guidance_out_of_user_trace(code: str, ui_language: str) -> None:
    guidance = _admission_block_guidance(code)

    async def _denied(_args: dict, _ctx) -> Any:
        return {"ok": False, "error": guidance, "error_code": code, "error_kind": "agent_admission_blocked", "profile": "compute"}

    runner = _make_runner({"subagent_spawn": _denied})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for event in bus.subscribe():
            captured.append(event)

    consumer = asyncio.create_task(_consume())
    language_token = set_current_ui_language(ui_language)
    try:
        invocation = await runner._execute_tool(
            ToolCall(id="call_denied", name="subagent_spawn", arguments={"profile": "compute", "task": "test"}),
            _make_ctx(),
            bus,
            parent_turn_id="t_test",
            subagent_id="lead_root",
            running_profile="lead",
        )
    finally:
        reset_current_ui_language(language_token)
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer

    assert guidance in _serialize(invocation)
    visible = [event.payload for event in captured if event.event_type in {"tool_result", "step_done", "tool_finding"}]
    assert all("Dispatch refused" not in repr(payload) and "dispatchable roster" not in repr(payload) for payload in visible)
    assert not any(event.event_type == "tool_finding" for event in captured)
    step = next(event.payload for event in captured if event.event_type == "step_done")
    assert step["state"] == "failure"
    assert step["error"]["code"] == code
    assert step["error"]["is_transient"] is False
    assert ("可用智能体" if ui_language == "zh-CN" else "available Agent") in step["error"]["message_zh"]


@pytest.mark.asyncio
async def test_execute_tool_surfaces_python_sandbox_tool_result_name() -> None:
    """skill_invoke(name=python_sandbox) must surface the same tool_name everywhere.

    Regression: step_started/step_done already surfaced python_sandbox, but
    tool_result still emitted the dispatcher wrapper skill_invoke. Admin trace
    and legacy/replay paths then disagreed for the same call_id.
    """

    async def _skill_invoke(args: dict, ctx) -> Any:
        return {
            "ok": True,
            "stdout": "hello\n",
            "exit_code": 0,
            "output_artifacts": [
                {
                    "artifact_id": "art_stdout",
                    "name": "sandbox-stdout.txt",
                    "mime": "text/plain",
                    "size": 6,
                    "data_role": "stdout",
                }
            ],
        }

    runner = _make_runner({"skill_invoke": _skill_invoke})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(
        id="call_py",
        name="skill_invoke",
        arguments={
            "name": "python_sandbox",
            "script": "runner.py",
            "input": {"code": "print('hello')"},
        },
    )
    await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_py",
        subagent_id="peer_compute",
        running_profile="compute",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_py", payload={}))
    await consumer

    started = next(e for e in captured if e.event_type == "step_started")
    result = next(e for e in captured if e.event_type == "tool_result")
    done = next(e for e in captured if e.event_type == "step_done")
    assert started.payload["tool_name"] == "python_sandbox"
    assert started.payload["code_input"] == {
        "language": "python",
        "code": "print('hello')",
        "truncated": False,
    }
    assert result.payload["tool_name"] == "python_sandbox"
    assert done.payload["tool_name"] == "python_sandbox"
    expected_artifacts = [
        {
            "artifact_id": "art_stdout",
            "filename": "sandbox-stdout.txt",
            "mime": "text/plain",
            "size": 6,
            "data_role": "stdout",
        }
    ]
    assert result.payload["result_meta"]["output_artifacts"] == expected_artifacts
    assert done.payload["result_meta"]["output_artifacts"] == expected_artifacts


@pytest.mark.asyncio
async def test_execute_tool_keeps_unlisted_skill_invoke_result_name() -> None:
    """Only curated inner skills surface; generic skill calls stay skill_invoke."""

    async def _skill_invoke(args: dict, ctx) -> Any:
        return {"ok": True, "summary": "done"}

    runner = _make_runner({"skill_invoke": _skill_invoke})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(
        id="call_skill",
        name="skill_invoke",
        arguments={"name": "analyst_sop", "input": "x"},
    )
    await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_skill",
        subagent_id="peer_compute",
        running_profile="compute",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_skill", payload={}))
    await consumer

    result = next(e for e in captured if e.event_type == "tool_result")
    done = next(e for e in captured if e.event_type == "step_done")
    assert result.payload["tool_name"] == "skill_invoke"
    assert done.payload["tool_name"] == "skill_invoke"


@pytest.mark.asyncio
async def test_execute_tool_stamps_provider_call_id_for_citation_seam() -> None:
    """接设计稿 P2 provenance id seam: a leaf tool's inv.call_id MUST equal the
    provider tc.id — that's the id the tool-call EVENT carries and that the
    source_ref_aggregator keys SourceRef.id on. `[num:..:src:S]` markers use
    S=inv.call_id, so without this the citation popover resolves to a stale
    source (pre-fix the invocation carried an independent auto `tl_*` id)."""

    async def _ok(args: dict, ctx) -> Any:
        return {"rows": [{"v": 123456789.0}]}

    runner = _make_runner({"bi_query": _ok})
    bus = TurnEventBus()
    consumer = asyncio.create_task(_consume_until_done(bus))
    tc = ToolCall(id="call_provider_xyz", name="bi_query", arguments={"query": "q"})
    inv = await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_test",
        subagent_id="lead_root",
        running_profile="lead",
    )
    assert inv.call_id == "call_provider_xyz"  # == tc.id, NOT an auto tl_*
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer


@pytest.mark.asyncio
async def test_execute_tool_excludes_subagent_spawn_from_id_stamp() -> None:
    """subagent_spawn's call_id is the trace nesting key (turn_runner
    `parent_subagent_id=inv.call_id`), so the citation id-seam stamp MUST skip it
    — the spawn invocation keeps its own id, only leaf tools get tc.id."""

    async def _spawn(args: dict, ctx) -> Any:
        return {"spawned": True}

    runner = _make_runner({"subagent_spawn": _spawn})
    bus = TurnEventBus()
    consumer = asyncio.create_task(_consume_until_done(bus))
    tc = ToolCall(id="call_spawn_tc", name="subagent_spawn", arguments={})
    inv = await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_test",
        subagent_id="lead_root",
        running_profile="lead",
    )
    assert inv.call_id != "call_spawn_tc"  # excluded → keeps its own (auto) id
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer


@pytest.mark.asyncio
async def test_execute_tool_keeps_auto_id_when_provider_omits_call_id() -> None:
    """Empty tc.id (some openai-compat responses omit tool-call ids → `id or ""`)
    must NOT wipe inv.call_id to "" — that would make step_id "tool_" and drop the
    value→source map key. The stamp is skipped, keeping the auto `tl_*` id."""

    async def _ok(args: dict, ctx) -> Any:
        return {"rows": [{"v": 1.0}]}

    runner = _make_runner({"bi_query": _ok})
    bus = TurnEventBus()
    consumer = asyncio.create_task(_consume_until_done(bus))
    tc = ToolCall(id="", name="bi_query", arguments={})
    inv = await runner._execute_tool(
        tc,
        _make_ctx(),
        bus,
        parent_turn_id="t_test",
        subagent_id="lead_root",
        running_profile="lead",
    )
    assert inv.call_id, "call_id must stay non-empty (auto tl_*) when tc.id is empty"
    assert inv.call_id != ""
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer


async def _consume_until_done(bus: TurnEventBus) -> list[OrchestratorEvent]:
    out: list[OrchestratorEvent] = []
    async for ev in bus.subscribe():
        out.append(ev)
        if ev.event_type == "done":
            break
    return out


@pytest.mark.asyncio
async def test_tool_result_ok_matches_step_done_ok_for_result_states() -> None:
    """Rejected attempts remain failures in both projections, even when retryable."""

    async def _invalid_args(args: dict, ctx) -> Any:
        return {"ok": False, "error_kind": "invalid_args", "error": "missing name"}

    async def _hard_fail(args: dict, ctx) -> Any:
        return {"ok": False, "error": "boom"}

    async def _run(name: str, handler: Any) -> tuple[bool, bool]:
        runner = _make_runner({name: handler})
        bus = TurnEventBus()
        captured: list[OrchestratorEvent] = []

        async def _consume() -> None:
            async for ev in bus.subscribe():
                captured.append(ev)

        consumer = asyncio.create_task(_consume())
        tc = ToolCall(id="call_x", name=name, arguments={})
        await runner._execute_tool(
            tc,
            _make_ctx(),
            bus,
            parent_turn_id="t_test",
            subagent_id="lead_root",
            running_profile="lead",
        )
        await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
        await consumer
        tr = next(e for e in captured if e.event_type == "tool_result")
        sd = next(e for e in captured if e.event_type == "step_done")
        return bool(tr.payload["ok"]), bool(sd.payload["ok"])

    tr_ok, sd_ok = await _run("ia_tool", _invalid_args)
    assert tr_ok is False and sd_ok is False, ("invalid_args must fail on both", tr_ok, sd_ok)

    tr_ok2, sd_ok2 = await _run("hf_tool", _hard_fail)
    assert tr_ok2 is False and sd_ok2 is False, ("clean ok:False should fail on both", tr_ok2, sd_ok2)


def _bi_query_result_with_heavy_metadata(n_rows: int) -> dict[str, Any]:
    """Few rows plus model-facing parse traces and hints, the shape that crossed the cap in production."""
    columns = ["region", "manager", "sales", "target", "attainment"]
    tokens = [{"word": f"term{i} ", "kind": "dimension", "field": f"field_{i}"} for i in range(120)]
    return {
        "columns": columns,
        "rows": [{c: f"{c}-{i}" for c in columns} for i in range(n_rows)],
        "row_count": n_rows,
        "rows_shown": n_rows,
        "total_rows": n_rows,
        "_columns_before_projection": 6,
        "artifact_id": "art_rows",
        "query_explain": {"filters": ["region = east"], "measures": ["sales", "target", "attainment"]},
        "_parse": {"tokens": tokens, "unknown_words": []},
        "_debug_cut_props": [{"word": t["word"], "prop": f"d{i}"} for i, t in enumerate(tokens)],
        "_how_to_use_in_sandbox": "Pass inputs to python_sandbox and read INPUTS['rows']['path']. " * 10,
    }


@pytest.mark.asyncio
async def test_bi_query_step_shows_rows_the_context_cap_withheld_from_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The context cap trims inline rows for the model only; the step card keeps the real rows and count."""
    from bowhead_core.tool_result_budget import DEFAULT_CAPS_TOKEN, cap_result

    monkeypatch.setitem(DEFAULT_CAPS_TOKEN, "bi_query", 1500)  # A small budget makes this fixture cross the cap.

    capped, _ = cap_result("bi_query", _bi_query_result_with_heavy_metadata(9))
    assert len(capped["rows"]) < 9, "precondition: model-visible metadata must push this result past the bi_query cap"

    async def _bi_query(args: dict, ctx) -> Any:
        return _bi_query_result_with_heavy_metadata(9)

    runner = _make_runner({"bi_query": _bi_query})
    ctx = _make_ctx()
    ctx.datarc = object()  # type: ignore[assignment]  # BI gate needs a bound connection; the stub handler never uses it
    ctx.active_datarc_project = "2"
    bus = TurnEventBus()
    consumer = asyncio.create_task(_consume_until_done(bus))
    inv = await runner._execute_tool(
        ToolCall(id="call_bi", name="bi_query", arguments={"queries": ["east managers"]}),
        ctx,
        bus,
        parent_turn_id="t_test",
        subagent_id="sub_1",
        running_profile="peer",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    captured = await consumer

    assert len(inv.result["rows"]) < 9, "the model still receives the capped result"
    done = next(e for e in captured if e.event_type == "step_done").payload
    assert done["output_preview"].startswith("9 rows"), done["output_preview"]
    assert len(done["result_inline"]["rows"]) == 9
    assert done["result_inline"]["artifact_id"] == "art_rows"
    tool_result = next(e for e in captured if e.event_type == "tool_result").payload
    assert tool_result["result_inline"] == done["result_inline"], "live tool_result and step_done must agree"


def test_bi_query_summary_without_inline_rows_reports_the_full_count() -> None:
    from bowhead_core.subagent import _prettify_output_preview

    summary = _prettify_output_preview("bi_query", {"rows": [], "columns": ["a", "b"], "total_rows": 9}, None, ui_language="en-US")
    assert summary == "9 rows · 2 cols"


@pytest.fixture
def zh_ui() -> Any:
    """The previews below assert zh-CN copy; without a turn interface language they are English."""
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    token = set_current_ui_language("zh-CN")
    yield
    reset_current_ui_language(token)


@pytest.mark.asyncio
async def test_expected_outcome_kinds_render_non_failure(zh_ui: Any) -> None:
    """feedback-0713 #1/#2: 预期内 outcome（needs_clarification 澄清哨兵 /
    not_accessible 预期 miss）绝不渲染成红色失败步骤。

    prod s_f7f79d3d2ceb: per-user 选工作区澄清（设计内 HITL）与 switch_context
    查无此名（错别字, 模型列候选问用户——行为正确）都被 step 状态机按
    ok=False → state=failure 渲染成「运行报错」+ FailureAckCard, 与澄清卡并存。
    契约: step_done.state == success、无 error payload、tool_result.ok == True、
    output_preview 是中性文案（非报错口吻）。
    """

    async def _clarification_sentinel(args: dict, ctx) -> Any:
        # tool_registry per-user dependency gate's tool_context_select sentinel shape
        return {
            "ok": False,
            "error_kind": "needs_clarification",
            "clarification": {
                "kind": "tool_context_select",
                "tool": "datarc",
                "prompt": "你在北极九章有 23 个可访问的工作区，这次查询用哪个？",
                "options": [{"value": "1", "label": "商超零售场景"}],
            },
            "rows": [],
        }

    async def _not_accessible(args: dict, ctx) -> Any:
        # tool_switch_context hard-miss shape (with did_you_mean)
        return {
            "ok": False,
            "error": "not_accessible",
            "error_kind": "not_accessible",
            "did_you_mean": ["商超零售场景"],
            "accessible": ["商超零售场景"],
        }

    async def _drive(name: str, handler: Any) -> dict[str, Any]:
        runner = _make_runner({name: handler})
        bus = TurnEventBus()
        captured: list[OrchestratorEvent] = []

        async def _consume() -> None:
            async for ev in bus.subscribe():
                captured.append(ev)

        consumer = asyncio.create_task(_consume())
        tc = ToolCall(id="call_x", name=name, arguments={})
        await runner._execute_tool(
            tc,
            _make_ctx(),
            bus,
            parent_turn_id="t_test",
            subagent_id="lead_root",
            running_profile="lead",
        )
        await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
        await consumer
        tr = next(e for e in captured if e.event_type == "tool_result")
        sd = next(e for e in captured if e.event_type == "step_done")
        return {"tool_result": tr.payload, "step_done": sd.payload}

    # Use a neutral tool name: bi_* hits the dispatch-layer Datarc dependency gate (ctx.datarc=None
    # raises DatarcUnavailableError before the handler); the state machine is independent of the tool.
    got = await _drive("picker_tool", _clarification_sentinel)
    assert got["step_done"]["state"] == "success"
    assert got["step_done"]["ok"] is True
    assert "error" not in got["step_done"], "预期内澄清不得携带 error payload（否则 FE 又渲染失败行）"
    assert got["tool_result"]["ok"] is True
    assert got["step_done"]["output_preview"].startswith("等待你选择"), got["step_done"]["output_preview"]

    got2 = await _drive("miss_tool", _not_accessible)
    assert got2["step_done"]["state"] == "success"
    assert got2["step_done"]["ok"] is True
    assert "error" not in got2["step_done"]
    assert got2["tool_result"]["ok"] is True
    assert "未匹配到该工作区" in got2["step_done"]["output_preview"]


@pytest.mark.asyncio
async def test_expected_outcome_family_followup_kinds_render_non_failure(zh_ui: Any) -> None:
    """feedback-0713 F4: B3 记录的同家族漏网收口——switch 同 turn 幂等再触发
    （pending_user_confirmation）与 resume 上拒绝二次澄清
    （clarification_refused_on_resume）都是设计内护栏, 不得渲染红色失败步骤。
    契约同 B3: state=success、无 error payload、tool_result.ok=True、中性预览。
    """

    async def _pending(args: dict, ctx) -> Any:
        # tool_registry switch_context same-turn idempotency guard shape
        return {
            "ok": False,
            "error": "pending_user_confirmation",
            "error_kind": "pending_user_confirmation",
            "accessible": ["商超零售场景", "电商场景"],
        }

    async def _refused(args: dict, ctx) -> Any:
        # clarification_tool same-turn resume guard shape
        return {
            "ok": False,
            "clarification_refused_on_resume": True,
            "error_kind": "clarification_refused_on_resume",
            "note": "同一轮不要再问第二次澄清",
        }

    async def _drive(name: str, handler: Any) -> dict[str, Any]:
        runner = _make_runner({name: handler})
        bus = TurnEventBus()
        captured: list[OrchestratorEvent] = []

        async def _consume() -> None:
            async for ev in bus.subscribe():
                captured.append(ev)

        consumer = asyncio.create_task(_consume())
        tc = ToolCall(id="call_x", name=name, arguments={})
        await runner._execute_tool(
            tc,
            _make_ctx(),
            bus,
            parent_turn_id="t_test",
            subagent_id="lead_root",
            running_profile="lead",
        )
        await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
        await consumer
        tr = next(e for e in captured if e.event_type == "tool_result")
        sd = next(e for e in captured if e.event_type == "step_done")
        return {"tool_result": tr.payload, "step_done": sd.payload}

    got = await _drive("pending_tool", _pending)
    assert got["step_done"]["state"] == "success"
    assert got["step_done"]["ok"] is True
    assert "error" not in got["step_done"]
    assert got["tool_result"]["ok"] is True
    assert "等待你确认" in got["step_done"]["output_preview"]

    got2 = await _drive("refused_tool", _refused)
    assert got2["step_done"]["state"] == "success"
    assert got2["step_done"]["ok"] is True
    assert "error" not in got2["step_done"]
    assert got2["tool_result"]["ok"] is True
    assert "本轮已澄清过" in got2["step_done"]["output_preview"]


@pytest.mark.asyncio
async def test_execute_tool_exception_emits_step_done_failure_orphan_prevention() -> None:
    """When tool handler raises, step_done STILL fires with state=failure +
    error.type=tool_exception — orphan-step prevention.
    """

    async def _raising_handler(args: dict, ctx) -> Any:
        raise ValueError("simulated tool boom")

    runner = _make_runner({"flaky": _raising_handler})
    ctx = _make_ctx()
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(id="call_x", name="flaky", arguments={})
    inv = await runner._execute_tool(
        tc,
        ctx,
        bus,
        parent_turn_id="t_err",
        subagent_id="peer_1",
        running_profile="compute",
    )
    # inv carries the exception text (registry's internal try/except or our
    # newer outer guard catches it and wraps as ToolInvocation.error). Exact
    # prefix can be either "unexpected_error" (registry) or "tool_exception"
    # (our guard); both signal handler failure.
    assert inv.error is not None
    assert "simulated tool boom" in inv.error

    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_err", payload={}))
    await consumer

    # Both step_started + step_done fire even on handler exception
    # (orphan-step prevented — FE never sees forever-active step).
    started = [e for e in captured if e.event_type == "step_started"]
    done = [e for e in captured if e.event_type == "step_done"]
    assert len(started) == 1
    assert len(done) == 1
    assert done[0].payload["state"] == "failure"
    # step_done.error.type is the classification (tool_exception per our mapping)
    assert done[0].payload["error"]["type"] == "tool_exception"
    assert done[0].payload["error"]["is_transient"] is True
    assert "simulated tool boom" in done[0].payload["error"]["message_zh"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["raise", "missing_ticket"])
async def test_durable_step_started_failure_aborts_before_handler(
    failure_mode: str,
) -> None:
    calls = 0

    async def _must_not_run(args: dict, ctx) -> Any:
        nonlocal calls
        calls += 1
        return {"ok": True}

    class _FailingDurableBus(TurnEventBus):
        async def emit_tool_step_started(
            self,
            event: OrchestratorEvent,
        ) -> None:
            del event
            if failure_mode == "raise":
                raise TerminalDurabilityAbort("durable marker failed")

    runner = _make_runner({"write_tool": _must_not_run})
    with pytest.raises(TerminalDurabilityAbort, match=r"durable .*marker|durable marker"):
        await runner._execute_tool(
            ToolCall(id="call_durable", name="write_tool", arguments={}),
            _make_ctx(),
            _FailingDurableBus(),
            parent_turn_id="t_test",
            subagent_id="lead_root",
            running_profile="lead",
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_pbi_gate_emits_stable_code_and_typed_diagnostics() -> None:
    async def _unavailable(args: dict, ctx) -> Any:
        return {"ok": False, "error_kind": "pbi", "error_code": "pbi_connection_unavailable"}

    runner = _make_runner({"teammcp.ibt_powerbi.pbi_execute_dax": _unavailable})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())
    invocation = await runner._execute_tool(
        ToolCall(
            id="pbi_gate",
            name="teammcp.ibt_powerbi.pbi_execute_dax",
            arguments={"dax": "EVALUATE X"},
        ),
        _make_ctx(),
        bus,
        parent_turn_id="t_pbi_gate",
        subagent_id="lead_root",
    )
    await bus.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="t_pbi_gate",
            payload={},
        ),
    )
    await consumer

    assert invocation.result["error_code"] == "pbi_connection_unavailable"
    done = next(event for event in captured if event.event_type == "step_done")
    assert done.payload["error"] == {
        "type": "pbi",
        "message_zh": "pbi_connection_unavailable",
        "is_transient": False,
        "code": "pbi_connection_unavailable",
        "diagnostics": {
            "schema_version": 1,
            "disclosure": "authenticated",
            "diagnostic_code": "powerbi_query_failure",
            "safe_detail": "phase=prepare; failure_class=state",
            "retryable": False,
        },
    }


@pytest.mark.asyncio
async def test_pbi_result_failure_emits_only_registered_diagnostics() -> None:
    from bowhead_core.published_diagnostics import (
        publish_powerbi_query_failure,
    )

    published = publish_powerbi_query_failure(
        "pbi_query_result_incomplete",
    )
    assert published is not None

    async def _incomplete(args: dict, ctx) -> Any:
        return {
            "ok": False,
            "error_kind": "pbi",
            "error_code": "pbi_query_result_incomplete",
            "error": "pbi_query_result_incomplete",
            "diagnostics": published.to_wire(),
        }

    runner = _make_runner({"teammcp.ibt_powerbi.pbi_execute_dax": _incomplete})
    ctx = _make_ctx()
    ctx.pbi = object()
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())
    await runner._execute_tool(
        ToolCall(
            id="pbi_result",
            name="teammcp.ibt_powerbi.pbi_execute_dax",
            arguments={"dax": "EVALUATE X"},
        ),
        ctx,
        bus,
        parent_turn_id="t_pbi_result",
        subagent_id="lead_root",
    )
    await bus.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="t_pbi_result",
            payload={},
        ),
    )
    await consumer

    done = next(event for event in captured if event.event_type == "step_done")
    assert done.payload["error"]["code"] == ("pbi_query_result_incomplete")
    assert done.payload["error"]["diagnostics"] == published.to_wire()
    assert "Authorization" not in repr(done.payload)


@pytest.mark.asyncio
async def test_unexpected_pbi_exception_is_raw_only_in_server_log(
    caplog,
) -> None:
    secret = "Authorization: Bearer raw-provider-secret"

    async def _unexpected(args: dict, ctx) -> Any:
        raise RuntimeError(secret)

    runner = _make_runner({"teammcp.ibt_powerbi.pbi_execute_dax": _unexpected})
    ctx = _make_ctx()
    ctx.pbi = object()
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())
    invocation = await runner._execute_tool(
        ToolCall(
            id="pbi_unexpected",
            name="teammcp.ibt_powerbi.pbi_execute_dax",
            arguments={"dax": "EVALUATE X"},
        ),
        ctx,
        bus,
        parent_turn_id="t_pbi_unexpected",
        subagent_id="lead_root",
    )
    await bus.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="t_pbi_unexpected",
            payload={},
        ),
    )
    await consumer

    assert invocation.error == "pbi_query_failed"
    user_events = [event.payload for event in captured if event.event_type in {"tool_result", "step_done"}]
    assert user_events
    assert all(secret not in repr(payload) for payload in user_events)
    assert all("raw-provider-secret" not in repr(payload) for payload in user_events)
    assert any(record.exc_info is not None and secret in str(record.exc_info[1]) for record in caplog.records)


@pytest.mark.asyncio
async def test_execute_tool_sequential_no_concurrency_group() -> None:
    """When concurrency_group_id is None (sequential mode), step_started carries None."""

    async def _ok(args: dict, ctx) -> Any:
        return "ok"

    runner = _make_runner({"seq_tool": _ok})
    ctx = _make_ctx()
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(id="c_seq", name="seq_tool", arguments={})
    await runner._execute_tool(
        tc,
        ctx,
        bus,
        parent_turn_id="t_seq",
        subagent_id="lead_root",
        running_profile="lead",
        # concurrency_group_id omitted → defaults None
    )

    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_seq", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started"]
    assert len(started) == 1
    assert started[0].payload["concurrency_group_id"] is None
    assert started[0].payload["parent_step_id"] is None


@pytest.mark.asyncio
async def test_turn_event_bus_increments_step_count_on_step_done() -> None:
    """Bus._step_count tracks step_done emit count (used by step_list_card_finalized)."""
    bus = TurnEventBus()
    assert bus._step_count == 0
    await bus.emit(OrchestratorEvent(event_type="step_started", turn_id="t", payload={}))
    assert bus._step_count == 0  # step_started doesn't increment
    await bus.emit(OrchestratorEvent(event_type="step_done", turn_id="t", payload={}))
    assert bus._step_count == 1
    await bus.emit(OrchestratorEvent(event_type="step_done", turn_id="t", payload={}))
    assert bus._step_count == 2


@pytest.mark.asyncio
async def test_execute_tool_does_not_emit_legacy_tool_call_event_b2_regression() -> None:
    """B2 regression — _execute_tool must NOT emit `tool_call` event.

    Audit doc: docs/chat-redesign-prd-gap-audit-2026-05-23.md §3 P1-1.
    Sprint chat-redesign p0-p1-followup B2: dual-emit cleanup.

    Before B2: subagent.py:2267 emitted `tool_call` + `step_started` in parallel
    (~2x SSE payload). After B2: only `step_started` fires; FE legacy
    translator is still in tree for historical turn replay but never
    triggered for new turns.
    """

    async def _ok(args: dict, ctx) -> Any:
        return "ok"

    runner = _make_runner({"any_tool": _ok})
    ctx = _make_ctx()
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(id="c_b2", name="any_tool", arguments={})
    await runner._execute_tool(
        tc,
        ctx,
        bus,
        parent_turn_id="t_b2",
        subagent_id="lead_root",
        running_profile="lead",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_b2", payload={}))
    await consumer

    types = [ev.event_type for ev in captured]
    # The whole point of B2 — turn_events row never carries a tool_call
    # event_type (regression gate per spec).
    assert "tool_call" not in types, f"B2 regression: tool_call still emitted; got types={types}"
    # Sanity: step_started + step_done STILL fire (B2 must not delete those).
    assert "step_started" in types
    assert "step_done" in types
    # tool_result remains (kept for result_inline / Inspector / FE legacy
    # translator on historical turn replay).
    assert "tool_result" in types


async def _run_tool_capturing(handler, call: ToolCall, *, subagent_id: str = "consultant_1"):
    runner = _make_runner({call.name: handler})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for event in bus.subscribe():
            captured.append(event)

    consumer = asyncio.create_task(_consume())
    invocation = await runner._execute_tool(
        call,
        _make_ctx(),
        bus,
        parent_turn_id="t_test",
        subagent_id=subagent_id,
        running_profile="consultant",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer
    return invocation, captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "root"),
    [
        ({"BOWHEAD_SKILL_WORKDIR_ROOT": "/srv/acme/bh-work"}, "/srv/acme/bh-work"),
        ({"BOWHEAD_INSTALL_DIR": "/opt/bowhead"}, "/opt/bowhead/sandbox"),
    ],
)
async def test_skill_invoke_events_carry_no_absolute_sandbox_paths(monkeypatch, env: dict[str, str], root: str) -> None:
    """Persisted step_started/tool_result/step_done payloads used to expose sandbox host paths.

    The root comes from the deployment's configuration, not a fixed /opt/bowhead/sandbox.
    The in-memory invocation keeps the host paths: consultant-deck and document
    registration read the rendered files from there after the tool loop.
    """
    for key in ("BOWHEAD_SKILL_WORKDIR_ROOT", "BOWHEAD_INSTALL_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    workdir = f"{root}/workdirs/skill_ab12cd"
    deck = f"{workdir}/out/output.pptx"
    raw = {
        "ok": True,
        "exit_code": 0,
        "stdout": f"wrote {deck}\ncwd {workdir}\n",
        "stderr": "",
        "result_json": {"ok": True, "slides": 6, "path": deck, "cache": f"{root}/cache/fonts.json"},
        "outputs": [deck],
        "output_artifacts": [],
        "workdir": workdir,
        "duration_ms": 12,
        "error": None,
    }

    async def _skill(_args: dict, _ctx) -> Any:
        return dict(raw)

    previous = f"{root}/workdirs/skill_old99/in/data.csv"
    invocation, captured = await _run_tool_capturing(
        _skill,
        ToolCall(id="call_deck", name="skill_invoke", arguments={"name": "consultant-deck", "script": "render_deck.py", "input": {"data": previous}}),
    )

    # Registration input is untouched.
    assert invocation.result["outputs"] == [deck]
    assert invocation.result["workdir"] == workdir
    visible = [event.payload for event in captured if event.event_type in {"step_started", "tool_result", "step_done"}]
    assert len(visible) == 3
    assert all(root not in json.dumps(payload, default=str) for payload in visible)
    started = next(event.payload for event in captured if event.event_type == "step_started")
    assert "data.csv" in started["arguments_preview"]
    inline = next(event.payload for event in captured if event.event_type == "tool_result")["result_inline"]
    assert inline["outputs"] == ["out/output.pptx"]
    assert inline["workdir"] is None
    assert inline["stdout"] == "wrote out/output.pptx\ncwd .\n"
    assert inline["result_json"] == {"ok": True, "slides": 6, "path": "out/output.pptx", "cache": "fonts.json"}


@pytest.mark.asyncio
async def test_tool_error_text_in_events_carries_no_sandbox_path(monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_INSTALL_DIR", raising=False)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", "/srv/acme/bh-work")

    async def _boom(_args: dict, _ctx) -> Any:
        raise FileNotFoundError("/srv/acme/bh-work/workdirs/skill_zz/out/output.pptx")

    invocation, captured = await _run_tool_capturing(_boom, ToolCall(id="call_err", name="skill_invoke", arguments={"name": "consultant-deck"}))

    assert "/srv/acme/bh-work/workdirs/skill_zz/out/output.pptx" in str(invocation.error)
    for event in captured:
        if event.event_type in {"tool_result", "step_done"}:
            dumped = json.dumps(event.payload, default=str)
            assert "/srv/acme/bh-work" not in dumped
            assert "output.pptx" in dumped


def test_host_path_redaction_matches_whole_path_prefixes_only(monkeypatch) -> None:
    from bowhead_core.subagent import _redact_host_paths

    monkeypatch.delenv("BOWHEAD_INSTALL_DIR", raising=False)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", "/srv/acme/bh-work")
    workdir = "/srv/acme/bh-work/w1"
    text = f"{workdir}/a.txt {workdir}2/b.txt /srv/acme/bh-workshop/c.txt {workdir}"
    assert _redact_host_paths(text, (workdir,)) == "a.txt b.txt /srv/acme/bh-workshop/c.txt ."
    # Nothing configured and no workdir: values pass through untouched.
    monkeypatch.delenv("BOWHEAD_SKILL_WORKDIR_ROOT")
    payload = {"error": "/opt/bowhead/sandbox/x"}
    assert _redact_host_paths(payload) is payload


@pytest.mark.parametrize(
    ("root", "untouched"),
    [
        ("/opt/bowhead/sandbox", "https://docs.example.com/opt/bowhead/sandbox/guide.html"),
        ("/data", "https://example.com/data/report.csv"),
        ("/data", "s3://bucket/data/2026/report.csv"),
        ("/data", "./data/report.csv and backup/data/report.csv"),
        ("/data", "file.name/data/x and a-b/data/y"),
    ],
)
def test_host_path_redaction_leaves_urls_and_relative_paths_alone(monkeypatch, root: str, untouched: str) -> None:
    from bowhead_core.subagent import _redact_host_paths

    monkeypatch.delenv("BOWHEAD_INSTALL_DIR", raising=False)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", root)
    workdir = f"{root}/w1"
    assert _redact_host_paths(untouched, (workdir,)) == untouched
    # Real host paths in the same string are still rewritten, wherever a token starts.
    mixed = f'{untouched} wrote {workdir}/out/a.pptx ("{root}/cache/f.json") path={workdir}/b.txt [{workdir}]'
    assert _redact_host_paths(mixed, (workdir,)) == f'{untouched} wrote out/a.pptx ("f.json") path=b.txt [.]'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "result", "code"),
    [
        # 116 s_beffa22dce1f: both showed "failed without a specific error".
        ("render_deck", {"ok": False, "error": "deck_render_not_admitted", "message": "Needs the original assignment."}, "deck_render_not_admitted"),
        ("agent_message", {"ok": False, "error": "collaboration_coordination_reply_only"}, "collaboration_coordination_reply_only"),
        ("agent_message", {"ok": False, "error": "some unlisted prose"}, None),
    ],
)
async def test_failed_step_carries_only_a_listed_finite_code(tool_name: str, result: dict, code: str | None) -> None:
    async def _refuse(_args: dict, _ctx) -> Any:
        return result

    runner = _make_runner({tool_name: _refuse})
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for event in bus.subscribe():
            captured.append(event)

    consumer = asyncio.create_task(_consume())
    await runner._execute_tool(ToolCall(id="call_x", name=tool_name, arguments={}), _make_ctx(), bus, parent_turn_id="t_test", subagent_id="sub_x")
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_test", payload={}))
    await consumer

    step = next(event.payload for event in captured if event.event_type == "step_done")
    assert step["state"] == "failure"
    assert step["error"].get("code") == code
    tool_result = next(event.payload for event in captured if event.event_type == "tool_result")
    if tool_name == "agent_message":
        # The mailbox never reaches ordinary Tool events; only the finite code does.
        assert tool_result["result_inline"] == ({"ok": False, "error": code} if code else {"ok": False})


def test_cap_reason_names_the_counted_failures_instead_of_guessing_datarc() -> None:
    from bowhead_core.subagent import ToolInvocation, _tool_failure_summary

    invocations = [
        *[
            ToolInvocation(tool_name="render_deck", arguments={}, call_id=f"r{i}", result={"ok": False, "error": "deck_render_not_admitted"})
            for i in range(3)
        ],
        ToolInvocation(tool_name="subagent_return", arguments={}, call_id="s", result={"ok": False, "error": "collaboration_unresolved"}),
        ToolInvocation(tool_name="bi_query", arguments={}, call_id="b", result={"ok": True}),
        ToolInvocation(tool_name="python_sandbox", arguments={}, call_id="p", error="exit_code=1"),
    ]
    assert _tool_failure_summary(invocations) == "render_deck: deck_render_not_admitted ×3, python_sandbox ×1"
    assert _tool_failure_summary([]) == "no Tool failure recorded"


def test_every_public_step_code_has_localized_copy() -> None:
    """The backend allowlist and the AgentTrace map must name the same codes."""
    import re
    from pathlib import Path

    from bowhead_core.subagent import PUBLIC_STEP_ERROR_CODES

    trace = (Path(__file__).resolve().parents[2] / "bowhead_frontend/components/chat/step-list/AgentTrace.tsx").read_text(encoding="utf-8")
    block = trace.split("const COLLABORATION_AND_DECK_ERROR_KEYS", 1)[1].split("};", 1)[0]
    assert set(re.findall(r"^\s+([a-z_]+): \"", block, flags=re.M)) == PUBLIC_STEP_ERROR_CODES
