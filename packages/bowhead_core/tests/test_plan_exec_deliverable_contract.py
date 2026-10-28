"""Plan 执行交付物契约测试 (planning-mode-audit 结构修 WP-1, tech-design §P1-a/P1-b/P1-d).

锁三件事:
1. adapter 三查 (sentinel → bail → 双空): ok=True 但实质无效的子代理结果不得再
   变成 DONE 步 (discovery: prod 10 个 done-EMPTY 步 + datarc 故障日 7 步假成功)。
2. _run_one 映射: PlanStepError 家族 → 干净 error_kind (非异常类名) + detail 透传。
3. driver writeback: needs_user_input → 步 BLOCKED(可恢复) + clarification JSON 落
   error 列 + step_blocked 事件; empty_output/bail → FAILED; plan 收 blocked_needs_user。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents.agent_runner_adapter import (
    make_legacy_agent_runner_fn,
    register_all_system_manifests,
)
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.agents.task_runner import LocalTaskRunner
from bowhead_core.agents.task_spec import (
    BusRef,
    PlanStepBailError,
    PlanStepEmptyOutputError,
    PlanStepError,
    PlanStepNeedsUserError,
    TaskFailureError,
    TaskResult,
    TaskSpec,
)
from bowhead_core.agents.turn_context import RuntimeTurnContext, SerializableTurnContext
from bowhead_core.plan_driver import PlanCaps, PlanDeps, PlanHooks, run_plan
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStore, StepStatus, new_step
from bowhead_core.subagent import SubagentResult

if TYPE_CHECKING:
    from pathlib import Path

# ── Shared stubs (like test_agent_runner_adapter / test_plan_driver) ──


@dataclass
class _StubBus:
    workspace_id: str = "ws_test"
    emitted: list[Any] = field(default_factory=list)

    async def emit(self, ev: Any) -> None:
        self.emitted.append(ev)


@dataclass
class _StubCtx:
    user_id: str = "u_1"
    workspace_id: str = "ws_test"
    role: str = "analyst"


@dataclass
class _StubRunner:
    next_result: SubagentResult | None = None

    async def run(self, **_kw: Any) -> SubagentResult:
        assert self.next_result is not None
        return self.next_result


def _mk_runtime_ctx() -> RuntimeTurnContext:
    sctx = SerializableTurnContext(
        user_id="u_1",
        workspace_id="ws_test",
        session_id="s_a",
        turn_id="t_b",
        role="analyst",
        bus_ref=BusRef(session_id="s_a", turn_id="t_b"),
    )
    return RuntimeTurnContext(serializable=sctx)


def _runner_fn_with(result: SubagentResult):
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")
    fn = make_legacy_agent_runner_fn(
        subagent_runner=_StubRunner(next_result=result),  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required argument is security guard.
    )
    return fn, manifest


def _sub(ok: bool = True, payload: dict | None = None, final_answer: str = "", invocations: list | None = None) -> SubagentResult:
    return SubagentResult(
        profile="compute",
        subagent_id="sub_x",
        ok=ok,
        final_answer=final_answer,
        payload=payload,
        tool_invocations=invocations or [],
        tokens_input=10,
        tokens_output=5,
    )


_SENTINEL_RESULT = {
    "ok": False,
    "error_kind": "needs_clarification",
    "clarification": {
        "kind": "tool_context_select",
        "tool": "datarc",
        "prompt": "你在北极九章有 26 个可访问的工作区，这次查询用哪个？",
        "options": [{"value": "1", "label": "商超零售场景"}],
    },
}


# ── 1. Adapter three checks ─────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_output_raises_typed() -> None:
    """双空 (payload=None, final_answer='') → PlanStepEmptyOutputError,不再返 {} 记 DONE。"""
    fn, manifest = _runner_fn_with(_sub(payload=None, final_answer=""))
    with pytest.raises(PlanStepEmptyOutputError):
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())


@pytest.mark.asyncio
async def test_bail_envelope_error_kind_raises_with_original_kind() -> None:
    """payload.error.kind (subagent_return bail 信封) → PlanStepBailError(原 kind)。"""
    fn, manifest = _runner_fn_with(_sub(payload={"title": "x", "error": {"kind": "domain_mismatch"}}))
    with pytest.raises(PlanStepBailError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert ei.value.error_kind == "domain_mismatch"


@pytest.mark.asyncio
async def test_pi_b_hard_bail_real_shape_raises() -> None:
    """π-B 硬 bail 的**真实生产形状** (WP-1 review P1-1a 修正): bail 标记只在
    subagent_return 的工具返回 dict (invocation.result), stashed payload 只有
    narrative/fallback_markdown — 检查必须扫 invocations 而非 payload。"""
    bail_inv = SimpleNamespace(
        result={
            "ok": True,
            "__terminal": True,
            "bail_kind": "ν1_ref_unresolvable",
            "payload": {"narrative": "无法解析引用。"},
            "summary": {"bail": True, "reason": "ν1_ref_unresolvable"},
        }
    )
    fn, manifest = _runner_fn_with(
        _sub(
            payload={"narrative": "无法解析引用。", "fallback_markdown": "无法解析引用。", "evidence_refs": []},
            invocations=[bail_inv],
        )
    )
    with pytest.raises(PlanStepBailError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert ei.value.error_kind == "ν1_ref_unresolvable"


@pytest.mark.asyncio
async def test_successful_terminal_return_not_killed() -> None:
    """⚠ 判据不变量: `__terminal` 每个成功 subagent_return 都带 — 只有 bail_kind
    才算 bail。成功 return + 正常 payload 必须原样通过。"""
    ok_inv = SimpleNamespace(
        result={
            "ok": True,
            "__terminal": True,
            "summary": {"insights_count": 2},
            "message": "Payload accepted.",
        }
    )
    fn, manifest = _runner_fn_with(_sub(payload={"title": "Q1", "exec_summary": "增长 3%", "insights": []}, invocations=[ok_inv]))
    payload, _ti, _to = await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert payload["title"] == "Q1"


@pytest.mark.asyncio
async def test_needs_user_input_envelope_carries_clarification_payload() -> None:
    """error.kind==needs_user_input 信封 → PlanStepNeedsUserError 且
    error.clarification_payload 透传 detail_json (WP-1 review P2-1b)。"""
    fn, manifest = _runner_fn_with(
        _sub(
            payload={
                "error": {
                    "kind": "needs_user_input",
                    "message": "指标写法歧义",
                    "clarification_payload": {"questions": [{"q": "利润率指哪个口径?"}]},
                }
            }
        )
    )
    with pytest.raises(PlanStepNeedsUserError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    detail = json.loads(ei.value.detail_json or "{}")
    assert detail.get("questions")


@pytest.mark.asyncio
async def test_sentinel_invocation_raises_needs_user_with_detail() -> None:
    """tool_invocations 带 tool_context_select sentinel → PlanStepNeedsUserError,
    clarification 透传 detail_json (此前: 子代理硬 break → ok=True 空产出 → DONE)。"""
    inv = SimpleNamespace(result=_SENTINEL_RESULT)
    fn, manifest = _runner_fn_with(_sub(payload=None, final_answer="", invocations=[inv]))
    with pytest.raises(PlanStepNeedsUserError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    detail = json.loads(ei.value.detail_json or "{}")
    assert detail.get("kind") == "tool_context_select"
    assert detail.get("options")


@pytest.mark.asyncio
async def test_sentinel_takes_precedence_over_empty() -> None:
    """三查顺序载重: sentinel peer 常常双空 — 必须命中 needs_user_input 而非 empty_output。"""
    inv = SimpleNamespace(result=_SENTINEL_RESULT)
    fn, manifest = _runner_fn_with(_sub(payload=None, final_answer="", invocations=[inv]))
    with pytest.raises(PlanStepNeedsUserError):
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())


@pytest.mark.asyncio
async def test_prose_only_result_not_killed() -> None:
    """有 prose 无 payload 不算空 — 走既有 fallback_markdown 包装 (P2 有消费者)。"""
    fn, manifest = _runner_fn_with(_sub(payload=None, final_answer="我需要 Lead 协调上游。"))
    payload, _ti, _to = await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert payload == {"fallback_markdown": "我需要 Lead 协调上游。"}


@pytest.mark.asyncio
async def test_flag_off_restores_legacy_empty_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOWHEAD_PLANNING_V2=0 → 三查关闭,回旧行为 (空 payload 直接返回)。"""
    monkeypatch.setenv("BOWHEAD_PLANNING_V2", "0")
    fn, manifest = _runner_fn_with(_sub(payload=None, final_answer=""))
    payload, _ti, _to = await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert payload == {}


