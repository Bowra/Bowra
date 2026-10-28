"""CeleryTaskRunner — distributed TaskRunner backend (v2 PR-#1).

实现 TaskRunner 协议 (与 LocalTaskRunner drop-in 互换). 用 Celery + redis 跨
worker fan-out, fall back to LocalTaskRunner 若 redis unhealthy.

设计 (sprint-multi-agent-v2.md §2.1 + tech-review-log-v2 P0/P1):
- ctor 注入 RedisHealthMonitor + dispatch_fn (DI 利于单测)
- dispatch_fn 真实 = `run_task_celery.apply_async`; 单测 mock
- await_all 用 asyncio.sleep poll AsyncResult.ready (avoid blocking event loop)
- fail-soft + 错误 wrap → TaskFailureError (与 LocalTaskRunner 一致)
- timeout: 沿用 compute_task_timeout (manifest.max_iterations x 5, ≤ 600s)
- cancel: 调 AsyncResult.revoke(terminate=True)

Worker 侧 task body 不在此模块 (跨进程, 见 celery_worker.py). 此 module 调度方.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol

from .metrics import emit_task_failed
from .task_runner import (
    LocalTaskRunner,
    compute_task_timeout,
)
from .task_spec import TaskFailureError, TaskResult, TaskSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from .manifest import AgentManifest
    from .redis_health import RedisHealthMonitor
    from .task_runner import AgentRunnerFn, RuntimeFactory, TaskRunner
    from .turn_context import SerializableTurnContext

logger = logging.getLogger(__name__)


class _AsyncResultLike(Protocol):
    """Minimal interface CeleryTaskRunner needs from celery.result.AsyncResult."""

    id: str

    def ready(self) -> bool: ...
    def failed(self) -> bool: ...
    @property
    def result(self) -> Any: ...
    def revoke(self, *, terminate: bool = False) -> None: ...


class _DispatchFn(Protocol):
    """Signature dispatch_fn must satisfy.

    Real: `run_task_celery.apply_async`. Mocked in tests.
    """

    def __call__(
        self,
        *,
        args: list[Any],
        expires: int,
        task_time_limit: int,
        soft_time_limit: int,
    ) -> _AsyncResultLike: ...


_DEFAULT_POLL_INTERVAL_S = 0.5


class CeleryTaskRunner:
    """v2 — Celery + redis 分布式 TaskRunner.

    Fail-soft: 错误 wrap 成 TaskFailureError; redis 故障 fallback to local.
    """

    def __init__(
        self,
        redis_health: RedisHealthMonitor,
        *,
        dispatch_fn: _DispatchFn | None = None,
        fallback: TaskRunner | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.redis_health = redis_health
        self._dispatch_fn = dispatch_fn  # lazy resolve via _resolve_dispatch_fn
        self._fallback: TaskRunner = fallback or LocalTaskRunner()
        self.poll_interval_s = poll_interval_s
        self._running: dict[str, _AsyncResultLike] = {}

    def _resolve_dispatch_fn(self) -> _DispatchFn:
        if self._dispatch_fn is not None:
            return self._dispatch_fn
        # Lazy import: production path. Tests inject dispatch_fn so never hit this.
        from .celery_worker import run_task_celery

        def _dispatch(
            *,
            args: list[Any],
            expires: int,
            task_time_limit: int,
            soft_time_limit: int,
        ) -> _AsyncResultLike:
            return run_task_celery.apply_async(
                args=args,
                expires=expires,
                task_time_limit=task_time_limit,
                soft_time_limit=soft_time_limit,
            )

        return _dispatch

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

        if not await self.redis_health.healthy():
            logger.warning(
                "redis unhealthy; CeleryTaskRunner falling back to LocalTaskRunner",
            )
            return await self._fallback.dispatch(
                tasks,
                sctx,
                runtime_factory,
                agent_runner_fn,
                manifest_lookup,
            )

        dispatch_fn = self._resolve_dispatch_fn()
        per_task: list[tuple[TaskSpec, _AsyncResultLike | None, float, str | None]] = []

        for spec in tasks:
            start = perf_counter()
            try:
                manifest = manifest_lookup(spec.agent_name)
            except KeyError:
                per_task.append((spec, None, start, "UnknownAgent"))
                continue
            except Exception as e:
                per_task.append((spec, None, start, f"ManifestLookupError:{e}"))
                continue

            # Explicit spec.timeout_s override (long plan-step tasks) takes precedence over the manifest fallback estimate.
            timeout_s = spec.timeout_s if spec.timeout_s is not None and spec.timeout_s > 0 else compute_task_timeout(manifest)
            try:
                ar = dispatch_fn(
                    args=[asdict(spec), _sctx_to_dict(sctx)],
                    expires=timeout_s,
                    task_time_limit=timeout_s,
                    soft_time_limit=max(timeout_s - 10, 30),
                )
            except Exception as e:
                logger.warning(
                    "celery apply_async failed for %s: %s",
                    spec.agent_name,
                    e,
                )
                per_task.append((spec, None, start, f"CeleryDispatchError:{e}"))
                continue
            self._running[spec.task_id] = ar
            per_task.append((spec, ar, start, None))

        return await self._await_all(per_task)

    async def _await_all(
        self,
        items: list[tuple[TaskSpec, _AsyncResultLike | None, float, str | None]],
    ) -> list[TaskResult]:
        results: list[TaskResult] = []
        for spec, ar, start, dispatch_err in items:
            if ar is None:
                duration_ms = int((perf_counter() - start) * 1000)
                reason = dispatch_err or "CeleryDispatchError"
                emit_task_failed(agent_name=spec.agent_name, reason=reason.split(":")[0])
                results.append(
                    TaskResult.failed(
                        spec,
                        TaskFailureError(reason.split(":")[0], Exception(reason)),
                        duration_ms=duration_ms,
                    )
                )
                continue
            try:
                result = await self._await_one(spec, ar, start)
            finally:
                self._running.pop(spec.task_id, None)
            results.append(result)
        return results

    async def _await_one(
        self,
        spec: TaskSpec,
        ar: _AsyncResultLike,
        start: float,
    ) -> TaskResult:
        try:
            # Poll AsyncResult.ready() — no Event API on celery results.
            while not ar.ready():  # noqa: ASYNC110
                await asyncio.sleep(self.poll_interval_s)
        except asyncio.CancelledError:
            duration_ms = int((perf_counter() - start) * 1000)
            with contextlib.suppress(Exception):
                ar.revoke(terminate=True)
            emit_task_failed(agent_name=spec.agent_name, reason="Cancelled")
            return TaskResult.failed(
                spec,
                TaskFailureError("Cancelled"),
                duration_ms=duration_ms,
            )

        duration_ms = int((perf_counter() - start) * 1000)
        if ar.failed():
            err = ar.result if isinstance(ar.result, BaseException) else Exception(str(ar.result))
            reason = type(err).__name__ if isinstance(err, BaseException) else "WorkerError"
            emit_task_failed(agent_name=spec.agent_name, reason=reason)
            return TaskResult.failed(
                spec,
                TaskFailureError(reason, err),
                duration_ms=duration_ms,
            )

        # Worker returned (payload, tokens_in, tokens_out)
        try:
            payload, tin, tout = ar.result
        except (TypeError, ValueError) as e:
            emit_task_failed(agent_name=spec.agent_name, reason="ResultShapeError")
            return TaskResult.failed(
                spec,
                TaskFailureError("ResultShapeError", e),
                duration_ms=duration_ms,
            )
        return TaskResult.succeeded(
            spec,
            payload,
            tokens_input=tin,
            tokens_output=tout,
            duration_ms=duration_ms,
        )

    async def cancel(self, task_ids: list[str]) -> None:
        for tid in task_ids:
            ar = self._running.get(tid)
            if ar is not None:
                try:
                    ar.revoke(terminate=True)
                    logger.info("revoked celery task %s", tid)
                except Exception as e:
                    logger.warning("revoke failed for %s: %s", tid, e)


def _sctx_to_dict(sctx: SerializableTurnContext) -> dict[str, Any]:
    """Convert SerializableTurnContext → JSON-safe dict for celery args.

    BusRef is dataclass; tuples → lists for JSON.
    """
    d = asdict(sctx)
    # asdict converts nested dataclasses recursively (BusRef → dict already)
    # tuples become lists in JSON; that's fine
    return d
