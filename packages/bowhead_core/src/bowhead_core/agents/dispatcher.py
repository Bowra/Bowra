"""Multi-agent dispatcher (sprint multi-agent-v1 §5.5 PR-#6).

封装 TaskRunner 的顶层入口:
- `get_task_runner()`: settings flag 切换 LocalTaskRunner v1 / CeleryTaskRunner v2
- `dispatch_parallel()`: shared entry for task dispatch callers
- `carve_token_budget()`: parent token 平分给 N task (为报告收口留预算)

Settings:
- `BOWHEAD_TASK_RUNNER` ∈ {"local", "celery"} — v1 默认 "local"
- "celery" 时 redis 不可用 → fail-fast (startup validation, T-SRE-P0.1)

PR-#6 不实现 CeleryTaskRunner (v2). 切 "celery" 当前 raise NotImplementedError.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .task_runner import LocalTaskRunner, TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from .manifest import AgentManifest
    from .task_runner import AgentRunnerFn, RuntimeFactory
    from .task_spec import TaskResult, TaskSpec
    from .turn_context import SerializableTurnContext


# Reserve tokens for report finalization (token budget carve-out).
_RESERVE_FOR_WRITER = 1000
# Minimum tokens per task (too few prevent useful LLM output).
_MIN_TOKENS_PER_TASK = 500
# Maximum tokens per task (T-BE-P1.5: prevent exceeding manifest max_output_tokens).
_MAX_TOKENS_PER_TASK = 8000


def carve_token_budget(parent_remaining: int, num_tasks: int) -> int:
    """Parent 剩余 token 平分给 N 个 task, 留 1k 给报告收口。

    T-BE-P1.5: 加上限 cap 防爆 manifest max_output_tokens.
    T-CTO-P1.3 留 future: 不公平 (复杂 agent 饿死), v2 加 token_budget_weight 字段.

    Args:
        parent_remaining: 父 turn 剩余 token (sctx.settings_snapshot 拿).
        num_tasks: 同一 batch 内 task 数 (≥1).

    Returns:
        per-task token (≥ 500, ≤ 8000).
    """
    if num_tasks < 1:
        raise ValueError("num_tasks must be ≥ 1")
    available = max(parent_remaining - _RESERVE_FOR_WRITER, 0)
    per_task = available // num_tasks if num_tasks else _MIN_TOKENS_PER_TASK
    return min(max(per_task, _MIN_TOKENS_PER_TASK), _MAX_TOKENS_PER_TASK)


def get_task_runner() -> TaskRunner:
    """Settings-driven TaskRunner factory.

    - "local" (default): LocalTaskRunner (in-process asyncio.gather).
    - "celery": CeleryTaskRunner (v2 PR-#1) — distributed via redis broker.
      Falls back to LocalTaskRunner automatically if redis unhealthy.
    """
    backend = os.environ.get("BOWHEAD_TASK_RUNNER", "local").lower()
    if backend == "celery":
        from .celery_runner import CeleryTaskRunner
        from .redis_health import RedisHealthMonitor

        return CeleryTaskRunner(redis_health=RedisHealthMonitor())
    if backend != "local":
        raise ValueError(f"Unknown BOWHEAD_TASK_RUNNER={backend!r} (expect 'local' or 'celery')")
    return LocalTaskRunner()


async def dispatch_parallel(
    tasks: list[TaskSpec],
    sctx: SerializableTurnContext,
    runtime_factory: RuntimeFactory,
    agent_runner_fn: AgentRunnerFn,
    manifest_lookup: Callable[[str], AgentManifest],
    *,
    runner: TaskRunner | None = None,
) -> list[TaskResult]:
    """Top-level dispatch entry for the task runner.

    Wrap TaskRunner.dispatch, 让上层不直接拿 TaskRunner instance.

    Args:
        tasks: 1..N TaskSpec.
        sctx: SerializableTurnContext.
        runtime_factory: SerializableTurnContext → RuntimeTurnContext.
        agent_runner_fn: 真跑单 task 的 callable (PR-#11 接 TaskAgentRunner).
        manifest_lookup: name → AgentManifest (registry.get).
        runner: override (test 用); default get_task_runner() 工厂.

    Returns:
        list[TaskResult], 顺序保持 input tasks 顺序, fail-soft.
    """
    actual_runner = runner if runner is not None else get_task_runner()
    return await actual_runner.dispatch(tasks, sctx, runtime_factory, agent_runner_fn, manifest_lookup)