# ── 2. _run_one mapping: clean kind + detail passthrough ────────


@pytest.mark.asyncio
async def test_run_one_maps_plan_step_error_to_clean_kind() -> None:
    """PlanStepError → TaskFailureError(reason=error_kind 非类名, detail 透传)。"""

    async def _raise_fn(_m: Any, _s: Any, _r: Any) -> tuple[dict, int, int]:
        raise PlanStepNeedsUserError(detail_json='{"kind": "tool_context_select"}')

    runner = LocalTaskRunner()
    sctx = SerializableTurnContext(
        user_id="u",
        workspace_id="w",
        session_id="s",
        turn_id="t",
        role="analyst",
        bus_ref=BusRef(session_id="s", turn_id="t"),
    )
    res = await runner._run_one(
        TaskSpec(agent_name="compute", user_question="q", timeout_s=5),
        sctx,
        lambda c: c,
        _raise_fn,
        lambda _n: object(),
    )
    assert res.ok is False
    assert res.error is not None
    assert res.error.reason == "needs_user_input"  # Not "PlanStepNeedsUserError".
    assert res.error.detail == '{"kind": "tool_context_select"}'


@pytest.mark.asyncio
async def test_run_one_maps_empty_output_kind() -> None:
    async def _raise_fn(_m: Any, _s: Any, _r: Any) -> tuple[dict, int, int]:
        raise PlanStepEmptyOutputError()

    runner = LocalTaskRunner()
    sctx = SerializableTurnContext(
        user_id="u",
        workspace_id="w",
        session_id="s",
        turn_id="t",
        role="analyst",
        bus_ref=BusRef(session_id="s", turn_id="t"),
    )
    res = await runner._run_one(
        TaskSpec(agent_name="compute", user_question="q", timeout_s=5),
        sctx,
        lambda c: c,
        _raise_fn,
        lambda _n: object(),
    )
    assert res.ok is False
    assert res.error is not None and res.error.reason == "empty_output"


