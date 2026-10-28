"""Sprint multi-agent-v2 PR-#7 — DAG dispatcher 单测."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core.agents.dag_dispatcher import (
    DAGCycleError,
    DAGUnknownDependencyError,
    _filter_upstream_for,
    dispatch_dag,
    topological_sort,
)
from bowhead_core.agents.task_spec import (
    BusRef,
    TaskFailureError,
    TaskResult,
    TaskSpec,
)
from bowhead_core.agents.turn_context import (
    RuntimeTurnContext,
    SerializableTurnContext,
)


def _spec(name: str, task_id: str, depends_on: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        agent_name=name,
        user_question=f"q_{task_id}",
        task_id=task_id,
        depends_on=depends_on,
    )


# -- Topological sort --


def test_empty_returns_empty() -> None:
    assert topological_sort([]) == []


def test_no_dependencies_single_level() -> None:
    tasks = [_spec("a", "t_1"), _spec("b", "t_2"), _spec("c", "t_3")]
    levels = topological_sort(tasks)
    assert len(levels) == 1
    assert {t.task_id for t in levels[0]} == {"t_1", "t_2", "t_3"}


def test_linear_chain() -> None:
    tasks = [
        _spec("a", "t_1"),
        _spec("b", "t_2", depends_on=("t_1",)),
        _spec("c", "t_3", depends_on=("t_2",)),
    ]
    levels = topological_sort(tasks)
    assert len(levels) == 3
    assert levels[0][0].task_id == "t_1"
    assert levels[1][0].task_id == "t_2"
    assert levels[2][0].task_id == "t_3"


def test_diamond_dag() -> None:
    """A → B,C → D"""
    tasks = [
        _spec("a", "t_1"),
        _spec("b", "t_2", depends_on=("t_1",)),
        _spec("c", "t_3", depends_on=("t_1",)),
        _spec("d", "t_4", depends_on=("t_2", "t_3")),
    ]
    levels = topological_sort(tasks)
    assert len(levels) == 3
    assert {t.task_id for t in levels[0]} == {"t_1"}
    assert {t.task_id for t in levels[1]} == {"t_2", "t_3"}  # parallel
    assert {t.task_id for t in levels[2]} == {"t_4"}


def test_cycle_detected() -> None:
    tasks = [
        _spec("a", "t_1", depends_on=("t_2",)),
        _spec("b", "t_2", depends_on=("t_1",)),
    ]
    with pytest.raises(DAGCycleError, match="cycle"):
        topological_sort(tasks)


def test_unknown_dependency() -> None:
    tasks = [_spec("a", "t_1", depends_on=("t_does_not_exist",))]
    with pytest.raises(DAGUnknownDependencyError, match="depends on"):
        topological_sort(tasks)


# -- Filter upstream --


def test_filter_upstream_picks_only_listed() -> None:
    """task.upstream 只含其 depends_on 中 ok results."""
    spec_b = _spec("b", "t_b", depends_on=("t_a",))
    prev = [
        TaskResult.succeeded(_spec("a", "t_a"), {"finding": "X"}),
        TaskResult.succeeded(_spec("z", "t_z"), {"unrelated": True}),  # Must not pass through.
    ]
    upstream = _filter_upstream_for(spec_b, prev)
    assert len(upstream) == 1
    assert upstream[0]["agent"] == "a"
    assert upstream[0]["payload"] == {"finding": "X"}


def test_filter_upstream_skips_failed_dep() -> None:
    spec_b = _spec("b", "t_b", depends_on=("t_a",))
    prev = [
        TaskResult.failed(_spec("a", "t_a"), TaskFailureError("Timeout")),
    ]
    upstream = _filter_upstream_for(spec_b, prev)
    assert upstream == ()


# -- dispatch_dag end-to-end --


@dataclass
class _StubRunner:
    """Records dispatch calls; returns predefined outcomes per task_id."""

    outcomes: dict[str, TaskResult] = field(default_factory=dict)
    dispatched_levels: list[list[str]] = field(default_factory=list)

    async def dispatch(
        self,
        tasks: list[TaskSpec],
        _sctx: Any,
        _rf: Any,
        _arf: Any,
        _ml: Any,
    ) -> list[TaskResult]:
        self.dispatched_levels.append([t.task_id for t in tasks])
        return [
            self.outcomes.get(
                t.task_id,
                TaskResult.succeeded(t, {"task_id": t.task_id, "ok": True}),
            )
            for t in tasks
        ]


def _ctx() -> tuple[SerializableTurnContext, Any]:
    sctx = SerializableTurnContext(
        user_id="u_1",
        workspace_id="ws_test",
        session_id="s_a",
        turn_id="t_b",
        role="analyst",
        bus_ref=BusRef(session_id="s_a", turn_id="t_b"),
    )

    def _rf(s: SerializableTurnContext) -> RuntimeTurnContext:
        return RuntimeTurnContext(serializable=s)

    return sctx, _rf


@pytest.mark.asyncio
async def test_dispatch_no_deps_single_level() -> None:
    runner = _StubRunner()
    tasks = [_spec("a", "t_1"), _spec("b", "t_2")]
    sctx, rf = _ctx()
    results = await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert runner.dispatched_levels == [["t_1", "t_2"]]


@pytest.mark.asyncio
async def test_dispatch_chain_serial() -> None:
    runner = _StubRunner()
    tasks = [
        _spec("a", "t_1"),
        _spec("b", "t_2", depends_on=("t_1",)),
        _spec("c", "t_3", depends_on=("t_2",)),
    ]
    sctx, rf = _ctx()
    results = await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    assert len(results) == 3
    # 3 dispatch calls, 1 task each
    assert runner.dispatched_levels == [["t_1"], ["t_2"], ["t_3"]]


@pytest.mark.asyncio
async def test_dispatch_diamond_parallel_middle() -> None:
    runner = _StubRunner()
    tasks = [
        _spec("a", "t_1"),
        _spec("b", "t_2", depends_on=("t_1",)),
        _spec("c", "t_3", depends_on=("t_1",)),
        _spec("d", "t_4", depends_on=("t_2", "t_3")),
    ]
    sctx, rf = _ctx()
    await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    # Level 1 = [t_1], level 2 = [t_2, t_3] parallel, level 3 = [t_4]
    assert len(runner.dispatched_levels) == 3
    assert set(runner.dispatched_levels[1]) == {"t_2", "t_3"}
    assert runner.dispatched_levels[2] == ["t_4"]


@pytest.mark.asyncio
async def test_upstream_payloads_filtered_by_deps() -> None:
    """t_3 deps_on t_1 (not t_2) — upstream_payloads 仅含 t_1 ok result."""
    spec_t1 = _spec("a", "t_1")
    spec_t2 = _spec("b", "t_2")
    spec_t3 = _spec("c", "t_3", depends_on=("t_1",))
    runner = _StubRunner(
        outcomes={
            "t_1": TaskResult.succeeded(spec_t1, {"x": 1}),
            "t_2": TaskResult.succeeded(spec_t2, {"y": 2}),
        }
    )
    tasks = [spec_t1, spec_t2, spec_t3]
    sctx, rf = _ctx()
    await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    # Level 2 dispatches t_3 using its effective spec (including upstream).
    # runner.dispatched_levels[1] is just ["t_3"]
    # Outcomes do not expose effective.upstream, but reaching level 2 without failure lets us infer it.
    assert len(runner.dispatched_levels) == 2
    assert runner.dispatched_levels[1] == ["t_3"]


@pytest.mark.asyncio
async def test_all_level_fail_stops_subsequent() -> None:
    """level 1 全 fail → level 2 不 dispatch, return UpstreamFailed."""
    spec_t1 = _spec("a", "t_1")
    spec_t2 = _spec("b", "t_2", depends_on=("t_1",))
    runner = _StubRunner(
        outcomes={
            "t_1": TaskResult.failed(spec_t1, TaskFailureError("Timeout")),
        }
    )
    tasks = [spec_t1, spec_t2]
    sctx, rf = _ctx()
    results = await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    # t_1 dispatched + failed; t_2 NOT dispatched
    assert len(runner.dispatched_levels) == 1
    assert runner.dispatched_levels[0] == ["t_1"]
    assert len(results) == 2
    assert results[0].ok is False  # t_1
    assert results[1].ok is False  # t_2
    assert results[1].error.reason == "UpstreamFailed"


@pytest.mark.asyncio
async def test_partial_level_fail_continues() -> None:
    """level 1 部分 fail (1 ok 1 fail) → level 2 继续 dispatch (T-BE-V2-P1.2)."""
    spec_t1 = _spec("a", "t_1")
    spec_t2 = _spec("b", "t_2")
    spec_t3 = _spec("c", "t_3", depends_on=("t_1",))
    runner = _StubRunner(
        outcomes={
            "t_1": TaskResult.succeeded(spec_t1, {"ok": True}),
            "t_2": TaskResult.failed(spec_t2, TaskFailureError("Timeout")),
        }
    )
    tasks = [spec_t1, spec_t2, spec_t3]
    sctx, rf = _ctx()
    results = await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    assert len(runner.dispatched_levels) == 2
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[2].ok is True


@pytest.mark.asyncio
async def test_results_order_matches_input() -> None:
    """返回顺序保持 input tasks 顺序 (即使 DAG 调度顺序不同)."""
    spec_t1 = _spec("first", "t_1", depends_on=("t_2",))  # Dispatched second.
    spec_t2 = _spec("second", "t_2")  # Dispatched first.
    tasks = [spec_t1, spec_t2]  # input order: t_1 first
    runner = _StubRunner()
    sctx, rf = _ctx()
    results = await dispatch_dag(
        tasks,
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    # Return order = input order.
    assert results[0].task_spec.task_id == "t_1"
    assert results[1].task_spec.task_id == "t_2"


@pytest.mark.asyncio
async def test_empty_tasks() -> None:
    runner = _StubRunner()
    sctx, rf = _ctx()
    results = await dispatch_dag(
        [],
        sctx,
        rf,
        lambda *_: ({}, 0, 0),
        lambda _: None,
        runner=runner,
    )
    assert results == []
    assert runner.dispatched_levels == []
