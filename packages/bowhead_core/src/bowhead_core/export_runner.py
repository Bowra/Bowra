"""Supervised single-process runner for durable Canvas export V2 jobs.

SQLite remains the queue and state authority. The in-memory event only wakes the
claim loop; losing it cannot lose a job. Format processors own normalization,
rendering, and validation and must move the claimed job through store phases.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from bowhead_core.export_store import (
    EXPORT_LEASE_SECONDS,
    ExportCompletion,
    ExportJobV2,
    ExportStore,
    ExportStoreError,
)

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactStore, StagedArtifact

logger = logging.getLogger(__name__)


class ExportAttemptStale(Exception):
    """The worker no longer owns this job attempt; all output must be discarded."""


class ExportProcessingError(Exception):
    """Bounded public failure emitted by a format processor."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        quality_status: str = "pending",
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.quality_status = quality_status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExportExecutionContext:
    store: ExportStore
    job_id: str
    worker_id: str
    attempt: int
    lease_seconds: int

    async def advance(
        self,
        phase: str,
        *,
        progress: dict[str, object] | None = None,
    ) -> None:
        changed = await self.store.advance_phase(
            job_id=self.job_id,
            worker_id=self.worker_id,
            attempt=self.attempt,
            phase=phase,
            progress=progress,
        )
        if not changed:
            raise ExportAttemptStale(self.job_id)

    async def heartbeat(self) -> None:
        renewed = await self.store.heartbeat(
            job_id=self.job_id,
            worker_id=self.worker_id,
            attempt=self.attempt,
            lease_seconds=self.lease_seconds,
        )
        if not renewed:
            raise ExportAttemptStale(self.job_id)

    async def ensure_current(self) -> None:
        current = await self.store.is_current_attempt(
            job_id=self.job_id,
            worker_id=self.worker_id,
            attempt=self.attempt,
        )
        if not current:
            raise ExportAttemptStale(self.job_id)

    async def publish_completion(
        self,
        *,
        artifact_store: ArtifactStore,
        output_artifact: StagedArtifact,
        manifest_artifact: StagedArtifact,
        completion: ExportCompletion,
    ) -> None:
        committed = await self.store.complete_job_with_staged_artifacts(
            job_id=self.job_id,
            worker_id=self.worker_id,
            attempt=self.attempt,
            completion=completion,
            artifact_store=artifact_store,
            output_artifact=output_artifact,
            manifest_artifact=manifest_artifact,
        )
        if not committed:
            raise ExportAttemptStale(self.job_id)


class ExportProcessor(Protocol):
    def supports(self, fmt: str, profile: str) -> bool: ...

    def supported_pairs(self) -> tuple[tuple[str, str], ...]: ...

    async def process(
        self,
        job: ExportJobV2,
        context: ExportExecutionContext,
    ) -> ExportCompletion: ...


ExportProcessorHandler = Callable[
    [ExportJobV2, ExportExecutionContext],
    Awaitable[ExportCompletion],
]


@dataclass
class ExportProcessorRegistry:
    _handlers: dict[tuple[str, str], ExportProcessorHandler] = field(default_factory=dict)

    def register(
        self,
        *,
        fmt: str,
        profile: str,
        handler: ExportProcessorHandler,
    ) -> None:
        key = (fmt, profile)
        if key in self._handlers:
            raise ValueError(f"duplicate export processor: {fmt}/{profile}")
        self._handlers[key] = handler

    def supports(self, fmt: str, profile: str) -> bool:
        return (fmt, profile) in self._handlers

    def supported_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._handlers))

    async def process(
        self,
        job: ExportJobV2,
        context: ExportExecutionContext,
    ) -> ExportCompletion:
        handler = self._handlers.get((job.fmt, job.profile))
        if handler is None:
            raise ExportProcessingError(
                "export_runner_unavailable",
                "该导出格式暂不可用，请稍后重试",
            )
        return await handler(job, context)


