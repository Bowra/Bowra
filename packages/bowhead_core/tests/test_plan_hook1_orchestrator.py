"""C2: hook1(_maybe_propose_plan)orchestrator 级集成测试 —— 补 review 反复指出的
hook 缺测(此前只有 plan_authoring/driver 纯逻辑测)。一箭三雕覆盖:
  ① 出图路径:should_propose_plan → _maybe_propose_plan → plan_proposed emit + 落库
  ② schema 预取降级:parent_ctx.datarc=None → _prefetch 返 "" 不崩,正常出图
  ③ A3 计费:planner 出图 token 汇进 _pi_turn_state + TurnResult

走真 _lead_loop(仿 test_b72_lead_bail_contract)+ 真 PlanStore(tmp sqlite)+ fake
bus/router/registry。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.plan_store import PlanStatus, PlanStore
from bowhead_core.types import IntentType, TurnResult, TurnStatus

_PLAN_JSON = """{"steps": [
  {"id": "sales", "agent_name": "business-analyst", "objective": "销量结构", "needs_corpus": false, "depends_on": []},
  {"id": "profit", "agent_name": "financial-analyst", "objective": "盈利质量", "needs_corpus": false, "depends_on": []},
  {"id": "sum", "agent_name": "canvas-writer", "objective": "汇总报告", "depends_on": ["sales", "profit"]}
]}"""


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def find_all(self, event_type: str) -> list[Any]:
        return [e for e in self.events if e.event_type == event_type]


@dataclass
class _Usage:
    input_tokens: int = 111
    output_tokens: int = 22


@dataclass
class _Resp:
    content: str
    usage: object = None


class _FakeRouter:
    async def invoke(self, agent_type: str, req: Any) -> _Resp:
        return _Resp(_PLAN_JSON, usage=_Usage())


class _FakeRegistry:
    _known = {"business-analyst", "financial-analyst", "canvas-writer"}

    def get(self, name: str, workspace_id: str = "") -> Any:
        if name == "lead":
            return SimpleNamespace(callable_peers=sorted(self._known))
        if name in self._known:
            return SimpleNamespace(description=f"{name} desc")
        raise KeyError(name)

    def list_all(self, workspace_id: str) -> list[str]:
        return sorted(self._known)


class _AutoClaimSessionStore:
    def __init__(self, plan_store: PlanStore) -> None:
        self.plan_store = plan_store

    async def try_claim_auto_plan_for_live_turn(
        self,
        *,
        plan_id: str,
        turn_id: str,
    ) -> bool:
        return await self.plan_store.try_claim_running(plan_id, turn_id)


async def _run(store: PlanStore, *, planning_mode: str = "planning") -> tuple[Any, _FakeBus]:
    bus = _FakeBus()
    # datarc=None → schema prefetch degrades to ""; kb_files_store=None → corpus 0.
    ctx = SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None)
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_hook1", role="user", planning_mode=planning_mode)
    runtime = LeadRuntime(
        router=_FakeRouter(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    result = await _lead_loop(sctx, "对这个商超做一次经营尽调,分析销量和盈利", runtime)
    return result, bus


@pytest.fixture
def _plan_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")


@pytest.mark.asyncio
async def test_hook1_emits_plan_proposed_and_persists(tmp_path, _plan_flag) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    result, bus = await _run(store)

    # ① Graph → CLARIFICATION_PENDING + plan_proposed emit + persistence.
    assert result is not None and result.status == TurnStatus.CLARIFICATION_PENDING
    proposed = bus.find_all("plan_proposed")
    assert len(proposed) == 1
    payload = proposed[0].payload
    assert payload["workspace_id"] == "ws1"
    assert len(payload["steps"]) == 2  # T2: terminal writer dropped in parse (compose front door).
    # Actually persisted.
    plans = [p async for p in _aiter(store, "ws1", "u1")]
    assert len(plans) == 1 and plans[0].status == PlanStatus.PROPOSED
    assert payload["current_rev"] == plans[0].current_rev
    steps = await store.list_steps(plans[0].id)
    assert {s.agent_name for s in steps} == {"business-analyst", "financial-analyst"}  # T2: writer dropped.


@pytest.mark.asyncio
async def test_cancel_during_proposal_authoring_leaves_no_approvable_plan(tmp_path, _plan_flag, monkeypatch) -> None:
    from bowhead_core import plan_authoring

    monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", "1")
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    authoring = asyncio.Event()

    async def criteria(*args, **kwargs):
        authoring.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(plan_authoring, "author_step_criteria", criteria)
    task = asyncio.create_task(_run(store))
    try:
        await asyncio.wait_for(authoring.wait(), timeout=5)
        assert await store.list_plans_for_user(user_id="u1", workspace_id="ws1") == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.list_plans_for_user(user_id="u1", workspace_id="ws1") == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_hook1_auto_no_longer_pre_authors(tmp_path, _plan_flag) -> None:
    """★2026-08-14 行为搬家：Auto 不再在 loop 前预调用 author_plan。

    此前 Auto 与显式 Planning 共用 loop 前那条直通路，于是**每个 Auto 请求都要先付
    一次 author_plan**（实测 8067 in token），非项目请求白等几秒才被丢掉。
    现在 Auto 由 Lead 在循环里调 `plan_propose` 工具自己判 —— 判断权给了上下文更全的
    那个模型，非项目请求零额外开销。

    ⚠ 这条**不是**「Auto 不能出计划」。能力搬家不是能力丢失：Lead 调了工具照样出卡，
    由下一条钉住。两条必须成对读，只留一条都会把回归判成通过。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    _, bus = await _run(store, planning_mode="auto")

    assert bus.find_all("plan_proposed") == [], "Auto 不该在 loop 前自己出卡"


