"""批2 S1 round 内核测试 (batch2-hitl-design §1-D4/§2-S1).

等价性由既有 test_plan_driver.py 零改动通过背书;本文件测 round 化新增语义:
hold(同 run 不重派)/un-block 级联/终止性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents.task_spec import TaskFailureError, TaskResult
from bowhead_core.plan_driver import PlanCaps, PlanDeps, PlanHooks, run_plan
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStatus, PlanStore, StepStatus, new_step

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeSctx:
    user_id: str = "u"
    workspace_id: str = "w"
    turn_id: str = "t_run"
    session_id: str = "s"


class CountingRunner:
    """按 agent 记派发次数; behavior: {agent: ("ok", payload)|("fail", reason)}."""

    def __init__(self, behavior: dict[str, tuple[str, Any]] | None = None) -> None:
        self.behavior = behavior or {}
        self.dispatch_count: dict[str, int] = {}

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        out = []
        for t in tasks:
            self.dispatch_count[t.agent_name] = self.dispatch_count.get(t.agent_name, 0) + 1
            beh = self.behavior.get(t.agent_name, ("ok", {"exec_summary": f"{t.agent_name} ok"}))
            if beh[0] == "ok":
                out.append(TaskResult.succeeded(t, beh[1], tokens_input=10, tokens_output=20))
            else:
                out.append(TaskResult.failed(t, TaskFailureError(beh[1])))
        return out


def _deps(runner) -> PlanDeps:
    return PlanDeps(
        sctx=FakeSctx(),
        runtime_factory=lambda c: c,
        agent_runner_fn=None,
        manifest_lookup=lambda name: object(),
        runner=runner,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


async def _store(db_path: str) -> PlanStore:
    s = PlanStore(db_path)
    await s.initialize()
    return s


async def _setup(store: PlanStore, specs: list[tuple[str, list[int]]]):
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}") for i, (a, _d) in enumerate(specs)]
    for st, (_a, dep_idx) in zip(steps, specs, strict=True):
        st.depends_on = tuple(steps[d].id for d in dep_idx)
    levels = assign_levels(steps)
    for st in steps:
        st.level = levels[st.id]
    await store.add_steps(steps)
    return plan, steps


@pytest.mark.asyncio
async def test_needs_user_input_blocked_dispatched_once_no_spin(db_path: str) -> None:
    """调度 review P0-1 回归: blocked(needs_user_input) 步永不答 → 只派一次、
    不空转、收敛 blocked_needs_user(与现状一致), 其余腿照常跑完."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", []), ("kb-analyst", [])])
    runner = CountingRunner({"compute": ("fail", "needs_user_input")})
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user"
    assert runner.dispatch_count["compute"] == 1  # hold: no same-run redispatch.
    assert runner.dispatch_count["kb-analyst"] == 1
    by_agent = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert by_agent["compute"].status == StepStatus.BLOCKED
    assert by_agent["compute"].error_kind == "needs_user_input"
    assert by_agent["kb-analyst"].status == StepStatus.DONE


@pytest.mark.asyncio
async def test_failed_step_dispatched_once_same_run(db_path: str) -> None:
    """FAILED hold: 同 run 不自动重试(重试语义=re-approve 下次 run_plan)."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", [])])
    runner = CountingRunner({"compute": ("fail", "boom")})
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user" and out.failed == 1
    assert runner.dispatch_count["compute"] == 1
    # Next run_plan (re-approve): clear held → eligible again → redispatch.
    runner2 = CountingRunner()
    out2 = await run_plan(plan.id, store=store, deps=_deps(runner2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed"
    assert runner2.dispatch_count["compute"] == 1


@pytest.mark.asyncio
async def test_criteria_unmet_not_retried_same_run(db_path: str) -> None:
    """enforce 的 criteria_unmet FAILED 同 run 不重派(A5 降级只跨 run)."""
    from bowhead_core.plan_verify import ITEM_UNMET, VERDICT_FAIL, VerifyItem, VerifyOutcome

    store = await _store(db_path)
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="DD")
    st = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="o", success_criteria=("含数字",))
    st.level = 0
    await store.add_steps([st])

    async def verify(step, payload):
        return VerifyOutcome(VERDICT_FAIL, items=(VerifyItem("含数字", ITEM_UNMET),))

    runner = CountingRunner()
    hooks = PlanHooks(verify=verify)
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user" and out.failed == 1
    assert runner.dispatch_count["compute"] == 1


@pytest.mark.asyncio
async def test_unblock_cascade_on_upstream_recovery(db_path: str) -> None:
    """un-block 级联: 上游重试成功 → 下游 BLOCKED(upstream_failed) 被解 →
    同 run 内继续跑完。hold 语义下没有级联则下游永滞 BLOCKED(设计 §1-D4);
    级联的真价值在 S4 revive(同 run 上游复活)场景, 此处用跨 run 形状先钉住机制."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("kb-analyst", [0]), ("financial-analyst", [1])])
    # run 1: compute fails → kb BLOCKED(upstream_failed) → financial BLOCKED(upstream_failed).
    r1 = CountingRunner({"compute": ("fail", "boom")})
    out1 = await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out1.status == "blocked_needs_user"
    by = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert by["kb-analyst"].error_kind == "upstream_failed"
    assert by["financial-analyst"].error_kind == "upstream_failed"
    # run 2 (re-approve): compute succeeds → cascade unblocks kb → kb succeeds → unblocks financial → all done.
    r2 = CountingRunner()
    out2 = await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed"
    final = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert all(s.status == StepStatus.DONE for s in final.values())
    assert all(s.error is None and s.error_kind is None for s in final.values())
    p = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert p.status == PlanStatus.COMPLETED


