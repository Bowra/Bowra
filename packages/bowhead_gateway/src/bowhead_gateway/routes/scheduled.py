"""Scheduled jobs REST API (#84 Step 1).

完整 CRUD + runs list + run-now. 独立的 APScheduler worker 在 Step 2 建. v1 没有 cron tick
驱动触发器 —— 只支持用户手动 run-now 来验证 job 配置.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bowhead_core.published_diagnostics import (
    SCHEDULED_FAILURE_CODES,
    normalize_scheduled_failure_code,
    publish_scheduled_failure,
)
from bowhead_core.scheduled_agent_binding import ScheduledAgentBindingError
from bowhead_core.scheduled_store import (
    ActiveRunExistsError,
    ScheduledJobStore,
    ScheduledRunSourceError,
)
from bowhead_core.scheduler_loop import (
    classify_run_status,
    cron_validation_error,
    next_run_for_job,
)
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.access_context import AccessContextError, build_access_context
from bowhead_gateway.public_entry_context import current_public_access_context

if TYPE_CHECKING:
    from bowhead_core.scheduled_agent_binding import (
        ScheduledAgentBinding,
        ScheduledAgentBindingSeed,
    )
    from bowhead_core.scheduled_store import ScheduledJob
    from fastapi import FastAPI

logger = logging.getLogger(__name__)
router = APIRouter()

# Keep run-now background tasks referenced so the event loop does not GC them mid-flight.
_inflight_run_now: set[asyncio.Task[None]] = set()


def _coded_failure(status_code: int, code: str) -> HTTPException:
    detail: dict[str, Any] = {"code": code}
    published = publish_scheduled_failure(code)
    if published is not None:
        detail["diagnostics"] = published.to_wire()
    return HTTPException(status_code, detail=detail)


async def _supersede_awaiting_runs(
    request: Request,
    store: ScheduledJobStore,
    job: ScheduledJob,
) -> None:
    """owner 手动重跑时，把该 job 挂起中的旧 run 作废让位（S3 · FR-5.5）。

    **只抢真的还停在歧义点上的那种**（turn 仍 `clarification_pending`，或 turn 已不存在的孤儿
    run）。turn 已经离开挂起态的一律不碰：
    - `running` = 用户刚答完、续跑在途。抢掉等于把这次回答连同已扣的费一起丢掉。
    - `completed` / `failed` / … = 已经跑完，**只差 reconciliation 把它收尾**（tech-design §3.3
      的「第三态」）。这条最险：抢了它，reconciliation 下一 tick 就找不到 awaiting 行，那份
      用户答出来的结果**永远不会送达**——账面上还记成 skipped，像什么都没发生过。tick 里靠
      「reconcile 先于过期执行」的顺序回避了它，run-now 没有这个顺序，只能自己判。
    读不到 turn（异常）也不抢：不确定时宁可让 owner 多等一个 tick，也不能吃掉一次交付。

    不抢时旧 run 仍占在飞位，下面的 `has_active_run` 会照常 409 —— 这是对的，409 的含义从
    「你被锁住了」变回「确实有一轮在跑/在收尾」，且至多一个 tick 后自动解开。

    赢下 CAS 才做副作用（作废旧卡 + 关徽章）：过期 / 收尾 / 换新三条路语义相反，只能有一个赢家。
    """
    from bowhead_gateway._scheduled_runner import (
        _resolve_needs_input,
        _turn_status_value,
    )
    from bowhead_gateway.clarification_invalidation import (
        publish_pending_clarification_invalidations,
    )

    try:
        awaiting = [r for r in await store.list_awaiting_runs() if r.job_id == job.id]
    except Exception as exc:
        # Failed reads mean none found: run-now uses its existing has_active_run branch, at worst reverting to old behavior (409).
        logger.warning("scheduled.awaiting_lookup_failed job=%s err=%s", job.id, exc)
        return
    orch = getattr(request.app.state, "orchestrator", None)
    session_store = getattr(orch, "session_store", None) if orch is not None else None
    for run in awaiting:
        if run.turn_id and session_store is not None:
            try:
                turn = await session_store.get_turn(run.turn_id)
            except Exception as exc:
                logger.warning("scheduled.supersede_turn_lookup_failed run=%s err=%s", run.id, exc)
                continue
            if turn is not None and _turn_status_value(turn) != "clarification_pending":
                logger.info(
                    "scheduled.run_now_leaves_run_alone job=%s run=%s turn_status=%s",
                    job.id,
                    run.id,
                    _turn_status_value(turn),
                )
                continue
        try:
            transition = await store.finish_awaiting_with_clarification_invalidation(
                run.id,
                run_status="skipped",
                run_error="scheduled_input_superseded",
                clarification_status="dismissed",
                clarification_reason="scheduled_input_superseded",
            )
        except Exception as exc:
            logger.warning("scheduled.supersede_failed job=%s run=%s err=%s", job.id, run.id, exc)
            continue
        if transition.won:
            logger.info("scheduled.awaiting_superseded job=%s run=%s", job.id, run.id)
            with contextlib.suppress(Exception):
                await publish_pending_clarification_invalidations(
                    request.app,
                    request_ids=[item.request_id for item in transition.invalidations],
                )
            # Close badges in the same batch: needs_input is in ACTION_REQUIRED and checks only state='open';
            # superseding invalidates the card requiring more information, so retaining it leaves a permanently red false pending action.
            await _resolve_needs_input(request.app, job, run.id, reason="superseded")


async def _advance_next_run_for_awaiting(store: ScheduledJobStore, job: ScheduledJob) -> None:
    """run-now 出身的挂起也要推进 next_run_at（S3 · FR-5.5 closure）。

    否则一个 cron 任务的 next_run_at 若已到期，run-now 挂起会在下一个 tick 就被过期分支
    秒杀记 `scheduled_input_timeout` —— Q2「挂起活满一个周期」对 run-now 出身不成立。
    `@manual` 无下一次触发时刻（`next_run_for_job` 返 None），保持 NULL 不动。
    """
    nxt = next_run_for_job(job.cron, job.tz, datetime.now(tz=UTC))
    if nxt is None:
        return
    with contextlib.suppress(Exception):
        await store.set_next_run_at(job.id, nxt.isoformat())


async def _run_now_and_finish(
    app: FastAPI,
    store: ScheduledJobStore,
    job: ScheduledJob,
    run_id: str,
) -> None:
    """Background body for run-now: dispatch the turn through the same gated path as the cron worker,
    then record the run outcome. Mirrors scheduler_loop._tick's classify + run_cb_failed handling."""
    from bowhead_gateway._scheduled_runner import run_scheduled_turn

    try:
        outcome = await run_scheduled_turn(app, job)
        # Suspension (S3): run-now can encounter ambiguity too. Move to awaiting rather than terminate, matching cron semantics;
        # otherwise identical work differs by entry (manual runs fail, scheduled runs suspend).
        if outcome.awaiting and outcome.turn_id:
            awaiting = await store.mark_run_awaiting_if_pending(
                run_id,
                outcome.turn_id,
            )
            if awaiting.won:
                notify = getattr(app.state, "notification_service", None)
                if notify is not None:
                    with contextlib.suppress(Exception):
                        await notify.emit_routine_needs_input(
                            job,
                            run_id=run_id,
                            session_id=outcome.session_id,
                        )
            else:
                logger.warning(
                    "scheduled.run_now_awaiting_rejected job=%s run=%s turn=%s cause=%s",
                    job.id,
                    run_id,
                    outcome.turn_id,
                    awaiting.cause,
                )
                await store.finish_run(
                    run_id,
                    status="failed",
                    turn_id=outcome.turn_id,
                    error="scheduled_turn_failed",
                    expected_status="running",
                )
            await _advance_next_run_for_awaiting(store, job)
            return
        code = normalize_scheduled_failure_code(outcome.error)
        await store.finish_run(
            run_id,
            status=classify_run_status(outcome.turn_id, code),
            turn_id=outcome.turn_id,
            error=code,
        )
    except Exception as exc:
        logger.exception(
            "run_now.failed job=%s run=%s error_type=%s",
            job.id,
            run_id,
            type(exc).__name__,
        )
        with contextlib.suppress(Exception):
            await store.finish_run(
                run_id,
                status="failed",
                error="scheduled_run_dispatch_failed",
            )


