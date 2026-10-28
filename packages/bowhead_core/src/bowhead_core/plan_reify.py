"""Plan reify — PlanStep ⇄ TaskSpec + level 分配 + agent allowlist (M1 block 2).

见 m1-execution-tech-design.md v2 §2. 纯函数, 无 I/O.

- `step_to_task`: PlanStep → 可派发的 TaskSpec (task_id = step.id).
- `assign_levels`: 用 `topological_sort` 一次性算每步 level (reify 时定死, M1 不重算 —
  D5 修订推 M2 → 无加步 → 无 level 重算, 规避 CTO P0-2 的算法坑).
  cycle / 未知依赖 → 抛 DAGCycleError / DAGUnknownDependencyError, 由审批前 caller
  捕获 reject 计划 (R2).
- `validate_agents`: 用 manifest_lookup(name) catch KeyError 判合法 —— **不用
  `registry.__contains__`** (CTO P1-5: `in` 漏 workspace user manifest).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agents.dag_dispatcher import topological_sort
from .agents.task_spec import TaskSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from .plan_store import PlanStep


def step_to_task(step: PlanStep) -> TaskSpec:
    """PlanStep → TaskSpec. task_id 复用 step.id (depends_on 引用的就是 step id)."""
    return TaskSpec(
        agent_name=step.agent_name,
        user_question=step.objective,
        task_id=step.id,
        depends_on=tuple(step.depends_on),
        # Dispatch criteria with task: this step's output contract, not merely a retrospective scorecard.
        success_criteria=tuple(getattr(step, "success_criteria", ()) or ()),
        # Batch 3-a: likewise dispatch chapter title; leg delivers a chapter within document, not a separate report.
        # Four callers (assign_levels / plan_driver at 175, 605, 801) share this assembly,
        # so wiring into step_to_task suffices; no caller-by-caller changes.
        deliverable=str(getattr(step, "deliverable", "") or ""),
        agent_choice=getattr(step, "agent_choice", None),
    )


def assign_levels(steps: list[PlanStep]) -> dict[str, int]:
    """{step_id: level} —— topological 深度. reify 时调一次, 持久化进 plan_steps.level.

    Raises:
        DAGCycleError: depends_on 成环.
        DAGUnknownDependencyError: depends_on 引用了不存在的 step id.
    """
    levels = topological_sort([step_to_task(s) for s in steps])
    return {t.task_id: idx for idx, level in enumerate(levels) for t in level}


def validate_agents(
    steps: list[PlanStep],
    manifest_lookup: Callable[[str], object],
) -> list[str]:
    """返回 agent_name 不在 registry 的 step_id 列表 (审批前 reject, R1/CTO P1-5).

    `manifest_lookup` = workspace-bound `registry.get` (单参, 抛 KeyError on 未知).
    """
    bad: list[str] = []
    for s in steps:
        try:
            manifest_lookup(s.agent_name)
        except KeyError:
            bad.append(s.id)
    return bad