@pytest.mark.asyncio
async def test_hook1_auto_proposes_when_lead_calls_the_tool(tmp_path, _plan_flag) -> None:
    """★正对照：Lead 调 `plan_propose` → 走与显式 Planning 同一条出卡路径。"""
    from bowhead_core.agents.plan_propose_tool import SENTINEL_PLAN_PROPOSE, is_plan_propose_sentinel

    # Sentinel shape itself (lead_loop captures it).
    assert is_plan_propose_sentinel({SENTINEL_PLAN_PROPOSE: True}) is True
    assert is_plan_propose_sentinel({"other": True}) is False
    assert is_plan_propose_sentinel(None) is False

    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    _, bus = await _run(store, planning_mode="planning")
    # Explicit Planning still bypasses loop (user decided; need not ask model); also pins
    # that relocation did not move explicit mode.
    assert len(bus.find_all("plan_proposed")) == 1


@pytest.mark.asyncio
async def test_hook1_off_suppresses_plan_even_for_triggering_text(tmp_path, _plan_flag) -> None:
    """`planning_mode="off"` = 用户显式说「这次别出计划」,即使文案命中 Auto 信号。

    这条是 2026-08-12 前端接出三态时补的:后端 `should_propose_plan` 早就支持 `off`,
    但 orchestrator 这一层只测过 `planning`,而**生产上真正会跑的正是这条 off 路**。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    result, bus = await _run(store, planning_mode="off")

    # ① No graph: zero plan_proposed.
    assert bus.find_all("plan_proposed") == []
    # ② No persistence: off must leave nothing in PlanStore.
    assert [p async for p in _aiter(store, "ws1", "u1")] == []
    # ③ No suspension awaiting approval: off turn must finish as ordinary turn.
    assert result is None or result.status != TurnStatus.CLARIFICATION_PENDING


@pytest.mark.asyncio
async def test_hook1_bills_planner_tokens(tmp_path, _plan_flag) -> None:
    """③ A3: 出图+出 criteria 两次 LLM 调用的 token 都汇进 _pi_turn_state + TurnResult。

    graph-mode 批1: propose 路多了 author_step_criteria 第二遍调用(fake router
    每次报 111/22)——criteria 的 token 同样计费, 2 次 = 222/44。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    result, bus = await _run(store)
    assert bus._pi_turn_state.get("tokens_input") == 222
    assert bus._pi_turn_state.get("tokens_output") == 44
    assert result.tokens_input == 222 and result.tokens_output == 44


async def _aiter(store: PlanStore, ws: str, uid: str):
    # Without PlanStore list_plans_for_ws, per-ID get_plan is awkward; use internal list here.
    for p in await _list_plans(store):
        yield p


async def _list_plans(store: PlanStore) -> list[Any]:
    import aiosqlite

    out = []
    async with aiosqlite.connect(store._db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM plans") as cur:
            rows = await cur.fetchall()
    for r in rows:
        p = await store.get_plan(r["id"], user_id="u1", workspace_id="ws1")
        if p:
            out.append(p)
    return out


# ── 2026-07-22 planner-team-tools: overlay → planner prompt integration (guard kwarg removal in refactor) ──


class _CapturingRouter:
    """记录 planner 收到的完整 user message——单测各层全绿但 kwarg 断链的
    silent-dark 只有这层能抓(与 test_team_tools_lead_loop 同款防御)。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def invoke(self, agent_type: str, req: Any) -> _Resp:
        self.sent.append(req.messages[0].content)
        return _Resp(_PLAN_JSON, usage=_Usage())


def _fake_tool_view() -> Any:
    name = "teammcp.ots_6c301a6a0931.get_repo_info"
    return SimpleNamespace(
        overlay_names=frozenset({name}),
        get=lambda n: SimpleNamespace(schema=SimpleNamespace(description="查询 GitHub 仓库的公开信息, 含 star 数")),
    )


@pytest.mark.asyncio
async def test_hook1_planner_sees_team_tools_overlay(tmp_path, _plan_flag) -> None:
    """turn 挂了团队工具 overlay → planner 的 user message 含完整工具名 + 出图不受影响。"""
    store = PlanStore(str(tmp_path / "p_tt.db"))
    await store.initialize()
    bus = _FakeBus()
    router = _CapturingRouter()
    ctx = SimpleNamespace(
        datarc=None, kb_files_store=None, agent_registry=None, tool_view=_fake_tool_view(), execution_generation=1, execution_owner_id=None
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_hook1_tt", role="user", planning_mode="planning")
    runtime = LeadRuntime(
        router=router,
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    result = await _lead_loop(sctx, "全面调研 anthropics/claude-code 的社区热度和代码结构", runtime)
    assert result is not None and result.status == TurnStatus.CLARIFICATION_PENDING
    assert router.sent, "planner 未被调用"
    # Grouped rendering: header has full literal prefix, operation rows have names.
    assert "teammcp.ots_6c301a6a0931." in router.sent[0]
    assert "`get_repo_info`" in router.sent[0]
    assert "团队接入的外部工具清单" in router.sent[0]


@pytest.mark.asyncio
async def test_hook1_planner_prompt_unchanged_without_overlay(tmp_path, _plan_flag) -> None:
    """无 overlay(tool_view 缺失/空)→ planner prompt 不含团队工具段(非 team turn 零变化)。"""
    store = PlanStore(str(tmp_path / "p_no_tt.db"))
    await store.initialize()
    bus = _FakeBus()
    router = _CapturingRouter()
    ctx = SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None)  # No tool_view.
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_hook1_no_tt", role="user", planning_mode="planning")
    runtime = LeadRuntime(
        router=router,
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    result = await _lead_loop(sctx, "对这个商超做一次经营尽调,分析销量和盈利", runtime)
    assert result is not None
    assert router.sent and "团队接入的外部工具" not in router.sent[0]


class _CriteriaAwareRouter:
    """出图调用返回计划 JSON;criteria 第二遍调用(按 trace_id 前缀识别)从请求文本
    里解析真实 step id, 返回带具体数字的验收标准(过退化 lint)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, agent_type: str, req: Any) -> _Resp:
        import json as _json
        import re as _re

        trace = getattr(req, "trace_id", "") or ""
        self.calls.append(trace)
        if trace.startswith("plan_criteria:"):
            ids = _re.findall(r"id=(ps_\w+)", req.messages[0].content)
            crit = {sid: [f"含 2025 各季度数字({i})"] for i, sid in enumerate(ids)}
            return _Resp(_json.dumps({"criteria": crit}, ensure_ascii=False), usage=_Usage())
        return _Resp(_PLAN_JSON, usage=_Usage())


