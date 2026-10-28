"""Workflow 固化 — distill 端点（workflow-solidify S1 / tech-design §2.1-§2.3）.

``POST /api/v1/workflow-solidify/distill``（flag-gated ``BOWHEAD_CCA_WORKFLOW_CREATOR``）：
读一轮 turn 的 ``turn_events`` → 确定性蒸馏（``workflow_distill.parse_trace``）→ **analyst
快档** 单次 LLM call 提取结构 → 校验消毒（``parse_distill_output``）.

鉴权 = §2.1 归属链（``Depends(_current_user)`` + turn→session→user owner），**跨 user → 404**
（反枚举，故意非 403——不泄漏 turn 存在性，区别于 ``assert_turn_access`` 的 replay 路径）.
T8（§2.3）：``tool_refs`` 后端从 trace 实际调用 **确定性计算**，覆盖 LLM 输出；LLM 超时/失败
→ ``{ok:false, fallback:"oral"}`` 仍带确定性 digest 供口头复述.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from bowhead_core.published_diagnostics import publish_skill_workflow_failure
from bowhead_core.workflow_creator_trigger import workflow_creator_enabled
from bowhead_core.workflow_distill import (
    TraceDigest,
    build_distill_prompt,
    compute_tool_refs,
    parse_distill_output,
    parse_trace,
)
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from bowhead_gateway.credit_admission import check_credit_admission
from bowhead_gateway.error_handler import localize_error_message
from bowhead_gateway.routes._deps import resolve_team_router
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.skill_workflow_errors import skill_workflow_http_error

if TYPE_CHECKING:
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)
router = APIRouter()

# Canonical flag helper: bowhead_core.workflow_creator_trigger (one flag for trigger registration, route mounting,
# and chip, without drift). Recheck at runtime here; flipping OFF immediately returns 404 (dark).
# Distillation is one structural-extraction call; analyst fast tier (profiles.compute) currently routes qwen3.6-plus, a
# **reasoning model** (router.invoke has no extra_body channel to disable thinking). Dogfood measured ~70s per
# distillation call, so timeout is 120s (oral fallback handles genuine stalls). Solidification is a deliberate one-off action;
# ~1min spinner is acceptable, better than frequent false 45s timeouts leaving oral output without a recipe (2026-06-09 dogfood lesson).
_DISTILL_TIMEOUT_S = 120.0

# S3 dry-run: one actual turn (heavy) + judge (light). run_turn has per_turn_timeout; limit judge separately.
_DRYRUN_RATE_LIMIT = 6  # Per-user hourly dry-run count (rate_limit_check).
_DRYRUN_RATE_WINDOW_S = 3600
_JUDGE_TIMEOUT_S = 60.0
_DISTILL_RATE_LIMIT = 12  # Per-user hourly distillation count (turn/session share a bucket).
_DISTILL_RATE_WINDOW_S = 3600
# Session-distillation read bounds, **enforced before loading**: shrink from newest turns backward on excess, rather than loading
# everything before compression. Row-by-row json.loads of a long session's events alone can block the event loop.
_SESSION_MAX_TURNS = 40
_SESSION_MAX_EVENTS = 8000


class DistillBody(BaseModel):
    turn_id: str | None = None
    # S2: solidify the whole conversation; mutually exclusive with turn_id (both/neither → 422). Different semantics:
    # silently choosing one makes users believe the whole session was solidified when only one turn was.
    session_id: str | None = None
    form_hint: str | None = None  # Advisory form preference from chip entry (skill|subagent).


async def _session_digest(
    session_store: Any,
    turn_log_store: Any,
    *,
    session_id: str,
    user_id: str,
) -> tuple[Any, bool]:
    """整段对话 → 合并 digest（S2 / PRD FR-4）。返回 ``(digest, 是否收过窗)``。

    上界**在加载前**生效：先按 turn 数从最新往回收窗，再逐 turn 累计 events 数封顶。
    先读全量再压缩等于把一个长会话的所有事件逐行 json.loads 进内存——这正是要避免的。
    """
    from bowhead_core.workflow_distill import merge_session_digests

    sess = await session_store.get_session(session_id)
    if sess is None or getattr(sess, "user_id", None) != user_id:
        raise skill_workflow_http_error(404, "workflow_turn_not_found")

    turns = await session_store.list_turns(session_id=session_id)

    # Solidify only turns that actually did work: pure chat has no reusable workflow and dilutes digest signals.
    # ⚠ Turn.status is **str**, not an enum with .value (that is TurnResult's shape).
    # Copying the runner's .status.value filters out every turn, making session distillation always return 404.
    def _status(t: Any) -> str:
        st = getattr(t, "status", None)
        return getattr(st, "value", st) or ""

    usable = [t for t in turns if _status(t) == "completed"]
    truncated = False
    if len(usable) > _SESSION_MAX_TURNS:
        # Window to recent turns. ⚠ Events are not read yet, so pure-chat turns cannot be identified and may occupy
        # slots (filtered after events load below). Event budget is the main event-loop protection gate,
        # turn count secondary; no prereads merely to save slots, which would spend the IO this is meant to avoid.
        usable = usable[-_SESSION_MAX_TURNS:]
        truncated = True

    digests = []
    budget = _SESSION_MAX_EVENTS
    for t in usable:
        if budget <= 0:
            truncated = True
            break
        events = await turn_log_store.list_events(t.id)
        if len(events) > budget:
            events = events[:budget]
            truncated = True
        budget -= len(events)
        d = parse_trace(t.id, events)
        # No tool calls or peers = pure chat, not a workflow step.
        if d.tool_names or d.spawn_count:
            digests.append(d)

    if not digests:
        raise skill_workflow_http_error(404, "workflow_turn_not_found")
    return merge_session_digests(session_id, digests), truncated


@router.post("/distill")
async def distill(
    body: DistillBody,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """蒸馏一轮 turn（或整段 session）→ recipe 草稿（flag OFF → 404；跨 user → 404）."""
    if not workflow_creator_enabled():
        raise skill_workflow_http_error(404, "workflow_creator_disabled")
    if bool(body.turn_id) == bool(body.session_id):
        raise skill_workflow_http_error(422, "request_validation_failed")

    session_store = getattr(request.app.state, "session_store", None)
    turn_log_store = getattr(request.app.state, "turn_log_store", None)
    if session_store is None or turn_log_store is None:
        raise skill_workflow_http_error(503, "workflow_store_unavailable")

    # Rate limit + credit precheck (new in S2): distill previously had **no throttling**, despite each call using
    # ~70s reasoning charged to the team; expanded session-level inputs need protection even more.
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is not None:
        # Credit before throttling: credit-denied requests must not consume a rate-limit slot.
        credit_denial = await check_credit_admission(
            user_store,
            team_id=user.team_id,
            user_id=user.id,
            workspace_id=None,
            metrics_log=getattr(request.app.state, "metrics_log", None),
        )
        if credit_denial is not None:
            raise skill_workflow_http_error(402, credit_denial)
        allowed = await user_store.rate_limit_check(
            f"distill:{user.id}",
            limit=_DISTILL_RATE_LIMIT,
            window_s=_DISTILL_RATE_WINDOW_S,
        )
        if not allowed:
            raise skill_workflow_http_error(429, "workflow_distill_rate_limited")

    truncated_session = False
    if body.session_id:
        digest, truncated_session = await _session_digest(
            session_store,
            turn_log_store,
            session_id=body.session_id,
            user_id=user.id,
        )
    else:
        # §2.1 ownership chain: other users/missing → 404 (anti-enumeration, intentionally no missing/not-yours distinction).
        turn = await session_store.get_turn(body.turn_id)
        if turn is None:
            raise skill_workflow_http_error(404, "workflow_turn_not_found")
        sess = await session_store.get_session(getattr(turn, "session_id", None) or "")
        if sess is None or sess.user_id != user.id:
            raise skill_workflow_http_error(404, "workflow_turn_not_found")

        events = await turn_log_store.list_events(body.turn_id)
        digest = parse_trace(body.turn_id, events)
    # T8 item ②: derive tool_refs deterministically (reachable=None; **authoritative authorization = save-time
    # team_subagents.py:82 _validate_tool_refs / skill-upload validation stack**, §2.3/§4.4).
    tool_refs = compute_tool_refs(digest)

    # build_distill_prompt has a soft trace cap (scripts dropped last, marked truncated, §2.3);
    # always retain fixed schema/few-shot/hard-rule suffix. No blind endpoint truncation displacing the output contract.
    system, user_prompt = build_distill_prompt(digest)

    # Batch 2-a call site 2: previously used process-level assembly router; dev got echo (parse failure → oral fallback),
    # prod raised (same oral fallback). **Users always got only an oral draft, with only logger.warning**.
    # Workflow-solidification flag defaults ON and is unset on 116, making this a currently live path.
    # Preserve failure-to-oral fallback (invariant 3 before errors), but log/count missing-router causes
    # instead of silence. Reuse the registered reason_code (new codes collide with three set-equality gates).
    router_obj, router_miss = await resolve_team_router(request.app.state, user.team_id, agent_type="analyst", site="workflow_distill")
    if router_obj is None:
        logger.error("workflow_solidify.distill_router_unavailable team=%s reason=%s", user.team_id, router_miss.value if router_miss else "?")
        return await _oral_fallback(
            request,
            digest,
            tool_refs,
            reason_code="workflow_distill_router_unavailable",
        )

    from bowhead_router.usage import usage_context

    from bowhead_router import LLMRequest, Message

    try:
        # Batch 2-a §3.6: attribution; otherwise recorded as unattributed/team_id=NULL.
        with usage_context(team_id=user.team_id, user_id=user.id, reason="workflow_distill"):
            resp = await asyncio.wait_for(
                router_obj.invoke(
                    "analyst",
                    LLMRequest(
                        model="",
                        messages=[Message(role="user", content=user_prompt)],
                        system=system,
                        max_output_tokens=2000,
                        temperature=0.2,
                        response_format={"type": "json_object"},
                    ),
                    profile="compute",  # §2.3 analyst fast tier (profiles.compute lane).
                ),
                timeout=_DISTILL_TIMEOUT_S,
            )
    except TimeoutError as e:
        logger.warning("workflow_solidify.distill_timeout src=%s err=%s", body.turn_id or body.session_id, e)
        return await _oral_fallback(
            request,
            digest,
            tool_refs,
            reason_code="workflow_distill_timeout",
        )
    except Exception as e:
        logger.warning("workflow_solidify.distill_llm_failed src=%s err=%s", body.turn_id or body.session_id, e)
        return await _oral_fallback(
            request,
            digest,
            tool_refs,
            reason_code="workflow_distill_failed",
        )

    data = _parse_json_loose(resp.content or "")
    if data is None:
        return await _oral_fallback(
            request,
            digest,
            tool_refs,
            reason_code="workflow_distill_response_invalid",
        )

    result = parse_distill_output(data, digest, tool_refs=tool_refs)
    # form_hint (explicit chip choice) overrides structural classification; both drafts (body_md / subagent_spec)
    # are always generated, so overriding the label is safe.
    if body.form_hint in ("skill", "subagent"):
        result["suggested_form"] = body.form_hint
        result["form_hint_applied"] = True
    if body.session_id:
        result["source"] = "session"
        result["turn_count"] = digest.turn_count
        # Disclose windowing honestly: users expect whole-session solidification, but only recent turns were covered.
        if truncated_session:
            result["truncated_notice_code"] = "workflow_session_too_large"
    return result


class DryRunBody(BaseModel):
    name: str  # Saved recipe (skill_instance) name → set_verdict target.
    trigger_query: str  # Test question for dry-run.
    form: str = "skill"  # skill|team-skill: verdict saved in skill_instance/team_skill respectively.
    checkpoints: list[str] = []  # Criteria from distillation output.
    workspace_id: str | None = None


@router.post("/dry-run")
async def dry_run(
    body: DryRunBody,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """试跑已保存 recipe → judge 判 checkpoint → 写 verdict（§7）.

    隔离：独立 scratch session（origin=workflow_dryrun，session_id≠原），绝不写原 canvas/turn.
    限流：每用户 6 次/小时. judge：单 LLM call（judge 跨-provider lane）按 checkpoints 判实质.
    ⚠️ run_turn dispatch 真行为在 task#8 全栈 e2e 验；本端点结构 + 隔离 + judge + 限流已单测.
    """
    if not workflow_creator_enabled():
        raise skill_workflow_http_error(404, "workflow_creator_disabled")
    st = request.app.state
    session_store = getattr(st, "session_store", None)
    user_store = getattr(st, "user_store", None)
    if session_store is None or user_store is None:
        raise skill_workflow_http_error(503, "workflow_store_unavailable")

    # Dry-run starts an actual turn: same credit admission as chat/send (before throttling; denied requests consume no slots).
    credit_denial = await check_credit_admission(
        user_store,
        team_id=user.team_id,
        user_id=user.id,
        workspace_id=body.workspace_id,
        metrics_log=getattr(st, "metrics_log", None),
    )
    if credit_denial is not None:
        raise skill_workflow_http_error(402, credit_denial)

    # Rate limit (rate_limit_check returns True=allowed; atomic check-and-increment).
    allowed = await user_store.rate_limit_check(
        f"dryrun:{user.id}",
        limit=_DRYRUN_RATE_LIMIT,
        window_s=_DRYRUN_RATE_WINDOW_S,
    )
    if not allowed:
        raise skill_workflow_http_error(
            429,
            "workflow_dry_run_rate_limited",
        )

    # P1 (review): dry-run executes in body.workspace_id (KB scope=workspace:{ws}, settings/canvas keyed on ws),
    # requiring the same membership gate as chat, or same-team members can dry-run into sibling workspaces they do not belong to
    # (horizontal privilege escalation). Enforcement OFF retains current permissive behavior, without blocking, same as chat.
    from bowhead_gateway.ws_authz import (
        authorize_ws_access,
        ws_membership_enforce_enabled,
    )

    if ws_membership_enforce_enabled():
        ws_team_id: str | None = None
        existing_role: str | None = None
        if user.team_id:
            try:
                ws_team_id = await user_store.get_workspace_team_id(body.workspace_id)
                existing_role = await user_store.get_member_role(body.workspace_id, user.id)
            except Exception as e:
                logger.warning("dryrun.member_lookup_failed user=%s ws=%s err=%s", user.id, body.workspace_id, e)
                raise skill_workflow_http_error(
                    503,
                    "authz_lookup_failed",
                    nested_error=True,
                ) from e
        deny = authorize_ws_access(
            enforce=True,
            existing_role=existing_role,
            ws_team_id=ws_team_id,
            caller_team_id=user.team_id,
        )
        if deny:
            raise skill_workflow_http_error(
                403,
                "ws_access_denied",
                nested_error=True,
            )

    # Batch 1/c4 (workspace-space-split): team ids no longer stand in for workspaces; require explicit workspace_id.
    ws_id = body.workspace_id or ""
    if not ws_id:
        raise skill_workflow_http_error(
            400,
            "missing_workspace_context",
            nested_error=True,
        )
    team_model_runtime = None
    if user.team_id:
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        team_model_runtime = await resolve_team_model_runtime(
            st,
            user.team_id,
            entrypoint="tool_continuation",
        )
    # Separate scratch session, isolated from the original conversation.
    scratch = await session_store.ensure_session(
        session_id=None,
        workspace_id=ws_id,
        user_id=user.id,
        origin="workflow_dryrun",
    )

    # Dry-run reuses chat datarc resolution + orchestrator.run_turn; failures yield error verdicts, not 500.
    from bowhead_router.usage import usage_context

    with usage_context(team_id=user.team_id, user_id=user.id, reason="workflow_dry_run"):
        final_answer, dispatch_error_code = await _dispatch_dryrun_turn(
            request,
            scratch.id,
            ws_id,
            user,
            body.trigger_query,
            team_model_runtime,
        )

    # judge.
    router_obj = team_model_runtime.router if team_model_runtime is not None and team_model_runtime.available else None
    failure_code = dispatch_error_code
    if dispatch_error_code:
        verdict, cp_results = "error", []
    elif router_obj is None:
        verdict, cp_results = "error", []
        failure_code = "workflow_dry_run_judge_unavailable"
    else:
        from bowhead_router.usage import usage_context

        with usage_context(team_id=user.team_id, user_id=user.id, reason="workflow_dry_run_judge"):
            verdict, cp_results, judge_error_code = await _judge_recipe(
                router_obj,
                body.trigger_query,
                final_answer,
                body.checkpoints,
            )
        failure_code = judge_error_code

    # Persist verdict — P2 (review): team-skill uses team_skill (name→id), personal uses
    # skill_instance_store (name). Previously team dry-run wrote skill_instance verdicts, a no-op,
    # leaving team management's VerdictBadge permanently empty.
    _verdict_json = json.dumps(
        {"verdict": verdict, "checkpoints": cp_results, "trigger_query": body.trigger_query},
        ensure_ascii=False,
    )
    if body.form == "team-skill":
        try:
            sk = await user_store.get_team_skill_by_name(
                user.team_id,
                body.name,
                only_enabled=False,
                # skill-team-governance P1b: verdict writeback is management; must opt out, or pending rows are not found,
                # writes silently do nothing, and pending approval-card verdict badges stay empty
                # (exactly reproducing the historical no-op bug documented above).
                only_approved=False,
            )
            if sk is not None:
                await user_store.set_team_skill_verdict(
                    user.team_id,
                    sk.id,
                    verdict=verdict,
                    verdict_json=_verdict_json,
                )
        except Exception as e:
            logger.warning("dryrun.set_team_verdict_failed name=%s err=%s", body.name, e)
    else:  # skill (default).
        sis = getattr(st, "skill_instance_store", None)
        if sis is not None:
            try:
                await sis.set_verdict(
                    user_id=user.id,
                    name=body.name,
                    verdict=verdict,
                    verdict_json=_verdict_json,
                )
            except Exception as e:
                logger.warning("dryrun.set_verdict_failed name=%s err=%s", body.name, e)

    dispatch_error = await localize_error_message(request, failure_code) if failure_code is not None else None
    published = publish_skill_workflow_failure(failure_code) if failure_code is not None else None
    return {
        "ok": failure_code is None,
        "verdict": verdict,
        "checkpoints": cp_results,
        "scratch_session_id": scratch.id,
        "final_answer_preview": (final_answer or "")[:500],
        "dispatch_error": dispatch_error,
        "dispatch_error_code": failure_code,
        "dispatch_error_diagnostics": (published.to_wire() if published is not None else None),
    }


def _http_error_code(exc: Exception) -> str | None:
    """Return the finite ``{"error": {"code": ...}}`` code carried by an HTTPException."""
    detail = getattr(exc, "detail", None)
    error = detail.get("error") if isinstance(detail, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else None


async def _dispatch_dryrun_turn(
    request: Request,
    session_id: str,
    workspace_id: str,
    user: User,
    trigger_query: str,
    team_model_runtime: Any = None,
) -> tuple[str, str | None]:
    """在 scratch session 跑一轮真 turn（reuse chat datarc 解析 helper）. 返 (final_answer, error).

    optional TurnRequest 字段（language_context/workspace_settings/paradigm_v2）走默认——
    dry-run sanity 够用；prod 精细化在 task#8 e2e 调.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return "", "workflow_dry_run_orchestrator_unavailable"
    try:
        from bowhead_core.types import TurnRequest

        # main #4 datarc per-user identity refactor: former chat._resolve_turn_datarc_runtime split into
        # datarc_context.resolve_datarc_turn_context (team-scoped client + project envelope).
        # dry-run is a scratch sanity check. Without an explicit team connection and
        # verified member resolution, BI tools remain unavailable.
        from bowhead_gateway.datarc_context import (
            resolve_datarc_turn_context,
        )

        _datarc_ctx = await resolve_datarc_turn_context(
            request,
            team_id=user.team_id,
            workspace_id=workspace_id,
            user_id=user.id,
            log=logger,
        )
        if user.team_id and (team_model_runtime is None or not team_model_runtime.available):
            return "", "team_model_runtime_unavailable"

        # Datarc failures disable only Datarc; unrelated workflow steps can still run.
        from bowhead_gateway.routes.chat import _resolve_optional_datarc_member
        from bowhead_gateway.ws_authz import ws_membership_enforce_enabled

        _datarc_ctx, member_uid = await _resolve_optional_datarc_member(
            request,
            _datarc_ctx,
            ws_enforce=ws_membership_enforce_enabled(),
            log=logger,
        )

        # 2026-08-07 actual-device dogfood: omitting access_context makes this endpoint **fail 100%** on
        # BOWHEAD_RESOURCE_GRANTS_ENFORCE=1 deployments; turn_runner.py's guard
        # (access_store exists + request lacks access_context + enforcement ON) directly returns
        # access_context_missing, failing the turn within the same second. 116 has precisely that configuration.
        # All 7 TurnRequest construction sites set this field; only this one omitted it.
        # Follow routes/chat.py:1220 (also user-initiated execution, operation=EXECUTE).
        access_context: dict[str, Any] | None = None
        user_store = getattr(request.app.state, "user_store", None)
        if user_store is not None:
            from bowhead_core.space_governance import SpaceOperation

            from bowhead_gateway.access_context import (
                AccessContextError,
                access_context_to_dict,
                build_access_context,
            )

            try:
                access_context = access_context_to_dict(
                    await build_access_context(
                        user_store=user_store,
                        access_store=getattr(request.app.state, "access_control_store", None),
                        user_id=user.id,
                        workspace_id=workspace_id,
                        operation=SpaceOperation.EXECUTE,
                    )
                )
            except (AccessContextError, Exception) as exc:  # noqa: B014 — Explicitly include AccessContextError in this branch.
                # **Best effort, no new failure modes**: log construction failures and proceed, letting turn_runner
                # decide by its own rules (enforcement ON→access_context_missing; OFF→normal execution).
                # The first version returned ws_access_denied directly on construction failure, blocking dry-runs that
                # **previously worked** without enforcement (existing
                # test_dryrun_turn_carries_team_router immediately caught this). Intent is to restore the missing field,
                # without incidentally adding a rejection branch.
                # Independent review P2: class-only logs make all six AccessContextError causes identical,
                # forcing the next person to query the database, exactly what this commit fixes; log .code too.
                logger.warning(
                    "dryrun.access_context_unavailable ws=%s err=%s code=%s",
                    workspace_id,
                    type(exc).__name__,
                    getattr(exc, "code", None),
                )

        req = TurnRequest(
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user.id,
            member_uid=member_uid,
            datarc_absence_reason=_datarc_ctx.datarc_absence_reason,
            text=trigger_query,
            team_id=user.team_id,
            active_datarc_project=_datarc_ctx.active_datarc_project,
            datarc_client=_datarc_ctx.datarc_client,
            datarc_unavailable=_datarc_ctx.datarc_unavailable,
            vector_client=_datarc_ctx.vector_client,
            team_router=(team_model_runtime.router if team_model_runtime is not None else None),
            team_kb_retriever=(team_model_runtime.kb_retriever if team_model_runtime is not None else None),
            access_context=access_context,
        )
        from bowhead_gateway.turn_service import (
            admit_legacy_chat_execution,
            spawn_turn,
        )

        admitted_bus = await admit_legacy_chat_execution(
            orchestrator,
            req,
        )
        worker = await spawn_turn(
            orchestrator,
            req,
            name_prefix="workflow_dryrun",
            admitted_legacy_bus=admitted_bus,
            cleanup_grace_s=0,
        )
        result = await worker
        if result is None:
            raise RuntimeError("workflow_dryrun_result_unavailable")
        err = getattr(result, "error", None)
        ans = getattr(result, "final_answer", "") or ""
        if err:
            # ★ Previously checked only if err, **discarding its content without logging**, giving users
            # '试运行未能完成，请稍后重试' + retryable=true, though access_context_missing
            # and similar errors never recover through retry. Actual error codes required querying scratch-session turn records
            # in the database, with nothing in logs. Do not make the next person investigate this way.
            logger.warning(
                "dryrun.turn_failed session=%s ws=%s error=%s error_kind=%s",
                session_id,
                workspace_id,
                err,
                getattr(result, "error_kind", None),
            )
        return (
            ans,
            "workflow_dry_run_execution_failed" if err else None,
        )
    except Exception as e:
        logger.warning("dryrun.dispatch_failed err=%s", e)
        return "", "workflow_dry_run_execution_failed"


