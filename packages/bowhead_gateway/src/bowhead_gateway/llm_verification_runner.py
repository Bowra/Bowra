"""In-process verification Batch runner with DB-authoritative Item leases."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import structlog
from bowhead_core.audit_actions import AuditAction
from bowhead_core.team_llm_batch_store import (
    ClaimedItem,
    ItemCompletion,
    TeamLlmBatchStore,
)

from bowhead_router import (
    CompatibleProbeRegistry,
    NotTestable,
    not_testable_result,
)

_TERMINAL_BATCH_STATES = {"completed", "cancelled", "interrupted"}


class LlmVerificationRunner:
    """Own active tasks for this process; never resumes old process work."""

    def __init__(self, db_path: str, *, process_concurrency: int = 12) -> None:
        self.store = TeamLlmBatchStore(db_path)
        self.registry = CompatibleProbeRegistry()
        self.process_slots = asyncio.Semaphore(process_concurrency)
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.maintenance_task: asyncio.Task[None] | None = None
        self.log = structlog.get_logger("bowhead_gateway.llm_verification")

    def start_maintenance(self) -> None:
        if self.maintenance_task is not None:
            return
        self.maintenance_task = asyncio.create_task(
            self._maintain(),
            name="llm-verification-maintenance",
        )

    def start(
        self,
        *,
        batch_id: str,
        team_id: str,
        runner_id: str,
        initiated_by: str,
    ) -> None:
        if batch_id in self.tasks:
            return
        task = asyncio.create_task(
            self._run(
                batch_id=batch_id,
                team_id=team_id,
                runner_id=runner_id,
                initiated_by=initiated_by,
            ),
            name=f"llm-verification:{batch_id}",
        )
        self.tasks[batch_id] = task
        task.add_done_callback(lambda _task, key=batch_id: self.tasks.pop(key, None))

    def cancel(self, batch_id: str) -> None:
        task = self.tasks.get(batch_id)
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        if self.maintenance_task is not None:
            self.maintenance_task.cancel()
            await asyncio.gather(
                self.maintenance_task,
                return_exceptions=True,
            )
            self.maintenance_task = None
        active = list(self.tasks.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def _maintain(self) -> None:
        next_retention_sweep = 0.0
        while True:
            try:
                interrupted = await self.store.interrupt_expired_runners()
                if interrupted:
                    self.log.warning(
                        "team_llm.verification_orphans_interrupted",
                        team_count=interrupted,
                    )
                loop_now = asyncio.get_running_loop().time()
                if loop_now >= next_retention_sweep:
                    sweep = await self.store.sweep_retention()
                    next_retention_sweep = loop_now + 6 * 60 * 60
                    self.log.info(
                        "team_llm.verification_retention_swept",
                        evidence=sweep.evidence,
                        items=sweep.items,
                        batches=sweep.batches,
                        plans=sweep.plans,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("team_llm.verification_maintenance_failed")
            await asyncio.sleep(10)

    async def _run(
        self,
        *,
        batch_id: str,
        team_id: str,
        runner_id: str,
        initiated_by: str,
    ) -> None:
        item_tasks: set[asyncio.Task[None]] = set()
        last_heartbeat = 0.0
        try:
            context = await self.store.get_probe_context(
                team_id=team_id,
                batch_id=batch_id,
            )
            while True:
                loop_now = asyncio.get_running_loop().time()
                if loop_now - last_heartbeat >= 10:
                    await self.store.heartbeat_runner(
                        team_id=team_id,
                        batch_id=batch_id,
                        runner_id=runner_id,
                    )
                    last_heartbeat = loop_now

                while len(item_tasks) < 3:
                    item = await self.store.claim_next_item(
                        team_id=team_id,
                        batch_id=batch_id,
                        runner_id=runner_id,
                    )
                    if item is None:
                        break
                    task = asyncio.create_task(
                        self._execute_item(
                            item=item,
                            context=context,
                            initiated_by=initiated_by,
                        ),
                        name=f"llm-verification-item:{item.item_id}",
                    )
                    item_tasks.add(task)

                if item_tasks:
                    done, item_tasks = await asyncio.wait(
                        item_tasks,
                        timeout=0.25,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        # A completion/lease write failure must fail the runner so
                        # `finally` interrupts every owned item. Swallowing it
                        # would leave a verifying item alive forever while this
                        # process keeps heartbeating the Batch.
                        task.result()
                    continue

                snapshot = await self.store.get_batch(
                    team_id=team_id,
                    batch_id=batch_id,
                )
                if snapshot is None or snapshot["state"] in _TERMINAL_BATCH_STATES:
                    if snapshot is not None:
                        with suppress(Exception):
                            await self.store.write_ops_audit(
                                team_id=team_id,
                                actor_user_id=initiated_by,
                                action=AuditAction.LLM_VERIFICATION_BATCH_COMPLETED,
                                batch_id=batch_id,
                                payload={
                                    "state": snapshot["state"],
                                    "counts": snapshot["counts"],
                                },
                            )
                    return
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            for task in item_tasks:
                task.cancel()
            if item_tasks:
                await asyncio.gather(*item_tasks, return_exceptions=True)
            raise
        except Exception:
            self.log.exception(
                "team_llm.verification_runner_failed",
                batch_id=batch_id,
            )
        finally:
            await self.store.interrupt_runner(
                team_id=team_id,
                batch_id=batch_id,
                runner_id=runner_id,
            )

    async def _execute_item(
        self,
        *,
        item: ClaimedItem,
        context: dict[str, Any],
        initiated_by: str,
    ) -> None:
        providers = context.get("providers")
        provider_context = providers.get(item.provider_id) if isinstance(providers, dict) else None
        display_name = str(provider_context.get("display_name") or item.provider_id) if isinstance(provider_context, dict) else item.provider_id
        completion = ItemCompletion(
            state="errored",
            safe_code="internal_probe_error",
            safe_http_status=None,
            safe_summary_code=None,
            duration_ms=0,
        )
        try:
            if not isinstance(provider_context, dict):
                raise ValueError("provider context is missing")
            provider = provider_context.get("config")
            plan = self.registry.plan(
                provider,
                provider_id=item.provider_id,
                provider_display_name=display_name,
                model_id=item.model_id,
                capability=item.capability,  # type: ignore[arg-type]
            )
            if isinstance(plan, NotTestable):
                # Untestable plans make no provider requests (execution policy no-provider-request-v1).
                result = not_testable_result()
                if plan.safe_code != "probe_not_supported":
                    from dataclasses import replace

                    result = replace(result, code=plan.safe_code)
            else:
                async with self.process_slots:
                    result = await self.registry.execute(
                        plan,
                        timeout=min(
                            float(provider.get("timeout_sec", 60)),
                            30.0,
                        ),
                    )
            if result is not None:
                completion = ItemCompletion(
                    state=result.terminal_state,
                    safe_code=result.code,
                    safe_http_status=result.safe_http_status,
                    safe_summary_code=result.safe_summary_code,
                    duration_ms=result.duration_ms,
                    vector_dimension=result.vector_dimension,
                    actual_model_id=result.actual_model_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception(
                "team_llm.verification_item_failed",
                batch_id=item.batch_id,
                item_id=item.item_id,
            )

        await self.store.complete_item(
            item=item,
            completion=completion,
            initiated_by=initiated_by,
            provider_display_name=display_name,
        )