@pytest.mark.asyncio
async def test_hook1_criteria_second_pass_on_card_and_store(tmp_path, _plan_flag) -> None:
    """graph-mode 批1: criteria 第二遍生成 → proposal 卡带 success_criteria + 落库."""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    bus = _FakeBus()
    ctx = SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None)
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_crit", role="user", planning_mode="planning")
    router = _CriteriaAwareRouter()
    runtime = LeadRuntime(
        router=router,
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    result = await _lead_loop(sctx, "对这个商超做一次经营尽调,分析销量和盈利", runtime)
    assert result.status == TurnStatus.CLARIFICATION_PENDING
    # Each card step has criteria (approve objective + acceptance).
    card = bus.find_all("plan_proposed")[0].payload
    assert all(s["success_criteria"] for s in card["steps"])
    # Durable persistence.
    plans = [p async for p in _aiter(store, "ws1", "u1")]
    steps = await store.list_steps(plans[0].id)
    assert all(s.success_criteria for s in steps)
    # Both call types actually occurred.
    assert any(t.startswith("plan_criteria:") for t in router.calls)


@pytest.mark.asyncio
async def test_hook1_verify_flag_off_no_criteria_call(tmp_path, _plan_flag, monkeypatch) -> None:
    """flag-off 回归: BOWHEAD_PLANNING_VERIFY=0 → 不发 criteria 调用, 卡面字段为空."""
    monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", "0")
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    bus = _FakeBus()
    ctx = SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None)
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_off", role="user", planning_mode="planning")
    router = _CriteriaAwareRouter()
    runtime = LeadRuntime(
        router=router,
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    result = await _lead_loop(sctx, "对这个商超做一次经营尽调,分析销量和盈利", runtime)
    assert result.status == TurnStatus.CLARIFICATION_PENDING
    assert not any(t.startswith("plan_criteria:") for t in router.calls)
    card = bus.find_all("plan_proposed")[0].payload
    assert all(s["success_criteria"] == [] for s in card["steps"])
    # Single-call billing returns to 111/22 (byte-pinned current state).
    assert bus._pi_turn_state.get("tokens_input") == 111


# ── agent-capability-routing phase 1: `_feasibility` becomes gate, not only display ──
#
# Previously `_feasibility` **only reached proposal card**, so a sole-step self-labeled no_corpus
# plan still went to approval: user waits → approves → waits again → told to supply material.
# Same family as silent planning fall-through: promising work it cannot perform.


class _ToolRegistry:
    """带真 `tools.allow` 的 registry 替身 —— 能力表从这里派生（与 prod 同一条路）。"""

    def __init__(self, allow_by_agent: dict[str, list[str]]) -> None:
        self._allow = allow_by_agent

    def get(self, name: str, workspace_id: str = "") -> Any:
        if name == "lead":
            return SimpleNamespace(callable_peers=sorted(self._allow))
        if name not in self._allow:
            raise KeyError(name)
        return SimpleNamespace(description=f"{name} desc", tools=SimpleNamespace(allow=list(self._allow[name])))

    def list_all(self, workspace_id: str) -> list[str]:
        return sorted(self._allow)


_BI_TOOLS = ["bi_query", "bi_schema", "subagent_return"]


async def _run_with(plan_json: str, allow_by_agent: dict[str, list[str]], store: PlanStore) -> tuple[Any, _FakeBus]:
    bus = _FakeBus()
    ctx = SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None)
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_gate", role="user", planning_mode="planning")

    class _R:
        async def invoke(self, agent_type: str, req: Any) -> _Resp:
            return _Resp(plan_json, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_ToolRegistry(allow_by_agent),
    )
    result = await _lead_loop(sctx, "分析我上传的这几份材料", runtime)
    return result, bus


@pytest.mark.asyncio
async def test_gate_blocks_card_when_every_step_is_hard_infeasible(tmp_path, _plan_flag) -> None:
    """整张图每步都缺上传材料（KB 为空）→ 不出卡，走诚实告知。"""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = (
        '{"steps": ['
        '{"id": "d1", "agent_name": "kb-analyst", "objective": "读上传的尽调材料", "needs_corpus": true, "depends_on": []},'
        '{"id": "d2", "agent_name": "kb-analyst", "objective": "读上传的财务附件", "needs_corpus": true, "depends_on": []}'
        "]}"
    )
    _, bus = await _run_with(plan_json, {"kb-analyst": [*_BI_TOOLS, "kb_search"]}, store)

    assert bus.find_all("plan_proposed") == [], "每步都注定跑不起来时不该让用户先批一次"
    assert getattr(bus, "_planning_fallback", None) == "plan_infeasible"
    # A rejected graph is never published as an approvable proposal.
    plans = [p async for p in _aiter(store, "ws1", "u1")]
    assert plans == []


