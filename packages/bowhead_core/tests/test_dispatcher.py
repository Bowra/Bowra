"""Sprint multi-agent-v1 PR-#6 — dispatcher 单测.

覆盖:
- carve_token_budget 边界 (各 num_tasks / parent_remaining 组合)
- get_task_runner backend 切换 (local / celery / unknown)
- dispatch_parallel 包 TaskRunner.dispatch 透传
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.agents.dispatcher import (
    carve_token_budget,
    dispatch_parallel,
    get_task_runner,
)
from bowhead_core.agents.manifest import AgentManifest
from bowhead_core.agents.task_runner import LocalTaskRunner
from bowhead_core.agents.task_spec import BusRef, TaskSpec
from bowhead_core.agents.turn_context import (
    RuntimeTurnContext,
    SerializableTurnContext,
)

# -- carve_token_budget --


def test_carve_basic() -> None:
    """8000 parent, 3 tasks: (8000-1000)/3 = 2333."""
    assert carve_token_budget(8000, 3) == 2333


def test_carve_caps_at_8000() -> None:
    """超大 parent: 单 task 不超 8000."""
    assert carve_token_budget(100_000, 2) == 8000


def test_carve_floors_at_500() -> None:
    """很小 parent / 多 tasks: 单 task 不低于 500."""
    assert carve_token_budget(2000, 10) == 500
    assert carve_token_budget(0, 5) == 500


def test_carve_one_task() -> None:
    """单 task: 全 parent - reserve."""
    assert carve_token_budget(5000, 1) == 4000


def test_carve_zero_tasks_raises() -> None:
    with pytest.raises(ValueError, match="num_tasks must be"):
        carve_token_budget(8000, 0)


# -- get_task_runner --


def test_get_runner_default_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_TASK_RUNNER", raising=False)
    runner = get_task_runner()
    assert isinstance(runner, LocalTaskRunner)


def test_get_runner_explicit_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TASK_RUNNER", "local")
    assert isinstance(get_task_runner(), LocalTaskRunner)


def test_get_runner_celery_returns_celery_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 PR-#1: BOWHEAD_TASK_RUNNER=celery → CeleryTaskRunner instance."""
    from bowhead_core.agents.celery_runner import CeleryTaskRunner

    monkeypatch.setenv("BOWHEAD_TASK_RUNNER", "celery")
    runner = get_task_runner()
    assert isinstance(runner, CeleryTaskRunner)


def test_get_runner_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TASK_RUNNER", "weird")
    with pytest.raises(ValueError, match="Unknown BOWHEAD_TASK_RUNNER"):
        get_task_runner()


# -- dispatch_parallel --


def _mk_manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "name": "compute",
            "model": "analyst",
            "trust_class": "read-mostly",
            "max_iterations": 10,
            "max_output_tokens": 1000,
            "description": "test",
            "system": {"file": "x.md"},
            "tools": {"default_enabled": False, "allow": ["bi_query"]},
        }
    )


def _mk_sctx() -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u_1",
        workspace_id="ws_1",
        session_id="s_a",
        turn_id="t_b",
        role="analyst",
        bus_ref=BusRef(session_id="s_a", turn_id="t_b"),
    )


def _mk_runtime_factory() -> Any:
    def _f(sctx: SerializableTurnContext) -> RuntimeTurnContext:
        return RuntimeTurnContext(serializable=sctx)

    return _f


@pytest.mark.asyncio
async def test_dispatch_parallel_uses_local_runner_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """default 不传 runner → get_task_runner() 拿 LocalTaskRunner."""
    monkeypatch.delenv("BOWHEAD_TASK_RUNNER", raising=False)
    spec = TaskSpec(agent_name="compute", user_question="q")

    async def _ok(_m, _s, _r):
        return {"ok": True}, 0, 0

    results = await dispatch_parallel(
        [spec],
        _mk_sctx(),
        _mk_runtime_factory(),
        _ok,
        manifest_lookup=lambda _: _mk_manifest(),
    )
    assert len(results) == 1
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_parallel_with_explicit_runner() -> None:
    """传 runner override 应直接用而不调 get_task_runner."""
    runner = LocalTaskRunner()
    spec = TaskSpec(agent_name="compute", user_question="q")

    async def _ok(_m, _s, _r):
        return {"name": "explicit"}, 0, 0

    results = await dispatch_parallel(
        [spec],
        _mk_sctx(),
        _mk_runtime_factory(),
        _ok,
        manifest_lookup=lambda _: _mk_manifest(),
        runner=runner,
    )
    assert len(results) == 1
    assert results[0].payload == {"name": "explicit"}


@pytest.mark.asyncio
async def test_dispatch_parallel_empty_tasks() -> None:
    runner = LocalTaskRunner()
    results = await dispatch_parallel(
        [],
        _mk_sctx(),
        _mk_runtime_factory(),
        lambda *_: None,  # Never called (empty).
        manifest_lookup=lambda _: _mk_manifest(),
        runner=runner,
    )
    assert results == []