# ── 3. Driver writeback: BLOCKED vs FAILED + plan convergence ───


@dataclass
class FakeSctx:
    user_id: str = "u"
    workspace_id: str = "w"
    turn_id: str = "t_run"
    session_id: str = "s"


class KindRunner:
    """behavior: {agent_name: ("ok", payload) | ("kind", reason[, detail])}."""

    def __init__(self, behavior: dict[str, tuple] | None = None) -> None:
        self.behavior = behavior or {}

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        out = []
        for t in tasks:
            beh = self.behavior.get(t.agent_name, ("ok", {"exec_summary": f"{t.agent_name} ok"}))
            if beh[0] == "ok":
                out.append(TaskResult.succeeded(t, beh[1], tokens_input=10, tokens_output=20))
            else:
                detail = beh[2] if len(beh) > 2 else None
                out.append(TaskResult.failed(t, TaskFailureError(beh[1], detail=detail)))
        return out


def _lookup(known: set[str]):
    def lk(name: str) -> object:
        if name in known:
            return object()
        raise KeyError(name)

    return lk


def _deps(runner: Any, known: set[str]) -> PlanDeps:
    return PlanDeps(
        sctx=FakeSctx(),
        runtime_factory=lambda c: c,
        agent_runner_fn=None,
        manifest_lookup=_lookup(known),
        runner=runner,
    )