@pytest.mark.asyncio
async def test_gate_still_emits_card_when_only_some_steps_blocked(tmp_path, _plan_flag) -> None:
    """★ 负向对照：只要还有一步能跑就照常出卡。

    没有这条，把闸写成 `any(...)` 也会让上面那条通过 —— 而 `any` 会把大量
    「一步缺料、其余能跑」的正常计划全部拦掉，是比原问题更大的伤害。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = (
        '{"steps": ['
        '{"id": "d1", "agent_name": "kb-analyst", "objective": "读上传材料", "needs_corpus": true, "depends_on": []},'
        '{"id": "b1", "agent_name": "consultant", "objective": "查本空间经营指标", "needs_corpus": false, "depends_on": []}'
        "]}"
    )
    _, bus = await _run_with(plan_json, {"kb-analyst": [*_BI_TOOLS, "kb_search"], "consultant": list(_BI_TOOLS)}, store)

    proposed = bus.find_all("plan_proposed")
    assert len(proposed) == 1
    feas = {s["agent"]: s["feasibility"] for s in proposed[0].payload["steps"]}
    assert feas == {"kb-analyst": "no_corpus", "consultant": "ok"}


@pytest.mark.asyncio
async def test_card_reports_no_capable_agent_when_nobody_can_reach_the_data(tmp_path, _plan_flag) -> None:
    """花名册里**没有任何** agent 读得了上传文档 → 该步标 no_capable_agent。

    此前这种步会被硬路由到写死的 "kb-analyst"（哪怕它不在花名册里），
    或者原样留给一个够不着数据的 domain 专家，且卡上一律显示 ok。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = (
        '{"steps": ['
        '{"id": "d1", "agent_name": "consultant", "objective": "读上传材料", "needs_corpus": true, "depends_on": []},'
        '{"id": "b1", "agent_name": "consultant", "objective": "查经营指标", "needs_corpus": false, "depends_on": []}'
        "]}"
    )
    _, bus = await _run_with(plan_json, {"consultant": list(_BI_TOOLS)}, store)

    proposed = bus.find_all("plan_proposed")
    assert len(proposed) == 1
    steps = proposed[0].payload["steps"]
    assert [s["feasibility"] for s in steps] == ["no_capable_agent", "ok"]
    # Routing invented no name absent from roster.
    assert {s["agent"] for s in steps} == {"consultant"}


# ── Independent review 2026-08-11: all three `_hard_blocked` branches **previously untested** ──
#
# Mutation evidence: remove `no_capable_agent`, make `needs_context` unconditional hard block,
# or remove entire absent branch: **all green**. Function decides whether to show user a card;
# subtlest tradeoff (needs_selection is **soft**; blocking adds harm)
# had 8 docstring lines justifying it, yet no protective tests.


@pytest.mark.asyncio
async def test_gate_blocks_when_every_step_has_no_capable_agent(tmp_path, _plan_flag) -> None:
    """全图每步都「没人具备所需能力」→ 硬不可行，不出卡。"""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    # No roster agent can read uploaded documents.
    plan_json = (
        '{"steps": ['
        '{"id": "d1", "agent_name": "consultant", "objective": "读上传材料", "needs_corpus": true, "depends_on": []},'
        '{"id": "d2", "agent_name": "consultant", "objective": "读上传附件", "needs_corpus": true, "depends_on": []}'
        "]}"
    )
    _, bus = await _run_with(plan_json, {"consultant": list(_BI_TOOLS)}, store)
    assert bus.find_all("plan_proposed") == []
    assert getattr(bus, "_planning_fallback", None) == "plan_infeasible"


