"""Planning Mode REST surface.

编辑/审批/状态类端点已接 `_current_user` + ownership 收口（生产可用）；仅
body 传 id 的 dev 便捷路由仍留 `BOWHEAD_AUTH_MODE=dev` 双门（见各端点）。
behind BOWHEAD_PLANNING_MODE。（2026-07-21 概念术语审计：更正过时的
「dev dogfood-only / no auth dep」自述——与实现不符。）

POST /api/v1/plans              author plan from query (real LLM) + reify + persist
GET  /api/v1/plans/{plan_id}    reload-from-state (status + steps)
POST /api/v1/plans/{plan_id}/approve   execute plan as a turn (M1: foreground await)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import Any

from bowhead_core.model_clock import viewer_clock_scope
from bowhead_core.plan_authoring import (
    PlanAuthoringError,
    amend_plan,
    author_plan,
    author_step_criteria,
    to_plan_steps,
)
from bowhead_core.plan_reify import assign_levels, validate_agents
from bowhead_core.plan_store import DeliverableKind, PlanStatus, PlanStep, PlanStore, StepStatus, new_step
from bowhead_core.plan_verify import outcome_from_json
from bowhead_core.planning_v2 import planning_verify_enabled
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.types import TurnRequest
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway.access_context import (
    build_access_context_for_user_from_request,
    has_workspace_access_source,
)
from bowhead_gateway.routes._deps import team_router_or_409
from bowhead_gateway.routes.auth import User, _current_user

# C1: move _planning_enabled to shared planning_flags (previously duplicated in plans.py + chat.py).
from bowhead_gateway.routes.planning_flags import planning_enabled as _planning_enabled
from bowhead_gateway.viewer_time_zone import viewer_time_zone_name

router = APIRouter(prefix="/api/v1/plans", tags=["planning"])

# P2 (CTO review fast-follow): amend is production-reachable and calls an actual LLM each time. Simple per-user
# fixed-window rate limiting prevents abuse (authed users repeatedly amend without changing plan state → unmetered provider cost).
# 116 single-instance memory suffices (use a shared store for multiple instances). Normal edits stay far below 10/minute.
_AMEND_RL: dict[str, list[float]] = {}
_AMEND_RL_MAX = 10
_AMEND_RL_WINDOW_S = 60.0


def _amend_rate_ok(user_id: str) -> bool:
    """True=放行。滑动窗口 _AMEND_RL_MAX 次 / _AMEND_RL_WINDOW_S 秒。"""
    now = time.monotonic()
    hist = [t for t in _AMEND_RL.get(user_id, ()) if now - t < _AMEND_RL_WINDOW_S]
    if len(hist) >= _AMEND_RL_MAX:
        _AMEND_RL[user_id] = hist
        return False
    hist.append(now)
    _AMEND_RL[user_id] = hist
    return True


def _dev_enabled() -> bool:
    """⚠️ 这些是 **dev dogfood** 路由(ids 走 body,无 auth dep)。双重门:planning flag ON
    **且** AUTH_MODE=dev —— 即使 prod 误开 BOWHEAD_PLANNING_MODE,非 dev-auth 环境也 404,
    杜绝 body-id 跨租户执行漏洞(CTO P0-2)。真用户入口走 in-turn hook1(authed),不走这里。
    生产级 authed approve 端点 = M1 收尾项。"""
    # default ON (0.71.5 re-enable); set BOWHEAD_PLANNING_MODE=0 to kill.
    # (AUTH_MODE==dev gate below still keeps these dev body-id routes off in prod.)
    return os.environ.get("BOWHEAD_PLANNING_MODE", "1") == "1" and os.environ.get("BOWHEAD_AUTH_MODE", "") == "dev"


_PROVENANCE_SEED_TURNS = 20


async def _session_user_texts(app: Any, session_id: str) -> str:
    """该会话最近的用户消息原文，给计划执行轮的 URL 来源闸做种子（只取用户本人写的字）。失败返回空串 = 不额外放行。"""
    store = getattr(app.state, "session_store", None)
    if store is None or not session_id:
        return ""
    try:
        turns = await store.list_turns(session_id=session_id, limit=_PROVENANCE_SEED_TURNS)
    except Exception:
        logging.getLogger("bowhead.gateway").warning("plans.provenance_seed_failed session=%s", session_id, exc_info=True)
        return ""
    return "\n".join(t.user_text for t in turns if t.user_text and t.user_text != "(planning-mode execution)")


async def _require_plan_space(req: Request, *, user_id: str, workspace_id: str, operation: SpaceOperation) -> None:
    """计划所在空间也要求调用者**现在**仍是成员（与报告 / turn 同一个统一授权入口）。

    调用方传请求里的 ``workspace_id``：``get_plan`` 的 WHERE 已要求计划所在空间与它相等。
    ownership（``get_plan`` 的 WHERE 里有 user_id + workspace_id）只证明「这是你的计划」，不证明「你还在这个空间」——
    被移出的人此前照样能读计划标题步骤、改状态、触发 amend / replan（调用模型、产生费用）。
    授权失败一律按「计划不存在」404（与未授权时同一个拒绝口径，不泄露计划是否存在）；生命周期拒绝（如归档空间里的写，409）原样上抛。
    ⚠ 取消（``/cancel``）**不**接这道判定：属主被移出后仍能停掉自己的计划，止住后续费用。
    """
    if not has_workspace_access_source(req):
        return
    # ★ Check the **space's owning team** (same as turns): omitting expected_team_id uses users.team_id (primary team),
    # falsely classifying multi-team users reading their own secondary-team plans as cross_team.
    workspace_team_id = await req.app.state.user_store.get_workspace_team_id(workspace_id)
    try:
        await build_access_context_for_user_from_request(
            req,
            user_id=user_id,
            workspace_id=workspace_id,
            operation=operation,
            expected_team_id=workspace_team_id or None,
        )
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(404, "plan not found") from None
        raise


async def _run_with_plan_claim_heartbeat(
    app: Any,
    store: Any,
    plan_id: str,
    turn_request: TurnRequest,
    *,
    heartbeat_interval_s: float = 10.0,
) -> Any:
    """Run a dev Plan while renewing its Turn-token claim.

    The shared ``spawn_turn`` path owns the chat dispatch lease. This wrapper
    additionally refreshes the PlanStore execution token; losing that exact
    token cancels the old worker before it can continue Plan side effects.
    """
    from bowhead_gateway.turn_service import (
        admit_legacy_chat_execution,
        spawn_turn,
    )

    orchestrator = app.state.orchestrator
    run_task: asyncio.Task[Any] | None = None
    run_error: BaseException | None = None
    try:
        admitted_bus = await admit_legacy_chat_execution(
            orchestrator,
            turn_request,
        )
        run_task = await spawn_turn(
            orchestrator,
            turn_request,
            name_prefix=f"dev_plan_run:{plan_id}",
            admitted_legacy_bus=admitted_bus,
            cleanup_grace_s=0,
        )
        while True:
            done, _ = await asyncio.wait(
                {run_task},
                timeout=heartbeat_interval_s,
            )
            if run_task in done:
                result = await run_task
                if result is None:
                    raise RuntimeError("dev_plan_result_unavailable")
                return result
            try:
                renewed = await store.bind_execution_turn(
                    plan_id,
                    turn_request.turn_id,
                )
            except Exception:
                logging.getLogger("bowhead.gateway").exception(
                    "dev plan claim heartbeat failed plan_id=%s turn_id=%s",
                    plan_id,
                    turn_request.turn_id,
                )
                renewed = False
            if not renewed:
                try:
                    tail_renewed = await store.heartbeat_execution_tail(
                        plan_id,
                        turn_request.turn_id,
                    )
                except Exception:
                    logging.getLogger("bowhead.gateway").exception(
                        "dev plan tail heartbeat failed plan_id=%s turn_id=%s",
                        plan_id,
                        turn_request.turn_id,
                    )
                    tail_renewed = False
                # PlanDriver may commit its outcome before Lead finishes report
                # composition and Turn terminal output.  The exact Turn token is
                # renewed atomically during that tail; a cleared/replaced token
                # or durable cancellation returns false and fences this worker.
                if not tail_renewed:
                    raise RuntimeError("plan_execution_claim_lost")
    except BaseException as exc:
        run_error = exc
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []

        async def _finish_plan_execution_tail() -> None:
            if run_task is not None and not run_task.done():
                run_task.cancel("plan_execution_claim_lost")
                with contextlib.suppress(BaseException):
                    await run_task
            if run_task is None:
                try:
                    # Admission failed before the shared worker existed, so it
                    # cannot own the Plan token cleanup. Once a task exists,
                    # its normal tail or durable recovery is the sole owner.
                    await store.release_execution_claim(
                        plan_id,
                        turn_request.turn_id,
                    )
                except BaseException as release_error:
                    cleanup_errors.append(("release execution claim", release_error))
                    logging.getLogger("bowhead.gateway").exception(
                        "dev plan preworker claim release failed plan_id=%s turn_id=%s",
                        plan_id,
                        turn_request.turn_id,
                    )

        cleanup_task = asyncio.create_task(
            _finish_plan_execution_tail(),
            name=f"dev_plan_tail_cleanup:{plan_id}:{turn_request.turn_id}",
        )
        cleanup_cancel: asyncio.CancelledError | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as cancel_error:
                if cleanup_cancel is None:
                    cleanup_cancel = cancel_error
                else:
                    cleanup_cancel.add_note(
                        f"plan tail cleanup was cancelled again: {cancel_error!r}",
                    )
            except BaseException as cleanup_error:
                cleanup_errors.append(("run cleanup task", cleanup_error))
                break

        if run_error is not None:
            if cleanup_cancel is not None:
                run_error.add_note(
                    f"plan tail cleanup was also cancelled: {cleanup_cancel!r}",
                )
            for operation, cleanup_error in cleanup_errors:
                run_error.add_note(
                    f"plan tail failed to {operation}: {cleanup_error!r}",
                )
        else:
            if cleanup_cancel is not None:
                raise cleanup_cancel
            for _operation, cleanup_error in cleanup_errors:
                if isinstance(cleanup_error, asyncio.CancelledError) or not isinstance(
                    cleanup_error,
                    Exception,
                ):
                    raise cleanup_error


class CreatePlanBody(BaseModel):
    query: str
    workspace_id: str
    user_id: str
    session_id: str
    team_id: str | None = None
    data_context: str | None = None  # Routing hint (DD: uploaded data-room documents → kb-analyst).


class ApproveBody(BaseModel):
    workspace_id: str
    user_id: str
    member_uid: str = ""
    team_id: str | None = None


# A (editable): FE sends all edited steps → backend validates + recalculates levels + replaces in one transaction.
class EditStep(BaseModel):
    id: str = ""  # Reuse plan_proposed/amend preview id; generate server-side if empty.
    agent: str
    objective: str = ""
    deliverable: str = ""
    depends_on: list[str] = []
    needs_corpus: bool = False
    # None = old clients omitted it, retain same-id constraints; False = user explicitly cleared it.
    needs_web: bool | None = None
    included: bool = True
    origin: str = "initial"
    # graph-mode batch 1 implementation review B2/F-1: None = client omission → restore existing criteria from the database
    # by step id (structured edits must not silently erase user-approved criteria); [] = explicitly absent.
    success_criteria: list[str] | None = None
    agent_choice: dict[str, Any] | None = None


class EditStepsBody(BaseModel):
    workspace_id: str
    user_id: str
    steps: list[EditStep]
    expected_rev: int | None = Field(default=None, strict=True, ge=1)


class AmendBody(BaseModel):
    workspace_id: str
    user_id: str
    instruction: str


class SkipStepBody(BaseModel):
    workspace_id: str
    expected_rev: int | None = Field(default=None, strict=True, ge=1)


class CancelBody(BaseModel):
    workspace_id: str


def _step_view(s: Any) -> dict[str, Any]:
    # graph-mode batch 1 (review B5/C1): serialization is an explicit allowlist; explicitly forward each new column here,
    # or persisted criteria/verify never reach FE.
    verify: dict[str, Any] | None = None
    _vo = outcome_from_json(getattr(s, "verify_json", None))
    if _vo is not None:
        verify = {
            "verdict": _vo.verdict,
            "unmet": list(_vo.unmet_texts()),
            "note": _vo.note,
            # Per-criterion status + **evidence**. Previously only unmet (original criteria) and outcome-level
            # note (populated only for skipped) were returned, so FE could only show ✕Unmet,
            # without identifying which criterion or why. 2026-08-15 measurements on 116: all three criteria were unmet,
            # but users saw three identical dots + a ✕, and note was an empty string.
            #
            # Evidence **was present all along**: plan_verify's prompt explicitly requires
            # one sentence (≤40 characters) in note explaining what is missing when unmet; parsing retains it (capped at 80 characters),
            # to_json persists it, but this API boundary dropped it. A typical case of present but hidden data.
            "items": [{"text": i.text, "status": i.status, "note": i.note} for i in _vo.items],
        }
    return {
        "id": s.id,
        "agent": s.agent_name,
        "objective": s.objective,
        "deliverable": s.deliverable,
        "seq": s.seq,
        "level": s.level,
        "status": s.status,
        "depends_on": list(s.depends_on),
        "needs_corpus": s.needs_corpus,
        "needs_web": getattr(s, "needs_web", False),
        "included": s.included,
        "origin": s.origin,
        "error_kind": s.error_kind,
        # Batch 3 (review R2-F8): forward error; after F5, failed steps' FE state had only raw kind strings,
        # while BlockedDetail/revision confirmation need readable reasons. Truncate to prevent huge clarification JSON strings.
        "error": (str(s.error)[:300] if getattr(s, "error", None) else None),
        "success_criteria": list(getattr(s, "success_criteria", ()) or ()),
        "verify": verify,
        # Batch 3: revision pointers (FE rendering filters/lineage) and actual upstream inputs (batch 5 visualization data).
        "superseded_by": getattr(s, "superseded_by", None),
        "input_manifest": _manifest_view(getattr(s, "input_manifest_json", None)),
        "agent_choice": _agent_choice_view(getattr(s, "agent_choice", None)),
    }


def _agent_choice_view(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    try:
        value = json.loads(choice.canonical_json())
    except (AttributeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _manifest_view(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


@router.post("")
async def create_plan(
    body: CreatePlanBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    if not _dev_enabled():
        raise HTTPException(404, "planning mode disabled")
    app = req.app
    store = app.state.plan_store
    registry = getattr(app.state.orchestrator, "multi_agent_registry", None)
    if registry is None:
        raise HTTPException(503, "multi_agent_registry unavailable")
    # credential-readiness D2.2: planner must use the team router (process-level Router is an
    # assembly placeholder, RefusalAdapter in prod). Derive team_id from _current_user identity,
    # never trust body (completes the server-derived identity missing from this file's :266 comment).
    planner_router = await team_router_or_409(
        app.state,
        user.team_id,
        entrypoint="plan",
    )

    roster, agent_caps, agent_prod, role_roster, role_profiles = await _actor_planner_roster(
        app,
        registry=registry,
        workspace_id=body.workspace_id,
        user=user,
    )
    # delivery-surface C3: planner also determines deliverable form (same as lead_orchestrator proposals).
    plan_meta: dict[str, Any] = {}
    authored = await author_plan(
        planner_router,
        user_text=body.query,
        agent_roster=roster,
        agent_capabilities=agent_caps,
        agent_produces=agent_prod,
        data_context=body.data_context or "",
        role_candidate_roster=role_roster,
        role_capable_profiles=role_profiles,
        trace_id="plan_create",
        plan_meta_out=plan_meta,
    )
    if not authored:
        raise HTTPException(422, "planner produced no steps")

    try:
        steps = to_plan_steps(authored, "", agent_capabilities=agent_caps)
    except PlanAuthoringError as e:
        raise HTTPException(422, f"plan reify failed: {e}") from e

    # Team-created agents are also eligible (shared with in-turn hook1); see _plan_lookup.
    _lookup = await _plan_lookup(app, names=[s.agent_name for s in steps], workspace_id=body.workspace_id, user=user)
    bad = validate_agents(steps, _lookup)
    if bad:
        raise HTTPException(422, f"unknown agents in steps: {bad}")
    levels = assign_levels(steps)
    for s in steps:
        s.level = levels[s.id]
    plan = await store.create_plan(
        session_id=body.session_id,
        workspace_id=body.workspace_id,
        user_id=body.user_id,
        title=body.query[:80],
        objective=body.query,
        deliverable_kind=(DeliverableKind.ANSWER if plan_meta.get("deliverable") == "answer" else DeliverableKind.REPORT),
        initial_steps=steps,
    )
    return {"plan_id": plan.id, "status": plan.status, "steps": [_step_view(s) for s in steps]}


@router.get("/{plan_id}")
async def get_plan(
    plan_id: str,
    workspace_id: str,
    user_id: str,
    req: Request,
) -> dict[str, Any]:
    if not _dev_enabled():
        raise HTTPException(404, "planning mode disabled")
    store = req.app.state.plan_store
    snapshot = await store.get_plan_with_steps(plan_id, user_id=user_id, workspace_id=workspace_id)
    if snapshot is None:
        raise HTTPException(404, "plan not found")
    plan, steps = snapshot
    return {
        "plan": {
            "id": plan.id,
            "status": plan.status,
            "title": plan.title,
            "workspace_id": plan.workspace_id,
            "deliverable_kind": plan.deliverable_kind,
            "canvas_id": plan.canvas_id,
            # Batch 3 D-C (implementation review P1-1): worker tail-window signal; False forces approve/
            # revise-confirm CAS rejection (report still composing); FE can back off or disable buttons.
            "retryable": getattr(plan, "execution_turn_id", None) is None,
            "current_rev": getattr(plan, "current_rev", 1),
            "replan_count": getattr(plan, "replan_count", 0),
        },
        "steps": [_step_view(s) for s in steps],
    }


@router.post("/{plan_id}/approve")
async def approve_plan(
    plan_id: str,
    body: ApproveBody,
    req: Request,
) -> dict[str, Any]:
    if not _dev_enabled():
        raise HTTPException(404, "planning mode disabled")
    app = req.app
    store = app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=body.user_id, workspace_id=body.workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    team_router = None
    team_kb_retriever = None
    if body.team_id:
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        team_runtime = await resolve_team_model_runtime(
            app.state,
            body.team_id,
            entrypoint="plan",
        )
        if not team_runtime.available:
            raise HTTPException(
                409,
                {"error": {"code": "team_model_runtime_unavailable"}},
            )
        team_router = team_runtime.router
        team_kb_retriever = team_runtime.kb_retriever
    tr = TurnRequest(
        session_id=plan.session_id,
        workspace_id=body.workspace_id,
        user_id=body.user_id,
        member_uid=body.member_uid or "",
        text="(planning-mode execution)",
        provenance_seed_text=await _session_user_texts(app, plan.session_id),
        team_id=body.team_id,
        plan_execution_id=plan_id,
        team_router=team_router,
        team_kb_retriever=team_kb_retriever,
        viewer_time_zone=viewer_time_zone_name(req),
    )
    # Claim and bind the execution identity in one PlanStore CAS.  Recovery
    # can therefore distinguish this request from an old or newer approval.
    if not await store.try_claim_running(plan_id, tr.turn_id, expected_rev=plan.current_rev):
        raise HTTPException(409, "plan already running or terminal")
    result = await _run_with_plan_claim_heartbeat(
        app,
        store,
        plan_id,
        tr,
    )  # M1 dogfood: foreground
    after = await store.get_plan(plan_id, user_id=body.user_id, workspace_id=body.workspace_id)
    steps = await store.list_steps(plan_id)
    return {
        "turn_id": result.turn_id,
        "turn_status": str(result.status),
        "final_answer": result.final_answer,
        "plan_status": after.status if after else None,
        "steps": [_step_view(s) for s in steps],
    }


@router.post("/{plan_id}/approve_async")
async def approve_plan_async(
    plan_id: str,
    body: ApproveBody,
    req: Request,
) -> dict[str, Any]:
    """FE-facing approve: spawn execution as a **background task** and return
    immediately so a multi-minute plan doesn't block the HTTP response (and the
    run survives client disconnect / proxy timeout). The FE polls GET /{plan_id}
    for live progress (plan_driver updates plan_store as steps run).

    Same ``_dev_enabled`` double-gate as the other dev routes (no new auth
    surface). The production authed + credit-deducting + SSE-streaming approve is
    the M1 follow-up (server-derived identity via _current_user)."""
    if not _dev_enabled():
        raise HTTPException(404, "planning mode disabled")
    app = req.app
    store = app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=body.user_id, workspace_id=body.workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    team_router = None
    team_kb_retriever = None
    if body.team_id:
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        team_runtime = await resolve_team_model_runtime(
            app.state,
            body.team_id,
            entrypoint="plan",
        )
        if not team_runtime.available:
            raise HTTPException(
                409,
                {"error": {"code": "team_model_runtime_unavailable"}},
            )
        team_router = team_runtime.router
        team_kb_retriever = team_runtime.kb_retriever
    tr = TurnRequest(
        session_id=plan.session_id,
        workspace_id=body.workspace_id,
        user_id=body.user_id,
        member_uid=body.member_uid or "",
        text="(planning-mode execution)",
        provenance_seed_text=await _session_user_texts(app, plan.session_id),
        team_id=body.team_id,
        plan_execution_id=plan_id,
        team_router=team_router,
        team_kb_retriever=team_kb_retriever,
        viewer_time_zone=viewer_time_zone_name(req),
    )
    # Same atomic claim + execution identity binding as the foreground route.
    if not await store.try_claim_running(plan_id, tr.turn_id, expected_rev=plan.current_rev):
        raise HTTPException(409, "plan already running or terminal")

    # Keep a strong ref to the task so it isn't GC'd mid-flight, and so a client
    # disconnect can't cancel a running plan (the task is independent of the
    # request coroutine). Orphans on gateway restart are handled by main.py's
    # recover_orphans boot hook.
    tasks: set[asyncio.Task[Any]] = getattr(app.state, "_plan_exec_tasks", None)  # type: ignore[assignment]
    if tasks is None:
        tasks = set()
        app.state._plan_exec_tasks = tasks

    async def _run() -> None:
        try:
            await _run_with_plan_claim_heartbeat(app, store, plan_id, tr)
        except Exception:  # background; plan_driver marks step/plan state
            logging.getLogger("bowhead.gateway").exception("plan execution failed plan_id=%s", plan_id)

    task = asyncio.create_task(_run())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"plan_id": plan_id, "accepted": True, "plan_status": "running"}


@router.patch("/{plan_id}/steps")
async def edit_plan_steps(
    plan_id: str,
    body: EditStepsBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """A(可编辑): 结构化编辑(改目标 / 删步 / 勾选 included / 排序)。

    FE 本地乐观改后把**整套编辑后 steps** 发来 → PROPOSED-only → validate_agents +
    assign_levels 重算 level + replace_steps 单事务 + bump_rev。depends_on 摘除悬空
    (删步自动断开下游依赖)。也用于「确认 amend 预览」(FE 发预览 steps)。
    **authed**(_current_user 派生身份 + ownership 进 get_plan WHERE) —— 2026-07 从
    dev-only 升生产可用(此前 _dev_enabled 双门 → 116 prod 编辑操作全 404,可编辑计划卡
    编辑功能生产死);planning-flag 门。body.user_id 忽略(留字段兼容旧 FE)。"""
    if not _planning_enabled():
        raise HTTPException(404, "planning mode disabled")
    app = req.app
    store = app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=user.id, workspace_id=body.workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    await _require_plan_space(req, user_id=user.id, workspace_id=body.workspace_id, operation=SpaceOperation.WRITE)
    if plan.status != PlanStatus.PROPOSED:
        raise HTTPException(409, "plan not editable (only PROPOSED can be edited)")
    if body.expected_rev is not None and body.expected_rev != plan.current_rev:
        raise HTTPException(409, {"error": {"code": "plan_not_approvable"}})
    if not body.steps:
        raise HTTPException(422, "plan must keep at least one step")
    registry = getattr(app.state.orchestrator, "multi_agent_registry", None)
    if registry is None:
        raise HTTPException(503, "multi_agent_registry unavailable")

    _, _, _, role_roster, role_profiles = await _actor_planner_roster(
        app,
        registry=registry,
        workspace_id=body.workspace_id,
        user=user,
    )
    known = {es.id for es in body.steps if es.id}
    existing_by_id = {s.id: s for s in await store.list_steps(plan_id)}
    steps: list[PlanStep] = []
    for i, es in enumerate(body.steps):
        sid = es.id or new_step(plan_id=plan_id, seq=i, agent_name=es.agent).id
        prior = existing_by_id.get(sid)
        deps = tuple(d for d in es.depends_on if d in known and d != sid)
        # review B2/F-1: replace_steps rewrites the whole table; failing to forward/restore criteria erases
        # all steps' criteria (including untouched steps) to [], skipping every verifier while old FE values hide it.
        if es.success_criteria is not None:
            crit = tuple(str(c).strip()[:60] for c in es.success_criteria if str(c).strip())[:3]
        else:
            crit = tuple(getattr(prior, "success_criteria", ()) or ()) if prior is not None else ()
        needs_web = es.needs_web if es.needs_web is not None else bool(getattr(prior, "needs_web", False))
        if role_roster is None:
            agent_choice = getattr(prior, "agent_choice", None) if prior is not None and prior.agent_name == es.agent else None
        else:
            agent_choice = _resolve_plan_agent_choice(
                agent_name=es.agent,
                submitted=es.agent_choice,
                existing=(getattr(prior, "agent_choice", None) if prior is not None else None),
                role_roster=role_roster,
                role_profiles=role_profiles,
            )
        steps.append(
            PlanStep(
                id=sid,
                plan_id=plan_id,
                seq=i,
                agent_name=es.agent,
                objective=es.objective,
                deliverable=es.deliverable,
                success_criteria=crit,
                depends_on=deps,
                needs_corpus=es.needs_corpus,
                needs_web=needs_web,
                included=es.included,
                origin=es.origin or "initial",
                agent_choice=agent_choice,
            )
        )

    # Team-created agents are also eligible (shared with in-turn hook1); see _plan_lookup.
    _lookup = await _plan_lookup(app, names=[s.agent_name for s in steps], workspace_id=body.workspace_id, user=user)

    bad = validate_agents(steps, _lookup)
    if bad:
        raise HTTPException(422, f"unknown agents in steps: {bad}")
    try:
        levels = assign_levels(steps)  # Recalculate; reject cycles.
    except Exception as e:  # Topology failure (cycle) → 422 for FE.
        raise HTTPException(422, f"invalid plan graph: {e}") from e
    for s in steps:
        s.level = levels[s.id]
    if not await store.replace_steps(plan_id, steps, expected_rev=plan.current_rev):
        raise HTTPException(409, {"error": {"code": "plan_not_approvable"}})
    # Return exactly this commit, not a later request's steps with our revision.
    return {"plan_id": plan_id, "status": plan.status, "current_rev": plan.current_rev + 1, "steps": [_step_view(s) for s in steps]}


@router.post("/{plan_id}/amend")
async def amend_plan_route(
    plan_id: str,
    body: AmendBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """A(可编辑): 自然语言改计划 —— amend_plan(LLM 带现有计划+指令重出全图)→ reify →
    **返回预览**(不落库)。FE 展示 diff(新增/删除/改),用户确认后走 PATCH /steps 持久。
    **authed**(_current_user + ownership) —— 2026-07 从 dev-only 升生产可用(同 edit_plan_steps);
    planning-flag 门。body.user_id 忽略(留字段兼容旧 FE)。"""
    if not _planning_enabled():
        raise HTTPException(404, "planning mode disabled")
    app = req.app
    store = app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=user.id, workspace_id=body.workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    await _require_plan_space(req, user_id=user.id, workspace_id=body.workspace_id, operation=SpaceOperation.EXECUTE)
    if plan.status != PlanStatus.PROPOSED:
        raise HTTPException(409, "plan not editable (only PROPOSED can be amended)")
    if not body.instruction.strip():
        raise HTTPException(422, "empty instruction")
    # P2: rate-limit before the LLM call (prevent unmetered provider costs); ownership/status checks already passed.
    if not _amend_rate_ok(user.id):
        raise HTTPException(429, "amend 太频繁,请稍后再试")
    registry = getattr(app.state.orchestrator, "multi_agent_registry", None)
    if registry is None:
        raise HTTPException(503, "multi_agent_registry unavailable")

    roster, agent_caps, agent_prod, role_roster, role_profiles = await _actor_planner_roster(
        app,
        registry=registry,
        workspace_id=body.workspace_id,
        user=user,
    )
    existing = await store.list_steps(plan_id)
    # planning-mode-audit WP-7 (P3-c): amend previously lacked all data context (even data_context was omitted,
    # confirmed by review fact-checking). Add a source overview so natural-language amendments no longer route blindly.
    _kb_store = getattr(app.state, "kb_files_store", None)
    _doc_n = 0
    if _kb_store is not None:
        try:
            _doc_n = await _kb_store.count_for_ws_shared(body.workspace_id)
        except Exception:
            _doc_n = 0
    _amend_data_ctx = f"可用数据源:① KB 上传文档 {_doc_n} 份(kb-analyst 读)② 本 workspace BI 数仓(domain 专家读)③ 联网公开信息(compute+needs_web)。"
    # credential-readiness D2.2: same as create — team router; unconfigured returns 409 with the same code as chat.
    amend_router = await team_router_or_409(
        app.state,
        user.team_id,
        entrypoint="plan",
    )
    # The amend preview's model calls date relative periods on the viewer's calendar.
    with viewer_clock_scope(viewer_time_zone_name(req)):
        authored = await amend_plan(
            amend_router,
            existing_steps=existing,
            instruction=body.instruction,
            agent_roster=roster,
            agent_capabilities=agent_caps,
            agent_produces=agent_prod,
            data_context=_amend_data_ctx,
            role_candidate_roster=role_roster,
            role_capable_profiles=role_profiles,
            trace_id="plan_amend",
        )
    if not authored:
        raise HTTPException(422, "amend produced no steps")
    try:
        steps = to_plan_steps(authored, plan_id, agent_capabilities=agent_caps)
    except PlanAuthoringError as e:
        raise HTTPException(422, f"amend reify failed: {e}") from e

    # Team-created agents are also eligible (shared with in-turn hook1); see _plan_lookup.
    _lookup = await _plan_lookup(app, names=[s.agent_name for s in steps], workspace_id=body.workspace_id, user=user)

    bad = validate_agents(steps, _lookup)
    if bad:
        raise HTTPException(422, f"unknown agents in steps: {bad}")
    try:
        levels = assign_levels(steps)
    except Exception as e:  # Topology failure (cycle) → 422.
        raise HTTPException(422, f"invalid plan graph: {e}") from e
    for s in steps:
        s.level = levels[s.id]
    # ★116-dogfood P1: **regenerate** criteria after amend, or criteria for the entire plan are erased.
    #
    # Flow: amend_plan reuses author_plan's schema, but AuthoredStep lacks a criteria field;
    # to_plan_steps **mints fresh ids for every step** + empty criteria → preview has none → FE uses
    # preview to PATCH /steps, rewriting the whole table → persisted success_criteria=[]. Batch 1 expected persistence to
    # restore criteria by step id, but all ids are new and cannot match. Loss affects **more than changed steps**:
    # untouched steps are erased too. This is more than a missing display row: empty criteria → verifier
    # skipped; more careful user amendments disable verification across the entire plan (silently defeating batch 1's selling point).
    # Actual device measurements (116, plan_c4899ee19444): 3 steps had criteria before amend; all became [] afterward.
    #
    # Fix: run the **same** second-pass criteria generation as proposals (author_step_criteria) on reified new steps;
    # treat changed/unchanged alike, without fragile id/text matching. Failure → empty criteria, honest degradation,
    # without blocking amend (same handling as _maybe_propose_plan).
    if planning_verify_enabled():
        try:
            with viewer_clock_scope(viewer_time_zone_name(req)):
                _crit = await author_step_criteria(amend_router, steps=steps, trace_id="plan_amend")
            for s in steps:
                s.success_criteria = _crit.get(s.id, ())
        except Exception:
            logging.getLogger("bowhead.gateway").warning("plan_amend.criteria_derive_failed plan=%s", plan_id, exc_info=True)
    # Preview without persistence; FE confirmation persists via PATCH /steps.
    return {"plan_id": plan_id, "preview": True, "expected_rev": plan.current_rev, "steps": [_step_view(s) for s in steps]}


async def _confirmed_recovery_snapshot(store: PlanStore, plan_id: str, *, user: User, workspace_id: str, expected_rev: int) -> dict[str, Any]:
    snapshot = await store.get_plan_with_steps(plan_id, user_id=user.id, workspace_id=workspace_id)
    if snapshot is None:
        raise HTTPException(404, "plan not found")
    plan, steps = snapshot
    if plan.current_rev != expected_rev or plan.status != PlanStatus.BLOCKED or plan.execution_turn_id is not None:
        # The mutation may have committed, but a newer graph/claim is not the
        # result the user confirmed. Refresh; never auto-execute that successor.
        raise HTTPException(409, {"code": "plan_busy_or_conflict"})
    return {"current_rev": plan.current_rev, "steps": [_step_view(step) for step in steps]}


@router.post("/{plan_id}/skip_step/{step_id}")
async def skip_plan_step(
    plan_id: str,
    step_id: str,
    body: SkipStepBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """B(受阻恢复): 跳过一个受阻/失败步 → 标 SKIPPED。用户随后 re-approve(chat/send
    plan_execution_id;try_claim 收 BLOCKED)续跑——resume_level 跳过该终态步,只重跑受阻步
    及下游,已完成步不重跑。**authed**(_current_user 派生身份 + ownership 进 get_plan WHERE);
    planning-flag 门(真用户可用)。重试本身无需端点=直接 re-approve。"""
    if not _planning_enabled():
        raise HTTPException(404, "planning mode disabled")
    store = req.app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=user.id, workspace_id=body.workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    await _require_plan_space(req, user_id=user.id, workspace_id=body.workspace_id, operation=SpaceOperation.EXECUTE)
    # review-fix (defense-in-depth): only skip when the PLAN is settled at BLOCKED —
    # never mutate execution-source-of-truth while the driver is mid-run (RUNNING).
    # Matches the FE, which only offers recovery when plan.status=="blocked".
    if plan.status != PlanStatus.BLOCKED:
        raise HTTPException(409, "plan not in a recoverable (blocked) state")
    step = next((s for s in await store.list_steps(plan_id) if s.id == step_id), None)
    if step is None:
        raise HTTPException(404, "step not found")
    if step.status not in (StepStatus.BLOCKED, StepStatus.FAILED):
        raise HTTPException(409, "step not skippable (must be blocked or failed)")
    expected_rev = plan.current_rev if body.expected_rev is None else body.expected_rev
    if not await store.skip_blocked_step(plan_id, step_id, expected_rev=expected_rev):
        raise HTTPException(409, {"code": "plan_busy_or_conflict"})
    snapshot = await _confirmed_recovery_snapshot(store, plan_id, user=user, workspace_id=body.workspace_id, expected_rev=expected_rev + 1)
    return {"plan_id": plan_id, "step_id": step_id, "status": "skipped", **snapshot}


# ── graph-mode batch 3: local replan (revise_step) ───────────────
# Contract = batch3-replan-design §2-S3: preview (LLM revises steps without persistence) → confirm
# (FE returns output unchanged + expected_rev; server validates and persists verbatim, **without another LLM call**;
# review R2-F1: approved output must equal persisted output).


class ReviseStepBody(BaseModel):
    workspace_id: str
    instruction: str = ""
    confirm: bool = False
    expected_rev: int | None = Field(default=None, strict=True, ge=1)
    revised: dict[str, Any] | None = None


def _replan_gate() -> None:
    from bowhead_core.planning_v2 import planning_replan_enabled

    if not (_planning_enabled() and planning_replan_enabled()):
        raise HTTPException(404, "planning replan disabled")


def _revise_error_context(step: Any) -> str:
    """失败上下文: error/error_kind + verify unmet + 产出摘要(criteria_unmet 步有
    payload 留证, plan_driver 阶段 B 写回清单)。"""
    parts: list[str] = []
    if step.error_kind:
        parts.append(f"错误类别: {step.error_kind}")
    if step.error:
        parts.append(f"错误信息: {str(step.error)[:400]}")
    _vo = outcome_from_json(getattr(step, "verify_json", None))
    if _vo is not None and _vo.unmet_texts():
        parts.append("未满足的验收: " + "; ".join(_vo.unmet_texts())[:300])
    if getattr(step, "result_digest", None):
        parts.append(f"产出摘要: {str(step.result_digest)[:300]}")
    elif getattr(step, "payload_json", None):
        try:
            _p = json.loads(step.payload_json)
            _es = str(_p.get("exec_summary") or "")[:300] if isinstance(_p, dict) else ""
            if _es:
                parts.append(f"产出摘要: {_es}")
        except (ValueError, TypeError):
            pass
    return "\n".join(parts) or "(无错误详情)"


def _chain_root_id(steps: list[Any], old_id: str) -> str:
    """沿 supersession 反向(谁的 superseded_by 指向我)走到谱系原始 id——下游
    depends_on 引用的是它(review R1-F7: 二代修订按 new1 算闭包恒空)。"""
    back = {s.superseded_by: s.id for s in steps if (s.superseded_by or "").startswith("ps_")}
    cur = old_id
    for _ in range(32):
        prev = back.get(cur)
        if prev is None or prev == cur:
            return cur
        cur = prev
    return cur


def _downstream_reset_ids(steps: list[Any], root_id: str) -> list[str]:
    """root 的传递闭包中 BLOCKED(upstream_failed) 行(供 supersede 复位谓词)。"""
    kids: dict[str, list[str]] = {}
    for s in steps:
        for dep in s.depends_on:
            kids.setdefault(dep, []).append(s.id)
    seen: set[str] = set()
    frontier = [root_id]
    while frontier:
        nxt: list[str] = []
        for fid in frontier:
            for cid in kids.get(fid, ()):
                if cid not in seen:
                    seen.add(cid)
                    nxt.append(cid)
        frontier = nxt
    by_id = {s.id: s for s in steps}
    return [cid for cid in seen if by_id[cid].status == StepStatus.BLOCKED and by_id[cid].error_kind == "upstream_failed"]


def _revision_generation(steps: list[Any], old_id: str) -> int:
    """old 所在谱系的既有修订代数(反向链长)——无进展 warning 判据。"""
    back = {s.superseded_by: s.id for s in steps if (s.superseded_by or "").startswith("ps_")}
    n = 0
    cur = old_id
    for _ in range(32):
        prev = back.get(cur)
        if prev is None or prev == cur:
            return n
        n += 1
        cur = prev
    return n


def _revise_warning(plan: Any, steps: list[Any], old_id: str) -> str | None:
    from bowhead_core.planning_v2 import planning_replan_max

    _max = planning_replan_max()
    gen = _revision_generation(steps, old_id)
    if gen >= _max:
        return f"该步已修订 {gen} 次仍失败,建议改用「重新规划」或跳过该步"
    if plan.replan_count >= _max:
        return f"本计划已修订/重规划 {plan.replan_count} 次,建议人工调整计划或跳过失败步"
    return None


async def _plan_lookup(app: Any, *, names: Any, workspace_id: str | None, user: Any) -> Any:
    """计划步 agent 的准入判定 —— HTTP 路由侧的**唯一**入口。

    ★ 2026-08-11 独立 review 逮到的真缺陷：R3 让团队自建 agent 能被规划、也能执行，
    但这五条 HTTP 路（create / edit_steps / amend / revise / replan）的 `validate_agents`
    仍是 registry-only ⇒ 一张含团队 agent 的计划**一编辑就 422 unknown agents**。
    能规划能执行、不能编辑 = 半接线。方向是 fail-closed（无安全洞），但功能是坏的。

    与 in-turn hook1 走**同一个** `build_plan_agent_admission`，caller 身份取
    `_current_user` 派生的 `user`（不信 body）—— 两侧的答案因此必然一致。
    """
    from bowhead_core.agents.plan_admission import build_plan_agent_admission

    registry = getattr(app.state.orchestrator, "multi_agent_registry", None)
    adm = await build_plan_agent_admission(
        names=names,
        registry=registry,
        workspace_id=workspace_id,
        subagent_runner=getattr(app.state.orchestrator, "subagent_runner", None),
        parent_ctx=SimpleNamespace(
            user_id=getattr(user, "id", None),
            team_id=getattr(user, "team_id", None),
            granted_team_subagent_ids=None,
            builtin_subagent_overrides=None,
        ),
    )
    return adm.lookup


def _planner_roster(
    registry: Any,
    workspace_id: str | None,
) -> tuple[list[tuple[str, str]], dict[str, frozenset[str]], dict[str, tuple[str, ...]]]:
    """planner/revise 两路共用的花名册 + 能力表 —— **同一个派生,别在别处再写一遍**。

    谁不能当计划步执行者走 `NON_PLANNABLE_AGENTS`(组稿工 + 调度员本身)——与 hook1 同源，
    别在这里另写一份判断。
    能力表与 roster 同源同 workspace,否则会出现「roster 里有、能力表里没有」→
    机械匹配把它当没能力,选不上。
    """
    from bowhead_core.agents.capabilities import capabilities_by_agent, produces_by_agent
    from bowhead_core.plan_authoring import NON_PLANNABLE_AGENTS

    names = [n for n in registry.list_all(workspace_id) if n not in NON_PLANNABLE_AGENTS]
    roster = [(n, getattr(registry.get(n, workspace_id=workspace_id), "description", "") or n) for n in names]
    return roster, capabilities_by_agent(registry, workspace_id, names), produces_by_agent(registry, workspace_id, names)


async def _actor_planner_roster(
    app: Any,
    *,
    registry: Any,
    workspace_id: str | None,
    user: Any,
) -> tuple[
    list[tuple[str, str]],
    dict[str, frozenset[str]],
    dict[str, tuple[str, ...]],
    Any,
    dict[str, frozenset[str]],
]:
    """HTTP plan authoring view of the same actor-scoped runtime roster."""
    from bowhead_core.agent_role_candidates import (
        build_runtime_role_candidate_roster,
    )
    from bowhead_core.agents.capabilities import derive_capabilities
    from bowhead_core.agents.plan_admission import list_visible_team_subagents

    roster, capabilities, produces = _planner_roster(registry, workspace_id)
    runner = getattr(app.state.orchestrator, "subagent_runner", None)
    store = getattr(runner, "team_subagent_store", None)
    team_id = str(getattr(user, "team_id", "") or "")
    user_id = str(getattr(user, "id", "") or "")
    overrides = None
    override_store = getattr(app.state.orchestrator, "builtin_subagent_override_store", None)

    if override_store is not None and team_id:
        try:
            overrides = await override_store.list_for_team(team_id)
        except Exception:
            logging.getLogger("bowhead.gateway").warning(
                "plan builtin Agent overrides unavailable team=%s",
                team_id,
                exc_info=True,
            )
    parent_ctx = SimpleNamespace(
        user_id=user_id,
        team_id=team_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=overrides,
        team_subagent_store=store,
    )

    visible_team = (
        await list_visible_team_subagents(
            store,
            team_id=team_id,
            user_id=user_id,
        )
        if team_id and user_id
        else []
    )
    known_names = {name for name, _description in roster}
    for name, description, tool_names in visible_team:
        if name not in known_names:
            roster.append((name, description or name))
            known_names.add(name)
        capabilities.setdefault(name, derive_capabilities(tool_names))
        produces.setdefault(name, ())

    from bowhead_core.agent_behavior_flags import agent_behavior_m3_enabled

    if not agent_behavior_m3_enabled():
        return roster, capabilities, produces, None, {}

    peers: list[Any] = []
    for name, description in roster:
        try:
            manifest = registry.get(name, workspace_id=workspace_id)
        except KeyError:
            continue
        peers.append(
            SimpleNamespace(
                name=name,
                description=description,
                source="system",
                agent_id=f"system:{name}",
                tool_names=frozenset(manifest.tools.allow),
                primary_capability=str(getattr(manifest, "primary_capability", "") or ""),
            )
        )
    if store is not None and team_id:
        try:
            rows = await store.list_team_subagents(team_id, only_enabled=True)
        except Exception:
            rows = []
        visible_by_name = {name: (description, frozenset(tool_names)) for name, description, tool_names in visible_team}
        for row in rows or []:
            visible = visible_by_name.get(str(getattr(row, "name", "") or ""))
            if visible is None:
                continue
            description, tool_names = visible
            peers.append(
                SimpleNamespace(
                    name=str(row.name),
                    description=description,
                    source="team",
                    agent_id=str(row.id),
                    tool_names=tool_names,
                    primary_capability=str(getattr(row, "primary_capability", "") or ""),
                )
            )

    role_roster = await build_runtime_role_candidate_roster(
        peers,
        subagent_runner=runner,
        parent_ctx=parent_ctx,
        team_subagent_store=store,
        stable_scope=f"plan-http:{team_id}:{user_id}",
    )
    role_profiles = {
        "image": frozenset(
            {
                # Tool grant is the execution truth. Do not let an unlabeled
                # custom image Agent bypass the reviewed exact-role roster.
                *(peer.name for peer in peers if "image_generate" in peer.tool_names),
                *(candidate.runtime_profile_name for candidate in role_roster.by_role.get("image", ())),
            }
        )
    }
    return roster, capabilities, produces, role_roster, role_profiles


def _resolve_plan_agent_choice(
    *,
    agent_name: str,
    submitted: dict[str, Any] | None,
    existing: Any,
    role_roster: Any,
    role_profiles: dict[str, frozenset[str]],
) -> Any:
    """Validate preview/edit data against the current actor-scoped roster."""
    from bowhead_core.agent_role_candidates import (
        ExactAgentRoleChoice,
        LeadRoleChoice,
        validate_structured_choice,
    )

    prior = existing if existing is not None and existing.runtime_profile_name == agent_name else None
    supplied = None
    if submitted is not None:
        try:
            supplied = ExactAgentRoleChoice.from_json(
                json.dumps(submitted, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception as exc:
            raise HTTPException(422, "invalid Agent choice") from exc
        if supplied.runtime_profile_name != agent_name:
            raise HTTPException(422, "Agent choice does not match step agent")
    target = supplied or prior
    role_ids = [role for role, names in role_profiles.items() if agent_name in names]
    if target is not None and target.role_id not in role_ids:
        role_ids.append(target.role_id)
    if not role_ids:
        if submitted is not None:
            raise HTTPException(422, "Agent choice is not valid for this step")
        return None

    candidates = [candidate for role in role_ids for candidate in role_roster.by_role.get(role, ()) if candidate.runtime_profile_name == agent_name]
    if target is not None:
        matches = [
            candidate
            for candidate in candidates
            if (
                candidate.role_id,
                candidate.agent_id,
                candidate.assignment_id,
                candidate.revision_ref,
                candidate.release_id,
                candidate.behavior_fingerprint,
            )
            == (
                target.role_id,
                target.agent_id,
                target.assignment_id,
                target.revision_ref,
                target.release_id,
                target.behavior_fingerprint,
            )
        ]
        if len(matches) != 1:
            raise HTTPException(409, {"code": "plan_agent_choice_stale"})
        selected = matches[0]
        if (
            supplied is not None
            and supplied.candidate_id != selected.candidate_id
            # Lead Turn rosters intentionally mint per-Turn opaque handles,
            # while HTTP preview/confirm uses a stable actor-scoped handle.
            # A byte-identical persisted choice may therefore be reminted on
            # the first edit; an arbitrary client-supplied handle may not.
            and supplied != prior
        ):
            raise HTTPException(409, {"code": "plan_agent_choice_stale"})
        reason_code = target.reason_code
    else:
        preferred = [candidate for candidate in candidates if candidate.is_preferred]
        if len(preferred) == 1:
            selected = preferred[0]
            reason_code = "user_preference"
        elif len(candidates) == 1:
            selected = candidates[0]
            reason_code = "user_selection"
        else:
            raise HTTPException(409, {"code": "plan_agent_choice_required"})
    try:
        choice = LeadRoleChoice(
            role_id=selected.role_id,
            candidate_id=selected.candidate_id,
            reason_code=reason_code,
        )
        validated = validate_structured_choice(role_roster, choice)
        return ExactAgentRoleChoice.freeze(choice=choice, candidate=validated)
    except Exception as exc:
        raise HTTPException(409, {"code": "plan_agent_choice_stale"}) from exc


def _sanitize_revised(raw: dict[str, Any], agent_capabilities: dict[str, frozenset[str]] | None = None) -> dict[str, Any]:
    from bowhead_core.plan_authoring import route_step_agent

    objective = str(raw.get("objective") or "").strip()[:200]
    agent = str(raw.get("agent") or "").strip()[:64]
    if not objective or not agent:
        raise HTTPException(422, "revised step missing agent/objective")
    needs_web = bool(raw.get("needs_web"))
    needs_corpus = bool(raw.get("needs_corpus")) and not needs_web
    crit_raw = raw.get("success_criteria")
    crit = tuple(str(c).strip()[:60] for c in crit_raw if str(c).strip())[:3] if isinstance(crit_raw, list) else ()
    # No capable agent found → retain the caller's agent (routes do not reject outright, same handling as core);
    # downstream validate_agents still blocks names outside the roster.
    routed = route_step_agent(
        agent,
        needs_web=needs_web,
        needs_corpus=needs_corpus,
        agent_capabilities=agent_capabilities,
    )
    return {
        # Fallback runs for both preview and confirm (review R2-F5).
        "agent_name": routed if routed is not None else agent,
        "objective": objective,
        "deliverable": str(raw.get("deliverable") or "").strip()[:160],
        "needs_corpus": needs_corpus,
        "needs_web": needs_web,
        "success_criteria": crit,
        "agent_choice": raw.get("agent_choice") if isinstance(raw.get("agent_choice"), dict) else None,
    }


async def _expire_superseded_midrun_cards(req: Request, plan_id: str, old_step: Any, remaining: list[Any]) -> None:
    """R1-F11: 被 supersede 步若无其余活的 needs_user_input 步共享 picker 卡,
    过期该 plan 的 pending mid-run 卡+补 resolved(skipped) 事件(F5 replay 收卡)。
    卡是 per-(tool,scope) 聚合的——还有别的步在等答案就不能动。fail-open。"""
    if old_step.error_kind != "needs_user_input" or not old_step.turn_id:
        return
    if any(s.status == StepStatus.BLOCKED and s.error_kind == "needs_user_input" for s in remaining):
        return
    clarify = getattr(req.app.state, "clarification_store", None)
    if clarify is None:
        return
    try:
        expired = await clarify.expire_pending_for_turn(old_step.turn_id, plan_id=plan_id)
        if expired:
            from bowhead_gateway.routes.clarifications import _ensure_clarification_resolved_event

            for rid in expired:
                await _ensure_clarification_resolved_event(
                    req,
                    turn_id=old_step.turn_id,
                    request_id=rid,
                    payload={
                        "request_id": rid,
                        "skipped": True,
                        "kind": "tool_context_select",
                        "chosen_value": "",
                        "chosen_values": [],
                        "answers": [],
                    },
                )
    except Exception:
        logging.getLogger("bowhead.gateway").exception("revise.midrun_expire_failed plan=%s", plan_id)


@router.post("/{plan_id}/revise_step/{step_id}")
async def revise_plan_step(
    plan_id: str,
    step_id: str,
    body: ReviseStepBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """批3 R3.2: 失败/受阻步的「修订重试」。preview → confirm 两段。"""
    _replan_gate()
    app = req.app
    store = app.state.plan_store
    snapshot = await store.get_plan_with_steps(plan_id, user_id=user.id, workspace_id=body.workspace_id)
    if snapshot is None:
        raise HTTPException(404, "plan not found")
    plan, steps = snapshot
    await _require_plan_space(req, user_id=user.id, workspace_id=body.workspace_id, operation=SpaceOperation.EXECUTE)
    if plan.status != PlanStatus.BLOCKED:
        raise HTTPException(409, "plan not in a revisable (blocked) state")
    step = next((s for s in steps if s.id == step_id), None)
    if step is None:
        raise HTTPException(404, "step not found")
    if step.status not in (StepStatus.BLOCKED, StepStatus.FAILED):
        raise HTTPException(409, "step not revisable (must be blocked or failed)")
    registry = getattr(app.state.orchestrator, "multi_agent_registry", None)
    if registry is None:
        raise HTTPException(503, "multi_agent_registry unavailable")
    roster, agent_caps, agent_prod, role_roster, role_profiles = await _actor_planner_roster(
        app,
        registry=registry,
        workspace_id=body.workspace_id,
        user=user,
    )

    if not body.confirm:
        # ── preview: LLM revises steps without persistence ──
        if not _amend_rate_ok(f"revise:{user.id}"):
            raise HTTPException(429, "修订太频繁,请稍后再试")
        from bowhead_core.plan_authoring import PlanAuthoringError, revise_step_author

        revise_router = await team_router_or_409(app.state, user.team_id, entrypoint="plan")
        try:
            with viewer_clock_scope(viewer_time_zone_name(req)):
                revised = await revise_step_author(
                    revise_router,
                    step=step,
                    error_context=_revise_error_context(step),
                    agent_roster=roster,
                    agent_capabilities=agent_caps,
                    agent_produces=agent_prod,
                    role_candidate_roster=role_roster,
                    role_capable_profiles=role_profiles,
                    instruction=body.instruction,
                    trace_id=f"plan_revise:{plan_id}",
                )
        except PlanAuthoringError as e:
            raise HTTPException(422, f"revise failed: {e}") from e
        candidate = _sanitize_revised(
            {
                "agent": revised.agent_name,
                "objective": revised.objective,
                "deliverable": revised.deliverable,
                "needs_corpus": revised.needs_corpus,
                "needs_web": revised.needs_web,
                "success_criteria": list(revised.criteria),
                "agent_choice": _agent_choice_view(revised.agent_choice),
            },
            agent_caps,
        )
        _probe = new_step(plan_id=plan_id, seq=0, agent_name=candidate["agent_name"])
        _lookup = await _plan_lookup(app, names=[candidate["agent_name"]], workspace_id=body.workspace_id, user=user)
        if validate_agents([_probe], _lookup):
            raise HTTPException(422, f"revised agent unknown: {candidate['agent_name']}")
        out: dict[str, Any] = {
            "plan_id": plan_id,
            "step_id": step_id,
            "preview": True,
            "revised": {**candidate, "success_criteria": list(candidate["success_criteria"])},
            "expected_rev": plan.current_rev,
        }
        if not candidate["success_criteria"]:
            out["criteria_missing"] = True
        _w = _revise_warning(plan, steps, step_id)
        if _w:
            out["warning"] = _w
        return out

    # ── confirm: persist returned output verbatim (no LLM call) ──
    if body.expected_rev is None or not isinstance(body.revised, dict):
        raise HTTPException(422, "confirm requires expected_rev and revised")
    # Implementation review P0-1: preview returns agent_name (_sanitize_revised output shape);
    # FE returns it verbatim, so confirm must accept both keys or preview→confirm always returns 422.
    # R2-F5 fallback also applies to confirm: FE's agent may have been altered, so **recheck capabilities** here
    # (previously hard-routed again by name). Same capability table/workspace as preview.
    candidate = _sanitize_revised(
        {**body.revised, "agent": body.revised.get("agent") or body.revised.get("agent_name")},
        agent_caps,
    )
    agent_choice = getattr(step, "agent_choice", None) if role_roster is None and step.agent_name == candidate["agent_name"] else None
    if role_roster is not None:
        agent_choice = _resolve_plan_agent_choice(
            agent_name=candidate["agent_name"],
            submitted=candidate.get("agent_choice"),
            existing=getattr(step, "agent_choice", None),
            role_roster=role_roster,
            role_profiles=role_profiles,
        )
    if not candidate["success_criteria"]:
        # review P3: criteria-bypass gate also applies to confirm (R2-F11); empty returned criteria
        # inherit the old step's criteria (same as authoring degradation), closing the post-revision verification bypass.
        candidate["success_criteria"] = tuple(step.success_criteria)
    _probe = new_step(plan_id=plan_id, seq=0, agent_name=candidate["agent_name"])
    _lookup = await _plan_lookup(app, names=[candidate["agent_name"]], workspace_id=body.workspace_id, user=user)
    if validate_agents([_probe], _lookup):
        raise HTTPException(422, f"revised agent unknown: {candidate['agent_name']}")
    max_seq = max((s.seq for s in steps), default=0)
    rev_step = new_step(
        plan_id=plan_id,
        seq=max_seq + 1,
        agent_name=candidate["agent_name"],
        objective=candidate["objective"],
        deliverable=candidate["deliverable"],
        success_criteria=candidate["success_criteria"],
        level=step.level,
        depends_on=tuple(step.depends_on),
        needs_corpus=candidate["needs_corpus"],
        needs_web=candidate["needs_web"],
        origin="revision",
        agent_choice=agent_choice,
    )
    root = _chain_root_id(steps, step_id)
    reset_ids = _downstream_reset_ids(steps, root)
    ok = await store.supersede_step(
        plan_id=plan_id,
        old_step_id=step_id,
        new_step=rev_step,
        downstream_reset_ids=reset_ids,
        expected_rev=body.expected_rev,
    )
    if not ok:
        # Any triple-CAS rejection: concurrent approve wins / worker tail window unreleased / stale rev / second double-click request.
        raise HTTPException(409, {"code": "plan_busy_or_conflict"})
    after_steps = await store.list_steps(plan_id)
    remaining = [s for s in after_steps if s.id != step_id and s.status != StepStatus.SUPERSEDED]
    await _expire_superseded_midrun_cards(req, plan_id, step, remaining)
    snapshot = await _confirmed_recovery_snapshot(store, plan_id, user=user, workspace_id=body.workspace_id, expected_rev=body.expected_rev + 1)
    return {
        "plan_id": plan_id,
        "step_id": step_id,
        "revised_step_id": rev_step.id,
        "downstream_reset": reset_ids,
        **snapshot,
    }


# ── graph-mode batch 3: full replan (regenerate unfinished subgraph only) ──


class ReplanBody(BaseModel):
    workspace_id: str
    instruction: str = ""
    confirm: bool = False
    expected_rev: int | None = Field(default=None, strict=True, ge=1)
    new_steps: list[dict[str, Any]] | None = None


def _merged_levels(done_steps: list[Any], new_steps: list[PlanStep]) -> dict[str, int]:
    """合并图(done+新步)重算 level——compute-only 悬空依赖剥离(review R1-F2/R2-F4):
    partial-ok 语义下 done 行的 depends_on 可指向被 superseded/skipped 的行,
    直接喂 assign_levels 必撞 DAGUnknownDependencyError。持久 depends_on 不动。"""
    from dataclasses import replace as _dc_replace

    ids = {s.id for s in done_steps} | {s.id for s in new_steps}
    merged = [_dc_replace(s, depends_on=tuple(d for d in s.depends_on if d in ids)) for s in [*done_steps, *new_steps]]
    return assign_levels(merged)


@router.post("/{plan_id}/replan")
async def replan_plan(
    plan_id: str,
    body: ReplanBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """批3 R3.3: 失败面过大时重出未完成子图(done 步产出保留为固定输入)。"""
    _replan_gate()
    app = req.app
    store = app.state.plan_store
    snapshot = await store.get_plan_with_steps(plan_id, user_id=user.id, workspace_id=body.workspace_id)
    if snapshot is None:
        raise HTTPException(404, "plan not found")
    plan, steps = snapshot
    await _require_plan_space(req, user_id=user.id, workspace_id=body.workspace_id, operation=SpaceOperation.EXECUTE)
    if plan.status != PlanStatus.BLOCKED:
        raise HTTPException(409, "plan not in a replannable (blocked) state")
    done_steps = [s for s in steps if s.status == StepStatus.DONE]
    live_unfinished = [s for s in steps if s.status not in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.SUPERSEDED)]
    registry = getattr(app.state.orchestrator, "multi_agent_registry", None)
    if registry is None:
        raise HTTPException(503, "multi_agent_registry unavailable")

    done_ids = frozenset(s.id for s in done_steps)

    if not body.confirm:
        # ── preview: LLM regenerates unfinished subgraph without persistence ──
        if not _amend_rate_ok(f"replan:{user.id}"):
            raise HTTPException(429, "重新规划太频繁,请稍后再试")
        from bowhead_core.plan_authoring import (
            PlanAuthoringError,
            author_step_criteria,
            replan_unfinished,
        )

        roster, agent_caps, agent_prod, role_roster, role_profiles = await _actor_planner_roster(
            app,
            registry=registry,
            workspace_id=body.workspace_id,
            user=user,
        )
        replan_router = await team_router_or_409(app.state, user.team_id, entrypoint="plan")
        # Preview model calls date relative periods on the viewer's calendar (bowhead_core.model_clock).
        with viewer_clock_scope(viewer_time_zone_name(req)):
            authored = await replan_unfinished(
                replan_router,
                existing_steps=steps,
                instruction=body.instruction,
                agent_roster=roster,
                agent_capabilities=agent_caps,
                agent_produces=agent_prod,
                role_candidate_roster=role_roster,
                role_capable_profiles=role_profiles,
                trace_id=f"plan_replan:{plan_id}",
            )
        if not authored:
            raise HTTPException(422, "replan produced no steps")
        try:
            new_steps = to_plan_steps(authored, plan_id, known_external_ids=done_ids, agent_capabilities=agent_caps)
        except PlanAuthoringError as e:
            raise HTTPException(422, f"replan reify failed: {e}") from e
        for s in new_steps:
            s.origin = "replan"
        _lookup = await _plan_lookup(app, names=[s.agent_name for s in new_steps], workspace_id=body.workspace_id, user=user)
        bad = validate_agents(new_steps, _lookup)
        if bad:
            raise HTTPException(422, f"unknown agents in replanned steps: {bad}")
        try:
            levels = _merged_levels(done_steps, new_steps)
        except Exception as e:
            raise HTTPException(422, f"invalid replanned graph: {e}") from e
        for s in new_steps:
            s.level = levels[s.id]
        # Second-pass criteria generation for new steps; failure → criteria_missing, honestly shown by confirmation bar (R2-F11).
        with viewer_clock_scope(viewer_time_zone_name(req)):
            crit_map = await author_step_criteria(replan_router, steps=new_steps, trace_id=f"plan_replan:{plan_id}")
        for s in new_steps:
            s.success_criteria = crit_map.get(s.id, ())
        out: dict[str, Any] = {
            "plan_id": plan_id,
            "preview": True,
            "new_steps": [_step_view(s) for s in new_steps],
            "kept_done": [s.id for s in done_steps],
            "superseded": [s.id for s in live_unfinished],
            "expected_rev": plan.current_rev,
        }
        _missing = [s.id for s in new_steps if not s.success_criteria]
        if _missing:
            out["criteria_missing"] = _missing
        from bowhead_core.planning_v2 import planning_replan_max

        if plan.replan_count >= planning_replan_max():
            out["warning"] = f"本计划已修订/重规划 {plan.replan_count} 次,建议人工调整计划或缩小范围"
        return out

    # ── confirm: persist returned new subgraph verbatim (no LLM call) ──
    if body.expected_rev is None or not isinstance(body.new_steps, list) or not body.new_steps:
        raise HTTPException(422, "confirm requires expected_rev and new_steps")
    # Implementation review P2-1: LLM path has _MAX_STEPS=12; direct confirm inserts must not bypass a soft cap.
    if len(body.new_steps) > 50:
        raise HTTPException(422, "too many steps")
    existing_ids = {s.id for s in steps}
    _max_seq = max((s.seq for s in steps), default=0)
    # Same as revise confirm: capability fallback before verbatim persistence (fetch registry once outside loop, not per step).
    _, _confirm_caps, _, role_roster, role_profiles = await _actor_planner_roster(
        app,
        registry=registry,
        workspace_id=body.workspace_id,
        user=user,
    )
    new_rows: list[PlanStep] = []
    new_ids: set[str] = set()
    for i, raw in enumerate(body.new_steps):
        cand = _sanitize_revised({**raw, "agent": raw.get("agent") or raw.get("agent_name")}, _confirm_caps)
        sid = str(raw.get("id") or "")
        # id allowlist (implementation review P2-1): preview mints ps_+12hex; forged overlong/special-character
        # ids must not enter PKs (parameterization prevents injection, but MB garbage/cross-plan PK collisions still deserve 422).
        if not re.fullmatch(r"ps_[0-9a-f]{6,32}", sid) or sid in existing_ids or sid in new_ids:
            raise HTTPException(422, f"invalid or colliding step id: {sid!r}")
        new_ids.add(sid)
        deps_raw = raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else []
        new_rows.append(
            PlanStep(
                id=sid,
                plan_id=plan_id,
                # seq follows existing rows (same as revise's max_seq+1 convention, review P2-7).
                seq=_max_seq + 1 + i,
                agent_name=cand["agent_name"],
                objective=cand["objective"],
                deliverable=cand["deliverable"],
                success_criteria=cand["success_criteria"],
                depends_on=tuple(str(d) for d in deps_raw),
                needs_corpus=cand["needs_corpus"],
                needs_web=cand["needs_web"],
                origin="replan",
                agent_choice=(
                    _resolve_plan_agent_choice(
                        agent_name=cand["agent_name"],
                        submitted=cand.get("agent_choice"),
                        existing=None,
                        role_roster=role_roster,
                        role_profiles=role_profiles,
                    )
                    if role_roster is not None
                    else None
                ),
            )
        )
    allowed = done_ids | new_ids
    for s in new_rows:
        _dangling = [d for d in s.depends_on if d not in allowed]
        if _dangling:
            raise HTTPException(422, f"step {s.id} 依赖未知步 {_dangling}")
    _lookup = await _plan_lookup(app, names=[s.agent_name for s in new_rows], workspace_id=body.workspace_id, user=user)
    bad = validate_agents(new_rows, _lookup)
    if bad:
        raise HTTPException(422, f"unknown agents in replanned steps: {bad}")
    try:
        levels = _merged_levels(done_steps, new_rows)
    except Exception as e:
        raise HTTPException(422, f"invalid replanned graph: {e}") from e
    for s in new_rows:
        s.level = levels[s.id]
    superseded_ids = [s.id for s in live_unfinished]
    level_updates = dict(levels)
    try:
        ok = await store.replan_steps(
            plan_id=plan_id,
            superseded_ids=superseded_ids,
            new_steps=new_rows,
            level_updates=level_updates,
            expected_rev=body.expected_rev,
        )
    except Exception as e:
        # IntegrityError such as cross-plan PK collisions: transaction fully rolled back; return 422, not 500.
        if "IntegrityError" in type(e).__name__ or "UNIQUE constraint" in str(e):
            raise HTTPException(422, "step id collides with an existing row") from e
        raise
    if not ok:
        raise HTTPException(409, {"code": "plan_busy_or_conflict"})
    # Superseded needs_user_input steps: full replan leaves no live awaiting-answer steps; unconditionally expire their cards.
    for old in live_unfinished:
        if old.error_kind == "needs_user_input":
            await _expire_superseded_midrun_cards(req, plan_id, old, remaining=[])
    snapshot = await _confirmed_recovery_snapshot(store, plan_id, user=user, workspace_id=body.workspace_id, expected_rev=body.expected_rev + 1)
    return {
        "plan_id": plan_id,
        "kept_done": [s.id for s in done_steps],
        "superseded": superseded_ids,
        "new": sorted(new_ids),
        **snapshot,
    }


@router.post("/{plan_id}/cancel")
async def cancel_plan(
    plan_id: str,
    body: CancelBody,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """C(durable cancel): 取消一个计划 → CANCELLED(终态 sticky,不可逆)。运行中的
    run_plan 在下个 level 边界查到 plans.status==CANCELLED → 停(durable pull-based
    cancel,不靠进程内信号)→ 不再烧钱。**authed**(_current_user + ownership);
    planning-flag 门。终态(completed/failed/已 cancelled)→ 409(update_plan_status 拒)。"""
    if not _planning_enabled():
        raise HTTPException(404, "planning mode disabled")
    store = req.app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=user.id, workspace_id=body.workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    ok = await store.update_plan_status(plan_id, PlanStatus.CANCELLED)
    if not ok:
        raise HTTPException(409, "plan already terminal (completed/failed/cancelled)")
    return {"plan_id": plan_id, "status": "cancelled"}


@router.get("/{plan_id}/state")
async def plan_state_authed(
    plan_id: str,
    workspace_id: str,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """authed 只读 plan 状态 (2026-07 planning-ux-reliability)。

    F5/归档 reconcile 用:事件 replay 是并发无序的(page.tsx 6 路并行),执行
    turn 的 step 事件可能在 plan_proposed seed 之前被丢 → 卡片状态漂移;卡片
    mount 时以此为权威纠偏(含 cancelled/别端已执行)。dev GET /{plan_id} 是
    dev-only 双门,真用户此前无任何只读入口。**authed**(_current_user 派生
    身份 + ownership 进 get_plan WHERE);planning-flag 门。
    """
    if not _planning_enabled():
        raise HTTPException(404, "planning mode disabled")
    store = req.app.state.plan_store
    snapshot = await store.get_plan_with_steps(plan_id, user_id=user.id, workspace_id=workspace_id)
    if snapshot is None:
        raise HTTPException(404, "plan not found")
    plan, steps = snapshot
    await _require_plan_space(req, user_id=user.id, workspace_id=workspace_id, operation=SpaceOperation.READ_HISTORY)
    return {
        "plan": {
            "id": plan.id,
            "status": plan.status,
            "title": plan.title,
            "workspace_id": plan.workspace_id,
            "deliverable_kind": plan.deliverable_kind,
            "canvas_id": plan.canvas_id,
            # Batch 3 D-C (implementation review P1-1): worker tail-window signal; False forces approve/
            # revise-confirm CAS rejection (report still composing); FE can back off or disable buttons.
            "retryable": getattr(plan, "execution_turn_id", None) is None,
            "current_rev": getattr(plan, "current_rev", 1),
            "replan_count": getattr(plan, "replan_count", 0),
        },
        "steps": [_step_view(s) for s in steps],
    }


# ── graph-mode batch 4: provenance export (batch4-provenance-design D-D) ──


@router.get("/{plan_id}/provenance")
async def plan_provenance(
    plan_id: str,
    workspace_id: str,
    req: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """R4.2: 给定 plan 导出完整执行溯源链(JSON)。只读;typed 四边全部从既有
    数据推导(PRODUCES/DERIVED_FROM/USED/SUPERSEDES),事件缺失如实计 completeness。"""
    from bowhead_core.plan_provenance import build_plan_provenance, tool_event_types
    from bowhead_core.planning_v2 import planning_provenance_enabled

    if not (_planning_enabled() and planning_provenance_enabled()):
        raise HTTPException(404, "planning provenance disabled")
    app = req.app
    store = app.state.plan_store
    plan = await store.get_plan(plan_id, user_id=user.id, workspace_id=workspace_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    await _require_plan_space(req, user_id=user.id, workspace_id=workspace_id, operation=SpaceOperation.READ_HISTORY)
    steps = await store.list_steps(plan_id)

    tool_events_by_turn: dict[str, list[Any]] = {}
    turn_log_store = getattr(app.state, "turn_log_store", None)
    if turn_log_store is not None:
        for tid in {s.turn_id for s in steps if s.turn_id}:
            try:
                tool_events_by_turn[tid] = await turn_log_store.list_events(tid, event_types=tool_event_types())
            except Exception:
                # Audit endpoint read failures must not masquerade as missing data (implementation review P3).
                logging.getLogger("bowhead.gateway").warning("plan_provenance.events_read_failed turn=%s", tid, exc_info=True)
                tool_events_by_turn[tid] = []

    sections: list[Any] = []
    canvas_store = getattr(app.state, "canvas_store", None)
    if canvas_store is not None:
        try:
            # plans.canvas_id is an unused column (batch4 §0.4); actual lookup uses the session key.
            canvas = await canvas_store.get_canvas_by_session(plan.session_id)
            if canvas is not None:
                sections = await canvas_store.list_sections(canvas.id, include_hidden=True)
        except Exception:
            logging.getLogger("bowhead.gateway").warning("plan_provenance.sections_read_failed plan=%s", plan_id, exc_info=True)
            sections = []

    return build_plan_provenance(plan=plan, steps=steps, tool_events_by_turn=tool_events_by_turn, sections=sections)