@dataclass
class DurableLocalExportRunner:
    store: ExportStore
    processor: ExportProcessor
    worker_id: str = field(default_factory=lambda: f"export-local-{uuid4().hex[:10]}")
    poll_interval_seconds: float = 1.0
    heartbeat_interval_seconds: float = 30.0
    lease_seconds: int = EXPORT_LEASE_SECONDS
    renderer_contract_versions: tuple[int, ...] = (1,)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _loop_task: asyncio.Task[None] | None = field(default=None, init=False)
    _active_task: asyncio.Task[None] | None = field(default=None, init=False)
    _active_job_id: str | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        configured_kind = os.environ.get("BOWHEAD_EXPORT_RUNNER_KIND", "local")
        if configured_kind != "local":
            raise RuntimeError(f"unsupported Canvas export runner topology: {configured_kind}")
        if self.poll_interval_seconds <= 0 or self.heartbeat_interval_seconds <= 0:
            raise ValueError("runner intervals must be positive")
        if self.lease_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("export lease must exceed the heartbeat interval")
        if (
            not self.renderer_contract_versions
            or any(type(version) is not int or version <= 0 for version in self.renderer_contract_versions)
            or len(self.renderer_contract_versions) != len(set(self.renderer_contract_versions))
        ):
            raise ValueError("renderer contract versions must be unique positive integers")
        self._stopping = False
        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name=f"canvas-export-claim-{self.worker_id}",
        )

    async def stop(self, *, grace_seconds: float = 120.0) -> None:
        self._stopping = True
        self._wake.set()
        active = self._active_task
        if active is not None and not active.done():
            try:
                await asyncio.wait_for(asyncio.shield(active), timeout=max(0.0, grace_seconds))
            except TimeoutError:
                active.cancel()
                with suppress(asyncio.CancelledError):
                    await active
        loop = self._loop_task
        if loop is not None and not loop.done():
            await loop
        self._loop_task = None
        self._active_task = None
        self._active_job_id = None

    def is_available_for(self, fmt: str, profile: str) -> bool:
        return bool(not self._stopping and self._loop_task is not None and not self._loop_task.done() and self.processor.supports(fmt, profile))

    async def enqueue(self, job_id: str) -> None:
        del job_id  # SQLite is authoritative; this signal intentionally carries no queue state.
        self._wake.set()

    async def cancel(self, job_id: str) -> None:
        if self._active_job_id != job_id:
            return
        active = self._active_task
        if active is not None and not active.done():
            active.cancel()

    async def _run_loop(self) -> None:
        while not self._stopping:
            supported_pairs = self.processor.supported_pairs()
            supported_targets = tuple(
                (fmt, profile, renderer_contract_version)
                for fmt, profile in supported_pairs
                for renderer_contract_version in self.renderer_contract_versions
            )
            queue_stage = "maintenance"
            try:
                failed_unclaimable = await self.store.fail_unclaimable_queued_jobs(
                    supported_pairs=supported_pairs,
                )
                if failed_unclaimable:
                    logger.warning(
                        "Failed %d queued Canvas exports without an enabled processor",
                        failed_unclaimable,
                    )
                if not supported_pairs:
                    await self._wait_for_work()
                    continue
                queue_stage = "claim"
                job = await self.store.claim_next(
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    supported_targets=supported_targets,
                )
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                if not isinstance(code, int) or code & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                logger.warning("canvas_export.queue_contention stage=%s sqlite_errorcode=%d", queue_stage, code)
                await self._wait_for_work()
                continue
            if job is None:
                await self._wait_for_work()
                continue
            self._active_job_id = job.id
            self._active_task = asyncio.create_task(
                self._execute(job),
                name=f"canvas-export-{job.id}-a{job.attempt}",
            )
            try:
                await self._active_task
            except asyncio.CancelledError:
                if self._stopping:
                    break
            finally:
                self._active_task = None
                self._active_job_id = None

    async def _wait_for_work(self) -> None:
        try:
            await asyncio.wait_for(
                self._wake.wait(),
                timeout=self.poll_interval_seconds,
            )
        except TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _execute(self, job: ExportJobV2) -> None:
        context = ExportExecutionContext(
            store=self.store,
            job_id=job.id,
            worker_id=self.worker_id,
            attempt=job.attempt,
            lease_seconds=self.lease_seconds,
        )
        process_task = asyncio.create_task(
            self.processor.process(job, context),
            name=f"canvas-export-processor-{job.id}-a{job.attempt}",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(context),
            name=f"canvas-export-heartbeat-{job.id}-a{job.attempt}",
        )
        try:
            done, _pending = await asyncio.wait(
                (process_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise RuntimeError("export heartbeat loop exited unexpectedly")
            completion = await process_task
            if completion.committed:
                published = await self.store.get_job(job.id)
                if (
                    published is None
                    or published.status != "done"
                    or published.artifact_id != completion.artifact_id
                    or published.manifest_artifact_id != completion.manifest_artifact_id
                ):
                    raise ExportStoreError(
                        "invalid_job_completion",
                    )
            else:
                await context.ensure_current()
                committed = await self.store.complete_job(
                    job_id=job.id,
                    worker_id=self.worker_id,
                    attempt=job.attempt,
                    completion=completion,
                )
                if not committed:
                    logger.info(
                        "canvas_export.stale_result job_id=%s attempt=%d",
                        job.id,
                        job.attempt,
                    )
        except ExportAttemptStale:
            logger.info("canvas_export.attempt_stale job_id=%s attempt=%d", job.id, job.attempt)
        except ExportProcessingError as exc:
            # embed-inline r4 lesson: subprocess stderr is buried in the __cause__ chain (e.g.
            # renderer_subprocess_failed:rc:stderr in ExportRenderSubprocessError);
            # logging only error_code hides the root cause. Log the full chain.
            logger.exception(
                "canvas_export.processing_failed job_id=%s attempt=%d code=%s",
                job.id,
                job.attempt,
                exc.code,
            )
            await self.store.fail_job(
                job_id=job.id,
                worker_id=self.worker_id,
                attempt=job.attempt,
                error_code=exc.code,
                public_error=exc.public_message,
                quality_status=exc.quality_status,
            )
        except ExportStoreError:
            logger.exception("canvas_export.processor_contract_failed job_id=%s", job.id)
            await self.store.fail_job(
                job_id=job.id,
                worker_id=self.worker_id,
                attempt=job.attempt,
                error_code="processor_protocol_error",
                public_error="导出处理失败，请重新导出",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("canvas_export.processor_failed job_id=%s", job.id)
            await self.store.fail_job(
                job_id=job.id,
                worker_id=self.worker_id,
                attempt=job.attempt,
                error_code="renderer_failed",
                public_error="导出处理失败，请重新导出",
            )
        finally:
            process_task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(
                process_task,
                heartbeat_task,
                return_exceptions=True,
            )

    async def _heartbeat_loop(self, context: ExportExecutionContext) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            try:
                await context.heartbeat()
            except ExportAttemptStale:
                raise
            except Exception as exc:
                raise ExportProcessingError(
                    "export_runner_unavailable",
                    "导出执行器暂时不可用，请重新导出",
                ) from exc


__all__ = [
    "DurableLocalExportRunner",
    "ExportAttemptStale",
    "ExportExecutionContext",
    "ExportProcessingError",
    "ExportProcessor",
    "ExportProcessorRegistry",
]