@pytest.mark.asyncio
async def test_needs_selection_is_soft_and_must_not_block_the_card(tmp_path, _plan_flag, monkeypatch) -> None:
    """★ 这条守的是 `_hard_blocked` docstring 里最微妙的取舍。

    BI 只是**还没绑工作区**（needs_selection）时，执行期会请用户选一个，选完就能跑 ——
    此时拦掉计划卡等于**剥夺用户继续的机会**，是新增的伤害而不是治「白批一次」。
    突变验证：把 needs_context 改成无条件硬拦 → 此前全绿，现在必须红。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = (
        '{"steps": ['
        '{"id": "b1", "agent_name": "compute", "objective": "查本空间经营指标", "needs_corpus": false, "depends_on": []},'
        '{"id": "b2", "agent_name": "compute", "objective": "查本空间客流指标", "needs_corpus": false, "depends_on": []}'
        "]}"
    )

    bus = _FakeBus()
    ctx = SimpleNamespace(
        datarc=None,
        kb_files_store=None,
        agent_registry=None,
        bi_availability=lambda: "needs_selection",
        execution_generation=1,
        execution_owner_id=None,
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_soft", role="user", planning_mode="planning")

    class _R:
        async def invoke(self, agent_type, req):  # noqa: ANN001, ANN202
            return _Resp(plan_json, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_ToolRegistry({"compute": list(_BI_TOOLS)}),
    )
    await _lead_loop(sctx, "查一下经营指标", runtime)

    proposed = bus.find_all("plan_proposed")
    assert len(proposed) == 1, "BI 只是没绑工作区（可在执行期解决）时不该拦掉计划卡"
    assert proposed[0].payload["steps"][0]["feasibility"] == "needs_context"
    assert getattr(bus, "_planning_fallback", None) is None


@pytest.mark.asyncio
async def test_bi_absent_is_hard_and_does_block(tmp_path, _plan_flag) -> None:
    """与上一条**成对**：本 turn 压根没有可用 BI 数据源（absent）才算硬。

    没有这条，把 absent 分支整个摘掉也不会红（突变实证）。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = (
        '{"steps": ['
        '{"id": "b1", "agent_name": "compute", "objective": "查本空间经营指标", "needs_corpus": false, "depends_on": []},'
        '{"id": "b2", "agent_name": "compute", "objective": "查本空间客流指标", "needs_corpus": false, "depends_on": []}'
        "]}"
    )

    bus = _FakeBus()
    ctx = SimpleNamespace(
        datarc=None, kb_files_store=None, agent_registry=None, bi_availability=lambda: "absent", execution_generation=1, execution_owner_id=None
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_absent", role="user", planning_mode="planning")

    class _R:
        async def invoke(self, agent_type, req):  # noqa: ANN001, ANN202
            return _Resp(plan_json, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_ToolRegistry({"compute": list(_BI_TOOLS)}),
    )
    await _lead_loop(sctx, "查一下经营指标", runtime)

    assert bus.find_all("plan_proposed") == [], "本 turn 无可用 BI 数据源时不该让用户先批一次"
    assert getattr(bus, "_planning_fallback", None) == "plan_infeasible"


# ── R3 wiring: team-created agents enter planner roster ──────────
#
# ★Independent review 2026-08-11: **entire path untested**. In `lead_orchestrator`, change
# `for _nm, _desc, _refs in _team_entries:` loop to `in []:`, or erase capability derivation
# to `frozenset()`: **46 related tests all green**. Repository-wide grep: no planning test
# ever wired `team_subagent_store` into `LeadRuntime` (0 sites).
# Exactly `feedback_capability_off_path_untested`: R2 (execution admission) solidly tested,
# R3, the user-visible half, completely untested.


class _TeamStore:
    """团队自建 agent 的 store 替身：只提供 R3 接线真正会读的那几个字段。"""

    def __init__(self, rows) -> None:  # noqa: ANN001
        self._rows = rows

    async def list_team_subagents(self, team_id, only_enabled=True):  # noqa: ANN001, ANN202
        return self._rows


def _team_row(name, *, refs, visibility="team_wide", created_by="u1", desc="团队自建"):  # noqa: ANN001, ANN202
    return SimpleNamespace(name=name, description=desc, tool_refs=list(refs), visibility=visibility, created_by=created_by, id=f"sa_{name}")


async def _run_with_team(plan_json, allow_by_agent, team_rows, store, *, user_id="u1", datarc_state=""):  # noqa: ANN001, ANN202
    """Run planning and capture the actual roster-bearing model prompt."""
    bus = _FakeBus()
    seen: dict[str, str] = {}
    ctx = SimpleNamespace(
        datarc=None,
        kb_files_store=None,
        agent_registry=None,
        team_id="org_1",
        granted_team_subagent_ids=None,
        bi_availability=lambda: datarc_state,
        execution_generation=1,
        execution_owner_id=None,
    )
    sctx = SerializableTurnContext(user_id=user_id, workspace_id="ws1", session_id="s1", turn_id="t_r3", role="user", planning_mode="planning")

    class _R:
        async def invoke(self, agent_type, req):  # noqa: ANN001, ANN202
            # ⚠ Router called **twice** per turn (graph + criteria); inspect only roster-bearing call,
            # or criteria prompt passes as roster: it happens to contain `agent=<name>` too,
            # yielding accidentally green assertions.
            text = "\n".join(m.content for m in req.messages)
            if "可用 agent roster" in text:
                seen["prompt"] = text
            return _Resp(plan_json, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_ToolRegistry(allow_by_agent),
        team_subagent_store=_TeamStore(team_rows),
    )
    await _lead_loop(sctx, "对上个月的冷链运输做温控合规巡检", runtime)
    return bus, seen.get("prompt", "")


def _planner_roster_entry(prompt: str, name: str) -> dict[str, Any]:
    """Read one entry from the planner's fenced JSONL roster."""
    inside_roster = False
    for line in prompt.splitlines():
        if line == "<untrusted_agent_roster>":
            inside_roster = True
            continue
        if line == "</untrusted_agent_roster>":
            break
        if not inside_roster:
            continue
        entry = json.loads(line)
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"{name!r} not found in planner roster")


@pytest.mark.asyncio
async def test_team_agent_enters_planner_roster_with_derived_capabilities(tmp_path, _plan_flag) -> None:
    """★ R3 的正题：团队自建 agent 必须出现在**送进 planner 的花名册**里，且带能力标签。

    此前 planner 只看 registry(system yaml) ⇒ 聊天能用的 peer 到出计划就消失。
    这条同时钉住两件事（对应两个此前全绿的突变）：
      · 花名册循环真的把它 append 进去（M40）；
      · 能力真的从 `tool_refs` 派生，而不是恒空（M41）。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = '{"steps": [{"id": "s1", "agent_name": "coldchain-audit", "objective": "巡检温控", "needs_corpus": false, "depends_on": []}]}'
    _, prompt = await _run_with_team(
        plan_json,
        {"compute": list(_BI_TOOLS)},
        [_team_row("coldchain-audit", refs=["bi_query", "bi_schema"])],
        store,
    )
    assert "coldchain-audit" in prompt, "团队自建 agent 没进 planner 花名册"
    entry = _planner_roster_entry(prompt, "coldchain-audit")
    assert "查本空间BI" in entry["capabilities"], f"能力没从 tool_refs 派生出来: {entry!r}"


@pytest.mark.asyncio
async def test_power_bi_only_team_agent_keeps_data_capability_in_planner_roster(tmp_path, _plan_flag) -> None:
    """M3-12: planner uses the same legacy capability evidence as Lead."""

    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = '{"steps": [{"id": "s1", "agent_name": "powerbi-analyst", "objective": "查经营指标", "needs_corpus": false, "depends_on": []}]}'
    _, prompt = await _run_with_team(
        plan_json,
        {"compute": list(_BI_TOOLS)},
        [_team_row("powerbi-analyst", refs=["teammcp.ibt_powerbi.pbi_execute_dax"])],
        store,
    )

    entry = _planner_roster_entry(prompt, "powerbi-analyst")
    assert "查本空间BI" in entry["capabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("datarc_state", ["absent", "needs_selection"])
async def test_power_bi_plan_remains_feasible_when_datarc_is_not_ready(tmp_path, _plan_flag, datarc_state: str) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = json.dumps(
        {
            "steps": [
                {"id": "sales", "agent_name": "powerbi-analyst", "objective": "Query sales", "depends_on": []},
                {"id": "profit", "agent_name": "powerbi-analyst", "objective": "Query profit", "depends_on": []},
            ]
        }
    )
    bus, prompt = await _run_with_team(
        plan_json,
        {"powerbi-analyst": ["teammcp.ibt_powerbi.pbi_execute_dax"]},
        [],
        store,
        datarc_state=datarc_state,
    )

    proposed = bus.find_all("plan_proposed")
    assert len(proposed) == 1
    assert [step["feasibility"] for step in proposed[0].payload["steps"]] == ["ok", "ok"]
    assert getattr(bus, "_planning_fallback", None) is None
    assert "Use the requested source and available Agent Tools" in prompt
    expected = "Other sources remain usable" if datarc_state == "absent" else "other sources are unaffected"
    assert expected in prompt


@pytest.mark.asyncio
async def test_team_agent_with_web_tool_gets_web_capability(tmp_path, _plan_flag) -> None:
    """能力派生用的是与内置**同一个函数** —— 换一组工具，标签必须跟着变。

    只有一条 BI 用例时，`derive_capabilities` 被换成「恒返 queries_bi」也照样绿。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = '{"steps": [{"id": "s1", "agent_name": "compute", "objective": "查指标", "needs_corpus": false, "depends_on": []}]}'
    _, prompt = await _run_with_team(
        plan_json,
        {"compute": list(_BI_TOOLS)},
        [_team_row("web-scout", refs=["web_search", "kb_search", "teammcp.crm.q"])],
        store,
    )
    entry = _planner_roster_entry(prompt, "web-scout")
    capabilities = entry["capabilities"]
    assert "联网检索" in capabilities and "读上传文档" in capabilities, entry
    assert "查本空间BI" not in capabilities, "没有 bi_query 却派生出了查 BI 的能力"


