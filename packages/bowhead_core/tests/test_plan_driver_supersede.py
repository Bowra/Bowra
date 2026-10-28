"""批 3 driver 别名内核测试 (batch3-replan-design §1 D-A).

修订别名: 下游 depends_on 指旧 id 不改写——旧行沿 superseded_by 链解析活尾步,
尾步 DONE 后以旧 id 注入镜像 result(agent 归属=尾步, review R1-F8), children
沿链聚合(un-block 级联)。链环/超限 fail-open(R1-F9)。双列表纪律(R1-F6):
superseded 不计 max_steps/rounds。UT 穿真 run_plan(mock runner), 禁 fixture 捷径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents.task_spec import TaskFailureError, TaskResult
from bowhead_core.plan_driver import PlanCaps, PlanDeps, PlanHooks, run_plan
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStore, StepOrigin, StepStatus, new_step

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeSctx:
    user_id: str = "u"
    workspace_id: str = "w"
    turn_id: str = "t_run"
    session_id: str = "s"


class RecordingRunner:
    """记录每次 dispatch 的 task(含 upstream_payloads); behavior 同 CountingRunner."""

    def __init__(self, behavior: dict[str, tuple[str, Any]] | None = None) -> None:
        self.behavior = behavior or {}
        self.dispatched: list[Any] = []

    async def dispatch(self, tasks, sctx, rf, arf, ml):
        out = []
        for t in tasks:
            self.dispatched.append(t)
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


async def _store(db_path: str) -> PlanStore:
    s = PlanStore(db_path)
    await s.initialize()
    return s


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "plan.db")


async def _setup(store: PlanStore, specs: list[tuple[str, list[int]]]):
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="B3")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}") for i, (a, _d) in enumerate(specs)]
    for st, (_a, dep_idx) in zip(steps, specs, strict=True):
        st.depends_on = tuple(steps[d].id for d in dep_idx)
    levels = assign_levels(steps)
    for st in steps:
        st.level = levels[st.id]
    await store.add_steps(steps)
    return plan, steps


async def _supersede(store: PlanStore, plan_id: str, old_id: str, *, level: int, agent: str = "agent-rev", reset: list[str] | None = None):
    """走生产 CAS 路(supersede_step), 不手捏行状态."""
    await store.update_plan_status(plan_id, "running")
    await store.update_plan_status(plan_id, "blocked")
    plan = await store.get_plan(plan_id, user_id="u", workspace_id="w")
    rev_step = new_step(plan_id=plan_id, seq=90 + level, agent_name=agent, objective="修订", level=level, origin=StepOrigin.REVISION)
    assert await store.supersede_step(
        plan_id=plan_id, old_step_id=old_id, new_step=rev_step, downstream_reset_ids=reset or [], expected_rev=plan.current_rev
    )
    return rev_step


@pytest.mark.asyncio
async def test_alias_feeds_downstream_with_tail_payload_and_agent(db_path: str) -> None:
    """铁断言: 旧 id 依赖在修订步 DONE 后解析出新 payload, agent 归属=修订步."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    # First run: compute fails → downstream blocked(upstream_failed).
    r1 = RecordingRunner({"compute": ("fail", "boom")})
    out1 = await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out1.status == "blocked_needs_user"
    # Revise compute (change agent), reset downstream.
    rev = await _supersede(store, plan.id, steps[0].id, level=0, agent="kb-analyst", reset=[steps[1].id])
    # Second run: revision succeeds → downstream must receive revision payload with agent=kb-analyst.
    r2 = RecordingRunner({"kb-analyst": ("ok", {"exec_summary": "修订产出", "kpis": [1]})})
    out2 = await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed"
    down_task = next(t for t in r2.dispatched if t.agent_name == "financial-analyst")
    ups = list(down_task.upstream_payloads)
    assert len(ups) == 1
    assert ups[0]["task_id"] == steps[0].id  # Old ID alias.
    assert ups[0]["payload"]["exec_summary"] == "修订产出"
    assert ups[0]["agent"] == "kb-analyst"  # R1-F8: owner = tail step.
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[steps[0].id].status == StepStatus.SUPERSEDED
    assert by_id[rev.id].status == StepStatus.DONE
    assert by_id[steps[1].id].status == StepStatus.DONE


@pytest.mark.asyncio
async def test_alias_unblock_cascade_within_single_run(db_path: str) -> None:
    """children 沿链聚合: 同 run 内修订步 DONE → 依赖旧 id 的下游经级联解 block 跑完.

    形状: down 在 round1 被 gate 成 blocked(upstream_failed, 修订步彼时未跑),
    修订步 DONE 写回时必须经 children[old→rev] 聚合找到 down 并解 block。
    """
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    r1 = RecordingRunner({"compute": ("fail", "boom")})
    await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    # Revise without downstream reset (down remains blocked/upstream_failed) → only cascade can unblock.
    rev = await _supersede(store, plan.id, steps[0].id, level=0, agent="kb-analyst", reset=[])
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[steps[1].id].status == StepStatus.BLOCKED
    assert by_id[steps[1].id].error_kind == "upstream_failed"
    r2 = RecordingRunner()
    out2 = await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed"
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[steps[1].id].status == StepStatus.DONE
    assert by_id[rev.id].status == StepStatus.DONE


