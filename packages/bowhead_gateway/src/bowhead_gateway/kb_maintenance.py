"""Bounded system observations and user-scoped, explicit rebuild jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import anyio
from bowhead_core.kb.eligibility import KbUseDecision, KbUseEligibility
from bowhead_core.kb.generations import KbGenerationStore, now_iso, use_guard_enabled
from bowhead_core.kb.maintenance import KbMaintenanceStore
from bowhead_core.kb.permission import KbPermission, ScopeContext

from bowhead_gateway.kb_authorization import kb_file_authorizer

log = logging.getLogger(__name__)


async def team_runtime(app, team_id):
    if not team_id:
        raise ValueError("team required")
    cfg, version = await app.state.user_store.get_team_llm_config_with_version(team_id)
    runtime = await app.state.team_kb_runtime_registry.get(team_id, cfg, version)
    if runtime is None:
        raise ValueError("team runtime unavailable")
    return runtime


async def reconcile_page(app):
    files = app.state.kb_files_store
    store = KbMaintenanceStore(files._db_path)
    page = await store.scan_page()
    if not page:
        await store.checkpoint("")
        return 0
    checked = 0
    deadline = asyncio.get_running_loop().time() + 55
    for file in page:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0 or not use_guard_enabled():
            break
        runtime = None
        try:
            async with asyncio.timeout(min(45, remaining)):
                runtime = await team_runtime(app, file.team_id)
                # System maintenance checks physical facts, never grants a user AI
                # access. Every read/execute consumer independently authorizes its viewer.
                scope = ScopeContext(
                    user_id=KbPermission.owner_of(file),
                    team_id=file.team_id,
                    workspace_id=file.scope_id if file.scope in {"space", "workspace"} else file.workspace_id,
                )

                async def same_identity(current, original=file):
                    return all(
                        getattr(current, key) == getattr(original, key)
                        for key in ("team_id", "scope", "scope_id", "workspace_id", "artifact_id", "version")
                    )

                decision = await KbUseEligibility(files, app.state.artifact_store, runtime.kb_store).check(file.file_id, scope, same_identity)
                generations = KbGenerationStore(files._db_path)
                if not file.active_generation_id:
                    from bowhead_core.kb.health import SourceRecord, check_storage_health, inspect_file_health

                    artifacts = app.state.artifact_store
                    artifact = await artifacts.get(file.artifact_id)

                    async def storage_health(artifacts=artifacts):
                        paths = await artifacts.list_recent_ready_paths(limit=20)
                        return await check_storage_health(artifacts.backend, [p for _, p in paths])

                    observation = await inspect_file_health(
                        file,
                        source=SourceRecord(artifact.path, artifact.status) if artifact else None,
                        backend=artifacts.backend,
                        storage_health=storage_health,
                    )
                    if observation.source_health == "confirmed_absent":
                        await generations.mark_absent(file)
                    reason = (
                        "generation_unverified"
                        if observation.source_health == "present"
                        else "source_missing"
                        if observation.source_health == "confirmed_absent"
                        else "source_unavailable"
                    )
                    decision = KbUseDecision("paused", reason, None, now_iso())
                for gid in await generations.claim_orphans(
                    file, index_version=runtime.kb_store.index_version, before=(datetime.now(UTC) - timedelta(days=1)).isoformat()
                ):
                    await runtime.kb_store.delete_generation(file, gid)
                    await generations.forget_orphan(gid)
        except Exception:
            decision = KbUseDecision("paused", "source_unavailable", file.active_generation_id, now_iso())
        await store.observe(file, decision, index_version=runtime.kb_store.index_version if runtime else "")
        await store.checkpoint(file.file_id)
        checked += 1
    log.info("kb.source_reconcile checked=%d", checked)
    return checked


async def maintenance_loop(app):
    while True:
        if use_guard_enabled():
            try:
                await reconcile_page(app)
            except Exception:
                log.exception("kb.source_reconcile failed")
        await anyio.sleep(60)


async def run_rebuild_job(app, job_id):
    files = app.state.kb_files_store
    jobs = KbMaintenanceStore(files._db_path)
    authorize = kb_file_authorizer(app.state.user_store, getattr(app.state, "access_control_store", None))
    try:
        job = await jobs.get_job(job_id)
        if not job or job["state"] != "running":
            await jobs.state(job_id, "cancelled", expected="cancelling")
            return
        outcomes = job["outcomes"]
        for position in range(job["cursor"], len(job["items"])):
            if not use_guard_enabled() or (await jobs.get_job(job_id))["state"] != "running":
                await jobs.state(job_id, "paused")
                await jobs.state(job_id, "cancelled", expected="cancelling")
                return
            item = job["items"][position]
            file = await files.get(item["file_id"])

            async def allowed(current, manifest=item):
                if current is None or not use_guard_enabled():
                    return False
                if any(getattr(current, key) != manifest[key] for key in ("team_id", "workspace_id", "scope", "scope_id", "artifact_id")):
                    return False
                if not await authorize(current, job["user_id"], job["team_id"], job["workspace_id"], write=True):
                    return False
                resolver = getattr(app.state, "entitlements", None)
                if resolver is None:
                    if getattr(app.state, "deployment_mode", "saas") == "local":
                        return False
                else:
                    if not await resolver.is_feature_licensed(job["team_id"], "kb"):
                        return False
                    await resolver.assert_can_run_turn(job["team_id"])
                return True

            if not await allowed(file):
                await jobs.state(job_id, "paused")
                await jobs.state(job_id, "cancelled", expected="cancelling")
                return
            outcome = "changed"
            # Publication and progress are separate commits. A durable claim lets
            # us recognize publication after a crash without paying for it twice.
            claimed = item.get("claimed_version")
            if (
                claimed is not None
                and file.version == claimed + 1
                and file.parse_status == "done"
                and file.active_generation_id != item["active_generation_id"]
            ):
                outcome = "completed"
            elif file.version == item.get("claimed_version", item["version"]):
                runtime = await team_runtime(app, job["team_id"])
                if runtime.worker is None:
                    raise ValueError("worker unavailable")
                if await jobs.claim_file(job_id, position, file):
                    try:
                        await runtime.worker.parse_and_index(file.file_id, authorize=allowed)
                    finally:
                        await jobs.interrupt_file(job_id)
                    latest = await files.get(file.file_id)
                    outcome = (
                        "completed"
                        if latest and latest.parse_status == "done" and latest.active_generation_id != item["active_generation_id"]
                        else "failed"
                    )
            outcomes.append({"file_id": item["file_id"], "outcome": outcome})
            await jobs.progress(job_id, position + 1, outcomes)
        await jobs.state(job_id, "completed")
        await jobs.state(job_id, "cancelled", expected="cancelling")
    except asyncio.CancelledError:
        await jobs.interrupt_file(job_id)
        await jobs.state(job_id, "paused")
        await jobs.state(job_id, "cancelled", expected="cancelling")
        raise
    except Exception:
        await jobs.interrupt_file(job_id)
        log.exception("kb.rebuild_job paused job=%s", job_id)
        await jobs.state(job_id, "paused")
        await jobs.state(job_id, "cancelled", expected="cancelling")


def start_rebuild_job(app, job_id):
    tasks = getattr(app.state, "kb_rebuild_tasks", None)
    if tasks is None:
        tasks = app.state.kb_rebuild_tasks = {}
    if job_id in tasks and not tasks[job_id].done():
        return
    task = asyncio.create_task(run_rebuild_job(app, job_id), name="kb_rebuild")
    tasks[job_id] = task
    task.add_done_callback(lambda _: tasks.pop(job_id, None))