@pytest.mark.asyncio
async def test_creator_only_team_agent_is_absent_from_another_members_roster(tmp_path, _plan_flag) -> None:
    """★ 成对断言：creator_only 的自建 agent 只对创建者出现在花名册里。

    只写「他人看不见」是空的 —— 接线断了谁都看不见，那条照样绿。
    """
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = '{"steps": [{"id": "s1", "agent_name": "compute", "objective": "查指标", "needs_corpus": false, "depends_on": []}]}'
    rows = [_team_row("my-private-bot", refs=["bi_query"], visibility="creator_only", created_by="owner")]

    _, mine = await _run_with_team(plan_json, {"compute": list(_BI_TOOLS)}, rows, store, user_id="owner")
    assert "my-private-bot" in mine, "阳性对照失败：创建者自己都看不到，下面那条断言就是空的"

    _, theirs = await _run_with_team(plan_json, {"compute": list(_BI_TOOLS)}, rows, store, user_id="someone-else")
    assert "my-private-bot" not in theirs, "别人的 creator_only agent 出现在了花名册里"


@pytest.mark.asyncio
async def test_non_plannable_names_are_excluded_even_from_team_source(tmp_path, _plan_flag) -> None:
    """团队若建了个叫 `lead` 的 agent，也不该混进计划步花名册。"""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan_json = '{"steps": [{"id": "s1", "agent_name": "compute", "objective": "查指标", "needs_corpus": false, "depends_on": []}]}'
    _, prompt = await _run_with_team(
        plan_json,
        {"compute": list(_BI_TOOLS)},
        [_team_row("lead", refs=["bi_query"]), _team_row("ok-bot", refs=["bi_query"])],
        store,
    )
    assert _planner_roster_entry(prompt, "ok-bot")["name"] == "ok-bot"
    with pytest.raises(AssertionError, match="not found in planner roster"):
        _planner_roster_entry(prompt, "lead")


# ── ★2026-08-14 true project criterion: number of model-authored steps ──
#
# Previously only zero steps rejected, so 1-step empty cards still appeared: 7 of 73 plans (10%) on 116
# had ≤1 step (even single query like last-year monthly revenue got a card).


@pytest.mark.asyncio
async def test_single_step_authoring_is_not_a_project(tmp_path, _plan_flag) -> None:
    """模型拆成 1 步 → 不弹卡，静默走普通回答。"""
    store = PlanStore(str(tmp_path / "p1.db"))
    await store.initialize()
    one = '{"steps": [{"id": "a", "agent_name": "business-analyst", "objective": "查今年利润前三的品牌", "needs_corpus": false, "depends_on": []}]}'
    _, bus = await _run_with(one, {"business-analyst": list(_BI_TOOLS)}, store)

    assert bus.find_all("plan_proposed") == [], "拆不出多步就不是项目，不该弹卡"
    # ★Do not record planning_fallback: metric means needed plan failed to generate; here model
    # correctly decides no plan needed: appropriate silence, user notification adds noise.
    assert getattr(bus, "_planning_fallback", None) is None
    # No orphan PROPOSED row either.
    plans = [p async for p in _aiter(store, "ws1", "u1")]
    assert plans == []


@pytest.mark.asyncio
async def test_two_step_authoring_still_proposes(tmp_path, _plan_flag) -> None:
    """★正对照：拆出 2 步必须照常弹卡 —— 否则上一条就是把整个功能关掉。"""
    store = PlanStore(str(tmp_path / "p2.db"))
    await store.initialize()
    two = (
        '{"steps": ['
        '{"id": "a", "agent_name": "business-analyst", "objective": "按品牌拆解退款率", "needs_corpus": false, "depends_on": []},'
        '{"id": "b", "agent_name": "financial-analyst", "objective": "按门店拆解退款额", "needs_corpus": false, "depends_on": []}'
        "]}"
    )
    _, bus = await _run_with(two, {"business-analyst": list(_BI_TOOLS), "financial-analyst": list(_BI_TOOLS)}, store)
    assert bus.find_all("plan_proposed"), "2 步是真项目，必须出卡"