@pytest.mark.asyncio
async def test_alias_two_hop_chain(db_path: str) -> None:
    """二代修订 old→rev1→rev2: 下游(依赖 old)吃到 rev2 的 payload."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    r1 = RecordingRunner({"compute": ("fail", "boom")})
    await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    rev1 = await _supersede(store, plan.id, steps[0].id, level=0, agent="kb-analyst")
    # rev1 also fails.
    r2 = RecordingRunner({"kb-analyst": ("fail", "boom2")})
    await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    rev2 = await _supersede(store, plan.id, rev1.id, level=0, agent="consultant")
    r3 = RecordingRunner({"consultant": ("ok", {"exec_summary": "二代产出"})})
    out3 = await run_plan(plan.id, store=store, deps=_deps(r3), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out3.status == "completed"
    down_task = next(t for t in r3.dispatched if t.agent_name == "financial-analyst")
    ups = list(down_task.upstream_payloads)
    assert ups and ups[0]["task_id"] == steps[0].id
    assert ups[0]["payload"]["exec_summary"] == "二代产出"
    assert ups[0]["agent"] == "consultant"
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[rev2.id].status == StepStatus.DONE


@pytest.mark.asyncio
async def test_supersession_cycle_fail_open_no_hang(db_path: str) -> None:
    """链环(手工弄脏数据): driver 不死循环, 按无镜像处理 → 下游保持 blocked."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    r1 = RecordingRunner({"compute": ("fail", "boom")})
    await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    # Dirty via SQL: self-cycle.
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE plan_steps SET status='superseded', superseded_by=? WHERE id=?",
            (steps[0].id, steps[0].id),
        )
        await db.commit()
    r2 = RecordingRunner()
    out2 = await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    # Converges (no hang); downstream blocked(upstream_failed): cycle fails open without mirror.
    assert out2.status == "blocked_needs_user"
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    assert by_id[steps[1].id].status == StepStatus.BLOCKED


@pytest.mark.asyncio
async def test_superseded_rows_not_counted_in_max_steps(db_path: str) -> None:
    """双列表纪律(R1-F6): superseded 死行不计 max_steps——带修订史的 plan 不被打成 FAILED."""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    r1 = RecordingRunner({"compute": ("fail", "boom")})
    await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    await _supersede(store, plan.id, steps[0].id, level=0, agent="kb-analyst", reset=[steps[1].id])
    # Live rows=3(rev+down+…)>2? Live: rev,down; superseded: compute. max_steps=2 exactly fits live rows.
    r2 = RecordingRunner({"kb-analyst": ("ok", {"exec_summary": "ok"})})
    out2 = await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(max_steps=2), parent_token_budget=1000)
    assert out2.status == "completed"  # Counting dead rows would give 3>2 → failed(max_steps_exceeded).


@pytest.mark.asyncio
async def test_mirror_result_not_double_billed(db_path: str) -> None:
    """设计 §3: 镜像 result 不重复计费(实现 review P2-4 缺席钉)——
    旧行 token 留证不变, 新步只记自己的账。"""
    store = await _store(db_path)
    plan, steps = await _setup(store, [("compute", []), ("financial-analyst", [0])])
    r1 = RecordingRunner({"compute": ("fail", "boom")})
    await run_plan(plan.id, store=store, deps=_deps(r1), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    old_tokens = (by_id[steps[0].id].tokens_input, by_id[steps[0].id].tokens_output)
    rev = await _supersede(store, plan.id, steps[0].id, level=0, agent="kb-analyst", reset=[steps[1].id])
    r2 = RecordingRunner({"kb-analyst": ("ok", {"exec_summary": "修订产出"})})
    out2 = await run_plan(plan.id, store=store, deps=_deps(r2), hooks=PlanHooks(), caps=PlanCaps(), parent_token_budget=1000)
    assert out2.status == "completed"
    by_id = {s.id: s for s in await store.list_steps(plan.id)}
    # Mirror never rewrites old-row accounting (retain evidence); new/downstream each counted once.
    assert (by_id[steps[0].id].tokens_input, by_id[steps[0].id].tokens_output) == old_tokens
    assert (by_id[rev.id].tokens_input, by_id[rev.id].tokens_output) == (10, 20)
    assert (by_id[steps[1].id].tokens_input, by_id[steps[1].id].tokens_output) == (10, 20)