class _TokenRunner:
    """第一派 needs_user_input 带真实 token 消耗;第二派成功。模拟 revive 双派."""

    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        self.calls += 1
        out = []
        for t in tasks:
            if self.calls == 1:
                r = TaskResult(task_spec=t, ok=False, error=TaskFailureError("needs_user_input"), tokens_input=7, tokens_output=3)
            else:
                r = TaskResult.succeeded(t, {"exec_summary": "ok"}, tokens_input=10, tokens_output=20)
            out.append(r)
        return out


@pytest.mark.asyncio
async def test_same_turn_double_dispatch_tokens_accumulate(db_path: str) -> None:
    """V6: 同 turn 双派(revive 形状)token 累加——BLOCKED 那次烧的钱不丢账."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", [])])
    runner = _TokenRunner()
    # run1: needs_user_input, spent 7/3.
    await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    got1 = (await store.list_steps(plan.id))[0]
    assert (got1.tokens_input, got1.tokens_output) == (7, 3)
    # run2 same turn_id (revive/same-turn retry): accumulate 7+10 / 3+20.
    await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    got2 = (await store.list_steps(plan.id))[0]
    assert got2.status == StepStatus.DONE
    assert (got2.tokens_input, got2.tokens_output) == (17, 23)


@pytest.mark.asyncio
async def test_cross_turn_retry_tokens_overwrite(db_path: str) -> None:
    """V6 反面: 跨 turn 重试覆盖——turn 级计费按 turn_id 分桶, 累加会双记."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", [])])
    runner = _TokenRunner()
    await run_plan(plan.id, store=store, deps=_deps(runner), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    # New-turn retry (actual re-approve shape).
    deps2 = PlanDeps(
        sctx=FakeSctx(turn_id="t_run2"),
        runtime_factory=lambda c: c,
        agent_runner_fn=None,
        manifest_lookup=lambda name: object(),
        runner=runner,
    )
    await run_plan(plan.id, store=store, deps=deps2, hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    got = (await store.list_steps(plan.id))[0]
    assert got.status == StepStatus.DONE
    assert (got.tokens_input, got.tokens_output) == (10, 20)  # This turn only.
    assert got.turn_id == "t_run2"


class _MidrunRunner:
    """compute: 第一派 needs_user_input,复活后第二派成功——mid-run 拾取全周期."""

    def __init__(self, always_block: bool = False) -> None:
        self.always_block = always_block
        self.dispatch_count: dict[str, int] = {}

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        out = []
        for t in tasks:
            n = self.dispatch_count.get(t.agent_name, 0) + 1
            self.dispatch_count[t.agent_name] = n
            if t.agent_name == "compute" and (self.always_block or n == 1):
                out.append(TaskResult(task_spec=t, ok=False, error=TaskFailureError("needs_user_input", detail='{"kind":"tool_context_select"}')))
            else:
                out.append(TaskResult.succeeded(t, {"exec_summary": f"{t.agent_name} ok"}, tokens_input=10, tokens_output=20))
        return out


@pytest.mark.asyncio
async def test_on_needs_user_hook_receives_step_and_detail(db_path: str) -> None:
    """S2: needs_user_input 写回后立即调 on_needs_user(步+detail);hook 抛错不炸盘."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", [])])
    calls: list[tuple[str, str]] = []

    async def on_needs_user(step, detail):
        calls.append((step.agent_name, detail))
        raise RuntimeError("closure bug")  # Defense in depth: must not crash writeback.

    hooks = PlanHooks(on_needs_user=on_needs_user)
    out = await run_plan(plan.id, store=store, deps=_deps(_MidrunRunner(always_block=True)), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user"
    assert calls == [("compute", '{"kind":"tool_context_select"}')]


@pytest.mark.asyncio
async def test_revive_midrun_full_cycle_single_run(db_path: str) -> None:
    """S4 旗舰: blocked → 用户答(revive 判可复活) → 同 run 重派 → done → completed."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", []), ("kb-analyst", [0])])
    runner = _MidrunRunner()
    revive_calls: list[int] = []

    async def revive_check(candidates):
        revive_calls.append(len(candidates))
        return [c.id for c in candidates]  # Simulate persisted binding.

    hooks = PlanHooks(revive_check=revive_check)
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    assert runner.dispatch_count["compute"] == 2  # Block once + revived redispatch.
    assert runner.dispatch_count["kb-analyst"] == 1  # Downstream runs after revived upstream done.
    final = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert all(s.status == StepStatus.DONE for s in final.values())
    assert all(s.error is None and s.error_kind is None for s in final.values())


