"""Lead delegation uses the current roster and keeps BI execution in peers."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.prompts.lead import (
    build_lead_system_prompt,
    build_tool_use_guidance,
    estimate_tokens,
)
from bowhead_core.agents.prompts.peer_block import (
    BI_DELEGATION_DISPATCH_RULE,
    PeerEntry,
    render_peer_block,
)
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
)

# ─────────────────────────────────────────────────────────────
# 1. Prompt variants (pure-function layer)
# ─────────────────────────────────────────────────────────────

_OFF_STEP2_MARKER = "data 路径 bi_schema→bi_query→narrate"  # Old copy removed at release; must not return
_ON_RULE_MARKER = "you have no bi_query"  # BI_DELEGATION_DISPATCH_RULE distinguishing fingerprint


def test_guidance_step2_data_path_is_spawn_compute() -> None:
    on = build_tool_use_guidance(planning_auto=False)
    assert _OFF_STEP2_MARKER not in on
    assert "MUST spawn compute" in on
    # Delivery self-determination (batch 5ⓐ): pass delivery only when the user stated/remembered it; copy still mentions both values
    assert 'delivery="chat"' in on and 'delivery="report"' in on and "otherwise do not pass it" in on
    assert "Classify strictly in 3 steps" in on  # Step count is independent of delegation
    # Independent of planning_auto (Step 0 lives in the tree)
    on4 = build_tool_use_guidance(planning_auto=True)
    assert "Step 0" in on4
    assert "Classify strictly in 4 steps" in on4
    assert _OFF_STEP2_MARKER not in on4


def test_full_prompt_has_no_lead_side_bi_query_guidance() -> None:
    """整份 Lead prompt 里不得残留任何教 Lead 自己执行 bi_query 的指引。

    转正后这条从「变体对照」变成**绝对断言**：Lead 的 whitelist 里根本没有
    bi_query，prompt 里任何一处教它用，都是在教模型调一个不存在的工具。
    （原先此处还借 enforce_token_budget 顺带受 token cap lint；该 cap 已随批 1-b 删除。）
    """
    on = build_lead_system_prompt("ws1", facts=[])
    # Decision tree
    assert _OFF_STEP2_MARKER not in on
    # λ F4 batch discipline removed (described orchestrating bi_query batches)
    assert "<plan_stage_discipline>" not in on
    # Parallelism block no longer teaches Lead-side bi_query batches
    assert "bi_query(queries=[" not in on
    assert "Batch ≥ 并行 tool_calls" not in on
    # Knowledge-boundary routing parentheses
    assert "(用 bi_query / kb_search)" not in on
    assert "(dispatch compute / use kb_search)" in on
    # Few-shot data example uses spawn, no longer referring to the retired hard-threshold concept
    assert '- "<metric> for <month>" → subagent_spawn(profile="compute"' in on
    assert "硬阈值" not in on
    # The default manifest roster includes the same delegation rule.
    assert BI_DELEGATION_DISPATCH_RULE in on
    # Remove D5's old instruction not to bi_query first (which implies Lead can)
    assert "先自己 bi_query 取数" not in on
    # Likewise the analogy to batching bi_query calls (removed from source at release, not with .replace)
    assert "就像 batch 多个 bi_query" not in on


def test_delegation_rule_reaches_default_and_explicit_rosters() -> None:
    """Default and explicit rosters keep the same delegation contract."""
    assert BI_DELEGATION_DISPATCH_RULE in build_lead_system_prompt("ws1", facts=[])
    # WP M3-12 batch 2: the constant's final deck exception renders by assignee too; exact matching in the dynamic block requires
    # both fetch and deck assignees in the roster (`BI_DELEGATION_DISPATCH_RULE` is
    # `render_bi_delegation_rule()` with all defaults). Without a deck assignee, the wording truthfully says none;
    # correct new behavior, not a broken single source of truth.
    assert BI_DELEGATION_DISPATCH_RULE in render_peer_block(
        [
            PeerEntry(
                name="compute", description="数据分析 peer", source="system", tool_names=frozenset({"bi_query"}), primary_capability="cap_data_query"
            ),
            PeerEntry(
                name="consultant",
                description="商业顾问",
                source="system",
                tool_names=frozenset({"skill_invoke"}),
                primary_capability="cap_presentation",
                # Since 073fd724f, deck eligibility = skill_invoke + code-side pptx output contract (as in the real consultant manifest).
                output_formats=("pptx",),
            ),
        ]
    )
    # After release, estimate_tokens remains a live budget guard (removing branches must not lengthen the prompt)
    assert estimate_tokens(build_lead_system_prompt("ws1", facts=[])) > 0


def _peers() -> list[PeerEntry]:
    # WP M3-12 batch 2: `BI_DELEGATION_DISPATCH_RULE`'s final deck exception renders by assignee too;
    # matching this all-default constant requires both fetch and deck assignees (as in production).
    # Without deck, the wording truthfully says none: correct new behavior, not a broken single source of truth.
    return [
        PeerEntry(
            name="compute", description="数据分析 peer", source="system", tool_names=frozenset({"bi_query"}), primary_capability="cap_data_query"
        ),
        PeerEntry(
            name="consultant",
            description="商业顾问",
            source="system",
            tool_names=frozenset({"skill_invoke"}),
            primary_capability="cap_presentation",
            output_formats=("pptx",),  # Deck eligibility requires pptx output contract (073fd724f)
        ),
    ]


def test_peer_block_carries_delegation_rule_not_thresholds() -> None:
    on = render_peer_block(_peers())
    assert BI_DELEGATION_DISPATCH_RULE in on
    # Old copy removed at release: hard thresholds and do not fetch first (both presume Lead has bi_query)
    assert "硬阈值" not in on
    assert "先决策再取数" not in on
    assert "Decide first, then dispatch" in on
    # Independent of image generation (marketing-analyst still routes images when present)
    on_img = render_peer_block(
        [
            *_peers(),
            PeerEntry(
                name="marketing-analyst",
                description="营销 peer",
                source="system",
                tool_names=frozenset({"image_generate"}),
                primary_capability="cap_visualize",
            ),
        ]
    )
    assert "Image generation MUST spawn marketing-analyst" in on_img
    assert BI_DELEGATION_DISPATCH_RULE in on_img


# ─────────────────────────────────────────────────────────────
# 2. tool_deny hard gate through runner.run (advertising + per-call gate, paired)
# ─────────────────────────────────────────────────────────────


def _recording_router(*rs: LLMResponse) -> tuple[Router, list[list[str]]]:
    """Scripted router，另记录每次 LLM 调用收到的 tools 名单（广告面取证）。"""
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = list(rs)
    seen_tools: list[list[str]] = []
    orig_invoke = a.invoke

    async def _recording_invoke(req: Any) -> LLMResponse:
        seen_tools.append([getattr(t, "name", str(t)) for t in (getattr(req, "tools", None) or [])])
        return await orig_invoke(req)

    a.invoke = _recording_invoke  # type: ignore[method-assign]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "compute", "analyst", "echo"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r, seen_tools


@dataclass
class _StubDatarc:
    async def aclose(self) -> None:
        pass


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_bidel_test",
        datarc=_StubDatarc(),  # type: ignore[arg-type]
    )


@pytest.fixture()
def _lead_profile_with_bi() -> Any:
    """临时把带 bi_query 的 lead profile 塞进 PROFILES（测试后还原）。"""
    prev = PROFILES.get("lead")
    PROFILES["lead"] = SubagentProfile(
        name="lead",
        llm_agent_type="lead",
        tool_whitelist=("bi_query", "bi_schema", "subagent_return"),
        system_prompt="(stub)",
        max_iterations=4,
    )
    try:
        yield
    finally:
        if prev is not None:
            PROFILES["lead"] = prev
        else:
            PROFILES.pop("lead", None)


@pytest.mark.asyncio
async def test_tool_deny_removes_bi_query_from_advertise_and_blocks_dispatch(
    _lead_profile_with_bi: Any,
) -> None:
    """review P0-1 后 deny 的**生产机制**是 run 级参数（gateway 注入的 runner 与
    peer spawn 共享实例, 构造器级会连坐 peer）——本测试穿 run(tool_deny=...)。"""
    router, seen_tools = _recording_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"queries": ["今年GMV"]})]),
        LLMResponse(content="final", tool_calls=[]),
    )
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
    )
    result = await runner.run(
        profile_name="lead",
        task="今年营业额多少",
        parent_turn_id="t_bidel_test",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
        tool_deny=frozenset({"bi_query"}),
    )
    # Advertising: bi_query absent from LLM-visible tools; bi_schema retained (remove only deny set)
    assert seen_tools, "LLM never invoked — harness broke"
    assert "bi_query" not in seen_tools[0]
    assert "bi_schema" in seen_tools[0]
    # Per-call gate: LLM explicitly calls bi_query → whitelist rejects before execution. Feed back a tool
    # Message without ToolInvocation (subagent.py pre-check; ψ-A side table removed in batch 1-a; this pre-check records no ToolInvocation; `_removed_emit_iter
    # = -1` synthetic), so assert zero bi_query invocations and continuation with rejection feedback
    # to the script's second response final, without crashing midway. Actual execution control is the next test.
    assert not [i for i in result.tool_invocations if i.tool_name == "bi_query"]
    assert result.final_answer == "final"


@pytest.mark.asyncio
async def test_no_deny_bi_query_advertised_and_dispatched(_lead_profile_with_bi: Any) -> None:
    """正对照：deny 缺省时同一脚本里 bi_query ①照常进广告面 ②真被 dispatch
    （invocation 记账，能否执行成功无所谓——它过了 whitelist 闸）。deny 接线被
    摘掉时上面那条的两个面会与这条趋同 → 上面红。"""
    router, seen_tools = _recording_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"queries": ["今年GMV"]})]),
        LLMResponse(content="final", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry())
    result = await runner.run(
        profile_name="lead",
        task="今年营业额多少",
        parent_turn_id="t_bidel_test",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
    )
    assert seen_tools
    assert "bi_query" in seen_tools[0]
    assert "bi_schema" in seen_tools[0]
    assert [i for i in result.tool_invocations if i.tool_name == "bi_query"], (
        "no deny → the bi_query call must reach dispatch (recorded as an invocation)"
    )


@pytest.mark.asyncio
async def test_run_level_dispatch_guard_blocks_post_payload_bi_query(_lead_profile_with_bi: Any) -> None:
    """ξ B1 复活（review P0-1 连带）：guard 经 **run 级参数**下传后真的拦——
    构造器不带 guard（=gateway 注入共享实例的生产形状）, run(dispatch_guard=...)
    携带含非 bail payload 的 guard, Lead 后续 bi_query 必须被前置拦截
    （与 whitelist 拒绝同形: 回喂 Message 不记 invocation）。"""
    from bowhead_core.subagent import LeadDispatchGuard

    payloads = [{"payload": {"title": "R", "kpis": [{"label": "x", "value": "1"}]}}]
    router, _seen = _recording_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"queries": ["今年GMV"]})]),
        LLMResponse(content="final", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry())
    result = await runner.run(
        profile_name="lead",
        task="今年营业额多少",
        parent_turn_id="t_bidel_guard",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
        dispatch_guard=LeadDispatchGuard(payloads_ref=payloads),
    )
    assert not [i for i in result.tool_invocations if i.tool_name == "bi_query"]
    assert result.final_answer == "final"


@pytest.mark.asyncio
async def test_no_guard_bi_query_dispatches(_lead_profile_with_bi: Any) -> None:
    """正对照：不传 run 级 guard（且构造器无 guard）→ bi_query 到达 dispatch。"""
    router, _seen = _recording_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"queries": ["今年GMV"]})]),
        LLMResponse(content="final", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry())
    result = await runner.run(
        profile_name="lead",
        task="今年营业额多少",
        parent_turn_id="t_bidel_guard2",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
    )
    assert [i for i in result.tool_invocations if i.tool_name == "bi_query"]


@pytest.mark.asyncio
async def test_ctor_level_deny_still_works_for_standalone_runner(_lead_profile_with_bi: Any) -> None:
    """构造器级 deny 保留给自建/独占 runner（裸 harness 回退路），与 run 级取并集。"""
    router, seen_tools = _recording_router(LLMResponse(content="直接回答", tool_calls=[]))
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        tool_deny=frozenset({"bi_query"}),
    )
    await runner.run(
        profile_name="lead",
        task="今年营业额多少",
        parent_turn_id="t_bidel_test",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
    )
    assert seen_tools
    assert "bi_query" not in seen_tools[0]
    assert "bi_schema" in seen_tools[0]


# ─────────────────────────────────────────────────────────────
# 3. Live wiring through _lead_loop (flag → prompt variant + runner construction)
# ─────────────────────────────────────────────────────────────


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _CapturingRunner:
    """生产形状的注入 runner 替身（runtime.subagent_runner 恒非 None——review P0-1）。
    记录 extra_instructions（= Lead prompt）与全量 run kwargs（deny/guard 接线取证）。"""

    captured: list[str | None] = field(default_factory=list)
    captured_kwargs: list[dict[str, Any]] = field(default_factory=list)

    async def run(
        self,
        *,
        profile_name: str,
        extra_instructions: str | None = None,
        **_kwargs: Any,
    ) -> SubagentResult:
        self.captured.append(extra_instructions)
        self.captured_kwargs.append(dict(_kwargs))
        return SubagentResult(profile=profile_name, subagent_id="sub_test", ok=True, final_answer="ok")


def _mk_runtime(runner: Any) -> LeadRuntime:
    ctx = SimpleNamespace(
        clarification_store=None,
        _m137_subagent_payloads=[],
        tool_view=None,
        execution_generation=1,
        execution_owner_id=None,
    )
    return LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        subagent_runner=runner,
    )


def _mk_sctx(turn_id: str) -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id=turn_id,
        role="user",
    )


@pytest.fixture(autouse=True)
def _no_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")


@pytest.fixture()
def _ensure_lead_profile() -> Any:
    pre = "lead" in PROFILES
    if not pre:
        PROFILES["lead"] = SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=("subagent_return", "clarification_ask"),
            system_prompt="(stub)",
            max_iterations=4,
        )
    try:
        yield
    finally:
        if not pre:
            PROFILES.pop("lead", None)


@pytest.mark.asyncio
async def test_lead_loop_hands_runner_delegated_prompt_and_run_level_deny(_ensure_lead_profile: Any) -> None:
    """生产形状（注入 runner）下**两面**都必须走到：prompt 交权文案 + run 级
    tool_deny/dispatch_guard（review P0-1：构造器级在注入路是死接线——只测 prompt
    不测 run kwargs 的话, 硬闸被摘掉本套件照绿）。

    deny 集断言写**完整集合**而不是「包含 bi_query」：收权（subagent_return）与
    交权共用这一个装配点，写成子集断言的话漏掉一个名字不会红。
    """
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner)
    from bowhead_core.agents.registry import AgentRegistry

    runtime.multi_agent_registry = AgentRegistry.bootstrap()
    result = await _lead_loop(_mk_sctx("t_bidel_loop1"), "今年营业额多少", runtime)
    assert result is not None
    assert runner.captured, "runner.run never reached"
    prompt = runner.captured[0] or ""
    assert _ON_RULE_MARKER in prompt
    assert _OFF_STEP2_MARKER not in prompt
    kwargs = runner.captured_kwargs[0]
    assert kwargs.get("tool_deny") == frozenset({"bi_query", "subagent_return"})
    # ξ B1 guard passes through the same channel at run level (constructor-level on an injected shared instance is always dead; review P0-1 fix)
    from bowhead_core.subagent import LeadDispatchGuard

    assert isinstance(kwargs.get("dispatch_guard"), LeadDispatchGuard)


class _RunnerCtorSpy:
    """替身 SubagentRunner 类：记录构造 kwargs（tool_deny 接线取证）。

    ⚠ review P0-1 后的定位：`runtime.subagent_runner=None` 的自建分支是**裸 harness
    回退路**（gateway 生产 runner 恒由 turn_runner 注入）——下面两条只保住回退路
    的冗余接线；生产接线的真测试是上面注入 runner + run kwargs 那两条。"""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = dict(kwargs)

    async def run(self, *, profile_name: str, **_kwargs: Any) -> SubagentResult:
        return SubagentResult(profile=profile_name, subagent_id="sub_spy", ok=True, final_answer="ok")


@pytest.mark.asyncio
async def test_lead_loop_constructs_fallback_runner_with_tool_deny(monkeypatch: pytest.MonkeyPatch, _ensure_lead_profile: Any) -> None:
    """runtime.subagent_runner=None → _lead_loop 自建 runner，也必须带同一个 deny 集。

    ⚠ 这条守的是**裸 harness 回退路**，生产走不到（gateway 恒注入 runner）——
    批1 的 P0 教训就是把硬闸只焊在这条死路上。生产接线的真测试是上面那条
    「注入 runner + run kwargs」。
    """
    monkeypatch.setattr("bowhead_core.subagent.SubagentRunner", _RunnerCtorSpy)
    _RunnerCtorSpy.last_kwargs = {}
    result = await _lead_loop(_mk_sctx("t_bidel_loop3"), "今年营业额多少", _mk_runtime(None))
    assert result is not None
    assert _RunnerCtorSpy.last_kwargs.get("tool_deny") == frozenset({"bi_query", "subagent_return"})