async def _setup(store: PlanStore, specs: list[tuple[str, list[int], bool]]):
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}", needs_corpus=nc) for i, (a, _d, nc) in enumerate(specs)]
    for st, (_a, dep_idx, _nc) in zip(steps, specs, strict=True):
        st.depends_on = tuple(steps[d].id for d in dep_idx)
    levels = assign_levels(steps)
    for st in steps:
        st.level = levels[st.id]
    await store.add_steps(steps)
    return plan, steps


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


@pytest.mark.asyncio
async def test_needs_user_input_failure_lands_step_blocked(db_path: str) -> None:
    """needs_user_input → 步 BLOCKED + clarification JSON 落 error + step_blocked 事件;
    plan 收 blocked_needs_user (可 re-approve)。"""
    store = PlanStore(db_path)
    await store.initialize()
    plan, steps = await _setup(store, [("financial-analyst", [], False)])
    clar = '{"kind": "tool_context_select", "options": [{"value": "1"}]}'
    runner = KindRunner({"financial-analyst": ("kind", "needs_user_input", clar)})
    events: list[tuple[str, dict]] = []

    async def rec(name: str, payload: dict) -> None:
        events.append((name, payload))

    out = await run_plan(
        plan.id,
        store=store,
        deps=_deps(runner, {"financial-analyst"}),
        hooks=PlanHooks(emit=rec),
        caps=PlanCaps(),
        parent_token_budget=20000,
    )
    assert out.status == "blocked_needs_user"
    s = (await store.list_steps(plan.id))[0]
    assert str(s.status) == str(StepStatus.BLOCKED)
    assert s.error_kind == "needs_user_input"
    assert json.loads(s.error)["kind"] == "tool_context_select"
    names = [n for n, _p in events]
    assert "step_blocked" in names
    assert "step_failed" not in names


@pytest.mark.asyncio
async def test_stale_agent_choice_lands_recoverable_blocked(db_path: str) -> None:
    store = PlanStore(db_path)
    await store.initialize()
    plan, _ = await _setup(store, [("marketing-analyst", [], False)])
    runner = KindRunner({"marketing-analyst": ("kind", "agent_candidate_stale")})
    events: list[tuple[str, dict]] = []

    async def rec(name: str, payload: dict) -> None:
        events.append((name, payload))

    out = await run_plan(
        plan.id,
        store=store,
        deps=_deps(runner, {"marketing-analyst"}),
        hooks=PlanHooks(emit=rec),
        caps=PlanCaps(),
        parent_token_budget=20000,
    )

    assert out.status == "blocked_needs_user"
    step = (await store.list_steps(plan.id))[0]
    assert step.status == StepStatus.BLOCKED
    assert step.error_kind == "agent_candidate_stale"
    assert any(name == "step_blocked" for name, _payload in events)
    assert all(name != "step_failed" for name, _payload in events)


@pytest.mark.asyncio
async def test_empty_output_failure_lands_step_failed(db_path: str) -> None:
    """empty_output → FAILED(可重试) + plan blocked_needs_user, 不再是 DONE 假成功。"""
    store = PlanStore(db_path)
    await store.initialize()
    plan, _ = await _setup(store, [("business-analyst", [], False)])
    runner = KindRunner({"business-analyst": ("kind", "empty_output")})
    out = await run_plan(
        plan.id,
        store=store,
        deps=_deps(runner, {"business-analyst"}),
        hooks=PlanHooks(),
        caps=PlanCaps(),
        parent_token_budget=20000,
    )
    assert out.status == "blocked_needs_user"
    s = (await store.list_steps(plan.id))[0]
    assert str(s.status) == str(StepStatus.FAILED)
    assert s.error_kind == "empty_output"