_JUDGE_SYSTEM = (
    "你是 recipe 试跑结果的严格评审。给你触发问题、recipe 试跑产出的最终答案、以及该 recipe 的"
    "自检 checkpoints。**逐条**判断最终答案是否满足每个 checkpoint（看实质内容不是措辞），给 "
    "pass/fail + 一句中文理由。整体 verdict：全部 checkpoint 通过=pass，否则=fail。只返 JSON。"
)


async def _judge_recipe(
    router_obj: Any,
    trigger_query: str,
    final_answer: str,
    checkpoints: list[str],
) -> tuple[str, list[dict], str | None]:
    """Judge one dry-run and return only a finite failure code."""

    if not checkpoints:
        return "skipped", [], None
    from bowhead_router import LLMRequest, Message

    cps = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(checkpoints))
    user_prompt = (
        f"触发问题：{trigger_query}\n\n试跑最终答案：\n{(final_answer or '(空)')[:3000]}\n\n"
        f"checkpoints：\n{cps}\n\n"
        '返回 JSON：{"verdict":"pass"|"fail","checkpoints":'
        '[{"text":"<原文>","pass":true,"reason":"<一句理由>"}]}'
    )
    try:
        resp = await asyncio.wait_for(
            router_obj.invoke(
                "judge",
                LLMRequest(
                    model="",
                    messages=[Message(role="user", content=user_prompt)],
                    system=_JUDGE_SYSTEM,
                    max_output_tokens=1500,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                ),
            ),
            timeout=_JUDGE_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning("dryrun.judge_failed err=%s", e)
        return "error", [], "workflow_dry_run_judge_failed"
    data = _parse_json_loose(resp.content or "")
    if not isinstance(data, dict):
        return "error", [], "workflow_dry_run_judge_failed"
    cp_results = data.get("checkpoints") if isinstance(data.get("checkpoints"), list) else []
    verdict = data.get("verdict")
    if verdict not in ("pass", "fail"):
        verdict = "pass" if cp_results and all(c.get("pass") for c in cp_results) else "fail"
    return verdict, cp_results, None


def _parse_json_loose(raw: str) -> dict | None:
    """json.loads；失败回退抓第一个 {...} 块（仿 kb_doc_types.py:242-252）."""
    raw = raw.strip()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return d if isinstance(d, dict) else None


async def _oral_fallback(
    request: Request,
    digest: TraceDigest,
    tool_refs: list[str],
    *,
    reason_code: str,
) -> dict[str, Any]:
    """§2.3：LLM 不可用/超时/坏 JSON → ok=false fallback=oral，仍带确定性 digest 供口头复述.

    返回与成功路径 **同构的键集**（name/description/body_md/subagent_spec/... 留空），FE 只按
    ``ok`` 分支即可，不会因缺键拿到 undefined（review test-quality P2-3）.
    """
    published = publish_skill_workflow_failure(reason_code)
    return {
        "ok": False,
        "fallback": "oral",
        "reason": await localize_error_message(request, reason_code),
        "reason_code": reason_code,
        "diagnostics": (published.to_wire() if published is not None else None),
        "suggested_form": digest.suggested_form,
        "spawn_count": digest.spawn_count,
        "name": "",
        "display_name": "",
        "description": "",
        "body_md": "",
        "subagent_spec": {"system_prompt": "", "output_schema": None, "tool_refs": tool_refs},
        "tool_refs": tool_refs,
        "slots": [],
        "checkpoints": [],
        "scripts": [{"name": s.name, "content": s.code, "from_seq": s.from_seq} for s in digest.scripts],
        "truncated_steps": digest.truncated_steps,
    }