@pytest.mark.asyncio
async def test_revive_guard_stops_reblock_loop(db_path: str) -> None:
    """S4 保险: 复活后又 block(ctx 回填失效等) → 每步每 run 只复活一次, 不烧循环."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", [])])
    runner = _MidrunRunner(always_block=True)

    async def revive_check(candidates):
        return [c.id for c in candidates]

    hooks = PlanHooks(revive_check=revive_check)
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "blocked_needs_user"
    assert runner.dispatch_count["compute"] == 2  # Original + revived dispatch, stop there.


@pytest.mark.asyncio
async def test_late_revive_unblocks_gated_downstream_cascade(db_path: str) -> None:
    """实现 review P3-e 转正: 下游先被 gate 成 BLOCKED(upstream_failed)、上游才
    复活 → DONE 写回级联解锁下游 → 同 run 全链跑完(级联的真实战场)."""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", []), ("kb-analyst", [0])])
    runner = _MidrunRunner()  # compute blocks first, succeeds second.
    revive_calls = {"n": 0}

    async def revive_check(candidates):
        revive_calls["n"] += 1
        if revive_calls["n"] < 2:  # First deny (downstream not yet gated) →
            return []  # upstream_failed gate; second allow → DONE cascade unlock.
        return [c.id for c in candidates]

    hooks = PlanHooks(revive_check=revive_check)
    out = await run_plan(plan.id, store=store, deps=_deps(runner), hooks=hooks, caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    final = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert all(s.status == StepStatus.DONE for s in final.values())
    assert all(s.error is None and s.error_kind is None for s in final.values())
    assert runner.dispatch_count == {"compute": 2, "kb-analyst": 1}


# ─────────────────────────────────────────────────────────────
# Batch 1-c (plan-report-assembly): on_step_composed binding hook.
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_step_composed_called_per_done_step_and_backfills_section_ids(db_path: str) -> None:
    """装订钩子每个 DONE 步调一次，返回值回填进 plan_steps.output_section_ids。

    这两件事今天都不成立：`output_section_ids` 读的是 `payload["section_ids"]`
    ——subagent payload 从无此键 ⇒ 该字段恒为 `[]`（discovery C0）。
    """
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("kb-analyst", [0])])
    seen: list[tuple[str, int]] = []

    async def _hook(step, all_steps):
        seen.append((step.id, len(all_steps)))
        return (f"sec_for_{step.seq}",)

    out = await run_plan(
        plan.id, store=store, deps=_deps(CountingRunner()), hooks=PlanHooks(on_step_composed=_hook), caps=PlanCaps(), parent_token_budget=1000
    )
    assert out.status == "completed"
    assert [sid for sid, _ in seen] == [steps[0].id, steps[1].id], seen
    assert all(n == 2 for _, n in seen), "钩子必须拿到全部步（排序要跨步才算得出来）"
    rows = {s.seq: s for s in await store.list_steps(plan.id)}
    assert tuple(rows[0].output_section_ids) == ("sec_for_0",)
    assert tuple(rows[1].output_section_ids) == ("sec_for_1",)


@pytest.mark.asyncio
async def test_no_hook_leaves_output_section_ids_untouched(db_path: str) -> None:
    """正对照：没有钩子时（普通轮 / 老 plan）行为不变，字段保持空。"""
    store = await _store(db_path)
    plan, _ = await _setup(store, [("compute", [])])
    out = await run_plan(plan.id, store=store, deps=_deps(CountingRunner()), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out.status == "completed"
    assert tuple((await store.list_steps(plan.id))[0].output_section_ids) == ()