def test_plan_step_error_family_kinds() -> None:
    assert PlanStepEmptyOutputError().error_kind == "empty_output"
    assert PlanStepNeedsUserError().error_kind == "needs_user_input"
    assert PlanStepBailError("domain_mismatch").error_kind == "domain_mismatch"
    assert PlanStepBailError().error_kind == "peer_bail"
    assert issubclass(PlanStepBailError, PlanStepError)


# ── 4b. WP-3 per-upstream budget (replaces blind [:4000] cut) ─────


def test_upstream_block_both_large_payloads_survive() -> None:
    """两个大 payload 都要在块里可见(旧盲切: 第一个 1.5 万字符挤掉第二个——
    07-16 prod 案 writer 只见 1/2 kb payload)。"""
    from bowhead_core.agents.agent_runner_adapter import render_upstream_block

    big_a = {
        "agent": "kb-analyst",
        "task_id": "ps_a",
        "payload": {"title": "行业新闻", "exec_summary": "x" * 9000, "insights": [{"title": f"i{i}", "thesis": "t" * 200} for i in range(10)]},
    }
    big_b = {"agent": "kb-analyst", "task_id": "ps_b", "payload": {"title": "行业数据", "exec_summary": "y" * 9000}}
    out = render_upstream_block([big_a, big_b])
    assert "ps_a" in out and "ps_b" in out
    assert "行业新闻" in out and "行业数据" in out
    assert "_truncated" in out  # Mark trimming; writer aware.


def test_upstream_block_total_budget_omits_with_note() -> None:
    from bowhead_core.agents.agent_runner_adapter import render_upstream_block

    items = [{"agent": "a", "task_id": f"ps_{i}", "payload": {"title": f"t{i}", "exec_summary": "z" * 3000}} for i in range(10)]
    out = render_upstream_block(items, item_chars=4000, total_chars=8000)
    assert "已省略" in out
    assert "ps_9" in out  # Name omitted entries.


def test_upstream_block_small_payloads_untouched() -> None:
    from bowhead_core.agents.agent_runner_adapter import render_upstream_block

    items = [{"agent": "a", "task_id": "ps_1", "payload": {"title": "小", "exec_summary": "ok"}}]
    out = render_upstream_block(items)
    assert "_truncated" not in out
    assert "已省略" not in out


# ── 5. WP-2 datarc preflight (after approve, before spending tokens) ──


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def find_all(self, event_type: str) -> list[Any]:
        return [e for e in self.events if e.event_type == event_type]


class _FakeClarStore:
    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.resume_ctx: dict[str, dict] = {}

    async def put(self, **kw: Any) -> Any:
        self.puts.append(kw)
        return SimpleNamespace(request_id="req_preflight_1")

    async def set_resume_context(self, rid: str, ctx: dict) -> bool:
        self.resume_ctx[rid] = ctx
        return True


def _preflight_runtime(ctx: Any) -> Any:
    return SimpleNamespace(
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        multi_agent_registry=AgentRegistry.bootstrap(),
    )


def _unbound_ctx(clar_store: Any = None, n_ws: int = 2) -> Any:
    return SimpleNamespace(
        resolve_datarc_project=lambda: "",
        accessible_datarc_workspaces=tuple({"id": str(i), "name": f"ws-{i}"} for i in range(1, n_ws + 1)),
        clarification_store=clar_store,
    )


def _preflight_sctx(turn_id: str = "t_pf") -> Any:
    return SimpleNamespace(user_id="u", workspace_id="w", session_id="s", turn_id=turn_id)


