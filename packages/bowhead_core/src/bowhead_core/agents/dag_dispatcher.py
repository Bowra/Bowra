"""DAG dispatcher (sprint multi-agent-v2 PR-#7).

把 TaskSpec.depends_on 解析成 DAG, 按 topological order 分 level 调 TaskRunner.dispatch.
同 level parallel; 跨 level 串行 (level N 全 done 再 level N+1).

Filter strategy: level N+1 task.upstream_payloads = level N 中 self.depends_on 列出的
ok 结果 (T-CTO-V2-P1.1 fix). 其他 level N 结果不喂.

Fail-soft: level N 中部分 fail, 但 ≥ 1 task ok → 进 level N+1 (T-BE-V2-P1.2);
全 fail → stop dispatch, return remaining N+1 tasks as failed (UpstreamFailed).
"""

from __future__ import annotations

import logging
from dataclasses import replace as _replace
from typing import TYPE_CHECKING

from .task_spec import TaskFailureError, TaskResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from .manifest import AgentManifest
    from .task_runner import AgentRunnerFn, RuntimeFactory, TaskRunner
    from .task_spec import TaskSpec
    from .turn_context import SerializableTurnContext

logger = logging.getLogger(__name__)


class DAGCycleError(Exception):
    """Topological sort detected a cycle."""


class DAGUnknownDependencyError(Exception):
    """Task.depends_on references task_id not in batch."""


def topological_sort(tasks: list[TaskSpec]) -> list[list[TaskSpec]]:
    """Returns list of "levels" — each level is independent (no cross-level deps).

    Within a level, tasks have no dependency on each other (can dispatch parallel).
    Across levels, level N tasks depend on level < N.

    Raises DAGCycleError on cycle, DAGUnknownDependencyError on unknown task_id ref.
    """
    if not tasks:
        return []

    by_id = {t.task_id: t for t in tasks}

    # Validate all depends_on references exist
    for t in tasks:
        for dep in t.depends_on:
            if dep not in by_id:
                raise DAGUnknownDependencyError(f"task {t.task_id!r} depends on unknown {dep!r}")

    # Kahn's algorithm
    in_deg: dict[str, int] = {t.task_id: len(t.depends_on) for t in tasks}
    levels: list[list[TaskSpec]] = []
    remaining = set(by_id)

    while remaining:
        # All tasks with in_deg 0 in remaining = current level (preserve input order)
        level = [t for t in tasks if t.task_id in remaining and in_deg[t.task_id] == 0]
        if not level:
            # Cycle: remaining non-empty but no zero in-degree
            raise DAGCycleError(f"DAG cycle detected, remaining={sorted(remaining)}")
        levels.append(level)
        for t in level:
            remaining.discard(t.task_id)
        # Decrement in_deg for tasks depending on level
        level_ids = {t.task_id for t in level}
        for tid in remaining:
            for dep in by_id[tid].depends_on:
                if dep in level_ids:
                    in_deg[tid] -= 1
    return levels


def filter_upstream_for(
    task: TaskSpec,
    prev_results: list[TaskResult],
) -> tuple[dict, ...]:
    """Pull only the ok results matching task.depends_on (T-CTO-V2-P1.1).

    Public (promoted from `_filter_upstream_for`) so plan_driver reuses the same
    upstream-filtering logic instead of forking it (m1-execution-tech-design v2 D-D1).
    """
    by_id = {r.task_spec.task_id: r for r in prev_results}
    out = []
    for dep_id in task.depends_on:
        r = by_id.get(dep_id)
        if r and r.ok and r.payload:
            out.append(
                {
                    "agent": r.agent_name,
                    "task_id": dep_id,
                    "payload": r.payload,
                }
            )
        elif r is not None:
            # planning-mode-audit WP-3: no more silent drops. P1-a already marks done-but-empty
            # as FAILED; most results reaching here are ok=False upstreams. Log them to diagnose downstream
            # missing-upstream problems (previously no logs or events).
            logger.debug(
                "filter_upstream_for: dep %s dropped for task %s (ok=%s payload_empty=%s)",
                dep_id,
                task.task_id,
                getattr(r, "ok", None),
                not getattr(r, "payload", None),
            )
    return tuple(out)


# Backward-compat alias (internal callers below + any external ref).
_filter_upstream_for = filter_upstream_for


async def dispatch_dag(
    tasks: list[TaskSpec],
    sctx: SerializableTurnContext,
    runtime_factory: RuntimeFactory,
    agent_runner_fn: AgentRunnerFn,
    manifest_lookup: Callable[[str], AgentManifest],
    *,
    runner: TaskRunner,
) -> list[TaskResult]:
    """Dispatch tasks honoring depends_on DAG.

    Returns: results in same input order (any task that can't run because
    upstream level fully failed is returned as TaskResult.failed("UpstreamFailed")).
    """
    if not tasks:
        return []

    levels = topological_sort(tasks)

    all_results: list[TaskResult] = []
    upstream_dead = False

    for level_idx, level_tasks in enumerate(levels):
        # If the upstream level is entirely dead, mark the current level UpstreamFailed.
        if upstream_dead:
            for t in level_tasks:
                all_results.append(
                    TaskResult.failed(
                        t,
                        TaskFailureError(
                            "UpstreamFailed",
                            Exception("DAG level had all-dead upstream; not dispatched"),
                        ),
                    )
                )
            continue

        # Build effective level_tasks with filtered upstream_payloads
        effective: list[TaskSpec] = []
        for t in level_tasks:
            if not t.depends_on:
                effective.append(t)
            else:
                upstream_filtered = _filter_upstream_for(t, all_results)
                effective.append(
                    _replace(
                        t,
                        upstream_payloads=(
                            *t.upstream_payloads,
                            *upstream_filtered,
                        ),
                    )
                )

        logger.info(
            "dag.dispatch level=%d tasks=%s",
            level_idx,
            [t.agent_name for t in effective],
        )
        level_results = await runner.dispatch(
            effective,
            sctx,
            runtime_factory,
            agent_runner_fn,
            manifest_lookup,
        )
        all_results.extend(level_results)

        # All fail → do not dispatch later levels.
        if level_results and not any(r.ok for r in level_results):
            logger.warning(
                "dag.dispatch level=%d all failed, stop subsequent levels",
                level_idx,
            )
            upstream_dead = True

    # Re-order to match input order
    by_id = {r.task_spec.task_id: r for r in all_results}
    return [by_id[t.task_id] for t in tasks]