def _store(request: Request) -> ScheduledJobStore:
    s: ScheduledJobStore | None = getattr(request.app.state, "scheduled_store", None)
    if s is None:
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_store_unavailable",
        )
    return s


def _user_id(request: Request) -> str:
    """P4.5.17: JWT > X-Bowhead-User-Id header > dev_default_user."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


async def _require_workspace_access(
    request: Request,
    workspace_id: str,
    operation: SpaceOperation,
) -> None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return
    try:
        await build_access_context(
            user_store=user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=_user_id(request),
            workspace_id=workspace_id,
            operation=operation,
        )
    except AccessContextError as exc:
        code = (
            "ws_access_denied"
            if exc.code.startswith("ws_access_denied:")
            else (exc.code if exc.code in SCHEDULED_FAILURE_CODES else "authz_lookup_failed")
        )
        # Keep the established access-context envelope so existing clients can
        # continue reading detail.error.code. Diagnostics are additive.
        detail: dict[str, Any] = {"error": {"code": code}}
        published = publish_scheduled_failure(code)
        if published is not None:
            detail["diagnostics"] = published.to_wire()
        raise HTTPException(exc.status_code, detail=detail) from exc


async def _owned_job(
    *,
    store: ScheduledJobStore,
    job_id: str,
    request: Request,
) -> ScheduledJob:
    try:
        job = await store.get(job_id)
    except Exception as exc:
        logger.exception(
            "scheduled.job_load_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_load_failed",
        ) from exc
    if job is None or job.user_id != _user_id(request):
        raise _coded_failure(
            status.HTTP_404_NOT_FOUND,
            "scheduled_job_not_found",
        )
    return job


async def _bind_recipe_ref(request: Request, raw: dict[str, Any] | None, *, user_id: str) -> str | None:
    """把请求里的 recipe_ref 过绑定单点，返回要落库的 JSON（None = 不绑/解绑）。

    校验、flag 闸、可绑性判定全在 ``workflow_binding.validate_and_bind`` —— 路由、对话工具、
    config-copilot 草稿三条写入通道共用它，所以不存在「某条通道漏了校验」的旁路。
    不可绑一律 404 + 单一稳定码（不区分「不存在」与「不可见」，同仓库既有反枚举约定）。
    """
    if raw is None:
        return None
    if raw.get("unbind"):
        return ""
    from bowhead_core.user_store import TeamSkillStoreAdapter
    from bowhead_core.workflow_binding import WorkflowBindingError, validate_and_bind

    app_state = request.app.state
    user_store = getattr(app_state, "user_store", None)
    team_id = None
    if user_store is not None:
        try:
            team_id = await user_store.get_user_team_id(user_id)
        except Exception as exc:
            logger.warning("scheduled.bind_team_lookup_failed err=%s", exc)
    try:
        return await validate_and_bind(
            raw,
            user_id=user_id,
            team_id=team_id,
            skill_instance_store=getattr(app_state, "skill_instance_store", None),
            team_skill_store=TeamSkillStoreAdapter(user_store) if user_store is not None else None,
            team_subagent_store=user_store,
        )
    except WorkflowBindingError as exc:
        raise _coded_failure(status.HTTP_404_NOT_FOUND, "scheduled_recipe_lost") from exc


async def _stored_recipe_error(
    request: Request,
    job: ScheduledJob,
    recipe_ref: str | None = None,
) -> str | None:
    """Revalidate a persisted binding through the fire-time resolver."""
    error, _fence = await _stored_recipe_check(request, job, recipe_ref)
    return error


async def _stored_recipe_check(
    request: Request,
    job: ScheduledJob,
    recipe_ref: str | None = None,
) -> tuple[str | None, tuple[str, str, str, int] | None]:
    """Revalidate a binding and snapshot the assignment used for restore."""
    effective_ref = recipe_ref if recipe_ref is not None else job.recipe_ref
    if not effective_ref:
        return None, None
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return "scheduled_recipe_lost", None
    try:
        team_id = await user_store.get_user_team_id(job.user_id)
    except Exception as exc:
        logger.warning("scheduled.recipe_team_lookup_failed job=%s err=%s", job.id, exc)
        return "scheduled_recipe_lost", None
    from bowhead_gateway._scheduled_runner import _resolve_job_workflow

    workflow, error = await _resolve_job_workflow(
        request.app,
        replace(job, recipe_ref=effective_ref),
        team_id,
    )
    if error is not None or workflow is None:
        return error or "scheduled_recipe_lost", None
    if workflow.form != "team_subagent":
        return None, None
    try:
        from bowhead_core.agent_assignment_store import AgentAssignmentStore

        assignment_store = AgentAssignmentStore(user_store.db_path)
        schema_state = await assignment_store.runtime_schema_state()
        if schema_state in {"pre_0025", "pre_cutover"}:
            return None, None
        if schema_state != "cutover" or not team_id:
            return "scheduled_recipe_lost", None
        assignment = await assignment_store.get(
            team_id=team_id,
            user_id=job.user_id,
            agent_id=workflow.name_or_id,
        )
    except Exception as exc:
        logger.warning("scheduled.assignment_fence_lookup_failed job=%s err=%s", job.id, exc)
        return "scheduled_recipe_lost", None
    if assignment is None or assignment.personal_state != "active":
        return "scheduled_recipe_lost", None
    return None, (team_id, job.user_id, workflow.name_or_id, assignment.row_version)


async def _prepare_agent_binding(
    request: Request,
    recipe_ref: str | None,
    *,
    user_id: str,
) -> ScheduledAgentBindingSeed | None:
    """Compile a team_subagent recipe into the current immutable release pin."""

    from bowhead_core.scheduled_agent_binding import ScheduledAgentBindingStore
    from bowhead_core.workflow_binding import FORM_TEAM_SUBAGENT, parse_ref

    parsed = parse_ref(recipe_ref)
    if parsed is None or parsed["form"] != FORM_TEAM_SUBAGENT:
        return None
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_update_failed",
        )
    try:
        team_id = await user_store.get_user_team_id(user_id)
        if not team_id:
            raise ScheduledAgentBindingError("scheduled_recipe_lost")
        return await ScheduledAgentBindingStore(
            user_store.db_path,
        ).prepare_current_team_binding(
            team_id=team_id,
            user_id=user_id,
            agent_id=parsed["id_or_name"],
            confirmed_by=user_id,
        )
    except ScheduledAgentBindingError as exc:
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            exc.code,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "scheduled.agent_binding_prepare_failed user=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_update_failed",
        ) from exc


async def _read_agent_binding(
    store: ScheduledJobStore,
    job_id: str,
) -> ScheduledAgentBinding | None:
    from bowhead_core.scheduled_agent_binding import ScheduledAgentBindingStore

    try:
        return await ScheduledAgentBindingStore(store.db_path).read(job_id)
    except Exception as exc:
        logger.warning(
            "scheduled.agent_binding_read_failed job=%s err=%s",
            job_id,
            exc,
        )
        return None


async def _read_agent_bindings(
    store: ScheduledJobStore,
    job_ids: list[str],
) -> dict[str, ScheduledAgentBinding]:
    from bowhead_core.scheduled_agent_binding import ScheduledAgentBindingStore

    try:
        return await ScheduledAgentBindingStore(store.db_path).read_many(job_ids)
    except Exception as exc:
        logger.warning(
            "scheduled.agent_bindings_read_failed jobs=%s err=%s",
            len(job_ids),
            exc,
        )
        return {}


class ScheduledJobCreateDTO(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    cron: str = Field(min_length=5, max_length=80)
    query: str = Field(min_length=1)
    workspace_id: str
    tz: str = "Asia/Shanghai"
    session_strategy: str = "fresh"
    anchor_session_id: str | None = None
    notify_channels: dict[str, Any] = Field(default_factory=dict)
    # scheduled-recipe-runs S1: bind a solidified workflow (JSON: {form, id_or_name}).
    # Validation + flag gating live solely in workflow_binding.validate_and_bind; routes do not decide independently.
    recipe_ref: dict[str, Any] | None = None


class ScheduledJobPatchDTO(BaseModel):
    name: str | None = None
    cron: str | None = None
    status: str | None = None  # active | paused | expired
    query: str | None = None
    tz: str | None = None
    session_strategy: str | None = None  # fresh | anchor
    anchor_session_id: str | None = None
    notify_channels: dict[str, Any] | None = None
    # {} or {"form": ...} binds; None leaves unchanged; {"unbind": true} unbinds.
    recipe_ref: dict[str, Any] | None = None


class ScheduledJobDTO(BaseModel):
    id: str
    user_id: str
    workspace_id: str
    name: str
    cron: str
    tz: str
    query: str
    session_strategy: str
    anchor_session_id: str | None = None
    notify_channels: dict[str, Any]
    status: str
    failure_count: int
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_turn_id: str | None = None
    created_at: str
    updated_at: str
    public_entry_id: str | None = None
    # scheduled-recipe-runs S1: pause reason (column long existed, never sent on the wire); FE distinguishes
    # user pauses from system pauses for invalid/changed workflows, offering rebinding for the latter.
    pause_reason: str | None = None
    recipe_ref: str | None = None
    # scheduled-recipe-runs S1 FR-1.5 / FR-3.2 (116 dogfood P2-1): readable bound-workflow name and author.
    # recipe_ref is JSON {form, id_or_name}; both team forms use unreadable oskill_* ids
    # for id_or_name, preventing FE from showing the bound workflow and author. Server enrichment avoids FE joining three
    # endpoints and supports US-6 delegated-permission visibility at list level (execution via team-solidified workflow X, authored by Y).
    # Unresolvable (deleted/lost access) → both fields None; pause_reason explains invalidation and offers rebinding.
    bound_display_name: str | None = None
    bound_author: str | None = None
    # M3-7: credential-free exact Agent pin.  The prompt and dependency
    # definitions remain server-side; clients only need identity/state.
    agent_binding_state: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    agent_release_id: str | None = None
    agent_binding_confirmed_at: str | None = None


class BindableWorkflowDTO(BaseModel):
    form: str
    id_or_name: str
    display_name: str
    description: str = ""
    author: str | None = None
    author_name: str | None = None  # Readable author name for UI; FE falls back to author if unresolvable.
    is_own: bool = False


class BindableWorkflowListDTO(BaseModel):
    items: list[BindableWorkflowDTO]


class ScheduledJobListDTO(BaseModel):
    items: list[ScheduledJobDTO]


class ScheduledJobRunDTO(BaseModel):
    id: str
    job_id: str
    turn_id: str | None = None
    session_id: str | None = None  # Resolved from turn → View results deep link (/?session=).
    started_at: str
    finished_at: str | None = None
    status: str
    error: str | None = None
    error_diagnostics: dict[str, Any] | None = None


class ScheduledJobRunListDTO(BaseModel):
    items: list[ScheduledJobRunDTO]


def _bound_labels(
    recipe_ref: str | None,
    bindable: dict[tuple[str, str], BindableWorkflowDTO] | None,
) -> tuple[str | None, str | None]:
    """`recipe_ref` JSON → (人话名, 作者名)。解析不到一律 (None, None)。

    数据源是可绑集（`_collect_bindable_workflows`）而不是重跑一遍绑定解析：可绑集就是
    「现在还能绑的东西」，匹配不上恰好等价于「这份流程没了/失权了」——与 fire 时的判定同源，
    不会出现列表说"绑着 X"而实际已经解析不到的自相矛盾。作者只在**非本人**时给：本人的流程
    标自己是噪声，而他人作者正是 US-6 要让人看见的那半句。
    """
    if not recipe_ref or not bindable:
        return (None, None)
    try:
        ref = json.loads(recipe_ref)
    except (TypeError, ValueError):
        return (None, None)
    if not isinstance(ref, dict):
        return (None, None)
    hit = bindable.get((str(ref.get("form") or ""), str(ref.get("id_or_name") or "")))
    if hit is None:
        return (None, None)
    return (hit.display_name, None if hit.is_own else (hit.author_name or hit.author))


def _job_to_dto(
    j: Any,
    bindable: dict[tuple[str, str], BindableWorkflowDTO] | None = None,
    agent_binding: Any | None = None,
) -> ScheduledJobDTO:
    bound_display_name, bound_author = _bound_labels(getattr(j, "recipe_ref", None), bindable)
    return ScheduledJobDTO(
        id=j.id,
        user_id=j.user_id,
        workspace_id=j.workspace_id,
        name=j.name,
        cron=j.cron,
        tz=j.tz,
        query=j.query,
        session_strategy=j.session_strategy,
        anchor_session_id=j.anchor_session_id,
        notify_channels=j.notify_channels,
        status=j.status,
        failure_count=j.failure_count,
        next_run_at=j.next_run_at,
        last_run_at=j.last_run_at,
        last_turn_id=j.last_turn_id,
        created_at=j.created_at,
        updated_at=j.updated_at,
        public_entry_id=getattr(j, "public_entry_id", None),
        pause_reason=getattr(j, "pause_reason", None),
        recipe_ref=getattr(j, "recipe_ref", None),
        bound_display_name=bound_display_name,
        bound_author=bound_author,
        agent_binding_state=(str(agent_binding.state) if agent_binding is not None else None),
        agent_id=(str(agent_binding.agent_id) if agent_binding is not None else None),
        agent_revision_id=(str(agent_binding.revision_ref) if agent_binding is not None else None),
        agent_release_id=(str(agent_binding.release_id) if agent_binding is not None and agent_binding.release_id else None),
        agent_binding_confirmed_at=(str(agent_binding.confirmed_at) if agent_binding is not None else None),
    )


def _run_to_dto(r: Any, session_id: str | None = None) -> ScheduledJobRunDTO:
    code = normalize_scheduled_failure_code(r.error)
    published = publish_scheduled_failure(code) if code is not None else None
    return ScheduledJobRunDTO(
        id=r.id,
        job_id=r.job_id,
        turn_id=r.turn_id,
        session_id=session_id,
        started_at=r.started_at,
        finished_at=r.finished_at,
        status=r.status,
        error=code,
        error_diagnostics=(published.to_wire() if published is not None else None),
    )


def _validate_cron(cron: str) -> None:
    """Reject sub-hourly / unschedulable cron (shared core rule). @manual is allowed."""
    err = cron_validation_error(cron)
    if err:
        raise _coded_failure(
            status.HTTP_400_BAD_REQUEST,
            "scheduled_invalid_cron",
        )


@router.post("/jobs", response_model=ScheduledJobDTO, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_not_readonly)])
async def create_job(
    body: ScheduledJobCreateDTO,
    request: Request,
    store: ScheduledJobStore = Depends(_store),
    # §5.1 entitlement — the dep resolves team from the AUTHENTICATED user (not the X-Bowhead-User-Id/dev
    # fallback used by _user_id), so in local an team-less/dev caller is denied (SEC-8). 101 saas = no-op.
    _feat: None = Depends(require_feature_licensed("scheduling")),
) -> ScheduledJobDTO:
    _validate_cron(body.cron)
    await _require_workspace_access(
        request,
        body.workspace_id,
        SpaceOperation.WRITE,
    )
    # Batch 2/c9 (PRD §3.9 decision 10): per-user mode **snapshots at creation** the owner's current Datarc
    # workspace into the job, explicitly fixing the routine's data domain so it cannot drift with owner preferences.
    _snapshot_ws: str | None = None

    try:
        _umem = getattr(request.app.state, "user_memory_store", None)
        _chosen = (
            await _umem.get_tool_context(
                _user_id(request),
                "datarc",
            )
            if _umem is not None
            else None
        )
        _snapshot_ws = str((_chosen or {}).get("id") or "") or None
        public_context = current_public_access_context()
        public_entry_id = public_context.entry_id if public_context else None
        if public_entry_id:
            public_entry_store = getattr(request.app.state, "public_entry_store", None)
            user_store = getattr(request.app.state, "user_store", None)
            workspace = await user_store.get_workspace(body.workspace_id) if user_store is not None else None
            team_id = workspace.get("team_id") if workspace else None
            try:
                entry = await public_entry_store.get(public_entry_id) if public_entry_store is not None else None
            except Exception:
                logger.exception("scheduled.entry_snapshot_unavailable")
                entry = None
            if entry is None or entry.status != "active" or entry.team_id != team_id:
                public_entry_id = None
        _recipe_ref = await _bind_recipe_ref(request, body.recipe_ref, user_id=_user_id(request))
        _agent_binding = await _prepare_agent_binding(
            request,
            _recipe_ref,
            user_id=_user_id(request),
        )
        job = await store.create(
            user_id=_user_id(request),
            workspace_id=body.workspace_id,
            name=body.name,
            cron=body.cron,
            query=body.query,
            tz=body.tz,
            session_strategy=body.session_strategy,
            anchor_session_id=body.anchor_session_id,
            notify_channels=body.notify_channels,
            datarc_workspace_id=_snapshot_ws,
            public_entry_id=public_entry_id,
            recipe_ref=_recipe_ref,
            agent_binding=_agent_binding,
        )
    except HTTPException:
        raise
    except ScheduledAgentBindingError as exc:
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            exc.code,
        ) from exc
    except ValueError as exc:
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            "scheduled_job_limit_reached",
        ) from exc
    except Exception as exc:
        logger.exception(
            "scheduled.create_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_create_failed",
        ) from exc
    # #126 Step 2: calculate next_run_at on creation for immediate scheduler pickup (interpret cron in job.tz).
    from datetime import datetime

    nxt = next_run_for_job(body.cron, body.tz, datetime.now(tz=UTC))
    if nxt is not None:
        try:
            await store.set_next_run_at(job.id, nxt.isoformat())
            job.next_run_at = nxt.isoformat()
        except Exception as exc:
            logger.exception(
                "scheduled.create_next_run_failed job=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            raise _coded_failure(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "scheduled_job_create_failed",
            ) from exc
    return _job_to_dto(
        job,
        agent_binding=await _read_agent_binding(store, job.id),
    )


@router.get("/bindable-workflows", response_model=BindableWorkflowListDTO)
async def list_bindable_workflows(request: Request) -> BindableWorkflowListDTO:
    """路由壳：聚合逻辑在 `_collect_bindable_workflows`（列表页的绑定回显复用同一份）。"""
    return BindableWorkflowListDTO(items=await _collect_bindable_workflows(request))


async def _collect_bindable_workflows(request: Request) -> list[BindableWorkflowDTO]:
    """列出调用者**可绑定**的固化流程（个人 + 团队可见集）。

    服务端聚合而不是让前端串三个端点：可绑性判定（enabled / visibility / 是不是固化产物）
    是授权语义，前端抄一份迟早与 fire 时的解析漂移；而且个人 skill 那侧没有「这是固化流程」
    的判别字段可直接过滤，需要读 source_type + source_turn_id。

    没有可绑流程时返空列表 —— 前端据此不渲染选择器，表单其余部分照常。
    """
    from bowhead_core.workflow_binding import (
        FORM_PERSONAL_SKILL,
        FORM_TEAM_SKILL,
        FORM_TEAM_SUBAGENT,
    )

    user_id = _user_id(request)
    app_state = request.app.state
    items: list[BindableWorkflowDTO] = []

    # ① Personal: list only **chat-solidified outputs** (user_generated); ordinary uploaded skills are not workflows.
    skill_store = getattr(app_state, "skill_instance_store", None)
    if skill_store is not None:
        try:
            for rec in await skill_store.list_for_user(user_id=user_id):
                if not getattr(rec, "enabled", False) or getattr(rec, "source_type", "") != "user_generated":
                    continue
                items.append(
                    BindableWorkflowDTO(
                        form=FORM_PERSONAL_SKILL,
                        id_or_name=rec.name,
                        display_name=rec.name,
                        description=(getattr(rec, "description", "") or "")[:280],
                        author=None,
                        is_own=True,
                    )
                )
        except Exception as exc:
            logger.warning("scheduled.bindable_personal_failed err=%s", exc)

    # ② / ③ Team: team_skill + team_subagent; creator_only is visible only to its creator.
    user_store = getattr(app_state, "user_store", None)
    team_id = None
    if user_store is not None:
        try:
            team_id = await user_store.get_user_team_id(user_id)
        except Exception as exc:
            logger.warning("scheduled.bindable_team_lookup_failed err=%s", exc)
    # Author display names: UI should not show u_xxx. Resolve all needed user_ids to readable names once; leave None on failure
    # so FE falls back to ids (awkward display is preferable to dropping the whole row).
    author_names: dict[str, str] = {}

    async def _author_name(uid: str | None) -> str | None:
        if not uid or user_store is None:
            return None
        if uid in author_names:
            return author_names[uid]
        try:
            u = await user_store.get_user(uid)
        except Exception:
            return None
        name = (getattr(u, "display_name", None) or getattr(u, "email", None) or "") if u else ""
        if name:
            author_names[uid] = name
        return name or None

    if user_store is not None and team_id:
        try:
            for rec in await user_store.list_team_skills(team_id, only_enabled=True):
                if (getattr(rec, "visibility", "team_wide") or "team_wide") != "team_wide" and rec.created_by != user_id:
                    continue
                items.append(
                    BindableWorkflowDTO(
                        form=FORM_TEAM_SKILL,
                        id_or_name=rec.id,
                        display_name=getattr(rec, "display_name", "") or rec.name,
                        description=(getattr(rec, "description", "") or "")[:280],
                        author=rec.created_by,
                        author_name=await _author_name(rec.created_by),
                        is_own=rec.created_by == user_id,
                    )
                )
        except Exception as exc:
            logger.warning("scheduled.bindable_team_skills_failed err=%s", exc)
        try:
            from bowhead_core.agents.plan_admission import team_agent_runtime_allowed

            for rec in await user_store.list_team_subagents(team_id, only_enabled=True):
                if not await team_agent_runtime_allowed(
                    user_store,
                    team_id=team_id,
                    user_id=user_id,
                    agent_id=str(rec.id),
                ):
                    continue
                items.append(
                    BindableWorkflowDTO(
                        form=FORM_TEAM_SUBAGENT,
                        id_or_name=rec.id,
                        display_name=getattr(rec, "display_name", "") or rec.name,
                        description=(getattr(rec, "description", "") or "")[:280],
                        author=rec.created_by,
                        author_name=await _author_name(rec.created_by),
                        is_own=rec.created_by == user_id,
                    )
                )
        except Exception as exc:
            logger.warning("scheduled.bindable_subagents_failed err=%s", exc)

    return items


@router.get("/jobs", response_model=ScheduledJobListDTO)
async def list_jobs(
    request: Request,
    store: ScheduledJobStore = Depends(_store),
) -> ScheduledJobListDTO:
    try:
        jobs = await store.list_for_user(_user_id(request))
    except Exception as exc:
        logger.exception(
            "scheduled.list_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_load_failed",
        ) from exc
    # Binding display (P2-1): aggregate bindable workflows only when bindings actually exist; unbound deployments incur no extra IO.
    # Silently degrade to no binding name: unavailable enrichment must not break the entire list page.
    bindable: dict[tuple[str, str], BindableWorkflowDTO] | None = None
    if any(getattr(job, "recipe_ref", None) for job in jobs):
        try:
            bindable = {(w.form, w.id_or_name): w for w in await _collect_bindable_workflows(request)}
        except Exception as exc:
            logger.warning("scheduled.bound_enrich_failed err=%s", exc)
    visible_jobs: list[Any] = []
    for job in jobs:
        try:
            await _require_workspace_access(
                request,
                job.workspace_id,
                SpaceOperation.READ_HISTORY,
            )
        except HTTPException as exc:
            if exc.status_code in {
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
            }:
                continue
            raise
        visible_jobs.append(job)
    agent_bindings = await _read_agent_bindings(
        store,
        [job.id for job in visible_jobs],
    )
    visible = [
        (
            _job_to_dto(
                job,
                bindable,
                agent_binding=agent_bindings.get(job.id),
            )
        )
        for job in visible_jobs
    ]
    return ScheduledJobListDTO(items=visible)


@router.get("/jobs/{job_id}", response_model=ScheduledJobDTO)
async def get_job(
    job_id: str,
    request: Request,
    store: ScheduledJobStore = Depends(_store),
) -> ScheduledJobDTO:
    job = await _owned_job(store=store, job_id=job_id, request=request)
    await _require_workspace_access(
        request,
        job.workspace_id,
        SpaceOperation.READ_HISTORY,
    )
    return _job_to_dto(
        job,
        agent_binding=await _read_agent_binding(store, job.id),
    )


@router.patch("/jobs/{job_id}", response_model=ScheduledJobDTO, dependencies=[Depends(require_not_readonly)])
async def patch_job(
    job_id: str,
    body: ScheduledJobPatchDTO,
    request: Request,
    store: ScheduledJobStore = Depends(_store),
) -> ScheduledJobDTO:
    job = await _owned_job(store=store, job_id=job_id, request=request)
    await _require_workspace_access(
        request,
        job.workspace_id,
        SpaceOperation.EXECUTE,
    )
    # Un-pausing must honor the active-job cap (create enforces it; patch did not → un-pause could exceed it).
    if body.status == "active" and job.status != "active":
        from bowhead_core.scheduled_store import MAX_ACTIVE_JOBS_PER_USER

        try:
            active_count = await store.count_active(job.user_id)
        except Exception as exc:
            logger.exception(
                "scheduled.active_count_failed error_type=%s",
                type(exc).__name__,
            )
            raise _coded_failure(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "scheduled_job_update_failed",
            ) from exc
        if active_count >= MAX_ACTIVE_JOBS_PER_USER:
            raise _coded_failure(
                status.HTTP_409_CONFLICT,
                "scheduled_job_limit_reached",
            )
    if body.cron is not None:
        _validate_cron(body.cron)
    # Binding changes share create's single validation entry. ⚠ Omitting this means DTO accepts recipe_ref but server
    # silently drops it: edit-page rebinding/unbinding/reconfirmation all fail while FE appears successful.
    _recipe_ref = await _bind_recipe_ref(request, body.recipe_ref, user_id=_user_id(request))
    effective_recipe_ref = job.recipe_ref if body.recipe_ref is None else (_recipe_ref or None)
    from bowhead_core.workflow_binding import FORM_TEAM_SUBAGENT, parse_ref

    effective_parsed = parse_ref(effective_recipe_ref)
    # Explicitly re-enabling a paused Team Agent routine is a reconfirm action.
    # Refresh both halves of the pin: normalized exact binding below and the
    # legacy workflow hash injected into Lead. Updating only one makes the old
    # bound_sha reject the newly confirmed release on this PATCH or next fire.
    if body.status == "active" and body.recipe_ref is None and effective_parsed is not None and effective_parsed["form"] == FORM_TEAM_SUBAGENT:
        _recipe_ref = await _bind_recipe_ref(
            request,
            {
                "form": FORM_TEAM_SUBAGENT,
                "id_or_name": effective_parsed["id_or_name"],
            },
            user_id=_user_id(request),
        )
        effective_recipe_ref = _recipe_ref or None
        effective_parsed = parse_ref(effective_recipe_ref)
    _agent_binding = None
    if body.recipe_ref is not None or body.status == "active":
        _agent_binding = await _prepare_agent_binding(
            request,
            effective_recipe_ref,
            user_id=_user_id(request),
        )
    _clear_agent_binding = body.recipe_ref is not None and (effective_parsed is None or effective_parsed["form"] != FORM_TEAM_SUBAGENT)
    assignment_fence: tuple[str, str, str, int] | None = None
    if body.status == "active":
        recipe_error, assignment_fence = await _stored_recipe_check(request, job, effective_recipe_ref) if effective_recipe_ref else (None, None)
        if recipe_error is not None:
            raise _coded_failure(
                status.HTTP_409_CONFLICT,
                "scheduled_recipe_lost",
            )
    try:
        patched = await store.patch(
            job_id,
            name=body.name,
            cron=body.cron,
            status=body.status,
            query=body.query,
            tz=body.tz,
            session_strategy=body.session_strategy,
            anchor_session_id=body.anchor_session_id,
            notify_channels=body.notify_channels,
            recipe_ref=_recipe_ref,
            agent_binding=_agent_binding,
            clear_agent_binding=_clear_agent_binding,
            expected_updated_at=(job.updated_at if body.status == "active" else None),
            expected_agent_assignment=assignment_fence,
        )
        if body.status == "active" and not patched:
            raise _coded_failure(
                status.HTTP_409_CONFLICT,
                "scheduled_job_update_failed",
            )
    except HTTPException:
        raise
    except ScheduledAgentBindingError as exc:
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            exc.code,
        ) from exc
    except ValueError as exc:
        raise _coded_failure(
            status.HTTP_400_BAD_REQUEST,
            "scheduled_job_update_failed",
        ) from exc
    except Exception as exc:
        logger.exception(
            "scheduled.update_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_update_failed",
        ) from exc
    # If the schedule (cron or tz) changed — or the job is being un-paused — recompute next_run_at, else
    # an edited schedule wouldn't take effect, and a re-activated job whose next_run_at was stale/NULL
    # would never be picked by the scheduler. (@manual → next_run_for_job returns None → cleared.)
    if body.cron is not None or body.tz is not None or body.status == "active":
        from datetime import datetime

        try:
            sched = await store.get(job_id)
            if sched is not None:
                nxt = next_run_for_job(
                    sched.cron,
                    sched.tz,
                    datetime.now(tz=UTC),
                )
                await store.set_next_run_at(
                    job_id,
                    nxt.isoformat() if nxt is not None else None,
                )
        except Exception as exc:
            logger.exception(
                "scheduled.next_run_update_failed error_type=%s",
                type(exc).__name__,
            )
            raise _coded_failure(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "scheduled_job_update_failed",
            ) from exc
    try:
        updated = await store.get(job_id)
    except Exception as exc:
        logger.exception(
            "scheduled.updated_job_load_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_update_failed",
        ) from exc
    if updated is None:
        raise _coded_failure(
            status.HTTP_404_NOT_FOUND,
            "scheduled_job_not_found",
        )
    return _job_to_dto(
        updated,
        agent_binding=await _read_agent_binding(store, job_id),
    )


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_not_readonly)])
async def delete_job(
    job_id: str,
    request: Request,
    store: ScheduledJobStore = Depends(_store),
) -> None:
    job = await _owned_job(store=store, job_id=job_id, request=request)
    await _require_workspace_access(
        request,
        job.workspace_id,
        SpaceOperation.WRITE,
    )
    try:
        result = await store.delete_with_clarification_invalidation(job_id)
    except Exception as exc:
        logger.exception(
            "scheduled.delete_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_delete_failed",
        ) from exc
    if not result.deleted:
        if result.cause in {
            "run_active",
            "resolution_in_flight",
            "turn_missing",
            "clarification_missing",
            "terminal_protocol_v1",
        }:
            raise _coded_failure(
                status.HTTP_409_CONFLICT,
                "scheduled_job_run_active",
            )
        if result.cause == "not_found":
            raise _coded_failure(
                status.HTTP_404_NOT_FOUND,
                "scheduled_job_not_found",
            )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_job_delete_failed",
        )
    if result.invalidations:
        from bowhead_gateway.clarification_invalidation import (
            publish_pending_clarification_invalidations,
        )

        with contextlib.suppress(Exception):
            await publish_pending_clarification_invalidations(
                request.app,
                request_ids=[item.request_id for item in result.invalidations],
            )
    # Deleted jobs invalidate their pending actions too (116 dogfood P2-4); otherwise the bell keeps requesting
    # scheduled-task information, deep-linking to a session with no job/run ownership, while scheduled.paused
    # remains pending for all admins. Best effort: deletion succeeded; incomplete notification cleanup must not become 5xx.
    notify = getattr(request.app.state, "notification_service", None)
    if notify is not None:
        try:
            closed = await notify.resolve_scheduled_job_notifications(job)
            if closed:
                logger.info("scheduled.delete_resolved_notifications job=%s n=%d", job_id, closed)
        except Exception as exc:
            logger.warning("scheduled.delete_notification_cleanup_failed job=%s err=%s", job_id, exc)


@router.get("/jobs/{job_id}/runs", response_model=ScheduledJobRunListDTO)
async def list_runs(
    job_id: str,
    request: Request,
    store: ScheduledJobStore = Depends(_store),
) -> ScheduledJobRunListDTO:
    job = await _owned_job(store=store, job_id=job_id, request=request)
    await _require_workspace_access(
        request,
        job.workspace_id,
        SpaceOperation.READ_HISTORY,
    )
    try:
        runs = await store.list_runs(job_id)
    except Exception as exc:
        logger.exception(
            "scheduled.runs_load_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_runs_load_failed",
        ) from exc
    # resolve each run's session (turn → session) so the UI can deep-link the result (/?session=)
    sess_store = getattr(request.app.state, "session_store", None)
    sess_map: dict[str, str] = {}
    if sess_store is not None and hasattr(sess_store, "session_ids_for_turns"):
        try:
            sess_map = await sess_store.session_ids_for_turns([r.turn_id for r in runs if r.turn_id])
        except Exception as exc:
            logger.warning("scheduled.run_session_lookup_failed job=%s err=%s", job_id, exc)
    return ScheduledJobRunListDTO(
        items=[_run_to_dto(r, sess_map.get(r.turn_id or "")) for r in runs],
    )


class RunNowResultDTO(BaseModel):
    ok: bool = True
    run_id: str
    status: str = "running"
    note: str = "run dispatched; poll GET /jobs/{id}/runs for the outcome"


@router.post("/jobs/{job_id}/run-now", response_model=RunNowResultDTO, dependencies=[Depends(require_not_readonly)])
async def run_now(
    job_id: str,
    request: Request,
    store: ScheduledJobStore = Depends(_store),
) -> RunNowResultDTO:
    """Dispatch the job's turn now, asynchronously. A turn takes ~tens of seconds, so we start it as a
    background task and return the run_id immediately — the client polls GET /jobs/{id}/runs for status.

    Routed through ``run_scheduled_turn`` (NOT ``orchestrator.run_turn`` directly) so the entitlement +
    credit gates are preserved — the "forgotten Nth caller" regression LICENSING §3.6 warns about."""
    job = await _owned_job(store=store, job_id=job_id, request=request)
    await _require_workspace_access(
        request,
        job.workspace_id,
        SpaceOperation.EXECUTE,
    )
    recipe_error = await _stored_recipe_error(request, job)
    if recipe_error is not None:
        try:
            await store.patch(
                job.id,
                status="paused",
                pause_reason=recipe_error,
                expected_updated_at=job.updated_at,
            )
        except Exception as exc:
            logger.warning("scheduled.run_now_auth_pause_failed job=%s err=%s", job.id, exc)
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            "scheduled_recipe_lost",
        )
    # Supersede old awaiting runs first (S3 · FR-5.5 · 116 dogfood P1-B). Manual reruns are the owner's escape route:
    # if a job asks for definitions every period and run-now always returns 409, owners are locked out of their routines; @manual
    # jobs are permanently unusable (next_run_at always NULL → never due → tick's expiry branch cannot reach them).
    # Superseding records skipped/scheduled_input_superseded (**no strike**: deliberate replacement is not failure) and invalidates old cards,
    # preventing deep links from resuming an unclaimed run.
    await _supersede_awaiting_runs(request, store, job)
    # Superseding an awaiting run advances the job row. Reload before the
    # transaction fence so our own state transition is not mistaken for an
    # external authorization race; then revalidate the binding on that latest
    # row before claiming a new run.
    job = await _owned_job(store=store, job_id=job_id, request=request)
    recipe_error = await _stored_recipe_error(request, job)
    if recipe_error is not None:
        with contextlib.suppress(Exception):
            await store.patch(
                job.id,
                status="paused",
                pause_reason=recipe_error,
                expected_updated_at=job.updated_at,
            )
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            "scheduled_recipe_lost",
        )
    try:
        has_active_run = await store.has_active_run(job_id)
    except Exception as exc:
        logger.exception(
            "scheduled.run_state_load_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_run_dispatch_failed",
        ) from exc
    if has_active_run:
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            "scheduled_run_conflict",
        )
    try:
        run = await store.start_run(
            job_id,
            source="run_now",
            expected_job_updated_at=job.updated_at,
        )
    except ActiveRunExistsError as exc:
        # Lost the race against a concurrent run-now / cron tick: the DB unique index is the real
        # guard (has_active_run above is only a fast-path). Surface the same 409.
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            "scheduled_run_conflict",
        ) from exc
    except ScheduledRunSourceError as exc:
        if exc.cause == "not_found":
            raise _coded_failure(
                status.HTTP_404_NOT_FOUND,
                "scheduled_job_not_found",
            ) from exc
        raise _coded_failure(
            status.HTTP_409_CONFLICT,
            "scheduled_run_conflict",
        ) from exc
    except Exception as exc:
        logger.exception(
            "scheduled.run_dispatch_failed error_type=%s",
            type(exc).__name__,
        )
        raise _coded_failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scheduled_run_dispatch_failed",
        ) from exc
    task = asyncio.create_task(_run_now_and_finish(request.app, store, job, run.id))
    _inflight_run_now.add(task)
    task.add_done_callback(_inflight_run_now.discard)
    return RunNowResultDTO(run_id=run.id)