@pytest.mark.asyncio
async def test_preflight_blocks_bi_steps_and_emits_picker(db_path: str) -> None:
    """三条件命中 → BI 步 BLOCKED、plan blocked、picker 卡、单 done(clarification_pending)、
    resume_context 带 plan_reapprove_plan_id、writer 步不动。"""
    from bowhead_core.agents.lead_orchestrator import _plan_context_preflight
    from bowhead_core.plan_store import PlanStatus
    from bowhead_core.types import TurnStatus

    store = PlanStore(db_path)
    await store.initialize()
    plan, steps = await _setup(
        store,
        [("financial-analyst", [], False), ("kb-analyst", [], True), ("canvas-writer", [0, 1], False)],
    )
    clar = _FakeClarStore()
    runtime = _preflight_runtime(_unbound_ctx(clar))
    res = await _plan_context_preflight(_preflight_sctx(), runtime, plan_store=store, plan_id=plan.id)

    assert res is not None and res.status == TurnStatus.CLARIFICATION_PENDING
    after = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert str(after["financial-analyst"].status) == str(StepStatus.BLOCKED)
    assert after["financial-analyst"].error_kind == "needs_user_input"
    assert str(after["kb-analyst"].status) == str(StepStatus.PENDING)
    assert str(after["canvas-writer"].status) == str(StepStatus.PENDING)
    p = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert p is not None and str(p.status) == str(PlanStatus.BLOCKED)

    bus = runtime.parent_bus
    assert len(bus.find_all("plan_blocked")) == 1
    # Review P2: frontend only recognizes per-step step_blocked; preflight emits each (step before plan).
    sb = bus.find_all("step_blocked")
    assert len(sb) == 1 and sb[0].payload["error_kind"] == "needs_user_input"
    clar_evs = bus.find_all("clarification_required")
    assert len(clar_evs) == 1
    cp = clar_evs[0].payload
    assert cp["kind"] == "tool_context_select"
    assert cp["clarification_id"] == "req_preflight_1"
    assert cp["remember_option"] is True
    assert len(cp["options"]) == 2
    dones = bus.find_all("done")
    assert len(dones) == 1
    assert dones[0].payload["status"] == "clarification_pending"
    assert dones[0].payload["error_kind"] == "plan_needs_context"
    assert clar.resume_ctx["req_preflight_1"] == {"plan_reapprove_plan_id": plan.id, "session_id": "s"}
    assert bus._pi_turn_state["error_kind"] == "plan_needs_context"


@pytest.mark.asyncio
async def test_preflight_noop_when_bound_or_single_or_no_bi(db_path: str) -> None:
    """负例三连: 已绑定 / 单工作区 / 无 BI 步 → 预检不触发返 None,步不动。"""
    from bowhead_core.agents.lead_orchestrator import _plan_context_preflight

    store = PlanStore(db_path)
    await store.initialize()
    plan, _ = await _setup(store, [("financial-analyst", [], False), ("canvas-writer", [0], False)])

    bound = SimpleNamespace(
        resolve_datarc_project=lambda: "1",
        accessible_datarc_workspaces=({"id": "1", "name": "a"}, {"id": "2", "name": "b"}),
        clarification_store=None,
    )
    assert await _plan_context_preflight(_preflight_sctx(), _preflight_runtime(bound), plan_store=store, plan_id=plan.id) is None

    single = _unbound_ctx(n_ws=1)
    assert await _plan_context_preflight(_preflight_sctx(), _preflight_runtime(single), plan_store=store, plan_id=plan.id) is None

    plan2, _ = await _setup(store, [("kb-analyst", [], True), ("canvas-writer", [0], False)])
    assert await _plan_context_preflight(_preflight_sctx(), _preflight_runtime(_unbound_ctx()), plan_store=store, plan_id=plan2.id) is None

    for s in await store.list_steps(plan.id):
        assert str(s.status) == str(StepStatus.PENDING)


