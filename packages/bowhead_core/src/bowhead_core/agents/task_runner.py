"""TaskRunner protocol + LocalTaskRunner (sprint multi-agent-v1 §5.4 PR-#5).

抽象 agent 执行后端, 使 v2 切 Celery 不重写 dispatcher.

设计 (T-CTO-P0.1 / P1.4 + T-SRE-P1.3 + T-BE-P1.4 / P2.1):
- `TaskRunner.dispatch(tasks, sctx, runtime_factory) -> list[TaskResult]`
- `TaskRunner.cancel(task_ids) -> None`
- LocalTaskRunner: in-process `asyncio.gather` + per-task `wait_for` 兜底超时 +
  TaskFailureError 统一错误 wrap
- CeleryTaskRunner v2: 接口预留, PR 不实现

`runtime_factory` 是 Callable[[SerializableTurnContext], RuntimeTurnContext] —
LocalTaskRunner 调用者拼好 RuntimeTurnContext 共享 in-process refs;
CeleryTaskRunner v2 worker 内 reconstruct.

`agent_runner_factory` 是 Callable[[manifest, task_spec, runtime_ctx], Awaitable[dict]] —
真实跑 task 的入口 (PR-#11 接 TaskAgentRunner). 测试用 mock callable 即可.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol

from .metrics import emit_task_failed
from .task_spec import PlanStepError, TaskFailureError, TaskResult, TaskSpec

if TYPE_CHECKING:
    from .manifest import AgentManifest
    from .turn_context import RuntimeTurnContext, SerializableTurnContext

logger = logging.getLogger(__name__)


# Per-task timeout cap (T-SRE-P1.3): at most 600s per task regardless of manifest size.
_TASK_TIMEOUT_CAP_S = 600
# Task max_iterations × this factor = timeout (roughly ≤5s per step).
_TIMEOUT_MULTIPLIER_S = 5
# Fallback timeout without manifest information.
_DEFAULT_TIMEOUT_S = 300


# Type alias: callable executing an actual task.
# Accepts (manifest, spec, runtime_ctx), returns (payload_dict, tokens_in, tokens_out).
AgentRunnerFn = Callable[
    ["AgentManifest", TaskSpec, "RuntimeTurnContext"],
    Awaitable[tuple[dict[str, Any], int, int]],
]

# Type alias: SerializableTurnContext → RuntimeTurnContext factory.
# LocalTaskRunner: caller has preassembled RuntimeTurnContext; this factory returns it directly
# (cookie mode). CeleryTaskRunner v2 reconstructs inside the worker.
RuntimeFactory = Callable[["SerializableTurnContext"], "RuntimeTurnContext"]


def compute_task_timeout(manifest: AgentManifest | None) -> int:
    """计算单 task 兜底超时 (T-SRE-P1.3).

    `min(manifest.max_iterations * 5, 600)` 估算; 没 manifest 信息用 300s default.
    """
    if manifest is None:
        return _DEFAULT_TIMEOUT_S
    return min(manifest.max_iterations * _TIMEOUT_MULTIPLIER_S, _TASK_TIMEOUT_CAP_S)


class TaskRunner(Protocol):
    """Agent 执行后端的抽象."""

    async def dispatch(
        self,
        tasks: list[TaskSpec],
        sctx: SerializableTurnContext,
        runtime_factory: RuntimeFactory,
        agent_runner_fn: AgentRunnerFn,
        manifest_lookup: Callable[[str], AgentManifest],
    ) -> list[TaskResult]:
        """Dispatch N tasks 并 await 全 done. fail-soft.

        Args:
            tasks: TaskSpec list (1..N).
            sctx: 跨 task 共享的 SerializableTurnContext.
            runtime_factory: sctx → RuntimeTurnContext (LocalTaskRunner 用).
            agent_runner_fn: 真跑单 task 的入口 (PR-#11 接 TaskAgentRunner).
            manifest_lookup: name → AgentManifest (registry.get 通常).

        Returns:
            list[TaskResult] 顺序保持 input tasks 顺序 (gather 默认).
            所有错误 wrap 成 TaskFailureError 进 TaskResult.failed.
        """
        ...

    async def cancel(self, task_ids: list[str]) -> None:
        """T-CTO-P1.4: 取消运行中 task (用户断 WebSocket / 点[取消])."""
        ...


class LocalTaskRunner:
    """v1 — in-process asyncio.gather 实现.

    每 task 包 asyncio.wait_for 兜底超时 (T-BE-P2.1).
    所有错误 wrap 成 TaskFailureError (T-BE-P1.4).
    cancel 调 asyncio.Task.cancel() (T-CTO-P1.4).

    线程安全注: 此类不是线程安全 (单 dispatcher 1 个 LocalTaskRunner instance).
    """

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[TaskResult]] = {}

    async def dispatch(
        self,
        tasks: list[TaskSpec],
        sctx: SerializableTurnContext,
        runtime_factory: RuntimeFactory,
        agent_runner_fn: AgentRunnerFn,
        manifest_lookup: Callable[[str], AgentManifest],
    ) -> list[TaskResult]:
        if not tasks:
            return []

        # Independent future per task (asyncio.create_task wraps wait_for + exception handling).
        atasks: list[asyncio.Task[TaskResult]] = []
        for spec in tasks:
            atask = asyncio.create_task(self._run_one(spec, sctx, runtime_factory, agent_runner_fn, manifest_lookup))
            self._running[spec.task_id] = atask
            atasks.append(atask)

        try:
            # gather: one task completing does not affect others; fail-soft.
            # _run_one wraps exceptions as TaskFailureError into TaskResult.failed,
            # so gather does not raise.
            results = await asyncio.gather(*atasks, return_exceptions=False)
        finally:
            # Clean up _running to prevent leaks.
            for spec in tasks:
                self._running.pop(spec.task_id, None)
        return results

    async def _run_one(
        self,
        spec: TaskSpec,
        sctx: SerializableTurnContext,
        runtime_factory: RuntimeFactory,
        agent_runner_fn: AgentRunnerFn,
        manifest_lookup: Callable[[str], AgentManifest],
    ) -> TaskResult:
        """单 task 执行 (timeout + 异常 wrap)."""
        start = perf_counter()
        manifest: AgentManifest | None = None
        try:
            manifest = manifest_lookup(spec.agent_name)
        except KeyError:
            duration_ms = int((perf_counter() - start) * 1000)
            return TaskResult.failed(
                spec,
                TaskFailureError(
                    "UnknownAgent",
                    KeyError(f"agent {spec.agent_name!r} not in registry"),
                ),
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = int((perf_counter() - start) * 1000)
            return TaskResult.failed(
                spec,
                TaskFailureError("ManifestLookupError", e),
                duration_ms=duration_ms,
            )

        # Explicit spec.timeout_s override (long plan steps) > manifest fallback estimate.
        timeout_s = spec.timeout_s if spec.timeout_s is not None and spec.timeout_s > 0 else compute_task_timeout(manifest)
        runtime_ctx = runtime_factory(sctx)

        try:
            payload, tokens_in, tokens_out = await asyncio.wait_for(
                agent_runner_fn(manifest, spec, runtime_ctx),
                timeout=timeout_s,
            )
        except TimeoutError:
            duration_ms = int((perf_counter() - start) * 1000)
            emit_task_failed(agent_name=spec.agent_name, reason="Timeout")
            return TaskResult.failed(
                spec,
                TaskFailureError("Timeout", TimeoutError(f"step exceeded {timeout_s}s")),
                duration_ms=duration_ms,
            )
        except asyncio.CancelledError as e:
            duration_ms = int((perf_counter() - start) * 1000)
            emit_task_failed(agent_name=spec.agent_name, reason="Cancelled")
            return TaskResult.failed(
                spec,
                TaskFailureError("Cancelled", e),
                duration_ms=duration_ms,
            )
        except PlanStepError as e:
            # planning-mode-audit P1-a/P1-b: typed plan-step failure → clean error_kind
            # (generic branch persists exception class as kind and leaks it into user caveat); pass detail through
            # (needs_user_input clarification JSON → plan_steps.error).
            duration_ms = int((perf_counter() - start) * 1000)
            emit_task_failed(agent_name=spec.agent_name, reason=e.error_kind)
            return TaskResult.failed(
                spec,
                TaskFailureError(e.error_kind, e, detail=e.detail_json),
                duration_ms=duration_ms,
            )
        except Exception as e:
            # Wrap every other exception (T-BE-P1.4).
            duration_ms = int((perf_counter() - start) * 1000)
            emit_task_failed(agent_name=spec.agent_name, reason=type(e).__name__)
            return TaskResult.failed(
                spec,
                TaskFailureError(type(e).__name__, e),
                duration_ms=duration_ms,
            )

        duration_ms = int((perf_counter() - start) * 1000)
        return TaskResult.succeeded(
            spec,
            payload,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            duration_ms=duration_ms,
        )

    async def cancel(self, task_ids: list[str]) -> None:
        """T-CTO-P1.4: cancel 还在跑的 task."""
        for tid in task_ids:
            atask = self._running.get(tid)
            if atask is not None and not atask.done():
                atask.cancel()
                logger.info("cancelled task %s", tid)