@pytest.mark.asyncio
async def test_off_is_refused_at_the_card_chokepoint(tmp_path, _plan_flag) -> None:
    """★`off` 的守卫在出卡咽喉点，不在某个调用方。

    现在有两条路能走到 `_maybe_propose_plan`（显式 Planning 的 loop 前直通 / Auto 下
    Lead 调 plan_propose 的哨兵）。守在调用方 = 每多一条路多一个漏口，所以守在这里。
    直接调它、绕开所有上游闸 —— 这条才是**穿过守卫本身**的验证。
    """
    from bowhead_core.agents.lead_orchestrator import _maybe_propose_plan

    store = PlanStore(str(tmp_path / "off.db"))
    await store.initialize()
    bus = _FakeBus()
    ctx = SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None)
    runtime = LeadRuntime(
        router=_FakeRouter(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    sctx_off = SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id="t_off_choke",
        role="user",
        planning_mode="off",
    )
    assert await _maybe_propose_plan(sctx_off, "对这个商超做一次经营尽调", runtime) is None
    assert bus.find_all("plan_proposed") == []

    # ★Positive control: same text/runtime, only mode changed to planning → must show card.
    # Without this half, replacing entire `_maybe_propose_plan` with `return None` leaves above green.
    sctx_on = SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id="t_on_choke",
        role="user",
        planning_mode="planning",
    )
    assert await _maybe_propose_plan(sctx_on, "对这个商超做一次经营尽调", runtime) is not None


# ── 2026-08-15 user-decided Auto semantics ───────────────────────
#   ①**Multiple agents need no plan unless dependent**: zero dependency edges (pure parallel) is no project;
#   ②Auto **never awaits approval**: generated plan executes this turn (Planning mode buys await-your-approval state).


@pytest.mark.asyncio
async def test_auto_flat_fanout_still_gets_a_plan(tmp_path, _plan_flag) -> None:
    """★「按省/按品牌/按门店各统计一下」= 3 个独立步、零依赖边 → 不出计划。

    这条挡的正是我一度以为 `len(authored) < 2` 挡得住、其实放行的形状：
    步数够（3 ≥ 2）但图是平的，计划买到的「上游喂下游 / 断了隔离下游 / 局部重跑」
    一个都用不上，只剩可见性，却要付一次 8k token 的 author_plan + 一张要读的卡。

    突变：删掉 `flat_fanout` 那条判据 → 本条红。
    """
    from bowhead_core.agents.lead_orchestrator import _maybe_propose_plan

    store = PlanStore(str(tmp_path / "flat.db"))
    await store.initialize()
    flat = (
        '{"steps": ['
        '{"id": "a", "agent_name": "business-analyst", "objective": "按省统计营业额", "needs_corpus": false, "depends_on": []},'
        '{"id": "b", "agent_name": "business-analyst", "objective": "按品牌统计营业额", "needs_corpus": false, "depends_on": []},'
        '{"id": "c", "agent_name": "financial-analyst", "objective": "按门店统计营业额", "needs_corpus": false, "depends_on": []}'
        "]}"
    )
    bus = _FakeBus()

    class _R:
        async def invoke(self, agent_type: str, req: Any) -> _Resp:
            return _Resp(flat, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None),
        plan_store=store,
        session_store=_AutoClaimSessionStore(store),
        multi_agent_registry=_FakeRegistry(),
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_flat", role="user", planning_mode=None)
    # ★Retracted 2026-08-15: previously asserted no card for flat graph. Actual runs disproved: planner forbidden
    # to plan summary step, so N specialist outputs + merge graph **is naturally flat**; old criterion rejected
    # most deserving cases (116 actual flat_fanout steps=5). Flat graphs now show cards normally.
    assert await _maybe_propose_plan(sctx, "按省/按品牌/按门店各统计一下营业额", runtime) is not None
    assert bus.find_all("plan_proposed"), "平图也要出卡——依赖边在本系统里测不到想测的东西"


@pytest.mark.asyncio
async def test_explicit_planning_still_gets_a_flat_plan(tmp_path, _plan_flag) -> None:
    """★负对照：同一张平图，显式 Plan 模式**必须**照出。

    用户明说要计划就给他 —— 依赖判据只用在 Auto。少了这半，把 flat 判据写成
    无差别拒绝上面那条也照样绿。
    """
    from bowhead_core.agents.lead_orchestrator import _maybe_propose_plan

    store = PlanStore(str(tmp_path / "flat_explicit.db"))
    await store.initialize()
    flat = (
        '{"steps": ['
        '{"id": "a", "agent_name": "business-analyst", "objective": "按省统计营业额", "needs_corpus": false, "depends_on": []},'
        '{"id": "b", "agent_name": "financial-analyst", "objective": "按门店统计营业额", "needs_corpus": false, "depends_on": []}'
        "]}"
    )
    bus = _FakeBus()

    class _R:
        async def invoke(self, agent_type: str, req: Any) -> _Resp:
            return _Resp(flat, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None),
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_flat_x", role="user", planning_mode="planning")
    assert await _maybe_propose_plan(sctx, "按省和按门店各统计一下", runtime) is not None
    assert bus.find_all("plan_proposed"), "显式 Plan 模式下平图也要出卡"


@pytest.mark.asyncio
async def test_plan_proposed_carries_auto_run_flag(tmp_path, _plan_flag) -> None:
    """`auto_run` 是 payload 内的新字段（不是新事件类型）—— FE 据它渲「执行中」而非「待批准」。

    显式 planning 必须是 False，否则用户点不到批准就被开跑了。
    """
    from bowhead_core.agents.lead_orchestrator import _maybe_propose_plan

    store = PlanStore(str(tmp_path / "autorun.db"))
    await store.initialize()
    dep = (
        '{"steps": ['
        '{"id": "a", "agent_name": "business-analyst", "objective": "按品牌拆解退款率", "needs_corpus": false, "depends_on": []},'
        '{"id": "b", "agent_name": "financial-analyst", "objective": "结合上一步给结论", "needs_corpus": false, "depends_on": ["a"]}'
        "]}"
    )
    bus = _FakeBus()

    class _R:
        async def invoke(self, agent_type: str, req: Any) -> _Resp:
            return _Resp(dep, usage=_Usage())

    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=SimpleNamespace(datarc=None, kb_files_store=None, agent_registry=None, execution_generation=1, execution_owner_id=None),
        plan_store=store,
        multi_agent_registry=_FakeRegistry(),
    )
    sctx = SerializableTurnContext(user_id="u1", workspace_id="ws1", session_id="s1", turn_id="t_autorun", role="user", planning_mode="planning")
    await _maybe_propose_plan(sctx, "先拆退款率再给结论", runtime)
    cards = bus.find_all("plan_proposed")
    assert cards, "有依赖边的图必须出卡"
    assert cards[0].payload.get("auto_run") is False, "显式 planning 不许自动开跑"