@pytest.mark.asyncio
async def test_preflight_no_clar_store_still_blocks_honestly(db_path: str) -> None:
    """clarification_store 缺席 → 不发无主卡(FE 需要 clarification_id 建卡),
    但仍 blocked + 文案指引手动重试。"""
    from bowhead_core.agents.lead_orchestrator import _plan_context_preflight
    from bowhead_core.types import TurnStatus

    store = PlanStore(db_path)
    await store.initialize()
    plan, _ = await _setup(store, [("business-analyst", [], False), ("canvas-writer", [0], False)])
    runtime = _preflight_runtime(_unbound_ctx(clar_store=None))
    res = await _plan_context_preflight(_preflight_sctx(), runtime, plan_store=store, plan_id=plan.id)
    assert res is not None and res.status == TurnStatus.CLARIFICATION_PENDING
    bus = runtime.parent_bus
    assert bus.find_all("clarification_required") == []
    assert "计划卡" in res.final_answer


@pytest.mark.asyncio
async def test_preflight_excludes_needs_web_compute_steps(db_path: str) -> None:
    """review P1(WP-2×WP-7): 纯联网 compute 步不碰 datarc —— 预检不许拦它。
    plan 只有 needs_web compute + writer 时, unbound 也不触发预检。"""
    from bowhead_core.agents.lead_orchestrator import _plan_context_preflight

    store = PlanStore(db_path)
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="行业新闻")
    web_step = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="联网检索行业新闻", needs_web=True)
    writer = new_step(plan_id=plan.id, seq=1, agent_name="canvas-writer", objective="汇总", depends_on=(web_step.id,))
    levels = assign_levels([web_step, writer])
    web_step.level, writer.level = levels[web_step.id], levels[writer.id]
    await store.add_steps([web_step, writer])
    res = await _plan_context_preflight(_preflight_sctx(), _preflight_runtime(_unbound_ctx(_FakeClarStore())), plan_store=store, plan_id=plan.id)
    assert res is None  # No preflight: web needs no space.
    for s in await store.list_steps(plan.id):
        assert str(s.status) == str(StepStatus.PENDING)


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [["teammcp.ibt_powerbi.pbi_execute_dax"], ["bi_schema", "teammcp.ibt_powerbi.pbi_execute_dax"]])
async def test_datarc_preflight_preserves_power_bi_steps(db_path: str, tools: list[str]) -> None:
    """Power BI queries must not wait for an unrelated Datarc workspace."""
    from bowhead_core.agents.lead_orchestrator import _plan_context_preflight

    class _Registry:
        def get(self, name: str, workspace_id: str | None = None) -> Any:
            if name == "lead":
                return SimpleNamespace(callable_peers=[])
            raise KeyError(name)

    class _TeamStore:
        async def list_team_subagents(self, team_id: str, only_enabled: bool = True) -> list[Any]:
            return [
                SimpleNamespace(
                    id="osa_powerbi",
                    name="powerbi-analyst",
                    display_name="Power BI Analyst",
                    description="Queries the team's Power BI model",
                    tool_refs=tools,
                    visibility="team_wide",
                    created_by="u",
                    system_prompt="",
                ),
            ]

    store = PlanStore(db_path)
    await store.initialize()
    plan, _ = await _setup(store, [("powerbi-analyst", [], False)])
    clar = _FakeClarStore()
    ctx = _unbound_ctx(clar)
    ctx.team_id = "team_1"
    runtime = SimpleNamespace(
        parent_bus=_FakeBus(),
        parent_ctx=ctx,
        multi_agent_registry=_Registry(),
        team_subagent_store=_TeamStore(),
    )

    result = await _plan_context_preflight(
        _preflight_sctx("t_powerbi_preflight"),
        runtime,
        plan_store=store,
        plan_id=plan.id,
    )

    assert result is None
    step = (await store.list_steps(plan.id))[0]
    assert str(step.status) == str(StepStatus.PENDING)
    assert clar.resume_ctx == {}


@pytest.mark.asyncio
async def test_needs_web_persists_roundtrip(db_path: str) -> None:
    """needs_web 列持久化(承重: 预检判据)——insert→list 往返 + 旧库幂等迁移。"""
    store = PlanStore(db_path)
    await store.initialize()
    await store.initialize()  # Idempotent.
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="t")
    st = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="联网", needs_web=True)
    await store.add_steps([st])
    got = (await store.list_steps(plan.id))[0]
    assert got.needs_web is True


