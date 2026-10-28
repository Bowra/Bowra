"""plan_reify unit tests (Planning Mode M1 block 2)."""

from __future__ import annotations

import pytest
from bowhead_core.agents.dag_dispatcher import (
    DAGCycleError,
    DAGUnknownDependencyError,
)
from bowhead_core.plan_reify import assign_levels, step_to_task, validate_agents
from bowhead_core.plan_store import new_step

# Closing review (2026-08-25): pure functions, no I/O, yet **no marker**,
# so `-m unit` (CLAUDE.md and this sprint's gate) never collected it,
# including batch 3-a step_to_task passthrough. Tests outside gate protect nothing.
pytestmark = pytest.mark.unit


def _step(agent: str, *, deps: tuple[str, ...] = (), seq: int = 0):
    return new_step(plan_id="p1", seq=seq, agent_name=agent, objective=f"obj-{agent}", depends_on=deps)


def test_step_to_task_maps_fields() -> None:
    s = new_step(plan_id="p1", seq=1, agent_name="kb-analyst", objective="法务", depends_on=("ps_x",))
    t = step_to_task(s)
    assert t.task_id == s.id
    assert t.agent_name == "kb-analyst"
    assert t.user_question == "法务"
    assert t.depends_on == ("ps_x",)


def test_assign_levels_linear() -> None:
    a = _step("a")
    b = _step("b", deps=(a.id,))
    c = _step("c", deps=(b.id,))
    levels = assign_levels([a, b, c])
    assert levels[a.id] == 0
    assert levels[b.id] == 1
    assert levels[c.id] == 2


def test_assign_levels_parallel_then_join() -> None:
    # diamond: a → {b, c} → d  (DD shape: legal+audit parallel → summary)
    a = _step("a")
    b = _step("b", deps=(a.id,))
    c = _step("c", deps=(a.id,))
    d = _step("d", deps=(b.id, c.id))
    levels = assign_levels([a, b, c, d])
    assert levels[a.id] == 0
    assert levels[b.id] == 1 and levels[c.id] == 1
    assert levels[d.id] == 2


def test_assign_levels_all_independent() -> None:
    a, b, c = _step("a"), _step("b"), _step("c")
    levels = assign_levels([a, b, c])
    assert levels[a.id] == levels[b.id] == levels[c.id] == 0


def test_assign_levels_cycle_raises() -> None:
    a = _step("a")
    b = _step("b", deps=(a.id,))
    a.depends_on = (b.id,)  # a↔b cycle
    with pytest.raises(DAGCycleError):
        assign_levels([a, b])


def test_assign_levels_unknown_dep_raises() -> None:
    a = _step("a", deps=("ps_does_not_exist",))
    with pytest.raises(DAGUnknownDependencyError):
        assign_levels([a])


def test_validate_agents_uses_lookup_not_contains() -> None:
    # lookup honours a workspace user manifest ("custom-x") that `in registry`
    # (system-only) would miss — CTO P1-5.
    known = {"kb-analyst", "compute", "custom-x"}

    def lookup(name: str) -> object:
        if name in known:
            return object()
        raise KeyError(name)

    steps = [_step("kb-analyst"), _step("custom-x"), _step("bogus-agent")]
    bad = validate_agents(steps, lookup)
    assert bad == [steps[2].id]  # only the genuinely-unknown one


def test_step_to_task_carries_success_criteria() -> None:
    """判据是这一步的**产出契约**，不只是事后评分表——派活时必须一起下发。

    2026-08-18 116 dogfood：这条缝是「Top N 判据大面积不达标」的根因。
    """
    from bowhead_core.plan_store import PlanStep

    step = PlanStep(
        id="ps_x",
        plan_id="plan_x",
        seq=0,
        agent_name="compute",
        objective="按门店查营业额 TOP10",
        depends_on=(),
        success_criteria=("含营业额最高的 3 个门店名称及其营业额数值",),
    )
    task = step_to_task(step)
    assert task.success_criteria == ("含营业额最高的 3 个门店名称及其营业额数值",)
    # Negative control: criteria-free step cannot invent content.
    bare = step_to_task(PlanStep(id="ps_y", plan_id="plan_x", seq=1, agent_name="compute", objective="o", depends_on=()))
    assert bare.success_criteria == ()


def test_step_to_task_carries_deliverable_as_chapter_name() -> None:
    """★同一条缝的第三例：`deliverable` 是这一步的**章名**，干活的 agent 从没见过。

    plan-report-assembly 批 3-a。批 0/1/2 把装订修好了——8 份报告排成 1 份 8 章——
    但腿自己仍以为在写一份完整报告：它拿到的只有 objective + success_criteria。
    `plan_steps.deliverable` planner 填了、落了库、渲染在计划卡上，就是没下发给执行者。
    与 `success_criteria`（task_spec.py:57 已记档的第一例）同一个修法。

    ⚠ 决议 #4：**只给章名，不给位次**。腿看不到前文，给「第 3/8 章」会诱导它写
    「如前文所述」去指一段它根本没读过的内容。
    """
    from bowhead_core.plan_store import PlanStep

    step = PlanStep(
        id="ps_x",
        plan_id="plan_x",
        seq=3,
        agent_name="compute",
        objective="将我方经营指标与行业基准逐项对标",
        depends_on=(),
        deliverable="交叉验证对标结论",
    )
    task = step_to_task(step)
    assert task.deliverable == "交叉验证对标结论"
    # Negative control: planner omits chapter title ⇒ step cannot invent one.
    bare = step_to_task(PlanStep(id="ps_y", plan_id="plan_x", seq=1, agent_name="compute", objective="o", depends_on=()))
    assert bare.deliverable == ""