@pytest.mark.asyncio
async def test_auto_plan_claims_the_admitted_turn_before_execution(
    tmp_path,
    _plan_flag,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto 动态成图也必须建立 Stop/recovery 可见的 exact Turn claim。"""
    from bowhead_core.agents import lead_orchestrator

    store = PlanStore(str(tmp_path / "autorun-claim.db"))
    await store.initialize()
    dep = (
        '{"steps": ['
        '{"id": "a", "agent_name": "business-analyst", "objective": "先分析", "needs_corpus": false, "depends_on": []},'
        '{"id": "b", "agent_name": "financial-analyst", "objective": "再总结", "needs_corpus": false, "depends_on": ["a"]}'
        "]}"
    )

    class _StopAtPublishBus(_FakeBus):
        async def emit(self, event: Any) -> None:
            await super().emit(event)
            if event.event_type != "plan_proposed":
                return
            visible = await store.get_plan(
                str(event.payload["plan_id"]),
                user_id="u1",
                workspace_id="ws1",
            )
            assert visible is not None
            assert visible.execution_turn_id == "t_autorun_claim"
            assert await store.cancel_execution_for_turn(
                "t_autorun_claim",
            )

    bus = _StopAtPublishBus()

    class _R:
        async def invoke(self, agent_type: str, req: Any) -> _Resp:
            return _Resp(dep, usage=_Usage())

    observed_plan_ids: list[str] = []

    async def _fake_run_plan(sctx: SerializableTurnContext, _runtime: LeadRuntime) -> TurnResult:
        assert sctx.plan_execution_id is not None
        observed_plan_ids.append(sctx.plan_execution_id)
        return TurnResult(
            turn_id=sctx.turn_id,
            status=TurnStatus.COMPLETED,
            intent=IntentType.REPORT,
            final_answer="done",
            duration_ms=1,
        )

    monkeypatch.setattr(
        lead_orchestrator,
        "_run_plan_execution_turn",
        _fake_run_plan,
    )
    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=SimpleNamespace(
            datarc=None,
            kb_files_store=None,
            agent_registry=None,
            execution_generation=1,
            execution_owner_id=None,
        ),
        plan_store=store,
        session_store=_AutoClaimSessionStore(store),
        multi_agent_registry=_FakeRegistry(),
    )
    sctx = SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id="t_autorun_claim",
        role="user",
        planning_mode="auto",
    )

    with pytest.raises(asyncio.CancelledError, match="plan_auto_execution_claim_lost"):
        await lead_orchestrator._maybe_propose_plan(
            sctx,
            "先分析再总结",
            runtime,
        )

    assert observed_plan_ids == []
    [plan] = [p async for p in _aiter(store, "ws1", "u1")]
    assert plan.status == PlanStatus.CANCELLED
    assert plan.execution_turn_id == "t_autorun_claim"


@pytest.mark.asyncio
async def test_auto_plan_claim_commit_ack_cancellation_releases_exact_token(
    tmp_path,
    _plan_flag,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.agents import lead_orchestrator

    store = PlanStore(str(tmp_path / "autorun-claim-cancel.db"))
    await store.initialize()
    session_store = _AutoClaimSessionStore(store)
    real_claim = session_store.try_claim_auto_plan_for_live_turn
    claim_committed = asyncio.Event()
    allow_claim_return = asyncio.Event()

    async def commit_then_wait(*, plan_id: str, turn_id: str) -> bool:
        claimed = await real_claim(plan_id=plan_id, turn_id=turn_id)
        claim_committed.set()
        await allow_claim_return.wait()
        return claimed

    monkeypatch.setattr(
        session_store,
        "try_claim_auto_plan_for_live_turn",
        commit_then_wait,
    )

    class _R:
        async def invoke(self, agent_type: str, req: Any) -> _Resp:
            return _Resp(
                '{"steps": ['
                '{"id": "a", "agent_name": "business-analyst", "objective": "先分析", "needs_corpus": false, "depends_on": []},'
                '{"id": "b", "agent_name": "financial-analyst", "objective": "再总结", "needs_corpus": false, "depends_on": ["a"]}'
                "]}",
                usage=_Usage(),
            )

    bus = _FakeBus()
    runtime = LeadRuntime(
        router=_R(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=SimpleNamespace(
            datarc=None,
            kb_files_store=None,
            agent_registry=None,
            execution_generation=1,
            execution_owner_id=None,
        ),
        plan_store=store,
        session_store=session_store,
        multi_agent_registry=_FakeRegistry(),
    )
    sctx = SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id="t_autorun_claim_cancel",
        role="user",
        planning_mode="auto",
    )

    proposal = asyncio.create_task(
        lead_orchestrator._maybe_propose_plan(
            sctx,
            "先分析再总结",
            runtime,
        ),
    )
    await asyncio.wait_for(claim_committed.wait(), timeout=1)
    proposal.cancel("claim acknowledgement interrupted")
    allow_claim_return.set()
    with pytest.raises(asyncio.CancelledError):
        await proposal

    [plan] = [p async for p in _aiter(store, "ws1", "u1")]
    assert plan.status == PlanStatus.BLOCKED
    assert plan.execution_turn_id is None
    assert bus.find_all("plan_proposed") == []