@pytest.mark.asyncio
async def test_preflight_production_ctx_shape_no_nameerror(db_path: str) -> None:
    """graph-mode 批2 review P2-12 回归: 生产 ToolExecutionContext 形状(有
    bi_availability 方法)命中 needs_selection 时, picker 选项来自 _accessible——
    此前它只在鸭子 ctx 的 else 分支赋值 → 生产分支 NameError(BI 步已 BLOCKED
    之后), picker 永远建不出来。测试全用 SimpleNamespace 所以全绿假象。"""
    from bowhead_core.agents.lead_orchestrator import _plan_context_preflight
    from bowhead_core.types import TurnStatus

    store = PlanStore(db_path)
    await store.initialize()
    plan, _ = await _setup(store, [("financial-analyst", [], False)])
    clar = _FakeClarStore()
    ctx = SimpleNamespace(
        bi_availability=lambda: "needs_selection",  # Production criterion path.
        resolve_datarc_project=lambda: "",
        accessible_datarc_workspaces=({"id": "1", "name": "ws-a"}, {"id": "2", "name": "ws-b"}),
        clarification_store=clar,
    )
    runtime = _preflight_runtime(ctx)
    res = await _plan_context_preflight(_preflight_sctx("t_pf_prod"), runtime, plan_store=store, plan_id=plan.id)
    assert res is not None and res.status == TurnStatus.CLARIFICATION_PENDING
    # Picker options actually built (key NameError regression assertion).
    assert clar.puts and len(clar.puts[0]["candidates"]) == 2
    assert runtime.parent_bus.find_all("clarification_required")


# ── Readable bail reason must cross adapter seam (116 dogfood 2026-08-14) ──
# Testing only `_bail_message` / `PlanStepBailError` misses seam: remove `message=` from both raise
# sites and those unit tests stay green. Actual user sees `Failed: transient: transient`,
# though envelope already held readable reason (required `error.message` contract), lost at this hop.


@pytest.mark.asyncio
async def test_bail_envelope_message_reaches_the_exception() -> None:
    """结构化 error 信封这条路。突变：摘掉 `message=_bail_message(_env_err)` → 红。"""
    fn, manifest = _runner_fn_with(_sub(payload={"title": "x", "error": {"kind": "transient", "message": "BI 查询下载超时 60s，数据源暂时不可用"}}))
    with pytest.raises(PlanStepBailError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert ei.value.error_kind == "transient", "kind 仍是干净枚举值（下游按它分支）"
    assert str(ei.value) == "BI 查询下载超时 60s，数据源暂时不可用", f"人话没穿过来: {ei.value!s}"


@pytest.mark.asyncio
async def test_hard_bail_message_reaches_the_exception() -> None:
    """π-B 硬 bail 那条路（信封是工具返回 dict，人话写在 reason 里）。"""
    bail_inv = SimpleNamespace(result={"ok": True, "__terminal": True, "bail_kind": "tool_failure", "reason": "bi_query 连续 5 次失败"})
    fn, manifest = _runner_fn_with(_sub(payload={"narrative": "x", "fallback_markdown": "x"}, invocations=[bail_inv]))
    with pytest.raises(PlanStepBailError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert ei.value.error_kind == "tool_failure"
    assert str(ei.value) == "bi_query 连续 5 次失败"


@pytest.mark.asyncio
async def test_bail_without_message_keeps_old_behaviour() -> None:
    """负对照：信封没给人话时回落 kind，不许编 —— 既有两条老用例的形状不变。"""
    fn, manifest = _runner_fn_with(_sub(payload={"title": "x", "error": {"kind": "domain_mismatch"}}))
    with pytest.raises(PlanStepBailError) as ei:
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert str(ei.value) == "domain_mismatch"
