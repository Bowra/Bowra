"""M156-M3b · Clarification HITL HTTP API.

GET    /api/v1/clarifications/turn/{turn_id}     → 当 turn 所有 pending 请求
GET    /api/v1/clarifications/{request_id}       → 单条详情
POST   /api/v1/clarifications/{request_id}/resolve → Resolve the selected candidate and mark the request resolved

resolve 副作用:
  1. ClarificationStore: status pending → resolved
  3. SSE: emit clarification.resolved event
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal
from uuid import uuid4

from bowhead_core.clarification_store import (
    MAX_DIRECTIVE_CHARS,
    MAX_EDITED_SUB_DESC_CHARS,
    MAX_EDITED_SUB_TITLE_CHARS,
    MAX_EDITED_SUBS_COUNT,
    ClarificationStore,
    normalize_clarification_resolution,
    resume_context_is_readable,
)
from bowhead_core.events import OrchestratorEvent, get_event_registry
from bowhead_core.language_context import RESUME_LANGUAGE_KEY, resume_language_context, resume_viewer_time_zone
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, model_validator

from bowhead_core import LanguageContext, Orchestrator, TurnRequest

logger = logging.getLogger(__name__)
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.access_context import (
    AccessContextError,
    access_context_to_dict,
    build_access_context,
)
from bowhead_gateway.credit_admission import check_credit_admission
from bowhead_gateway.rule_clarification_errors import (
    normalize_rule_clarification_access_code,
    rule_clarification_http_error,
)
from bowhead_gateway.terminal_protocol_rollout import (
    PersistedChatExecutionProtocol,
    resolve_persisted_chat_execution_protocol,
)
from bowhead_gateway.turn_service import (
    clarification_requires_legacy_resume_fence,
    wait_for_legacy_turn_resume_ready,
)

router = APIRouter()

CLARIFICATION_SKIP_VALUE = "__bowhead_clarification_skip__"
QUESTION_SKIP_VALUE = "__bowhead_question_skip__"
CLARIFICATION_SKIP_TEXT = "用户跳过澄清，请根据上下文自行判断。"
QUESTION_SKIP_TEXT = "用户跳过该问题，请根据上下文判断。"
CLARIFICATION_ANSWERS_CONTEXT_KEY = "clarification_answers"

# PreferenceWriter fire-and-forget task set: strong references prevent GC (same as routes.chat._inflight_tasks).
# Tests can deterministically await writes through it, without sleep polling.
_preference_writer_tasks: set[asyncio.Task[None]] = set()


def _is_skip_value(value: str | None) -> bool:
    return value == CLARIFICATION_SKIP_VALUE


def _is_question_skip_value(value: str | None) -> bool:
    return value == QUESTION_SKIP_VALUE


def _skip_resume_answer(req: Any) -> str:
    token = (getattr(req, "token", "") or "").strip()
    if token:
        return f"{token}: {CLARIFICATION_SKIP_TEXT}"
    return CLARIFICATION_SKIP_TEXT


def _question_skip_resume_answer(question: str) -> str:
    q = (question or "").strip()
    return f"{q}: {QUESTION_SKIP_TEXT}" if q else QUESTION_SKIP_TEXT


def _clarify_store(request: Request) -> ClarificationStore:
    s = getattr(request.app.state, "clarification_store", None)
    if s is None:
        raise rule_clarification_http_error(
            503,
            "clarification_store_unavailable",
        )
    return s


def _current_user_id(request: Request) -> str:
    """P4.5.17: JWT > X-Bowhead-User-Id header > dev_default_user. P0 #2 多用户隔离."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


def _clarification_decision_action(clarification: Any) -> Any:
    from bowhead_core.terminal_protocol import DecisionAction

    resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else None
    if resume_context and resume_context.get("plan_reapprove_plan_id"):
        return DecisionAction.RESOLVE_WITHOUT_RESUME
    if clarification.kind in ("ambiguous_turn", "options", "tool_context_select") and resume_context is not None:
        return DecisionAction.RESUME
    return DecisionAction.RESOLVE_WITHOUT_RESUME


async def _terminal_clarification_factory(
    request: Request,
    clarification: Any,
    *,
    user_id: str,
) -> tuple[Any | None, str | None, str | None]:
    """Select the writer from persisted Turn version after surface ACL."""

    state = request.app.state
    # ``create_app()`` unit/integration harnesses intentionally skip the
    # lifespan and therefore have no protocol binding service at all.  They
    # exercise pre-0008 legacy rows.  A completed production lifespan always
    # installs the attribute: ``None`` is accepted only when startup proved a
    # zero-footprint legacy schema; every other missing reader fails closed.
    if not hasattr(state, "terminal_protocol_store"):
        resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else {}
        source_session_id = str(clarification.session_id or resume_context.get("session_id") or "").strip()
        return None, None, source_session_id or None
    protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
    if protocol_store is None:
        if getattr(state, "legacy_session_lifecycle_allowed", None) is True:
            resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else {}
            source_session_id = str(clarification.session_id or resume_context.get("session_id") or "").strip()
            return None, None, source_session_id or None
        raise rule_clarification_http_error(
            503,
            "terminal_protocol_writer_not_ready",
            nested_error=True,
        )
    resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else {}
    source_session_id = str(clarification.session_id or resume_context.get("session_id") or "").strip()
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise rule_clarification_http_error(
            503,
            "terminal_protocol_writer_not_ready",
            nested_error=True,
        )
    team_id = await user_store.get_workspace_team_id(clarification.workspace_id)
    if not team_id:
        raise rule_clarification_http_error(
            404,
            "clarification_not_found",
        )
    try:
        session_id, version = await protocol_store.read_persisted_turn_protocol_binding(
            team_id=team_id,
            turn_id=clarification.turn_id,
        )
        protocol = resolve_persisted_chat_execution_protocol(version)
    except Exception as exc:
        from bowhead_gateway.terminal_protocol_http import (
            terminal_protocol_http_error,
        )

        raise terminal_protocol_http_error(exc) from exc
    if source_session_id and source_session_id != session_id:
        raise rule_clarification_http_error(
            409,
            "terminal_protocol_malformed",
            nested_error=True,
        )
    if protocol is PersistedChatExecutionProtocol.LEGACY_V0:
        return None, team_id, session_id
    factory = getattr(request.app.state, "terminal_runtime_factory", None)
    if factory is None:
        raise rule_clarification_http_error(
            503,
            "terminal_protocol_writer_not_ready",
            nested_error=True,
        )
    return factory, team_id, session_id


async def _commit_tool_context_choice_effect(
    request: Request,
    clarification: Any,
    *,
    user_id: str,
    primary: str,
    remember: bool | None,
    strict: bool = False,
) -> str:
    if clarification.kind != "tool_context_select" or _is_skip_value(primary):
        return primary
    candidate = next(
        (item for item in clarification.candidates if item.value == primary),
        None,
    ) or next(
        (item for item in clarification.candidates if item.label == primary),
        None,
    )
    if candidate is None:
        logger.warning(
            "clarify.tool_context_choice_unmatched req=%s chosen=%r",
            clarification.request_id,
            primary,
        )
        if strict:
            raise RuntimeError("terminal_connector_context_source_changed")
        return primary
    from bowhead_gateway.connector_context_provider import (
        commit_connector_context_choice,
    )

    resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else {}
    committed = await commit_connector_context_choice(
        getattr(request.app.state, "user_memory_store", None),
        user_id=user_id,
        tool=(clarification.tool or "datarc"),
        context_id=candidate.value,
        context_name=candidate.label,
        remember=remember is not False,
        session_id=str(resume_context.get("session_id") or ""),
        log=logger,
    )
    if strict and not committed:
        raise RuntimeError("terminal_connector_context_projection_failed")
    return candidate.value if committed else primary


async def _require_clarification_lifecycle(
    clarification: Any,
    request: Request,
    *,
    operation: SpaceOperation,
) -> None:
    from bowhead_gateway.routes.auth import _dev_user_fallback_enabled

    if _dev_user_fallback_enabled(request):
        return
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise rule_clarification_http_error(
            503,
            "authz_unavailable",
            nested_error=True,
        )
    try:
        target_team_id = await user_store.get_workspace_team_id(clarification.workspace_id)
        if not target_team_id:
            raise AccessContextError("workspace_unavailable", 409)
        await build_access_context(
            user_store=user_store,
            access_store=getattr(
                request.app.state,
                "access_control_store",
                None,
            ),
            user_id=clarification.user_id,
            workspace_id=clarification.workspace_id,
            operation=operation,
            expected_team_id=target_team_id,
        )
    except AccessContextError as exc:
        raise rule_clarification_http_error(
            exc.status_code,
            normalize_rule_clarification_access_code(exc.code),
            nested_error=True,
        ) from exc


def _merge_clarified_user_text(original: str, chosen_values: list[str]) -> str:
    """Sprint followup-context-deck / clarification-true-continuation (B1) —
    build second-pass user_text after an empty-token (heuristic / options /
    multi-question) clarification resolve.

    APPENDS the pick(s) to the original ask instead of REPLACING it, so the
    deliverable/format intent survives: "帮我生成一个分析ppt" + pick "销售业绩"
    -> "帮我生成一个分析ppt (已明确: 销售业绩)", not the bare "销售业绩" (which
    dropped "ppt" -> Lead built a report, not a deck).

    B1 (clarification-true-continuation): APPEND ALL answers, not just the first.
    A multi-question clarification stores one entry per question in chosen_values
    (each already prefixed "问题: 答案"); the old single-value merge silently
    dropped questions 2..N -- prod t_f2f6a88ad3cc lost the "时间范围: 今年至今"
    answer, so the resumed turn ran without the time filter. Falls back to the
    pick(s) alone when the original ask was empty.
    """
    orig = (original or "").strip()
    vals = [v.strip() for v in chosen_values if v and v.strip()]
    if not vals:
        return original or ""
    merged = "; ".join(vals)
    return f"{orig} (已明确: {merged})" if orig else merged


def _project_answer_value(answer: ClarificationAnswerDTO) -> str:
    question = answer.question.strip()
    value = QUESTION_SKIP_VALUE if answer.source == "skip" else (answer.answer or "").strip()
    return f"{question}: {value}" if question else value


def _answer_to_resume_text(answer: ClarificationAnswerDTO) -> str:
    if answer.source == "skip":
        return _question_skip_resume_answer(answer.question)
    return _project_answer_value(answer)


def _split_legacy_question_value(value: str) -> tuple[str, str]:
    # Legacy projection is "{question}: {answer}". Split from the right so
    # question text like "Ratio (a:b): 3" keeps the colon inside the question.
    left, sep, right = (value or "").rpartition(": ")
    if not sep:
        return "", value.strip()
    return left.strip(), right.strip()


def _legacy_answers_from_values(values: list[str]) -> list[ClarificationAnswerDTO]:
    answers: list[ClarificationAnswerDTO] = []
    for raw in values:
        question, value = _split_legacy_question_value(raw)
        if _is_question_skip_value(value):
            answers.append(
                ClarificationAnswerDTO(
                    question=question or "clarification",
                    source="skip",
                    skipped=True,
                )
            )
        elif value:
            answers.append(
                ClarificationAnswerDTO(
                    question=question or "clarification",
                    source="custom",
                    answer=value,
                )
            )
    return answers


def _render_legacy_values_for_resume(values: list[str]) -> list[str]:
    rendered: list[str] = []
    for raw in values:
        question, value = _split_legacy_question_value(raw)
        if _is_question_skip_value(value):
            rendered.append(_question_skip_resume_answer(question))
        elif raw and raw.strip():
            rendered.append(raw.strip())
    return rendered


async def _plan_card_action(
    request: Request,
    *,
    rc: dict | None,
    user_id: str,
    workspace_id: str,
) -> tuple[str, str | None] | None:
    """plan 卡 resolve 的统一 action 分派(graph-mode batch2-hitl-design §1-D2).

    主路/重放路/recovery **三处**分派点共用(review P0-2: 漏任何一处=幂等破坏)。
    返回 (action, reapprove_plan_id);非 plan 卡返回 None(走泛 turn_resume/noop)。

    - `plan_reapprove_plan_id`(预检卡) → 恒 plan_reapprove(WP-2 既有语义);
    - `plan_midrun_plan_id`(批2 mid-run 卡) → 按 plan **实时状态**三值分派:
        RUNNING → plan_midrun_ack(不 spawn/不建 lease/不 reapprove; driver 拾取)
        BLOCKED → plan_reapprove(既有 FE 自动重批链, 收敛后答卡自动续跑)
        其余(终态/查无/读异常) → plan_midrun_ack(诚实 ack——绝不自动 reapprove:
          误复活刚 cancel 的 plan 是错的, 且撞 try_claim_running 收单集必报错;
          读失败兜底=ack 最坏多点一次, reapprove 最坏报错+误复活。review P1-5)
    """
    if not rc:
        return None
    if rc.get("plan_reapprove_plan_id"):
        return ("plan_reapprove", str(rc["plan_reapprove_plan_id"]))
    mid = rc.get("plan_midrun_plan_id")
    if not mid:
        return None
    plan_id = str(mid)
    status = ""
    try:
        plan_store = getattr(request.app.state, "plan_store", None)
        if plan_store is not None:
            plan = await plan_store.get_plan(plan_id, user_id=user_id, workspace_id=workspace_id)
            status = str(getattr(plan, "status", "") or "")
    except Exception:
        logger.warning("plan_midrun.action_status_read_failed plan=%s", plan_id)
        status = ""
    if status == "blocked":
        return ("plan_reapprove", plan_id)
    return ("plan_midrun_ack", None)


async def scheduled_run_for_turn(app: Any, turn_id: str) -> Any | None:
    """定时任务出身反查（scheduled-recipe-runs S3）：turn → `scheduled_job_runs` 行 / None。

    判据用 run 表反查而不是 session origin / `t_sched_` 前缀——fresh 定时 session 的 origin 是
    'normal'，anchor 模式下 session 本就是用户自己的会话，两者都判不准（前缀只作 defense-in-depth）。
    Web resolve/dismiss 用它校验 scheduled run 的终态约束，`/resume_turn` 用它水合定时任务语义。
    所有决议入口的 dispatch 就绪门独立且统一，不再按 scheduled 来源豁免。反查读失败必须
    fail closed；把「无法证明 ownership」降级成普通 chat 会绕过 scheduled awaiting 状态机。
    """
    store = getattr(app.state, "scheduled_store", None)
    if store is None:
        return None
    try:
        return await store.get_run_by_turn(turn_id)
    except Exception as exc:
        logger.exception("clarify.scheduled_origin_lookup_failed turn=%s", turn_id)
        raise RuntimeError("scheduled clarification ownership unavailable") from exc


def _record_stale_resolve(source: str) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_clarification_stale_resolve_total",
            labels={"source": source},
            help="Clarification resolve attempts after a terminal winner.",
        )
    except Exception:  # telemetry must not change resolve semantics  # noqa: S110
        pass


async def _ensure_clarification_resolved_event(
    request: Request,
    *,
    turn_id: str,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    """Persist the resolution receipt once before any resume is acknowledged."""
    turn_log_store = getattr(request.app.state, "turn_log_store", None)

    from bowhead_gateway.turn_service import turn_seq_seed

    registry = get_event_registry()
    seed = await turn_seq_seed(turn_log_store, turn_id)
    await registry.emit_out_of_band(
        turn_id,
        OrchestratorEvent(
            event_type="clarification.resolved",
            turn_id=turn_id,
            payload=payload,
            persist_before_delivery=True,
            durable_event_id=f"evt_clarification_resolution_{request_id}",
        ),
        fallback_initial_seq=seed,
    )


async def _canonical_resolution_outcome(
    clarify: ClarificationStore,
    *,
    request_id: str,
    turn_id: str,
    legacy_fallback: dict[str, Any],
) -> dict[str, Any]:
    """Read the winner's outbox payload, waiting for an in-flight winner.

    Rows resolved before the outbox existed use the deterministic legacy
    fallback after a short wait. New concurrent losers never publish their
    request-local answer or side-effect facts.
    """
    for _ in range(100):
        outcome = await clarify.get_resolution_outcome(request_id)
        if outcome is not None:
            return outcome
        await asyncio.sleep(0.02)
    state_probe = getattr(clarify, "get_resolution_work_state", None)
    state = await state_probe(request_id) if callable(state_probe) else None
    if state == "pending":
        # A live or recoverable owner still owns the frozen work. A loser must
        # never turn a timeout into a fabricated completed outcome.
        raise rule_clarification_http_error(
            503,
            "clarification_resolution_pending",
            nested_error=True,
            headers={"Retry-After": "1"},
        )
    return await clarify.record_resolution_outcome(
        request_id=request_id,
        turn_id=turn_id,
        payload=legacy_fallback,
    )


async def _revalidate_resume_access_context(
    request: Request,
    *,
    user_id: str,
    workspace_id: str,
    team_id: str | None,
    access_context: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return team_id, access_context
    try:
        target_team_id = team_id or await user_store.get_workspace_team_id(workspace_id)
        if not target_team_id:
            raise AccessContextError("workspace_unavailable", 409)
        ctx = await build_access_context(
            user_store=user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user_id,
            workspace_id=workspace_id,
            operation=SpaceOperation.EXECUTE,
            expected_team_id=target_team_id,
        )
    except AccessContextError as exc:
        raise rule_clarification_http_error(
            exc.status_code,
            normalize_rule_clarification_access_code(exc.code),
            nested_error=True,
        ) from exc
    return ctx.team_id, access_context_to_dict(ctx)


class CandidateDTO(BaseModel):
    value: str
    label: str
    hint: str = ""
    confidence: str = ""


class ClarificationDTO(BaseModel):
    request_id: str
    turn_id: str
    workspace_id: str
    token: str
    rationale: str
    candidates: list[CandidateDTO]
    status: str
    chosen_value: str | None = None
    chosen_values: list[str] | None = None  # V2.7: options/task_plan multiselect write-back.
    chosen_at: str | None = None
    created_at: str
    # V2.7 P1/P2: frontend renders different UI by kind.
    kind: str = "token"
    multi_select: bool = False
    questions: list[dict[str, Any]] = Field(default_factory=list)


class ResumeFailureDTO(BaseModel):
    request_id: str
    turn_id: str
    execution_generation: int
    code: Literal["clarification_resume_unavailable"]
    recovery_action: Literal["copy_diagnostics"] = "copy_diagnostics"
    diagnostics: dict[str, Any] | None = None


class ListDTO(BaseModel):
    items: list[ClarificationDTO]
    resume_failure: ResumeFailureDTO | None = None


async def _ordinary_resume_failure(request: Request, store: ClarificationStore, turn_id: str) -> dict[str, Any] | None:
    """Project current unconsumed-answer availability without scheduling work.

    Resolution and continuation are separate durable facts. Reuse the resume
    preflight for the original card; no failure ledger or historical backfill
    is needed. Only a settled, still-paused generation can publish this notice.
    """
    from types import SimpleNamespace

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.ordinary_lead_continuation import ordinary_card_id_in_tx, ordinary_resume_request_material

    from bowhead_gateway.rule_clarification_errors import is_resume_probe_unavailable, rule_clarification_error_payload
    from bowhead_gateway.turn_service import _restore_ordinary_question_inputs

    orchestrator = getattr(request.app.state, "orchestrator", None)
    sessions = getattr(orchestrator, "session_store", None)
    if sessions is None:
        return None
    turn = await sessions.get_turn(turn_id)
    if turn is None or turn.status != "clarification_pending" or not await sessions.is_turn_resume_ready(turn_id):
        return None
    async with open_shared_ctx(store.db_path) as db:
        await db.execute("BEGIN")
        card_id = await ordinary_card_id_in_tx(db, turn_id=turn_id)
    card = await store.get(card_id) if card_id else None
    if card is None or card.user_id != _current_user_id(request) or card.status != "resolved" or card.injected_at is not None:
        return None
    try:
        _, context = ordinary_resume_request_material(card)
        await _restore_ordinary_question_inputs(
            orchestrator,
            SimpleNamespace(
                ordinary_clarification_id=card.request_id,
                team_id=context["team_id"],
                user_id=card.user_id,
                workspace_id=card.workspace_id,
                session_id=card.session_id,
                turn_id=card.turn_id,
                active_datarc_project=context.get("active_datarc_project"),
            ),
        )
    except Exception as exc:
        if not is_resume_probe_unavailable(exc):
            raise
        # Discard a slow probe if another worker resumed or settled this Turn.
        current = await sessions.get_turn(turn_id)
        if (
            current is None
            or current.status != "clarification_pending"
            or current.execution_generation != turn.execution_generation
            or not await sessions.is_turn_resume_ready(turn_id)
        ):
            return None
        return {
            "request_id": card.request_id,
            "turn_id": turn_id,
            "execution_generation": turn.execution_generation,
            **rule_clarification_error_payload("clarification_resume_unavailable", clarification_stage="clarification_resume_preflight"),
        }
    return None


class EditedSubtaskDTO(BaseModel):
    """Editable title and description on historical task-plan cards.

    Other persisted subtask fields retain their original values.
    """

    title: str = Field(max_length=MAX_EDITED_SUB_TITLE_CHARS)
    description: str = Field(default="", max_length=MAX_EDITED_SUB_DESC_CHARS)


class ClarificationAnswerDTO(BaseModel):
    """Canonical per-question answer semantics for grouped clarification.

    `chosen_values` remains a legacy projection, but orchestration should prefer
    this shape when present so localized UI copy never becomes protocol.
    """

    question: str = Field(min_length=1, max_length=500)
    source: Literal["candidate", "custom", "skip"]
    answer: str | None = Field(default=None, max_length=256)
    skipped: bool = False
    option_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_semantics(self) -> ClarificationAnswerDTO:
        self.question = self.question.strip()
        if not self.question:
            raise ValueError("answer question required")
        if self.source == "skip":
            if not self.skipped:
                raise ValueError("skip answer requires skipped=true")
            self.answer = None
            self.option_index = None
            return self
        answer = (self.answer or "").strip()
        if not answer:
            raise ValueError("candidate/custom answer requires non-empty answer")
        self.answer = answer
        self.skipped = False
        return self


def _answers_to_context(answers: list[ClarificationAnswerDTO]) -> list[dict[str, Any]]:
    return [answer.model_dump(exclude_none=True) for answer in answers]


def _answers_from_context(ctx: dict[str, Any] | None) -> list[ClarificationAnswerDTO]:
    if not ctx:
        return []
    raw = ctx.get(CLARIFICATION_ANSWERS_CONTEXT_KEY)
    if not isinstance(raw, list):
        return []
    answers: list[ClarificationAnswerDTO] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            answers.append(ClarificationAnswerDTO.model_validate(item))
        except Exception:
            logger.debug("clarify.answers_context_invalid item=%r", item)
    return answers


class ResolveRequest(BaseModel):
    # V2.4 (token kind): single select, chosen_value required.
    # V2.7 P1/P2 (options/task_plan kind): multiselect, chosen_values required; chosen_value
    # remains accepted (old frontend / token compatibility), but chosen_values takes priority.
    chosen_value: str | None = Field(default=None, max_length=256)
    chosen_values: list[str] | None = Field(default=None, max_length=20)
    # Canonical per-question semantic contract. Existing chosen_values stays as
    # a compatibility projection for older clients and store rows.
    answers: list[ClarificationAnswerDTO] | None = Field(default=None, max_length=20)
    # User intentionally skips the clarification; the resumed turn should let
    # the model choose from context instead of forcing a candidate.
    skip: bool = False
    # M158 #10 P1: editable HITL, meaningful only for task_plan kind.
    # edited_subs: user-edited plan, replacing the default inferred from chosen_values + original candidates.
    # user_directive: long free-form instruction; hard cap uses clarification_store.MAX_DIRECTIVE_CHARS
    # as single source (CTO review #7 DRY). Next turn_runner force_spawn injects subagent extra_instructions.
    edited_subs: list[EditedSubtaskDTO] | None = Field(default=None, max_length=MAX_EDITED_SUBS_COUNT)
    user_directive: str | None = Field(default=None, max_length=MAX_DIRECTIVE_CHARS)
    # Batch 2 (PRD §2.1 step 3): tool_context_select-specific remember-choice modes:
    # None/True = explicit permanent preference; False = session preference (no repeat question in this session,
    # reconfirm on next session's first query). Ignore for other kinds. Old FE omission = remember (safe default).
    remember: bool | None = None


class ResolveResultDTO(BaseModel):
    ok: bool
    request_id: str
    chosen_value: str  # legacy / primary value
    chosen_values: list[str]  # V2.7: all selected options.
    answers: list[ClarificationAnswerDTO] = Field(default_factory=list)
    skipped: bool = False
    workspace_id: str  # Actual written workspace_id (* or specific ID).
    decision_id: str | None = None
    decision_state: str | None = None
    # Sprint chat-v4-streaming Phase 4C step 3 — HITL same-turn-resume action.
    # action="turn_resume": resolve starts resume before ACK and returns stream/generation;
    # old clients may still call /resume_turn idempotently.
    #   (kind=ambiguous_turn / options with resume_context already set)
    # action="noop" (default): FE fallback starts a new turn (chat-send omits prior_turn_id, removed in 0.26.2).
    #   Retain because old clients / early clarification kinds may lack resume_context.
    action: str = "noop"
    resume_turn_id: str | None = None  # = req.turn_id when action="turn_resume"
    stream_url: str | None = None
    execution_generation: int | None = None
    # planning-mode-audit WP-2: plan preflight picker completes the loop:
    # action="plan_reapprove": FE calls existing approvePlanViaSend(reapprove_plan_id)
    # to reapprove automatically (plan execution turn cannot resume; never return turn_resume).
    reapprove_plan_id: str | None = None


async def _recover_web_resolution_work(
    *,
    request_id: str,
    req: Any,
    work: dict[str, Any],
    resolution_owner: str,
    request: Request,
    response: Response,
    clarify: ClarificationStore,
) -> ResolveResultDTO:
    """Finish expired, fenced web resolution work from its frozen input."""
    payload = ResolveRequest.model_validate(work.get("payload") or {})
    values = list(req.chosen_values or ([req.chosen_value] if req.chosen_value else []))
    primary = req.chosen_value or (values[0] if values else "")
    skip_resolve = _is_skip_value(primary)
    semantic_answers = [ClarificationAnswerDTO.model_validate(value) for value in (req.answers or [])] or payload.answers or []
    response_answers = [] if skip_resolve else (semantic_answers or _legacy_answers_from_values(values))
    if not req.answers and semantic_answers and isinstance(req.resume_context, dict):
        ctx = dict(req.resume_context)
        ctx[CLARIFICATION_ANSWERS_CONTEXT_KEY] = _answers_to_context(semantic_answers)
        await clarify.set_resume_context(request_id, ctx)
        req.resume_context = ctx

    if req.kind == "tool_context_select" and not skip_resolve:
        candidate = next((c for c in req.candidates if c.value == primary), None) or next(
            (c for c in req.candidates if c.label == primary),
            None,
        )
        if candidate is not None:
            from bowhead_gateway.connector_context_provider import (
                commit_connector_context_choice,
            )

            rc = req.resume_context if isinstance(req.resume_context, dict) else {}
            await commit_connector_context_choice(
                getattr(request.app.state, "user_memory_store", None),
                user_id=req.user_id,
                tool=(req.tool or "datarc"),
                context_id=candidate.value,
                context_name=candidate.label,
                remember=payload.remember is not False,
                session_id=str(rc.get("session_id") or ""),
                log=logger,
            )
            primary = candidate.value
            if values:
                values[0] = primary

    target_ws = req.workspace_id
    canonical = await clarify.record_resolution_outcome(
        request_id=request_id,
        turn_id=req.turn_id,
        payload={
            "request_id": request_id,
            "chosen_value": primary,
            "chosen_values": values,
            "answers": [answer.model_dump() for answer in response_answers],
            "skipped": skip_resolve,
            "kind": req.kind,
            "workspace_id": target_ws,
            "has_directive": bool(req.user_directive),
            "edited_subs_count": len(req.edited_subs or []),
        },
        resolution_owner=resolution_owner,
    )
    await _ensure_clarification_resolved_event(
        request,
        turn_id=req.turn_id,
        request_id=request_id,
        payload=canonical,
    )

    action = "noop"
    resume_turn_id: str | None = None
    reapprove_plan_id: str | None = None
    rc = req.resume_context if isinstance(req.resume_context, dict) else None
    _pa = await _plan_card_action(request, rc=rc, user_id=req.user_id, workspace_id=req.workspace_id)
    if _pa is not None:
        action, reapprove_plan_id = _pa
    elif req.kind in ("ambiguous_turn", "options", "tool_context_select") and rc is not None:
        action = "turn_resume"
        resume_turn_id = req.turn_id
    resumed = None
    if action == "turn_resume":
        resumed = await resume_turn(request_id, request, response, clarify)
    return ResolveResultDTO(
        ok=True,
        request_id=request_id,
        chosen_value=str(canonical.get("chosen_value") or ""),
        chosen_values=[str(value) for value in canonical.get("chosen_values") or []],
        answers=canonical.get("answers") or [],
        skipped=bool(canonical.get("skipped", False)),
        workspace_id=str(canonical.get("workspace_id") or req.workspace_id),
        action=action,
        resume_turn_id=resume_turn_id,
        stream_url=(resumed.stream_url if resumed else None),
        execution_generation=(resumed.execution_generation if resumed else None),
        reapprove_plan_id=reapprove_plan_id,
    )


def _to_dto(req: Any) -> ClarificationDTO:
    from bowhead_core.clarification_resume_context import (
        DocumentResumeContext,
        LegacyResumeContext,
        OrdinaryLeadResumeContext,
        decode_clarification_context,
    )
    from bowhead_core.clarification_rounds import CONTEXT_KEY, QUESTIONS_KEY
    from bowhead_core.document_clarification_lifecycle import document_question

    questions = []
    if req.resume_context and resume_context_is_readable(req.resume_context):
        decoded = decode_clarification_context(req)
        if isinstance(decoded, DocumentResumeContext):
            questions = document_question(req)
        elif isinstance(decoded, OrdinaryLeadResumeContext):
            questions = decoded.questions
        elif isinstance(decoded, LegacyResumeContext) and isinstance(decoded.snapshot.get(CONTEXT_KEY), dict):
            # Bounded cards keep their questions beside the round marker. Cards written
            # before that key existed have only hashes and keep returning [].
            stored = decoded.snapshot.get(QUESTIONS_KEY)
            questions = [q for q in stored if isinstance(q, dict)] if isinstance(stored, list) else []
    return ClarificationDTO(
        request_id=req.request_id,
        turn_id=req.turn_id,
        workspace_id=req.workspace_id,
        token=req.token,
        rationale=req.rationale,
        candidates=[
            CandidateDTO(
                value=c.value,
                label=c.label,
                hint=c.hint,
                confidence=c.confidence,
            )
            for c in req.candidates
        ],
        status=req.status,
        chosen_value=req.chosen_value,
        chosen_values=req.chosen_values,
        chosen_at=req.chosen_at,
        created_at=req.created_at,
        kind=getattr(req, "kind", "token"),
        multi_select=bool(getattr(req, "multi_select", False)),
        questions=questions,
    )


@router.get("/turn/{turn_id}", response_model=ListDTO)
async def list_for_turn(
    turn_id: str,
    request: Request,
    store: ClarificationStore = Depends(_clarify_store),
) -> ListDTO:
    from bowhead_gateway.routes.turns import _require_turn_access

    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    items = await store.get_pending_for_turn(turn_id)
    cur_uid = _current_user_id(request)
    visible = []
    from bowhead_core.clarification_resume_context import DocumentResumeContext, OrdinaryLeadResumeContext, decode_clarification_context

    for item in items:
        if item.user_id != cur_uid:
            continue
        if (
            item.resume_context
            and resume_context_is_readable(item.resume_context)
            and isinstance(decode_clarification_context(item), (DocumentResumeContext, OrdinaryLeadResumeContext))
        ):
            orchestrator = getattr(request.app.state, "orchestrator", None)
            session_store = getattr(orchestrator, "session_store", None)
            if session_store is None:
                raise rule_clarification_http_error(503, "clarification_orchestrator_unavailable")
            if not await session_store.is_turn_resume_ready(turn_id):
                # Live and history readers use this list to invalidate cards.
                # An unsettled pause is not an authoritative empty pending set.
                raise rule_clarification_http_error(409, "clarification_pause_pending")
        visible.append(_to_dto(item))
    return ListDTO(items=visible, resume_failure=await _ordinary_resume_failure(request, store, turn_id) if not visible else None)


@router.get("/{request_id}", response_model=ClarificationDTO)
async def get_one(
    request_id: str,
    request: Request,
    store: ClarificationStore = Depends(_clarify_store),
) -> ClarificationDTO:
    req = await store.get(request_id)
    if not req:
        raise rule_clarification_http_error(404, "clarification_not_found")

    cur_uid = _current_user_id(request)
    if req.user_id != cur_uid:
        raise rule_clarification_http_error(404, "clarification_not_found")
    await _require_clarification_lifecycle(
        req,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    from bowhead_core.clarification_resume_context import DocumentResumeContext, OrdinaryLeadResumeContext, decode_clarification_context

    if req.status == "pending" and req.resume_context and resume_context_is_readable(req.resume_context):
        if isinstance(decode_clarification_context(req), (DocumentResumeContext, OrdinaryLeadResumeContext)):
            orchestrator = getattr(request.app.state, "orchestrator", None)
            session_store = getattr(orchestrator, "session_store", None)
            if session_store is None:
                raise rule_clarification_http_error(503, "clarification_orchestrator_unavailable")
            if not await session_store.is_turn_resume_ready(req.turn_id):
                raise rule_clarification_http_error(409, "clarification_pause_pending")
    return _to_dto(req)


@router.post("/{request_id}/resolve", response_model=ResolveResultDTO, dependencies=[Depends(require_not_readonly)])
async def resolve_one(
    request_id: str,
    payload: ResolveRequest,
    request: Request,
    response: Response,
    clarify: ClarificationStore = Depends(_clarify_store),
) -> ResolveResultDTO:
    """Resolve the selected candidate and emit the durable result.

    P0 #2: 校验 req 属于当前用户; 跨用户 resolve 拒.
    """
    resolution_owner = f"web_{uuid4().hex}"
    # Peek without changing status, then validate ownership.
    pre = await clarify.get(request_id)
    if not pre:
        raise rule_clarification_http_error(404, "clarification_not_found")
    cur_uid = _current_user_id(request)
    if pre.user_id != cur_uid:
        raise rule_clarification_http_error(404, "clarification_not_found")
    await _require_clarification_lifecycle(
        pre,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    # Reject before claiming resolution work or consuming an answer. An
    # unsupported snapshot must never fall through to a new-Turn action.
    if pre.resume_context is not None and not resume_context_is_readable(pre.resume_context):
        raise rule_clarification_http_error(409, "clarification_resume_unavailable")
    from bowhead_core.clarification_resume_context import DocumentResumeContext, decode_clarification_context

    try:
        document_card = pre.resume_context is not None and isinstance(decode_clarification_context(pre), DocumentResumeContext)
    except ValueError as exc:
        raise rule_clarification_http_error(409, "clarification_resume_unavailable") from exc
    if document_card and pre.status in {"pending", "dismissed"}:
        answer_skip = bool(
            payload.answers and len(payload.answers) == 1 and payload.answers[0].source == "skip" and payload.answers[0].question == pre.rationale
        )
        if payload.skip or answer_skip:
            await dismiss_clarification(request_id, request, clarify)
            current = await request.app.state.orchestrator.session_store.get_turn(pre.turn_id)
            return ResolveResultDTO(
                ok=True,
                request_id=request_id,
                chosen_value="",
                chosen_values=[],
                answers=[],
                skipped=True,
                workspace_id=pre.workspace_id,
                action="turn_settled",
                resume_turn_id=pre.turn_id,
                stream_url=f"/api/v1/chat/stream/{pre.turn_id}",
                execution_generation=current.execution_generation,
            )
        if pre.status == "pending":
            from dataclasses import replace

            from bowhead_core.document_clarification_lifecycle import document_answer

            try:
                document_answer(replace(pre, status="resolved", answers=[answer.model_dump() for answer in (payload.answers or [])]))
            except ValueError as exc:
                raise rule_clarification_http_error(422, "clarification_answer_required") from exc
    terminal_factory, terminal_team_id, terminal_session_id = await _terminal_clarification_factory(
        request,
        pre,
        user_id=cur_uid,
    )

    # Sprint followup-context-deck (P0-C) — idempotency guard. Without it the FE
    # (double-click / retry / a card not disabled after submit) resolves the
    # same clarification more than once; each call re-emits clarification.resolved
    # and re-runs preference side effects. In prod trace t_0e818cb5 the
    # SAME request was resolved 3× (2 identical + 1 DIFFERENT option fired 19s
    # into the already-running resume turn) — the "HITL twice" the user saw.
    # Once resolved, the answer is locked: return the original outcome, emit
    # nothing, run no side effects. (resume_turn has its own idempotency guard.)
    if pre.status == "resolved" and terminal_factory is None:
        claim_work = getattr(clarify, "claim_resolution_work", None)
        if callable(claim_work):
            frozen_work = await claim_work(
                request_id=request_id,
                channel="web",
                resolution_owner=resolution_owner,
            )
            if frozen_work is not None:
                return await _recover_web_resolution_work(
                    request_id=request_id,
                    req=pre,
                    work=frozen_work,
                    resolution_owner=resolution_owner,
                    request=request,
                    response=response,
                    clarify=clarify,
                )
        _existing_vals = pre.chosen_values or ([pre.chosen_value] if pre.chosen_value else [])
        _existing_skipped = _is_skip_value(pre.chosen_value)
        _replay_action = "noop"
        _replay_turn_id: str | None = None
        _replay_reapprove: str | None = None
        _replay_rc = pre.resume_context if isinstance(pre.resume_context, dict) else None
        # Batch 2 D2 (review P0-2): all three dispatch sites use _plan_card_action. Missing replay turns double-clicks
        # from idempotent success into 409, rebreaking followup-context-deck P0-C's idempotency fix.
        _replay_pa = await _plan_card_action(request, rc=_replay_rc, user_id=pre.user_id, workspace_id=pre.workspace_id)
        if _replay_pa is not None:
            _replay_action, _replay_reapprove = _replay_pa
        elif pre.kind in ("ambiguous_turn", "options", "tool_context_select") and pre.resume_context is not None:
            _replay_action = "turn_resume"
            _replay_turn_id = pre.turn_id
        stored_answers = [ClarificationAnswerDTO.model_validate(value) for value in (pre.answers or [])] or _answers_from_context(pre.resume_context)
        replay_answers = [] if _existing_skipped else (stored_answers or _legacy_answers_from_values(_existing_vals))
        legacy_payload = {
            "request_id": request_id,
            "chosen_value": pre.chosen_value or (_existing_vals[0] if _existing_vals else ""),
            "chosen_values": _existing_vals,
            "answers": [answer.model_dump() for answer in replay_answers],
            "skipped": _existing_skipped,
            "kind": pre.kind,
            "workspace_id": pre.workspace_id,
            "has_directive": bool(pre.user_directive),
            "edited_subs_count": len(pre.edited_subs or []),
        }
        outcome = await _canonical_resolution_outcome(
            clarify,
            request_id=request_id,
            turn_id=pre.turn_id,
            legacy_fallback=legacy_payload,
        )
        replay_resume = None
        # A lost response or durable event failure is repaired for every
        # clarification kind, not only same-Turn resumes.
        try:
            await _ensure_clarification_resolved_event(
                request,
                turn_id=pre.turn_id,
                request_id=request_id,
                payload=outcome,
            )
        except Exception as exc:
            logger.exception("clarify.event_repair_failed turn=%s", pre.turn_id)
            raise rule_clarification_http_error(
                503,
                "clarification_receipt_not_durable",
                nested_error=True,
            ) from exc
        if _replay_action == "turn_resume":
            replay_resume = await resume_turn(
                request_id,
                request,
                response,
                clarify,
            )
        return ResolveResultDTO(
            ok=True,
            request_id=request_id,
            chosen_value=str(outcome.get("chosen_value") or ""),
            chosen_values=[str(value) for value in outcome.get("chosen_values") or []],
            answers=outcome.get("answers") or [],
            skipped=bool(outcome.get("skipped", False)),
            workspace_id=str(outcome.get("workspace_id") or pre.workspace_id),
            action=_replay_action,
            resume_turn_id=_replay_turn_id,
            stream_url=(replay_resume.stream_url if replay_resume else None),
            execution_generation=(replay_resume.execution_generation if replay_resume else None),
            reapprove_plan_id=_replay_reapprove,
        )
    if pre.status == "expired":
        _exp_rc = pre.resume_context if isinstance(pre.resume_context, dict) else None
        if _exp_rc is not None and _exp_rc.get("plan_midrun_plan_id"):
            # Batch 3 (review R1-F11, impl review P1-2): mid-run cards already targeted for expiry by revision/replanning
            # receive an honest ACK (no 409, never reapprove: answering a dead card must not rerun the entire new graph
            # as an accidental execution trigger). Choices have nowhere to take effect; use skipped shape.
            return ResolveResultDTO(
                ok=True,
                request_id=request_id,
                chosen_value="",
                chosen_values=[],
                answers=[],
                skipped=True,
                workspace_id=pre.workspace_id,
                action="plan_midrun_ack",
                resume_turn_id=None,
                stream_url=None,
                execution_generation=None,
                reapprove_plan_id=None,
            )
    if pre.status != "pending" and not (pre.status == "resolved" and terminal_factory is not None):
        # dismissed / other terminal state — not resolvable.
        _record_stale_resolve("terminal_row")
        raise rule_clarification_http_error(
            409,
            "clarification_not_resolvable",
        )

    is_same_turn_resume = clarification_requires_legacy_resume_fence(pre)
    if is_same_turn_resume:
        # Credit admission must precede **answer consumption**. Once resolve CAS marks the row resolved, the card disappears
        # from chat (multi-question cards leave no 402 message), stranding this turn clarification_pending with
        # no clickable continuation (116 dead end observed 2026-09-20). Gate here to retain pending row/card;
        # after recharge, another click continues.
        # Retain resume_turn's same-criterion gate for direct /resume_turn calls, resolved replay, and a race where
        # another session exhausts the balance after this admission.
        credit_store = getattr(request.app.state, "user_store", None)
        credit_team_id = None
        if credit_store is not None:
            credit_user = await credit_store.get_user(cur_uid)
            credit_team_id = getattr(credit_user, "team_id", None)
        credit_denial = await check_credit_admission(
            credit_store,
            team_id=credit_team_id,
            user_id=cur_uid,
            workspace_id=pre.workspace_id,
            metrics_log=getattr(request.app.state, "metrics_log", None),
            turn_id=pre.turn_id,
        )
        if credit_denial is not None:
            raise rule_clarification_http_error(402, credit_denial, nested_error=True)
    if is_same_turn_resume and terminal_factory is None:
        # Do not consume the answer while the producing worker is still between
        # visible clarification_required, durable clarification_pending, and
        # dispatch settlement. A 409 leaves the store row pending and the card
        # actionable; retry cannot create a resolved-without-worker tombstone.
        orchestrator = getattr(request.app.state, "orchestrator", None)
        session_store = getattr(orchestrator, "session_store", None)
        if session_store is None:
            raise rule_clarification_http_error(
                503,
                "clarification_orchestrator_unavailable",
            )
        try:
            scheduled_origin = await scheduled_run_for_turn(request.app, pre.turn_id)
        except RuntimeError as exc:
            raise rule_clarification_http_error(
                503,
                "clarification_store_unavailable",
                nested_error=True,
            ) from exc
        if scheduled_origin is not None and scheduled_origin.status != "awaiting_input":
            if scheduled_origin.status in {
                "success",
                "failed",
                "skipped",
                "expired",
            }:
                error = str(scheduled_origin.error or "")
                if error == "scheduled_input_timeout":
                    repair_status = "expired"
                    repair_reason = "scheduled_input_timeout"
                elif error == "scheduled_input_superseded":
                    repair_status = "dismissed"
                    repair_reason = "scheduled_input_superseded"
                elif error == "scheduled_anchor_session_deleted":
                    repair_status = "dismissed"
                    repair_reason = "scheduled_session_deleted"
                else:
                    repair_status = "dismissed"
                    repair_reason = "scheduled_run_terminal_repair"
                repaired = await clarify.repair_terminal_invalidation(
                    request_id,
                    target_status=repair_status,
                    reason=repair_reason,
                )
                if repaired.invalidation is not None:
                    from bowhead_gateway.clarification_invalidation import (
                        publish_pending_clarification_invalidations,
                    )

                    try:
                        await publish_pending_clarification_invalidations(
                            request.app,
                            request_ids=[request_id],
                        )
                    except Exception:
                        logger.exception(
                            "clarify.terminal_repair_publish_failed request=%s",
                            request_id,
                        )
                _record_stale_resolve("scheduled_terminal_run")
                raise rule_clarification_http_error(
                    409,
                    "clarification_not_resolvable",
                )
            raise rule_clarification_http_error(
                409,
                "clarification_resume_not_ready",
            )
        resume_ready = await wait_for_legacy_turn_resume_ready(
            session_store,
            pre.turn_id,
        )
        if not resume_ready:
            # Another identical resolve may have won the pending→resolved CAS
            # while this request was polling the producer fence, then advanced
            # the Turn to running before our next readiness probe.  Re-enter
            # the canonical resolved replay path instead of waiting seven
            # seconds and misreporting a successful resolve as not-ready.
            resolved_during_wait = await clarify.get(request_id)
            if resolved_during_wait is not None and resolved_during_wait.status == "resolved":
                return await resolve_one(
                    request_id,
                    payload,
                    request,
                    response,
                    clarify,
                )
            raise rule_clarification_http_error(
                409,
                "clarification_resume_not_ready",
            )

    # V2.7 P1: normalize chosen_value/chosen_values. token single-select; options/task_plan multiselect.
    values: list[str] = []
    semantic_answers = payload.answers or []
    if payload.skip and pre.resume_context and pre.resume_context.get("__schema_version") == 4:
        from bowhead_core.clarification_resume_context import OrdinaryLeadResumeContext, decode_clarification_context

        ordinary_context = decode_clarification_context(pre)
        if not isinstance(ordinary_context, OrdinaryLeadResumeContext):
            raise rule_clarification_http_error(409, "clarification_resume_unavailable")
        semantic_answers = [
            ClarificationAnswerDTO(question=question["question"], source="skip", skipped=True) for question in ordinary_context.questions
        ]
    if pre.status == "resolved" and terminal_factory is not None:
        values = pre.chosen_values or ([pre.chosen_value] if pre.chosen_value else [])
        semantic_answers = [ClarificationAnswerDTO.model_validate(value) for value in (pre.answers or [])] or _answers_from_context(
            pre.resume_context
        )
    elif semantic_answers:
        values = [_project_answer_value(answer) for answer in semantic_answers]
    elif payload.chosen_values:
        values = [v for v in payload.chosen_values if v]
    elif payload.chosen_value:
        values = [payload.chosen_value]
    elif payload.skip:
        values = [CLARIFICATION_SKIP_VALUE]
    if not values:
        raise rule_clarification_http_error(
            422,
            "clarification_answer_required",
        )
    primary = values[0]
    skip_resolve = _is_skip_value(primary)
    response_answers = [] if skip_resolve else (semantic_answers or _legacy_answers_from_values(values))

    # M158 #10 P1: accept edited_subs + user_directive only for task_plan kind; ignore for others.
    edited_subs_payload: list[dict[str, Any]] | None = None
    user_directive_payload: str | None = None
    if pre.kind == "task_plan":
        if pre.status == "resolved" and terminal_factory is not None:
            edited_subs_payload = pre.edited_subs
            user_directive_payload = pre.user_directive
        elif payload.edited_subs:
            edited_subs_payload = [{"title": s.title, "description": s.description} for s in payload.edited_subs]
        if pre.status != "resolved" and payload.user_directive:
            user_directive_payload = payload.user_directive

    # tool_context_select label→id is pure normalization, required before source CAS;
    # preference writes are fallible effects, allowed only after durable decision commit.
    if pre.kind == "tool_context_select" and not skip_resolve:
        canonical_candidate = next((c for c in pre.candidates if c.value == primary), None) or next(
            (c for c in pre.candidates if c.label == primary),
            None,
        )
        if canonical_candidate is not None:
            primary = canonical_candidate.value
            values[0] = primary
        else:
            logger.warning(
                "clarify.tool_context_choice_unmatched req=%s chosen=%r",
                request_id,
                primary,
            )

    try:
        normalized = normalize_clarification_resolution(
            kind=pre.kind,
            chosen_value=primary,
            chosen_values=values,
            edited_subs=edited_subs_payload,
            user_directive=user_directive_payload,
            answers=[answer.model_dump() for answer in semantic_answers],
        )
    except ValueError as ve:
        # CTO review #14: prompt-injection denylist hit -> 422 + metric, frontend toast.
        try:
            from bowhead_core import metrics as _m

            _m.inc(
                "bowhead_clarification_directive_blocked_total",
                labels={"reason": "injection_blocklist"},
                help="P1: directive 被 injection 黑名单挡 (注入防御).",
            )
        except Exception:  # noqa: S110
            pass
        raise rule_clarification_http_error(
            422,
            "clarification_input_rejected",
        ) from ve

    prepared = None
    transitioned = True
    if terminal_factory is None:
        req, transitioned = await clarify.resolve_once(
            request_id,
            primary,
            chosen_values=values,
            edited_subs=edited_subs_payload,
            answers=[answer.model_dump() for answer in semantic_answers],
            user_directive=user_directive_payload,
            resolution_channel="web",
            resolution_work={"payload": payload.model_dump(mode="json")},
            resolution_owner=resolution_owner,
        )
    else:
        from bowhead_core.terminal_protocol import (
            DecisionPrincipalSet,
            DecisionSourceKind,
        )

        if terminal_team_id is None or terminal_session_id is None:
            raise rule_clarification_http_error(500, "decision_source_malformed", nested_error=True)
        action = _clarification_decision_action(pre)
        try:
            from bowhead_gateway.terminal_decision_authorization import (
                TerminalDecisionAuthorizer,
            )

            authorizer = TerminalDecisionAuthorizer(app=request.app)
            principals = DecisionPrincipalSet(
                cur_uid,
                cur_uid,
                cur_uid,
            )
            existing = await terminal_factory.read_existing_source_decision(
                team_id=terminal_team_id,
                session_id=terminal_session_id,
                turn_id=pre.turn_id,
                source_kind=DecisionSourceKind.CLARIFICATION,
                source_id=request_id,
                action=action,
                principals=principals,
            )
            if existing is None:
                await authorizer.authorize_pending_source(
                    team_id=terminal_team_id,
                    session_id=terminal_session_id,
                    workspace_id=pre.workspace_id,
                    requester_ref=cur_uid,
                )
            prepared = await terminal_factory.record_source_decision(
                team_id=terminal_team_id,
                session_id=terminal_session_id,
                turn_id=pre.turn_id,
                source_kind=DecisionSourceKind.CLARIFICATION,
                source_id=request_id,
                action=action,
                principals=principals,
                source_values=normalized,
                side_effect_specs=(
                    (
                        "clarification_projection",
                        request_id,
                        {
                            "remember": payload.remember is not False,
                        },
                    ),
                ),
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import terminal_protocol_http_error

            raise terminal_protocol_http_error(exc) from exc
        try:
            from bowhead_core.terminal_protocol import DecisionState

            if prepared.decision.state is DecisionState.DECISION_RECORDED:
                await authorizer.authorize(
                    prepared,
                    workspace_id=pre.workspace_id,
                )
                if action.value == "resolve_without_resume":
                    prepared = await terminal_factory.commit_decision_control(
                        prepared,
                        reason_code="clarification_resolved",
                        message_key=("turn.clarification_resolved_for_next_turn"),
                    )
        except Exception as exc:
            from bowhead_core.terminal_protocol_store import (
                DecisionAccessRevokedError,
            )

            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
                terminal_protocol_http_error,
            )

            if isinstance(exc, DecisionAccessRevokedError):
                await terminal_factory.commit_decision_authorization_denial(
                    prepared,
                    denial_reason_code=getattr(
                        exc,
                        "reason_code",
                        "access_revoked",
                    ),
                )
                raise terminal_protocol_http_error(exc) from exc
            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        req = await clarify.get(request_id)
    if not req:
        if prepared is not None:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                RuntimeError("terminal_clarification_projection_missing"),
                decision_id=prepared.decision.context.decision_id,
            )
        raise rule_clarification_http_error(404, "clarification_not_found")
    if not transitioned:
        # A concurrent request won the pending→resolved CAS. Re-enter the
        # resolved replay path so only its canonical values and outbox facts
        # drive side effects, event repair, response, and resume.
        return await resolve_one(
            request_id,
            payload,
            request,
            response,
            clarify,
        )

    values = list(req.chosen_values or ([req.chosen_value] if req.chosen_value else []))
    primary = req.chosen_value or (values[0] if values else "")
    skip_resolve = _is_skip_value(primary)
    response_answers = [] if skip_resolve else (semantic_answers or _legacy_answers_from_values(values))

    claimed_effects: tuple[Any, ...] = ()
    effect_payload: dict[str, Any] = {}
    if prepared is not None:
        protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
        if protocol_store is None:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                RuntimeError("terminal_protocol_writer_not_ready"),
                decision_id=prepared.decision.context.decision_id,
            )
        try:
            claimed_effects = await protocol_store.claim_decision_effects(
                decision_id=prepared.decision.context.decision_id,
                owner=f"web-clarification:{cur_uid}",
                expected_authz_snapshot_digest=(prepared.preflight.authz_snapshot_digest),
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        if claimed_effects:
            decoded = json.loads(claimed_effects[0].plan.payload_json)
            if not isinstance(decoded, dict):
                raise rule_clarification_http_error(500, "decision_source_malformed", nested_error=True)
            effect_payload = decoded
    run_projection_effects = prepared is None or bool(claimed_effects)

    # Decision is already durable.  Every remaining write is a retryable,
    # decision-idempotent projection/effect and cannot change its winner.
    projection_effects_ok = True
    if prepared is None and not req.answers and run_projection_effects and semantic_answers and isinstance(pre.resume_context, dict):
        ctx = dict(pre.resume_context)
        ctx[CLARIFICATION_ANSWERS_CONTEXT_KEY] = _answers_to_context(semantic_answers)
        try:
            await clarify.set_resume_context(request_id, ctx)
            req.resume_context = ctx
        except Exception as e:
            if prepared is not None:
                projection_effects_ok = False
            logger.debug("clarify.answers_context_write_failed req=%s err=%s", request_id, e)
    if run_projection_effects:
        try:
            primary = await _commit_tool_context_choice_effect(
                request,
                req,
                user_id=cur_uid,
                primary=primary,
                remember=(bool(effect_payload.get("remember")) if prepared is not None else payload.remember),
                strict=prepared is not None,
            )
        except Exception as e:
            projection_effects_ok = False
            logger.warning("clarify.tool_context_effect_failed req=%s err=%s", request_id, e)

    target_ws = req.workspace_id

    # M158-11 paradigm v2 · PreferenceWriter on clarification.resolved
    # Write only kind=task_plan (other kinds never enter memory).
    # Fire-and-forget without blocking resolve response. Dual-store writes: user_memory + project_memory.
    if run_projection_effects and req.kind == "task_plan" and not skip_resolve:
        # CTO review #3: prefer edited_subs; otherwise chosen_values uses candidates[index].label and writes the original
        # stale label when user edited title. Without edits, retain original fallback logic.
        edited_titles_for_memory: list[str] = []
        if edited_subs_payload:
            for s in edited_subs_payload:
                t = (s.get("title") or "").strip()
                if t:
                    edited_titles_for_memory.append(t)

        async def _write_prefs() -> None:
            nonlocal projection_effects_ok
            try:
                user_store = getattr(request.app.state, "user_memory_store", None)
                proj_store = getattr(request.app.state, "project_memory_store", None)
                # Picked labels prefer edited_subs user-edited titles (CTO #3);
                # otherwise fall back to chosen_values index -> candidate.label.
                picked_labels: list[str] = []
                if edited_titles_for_memory:
                    picked_labels = list(edited_titles_for_memory)
                else:
                    for v in values:
                        cand = next((c for c in req.candidates if c.value == v), None)
                        if cand:
                            picked_labels.append(cand.label or v)
                        else:
                            picked_labels.append(v)
                # user_memory: count kind="preference", key=f"task_plan_pick_{label}".
                if user_store is not None:
                    for index, lbl in enumerate(picked_labels):
                        try:
                            if prepared is not None:
                                await user_store.upsert_operational_memory(
                                    user_id=req.user_id,
                                    kind="task_plan_pick_event",
                                    key=f"{prepared.decision.context.decision_id}:{index}",
                                    value={"label": lbl},
                                    source="terminal_decision_effect",
                                )
                                continue
                            existing = await user_store.list_for_user(req.user_id)
                            cur_count = 0
                            mem_key = f"task_plan_pick_{lbl[:64]}"
                            for e in existing:
                                if e.kind == "preference" and e.key == mem_key:
                                    parsed = e.parsed_value()
                                    if isinstance(parsed, dict):
                                        cur_count = int(parsed.get("pick_count", 0))
                                    break
                            await user_store.upsert_operational_memory(
                                user_id=req.user_id,
                                kind="preference",
                                key=mem_key,
                                value={"pick_count": cur_count + 1, "last_label": lbl},
                                source="paradigm_v2_clarify_resolve",
                            )
                        except Exception as e:
                            if prepared is not None:
                                projection_effects_ok = False
                            logger.warning(
                                "preference_writer.user_memory_failed err=%s",
                                e,
                            )
                # project_memory: workspace-level pick_count, aggregated across users.
                if proj_store is not None:
                    for index, lbl in enumerate(picked_labels):
                        try:
                            if prepared is not None:
                                await proj_store.upsert(
                                    workspace_id=req.workspace_id,
                                    kind="task_plan_pick_event",
                                    key=f"{prepared.decision.context.decision_id}:{index}",
                                    value={"label": lbl},
                                    source="terminal_decision_effect",
                                )
                                continue
                            await proj_store.increment_counter(
                                workspace_id=req.workspace_id,
                                kind="task_plan_pick_count",
                                key=lbl[:64],
                            )
                        except Exception as e:
                            if prepared is not None:
                                projection_effects_ok = False
                            logger.warning(
                                "preference_writer.project_memory_failed err=%s",
                                e,
                            )
                logger.info(
                    "preference_writer.done user=%s ws=%s n_picks=%d",
                    req.user_id,
                    req.workspace_id,
                    len(picked_labels),
                )
            except Exception as e:
                if prepared is not None:
                    projection_effects_ok = False
                logger.warning("preference_writer.fatal err=%s", e)

        if prepared is not None:
            # v1 projection is awaited and uses decision-scoped idempotent
            # event rows; it must not leave an untracked fire-and-forget gap.
            try:
                await _write_prefs()
            except Exception:
                projection_effects_ok = False
        else:
            # Legacy retains its historical best-effort behavior.
            _task = asyncio.create_task(_write_prefs())
            _preference_writer_tasks.add(_task)
            _task.add_done_callback(_preference_writer_tasks.discard)

    if claimed_effects:
        protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
        if protocol_store is None:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                RuntimeError("terminal_protocol_writer_not_ready"),
                decision_id=prepared.decision.context.decision_id,
            )
        try:
            for claimed in claimed_effects:
                settled = await protocol_store.settle_decision_effect(
                    claimed,
                    applied=projection_effects_ok,
                    code=(None if projection_effects_ok else "clarification_projection_failed"),
                )
                if not settled:
                    raise RuntimeError("terminal_admission_recovery_pending")
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        if not projection_effects_ok:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                RuntimeError("clarification_projection_failed"),
                decision_id=prepared.decision.context.decision_id,
            )

    # SSE event, dingsiyan #11: must use emit_out_of_band. Plain get_or_create+emit previously failed after pause
    # in two ways: <5s hit closed gen1 bus, silently discarded without persistence; >5s new bus started seq at 0,
    # colliding with early turn events and losing them to FE replay dedupe (prod t_1ccf9e0aec11 confirmed payload seq=1).
    # Card removal never worked in production, the delivery-layer cause of repeated clicks / panels staying open.
    resolution_event_payload = {
        "request_id": request_id,
        "chosen_value": primary,
        "chosen_values": values,
        "answers": [a.model_dump() for a in response_answers],
        "skipped": skip_resolve,
        "kind": req.kind,
        "workspace_id": target_ws,
        "has_directive": bool(user_directive_payload),
        "edited_subs_count": len(edited_subs_payload or []),
    }
    if terminal_factory is None:
        resolution_event_payload = await clarify.record_resolution_outcome(
            request_id=request_id,
            turn_id=req.turn_id,
            payload=resolution_event_payload,
            resolution_owner=resolution_owner,
        )
        try:
            await _ensure_clarification_resolved_event(
                request,
                turn_id=req.turn_id,
                request_id=request_id,
                payload=resolution_event_payload,
            )
        except Exception as e:
            logger.exception("clarify.event_emit_failed turn=%s", req.turn_id)
            raise rule_clarification_http_error(
                503,
                "clarification_receipt_not_durable",
                nested_error=True,
            ) from e

    # M158 #10 P1 metrics: directive/edit adoption rates (design §"验收 metric").
    # CTO review #15: add directive_length and sub_edit_count histograms to inspect distributions.
    if run_projection_effects and req.kind == "task_plan" and not skip_resolve:
        try:
            from bowhead_core import metrics as _m

            if user_directive_payload:
                _m.inc(
                    "bowhead_clarification_directive_used_total",
                    labels={"workspace_id": target_ws},
                    help="P1: directive 字段非空的 resolve 数 (灰度看启用率).",
                )
                _m.observe(
                    "bowhead_clarification_directive_length_chars",
                    value=float(len(user_directive_payload)),
                    labels={"workspace_id": target_ws},
                    buckets=(50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0),
                    help="P1 CTO review #15: directive 长度分布, 看用户实际写多长.",
                )
            if edited_subs_payload:
                _m.inc(
                    "bowhead_clarification_subs_edited_total",
                    labels={"workspace_id": target_ws},
                    help="P1: 用户改了至少 1 个 sub 的 resolve 数.",
                )
                _m.observe(
                    "bowhead_clarification_subs_edited_count",
                    value=float(len(edited_subs_payload)),
                    labels={"workspace_id": target_ws},
                    buckets=(1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0),
                    help="P1 CTO review #15: 用户提交的 sub 数 (含原 + 加 - 删).",
                )
        except Exception:  # noqa: S110
            pass

    # V2.4.1 metric: auto-pick error rate (chosen_value != candidates[0].value means
    # auto-pick was wrong and user chose another). Only token kind supports auto-pick.
    if run_projection_effects and req.kind == "token" and not skip_resolve:
        try:
            from bowhead_core import metrics as _m

            top = req.candidates[0] if req.candidates else None
            was_auto_pick_correct = top is not None and top.value == primary
            _m.inc(
                "bowhead_clarify_user_corrected_total",
                labels={
                    "auto_pick_correct": "yes" if was_auto_pick_correct else "no",
                    "confidence": (top.confidence if top else "unknown") or "unknown",
                },
                help="V2.4.1: clarification user 后纠 (auto_pick_correct=no 即 V2.4 auto-pick 错了用户改了).",
            )
        except Exception:  # noqa: S110
            pass

    # Sprint chat-v4-streaming Phase 4C step 3: compute resume action.
    # Only emit "turn_resume" when (1) kind supports resume + (2) resume_context
    # was persisted by Lead at sentinel emit. Falls back to "noop" (old chat-send
    # path) for back-compat with sprint-pre rows or non-supported kinds.
    _resume_action = "noop"
    _resume_turn_id: str | None = None
    _reapprove_plan_id: str | None = None
    _rc = req.resume_context if isinstance(req.resume_context, dict) else None
    # Plan-card dispatch always uses _plan_card_action (preflight reapprove + batch 2 mid-run three-way result).
    # This must precede generic turn_resume classification; nonempty resume_context would otherwise spawn
    # an unrelated ordinary Lead turn on the plan turn_id (review contract F4).
    _pa = await _plan_card_action(request, rc=_rc, user_id=req.user_id, workspace_id=req.workspace_id)
    if _pa is not None:
        _resume_action, _reapprove_plan_id = _pa
    elif req.kind in ("ambiguous_turn", "options", "tool_context_select") and req.resume_context is not None:
        _resume_action = "turn_resume"
        _resume_turn_id = req.turn_id

    if run_projection_effects:
        try:
            from bowhead_core import metrics as _m

            labels = {"kind": req.kind, "workspace_id": target_ws}
            if skip_resolve:
                _m.inc(
                    "bowhead_clarification_whole_card_skip_total",
                    labels=labels,
                    help="Clarification whole-card skip resolves.",
                )
                _m.inc(
                    "bowhead_clarification_skip_resume_result_total",
                    labels={**labels, "action": _resume_action},
                    help="Clarification skip resolve resume result.",
                )
            else:
                custom_count = sum(1 for answer in response_answers if answer.source == "custom")
                per_question_skip_count = sum(1 for answer in response_answers if answer.source == "skip")
                if custom_count:
                    _m.inc(
                        "bowhead_clarification_custom_answer_total",
                        labels=labels,
                        help="Clarification custom answer count.",
                        value=float(custom_count),
                    )
                if per_question_skip_count:
                    _m.inc(
                        "bowhead_clarification_question_skip_total",
                        labels=labels,
                        help="Clarification per-question skip count.",
                        value=float(per_question_skip_count),
                    )
        except Exception:  # noqa: S110
            pass

    resume_result = None
    if _resume_action == "turn_resume":
        # The resolved clarification is the durable retry intent. Keep the
        # request open until spawn_resume_turn has published and claimed its
        # generation lease; clients never have to bridge a lossy resolve→resume
        # two-request gap.
        resume_result = await resume_turn(
            request_id,
            request,
            response,
            clarify,
        )

    return ResolveResultDTO(
        ok=True,
        request_id=request_id,
        chosen_value=str(resolution_event_payload.get("chosen_value") or ""),
        chosen_values=[str(value) for value in resolution_event_payload.get("chosen_values") or []],
        answers=resolution_event_payload.get("answers") or [],
        skipped=bool(resolution_event_payload.get("skipped", False)),
        workspace_id=str(resolution_event_payload.get("workspace_id") or target_ws),
        action=_resume_action,
        resume_turn_id=_resume_turn_id,
        stream_url=resume_result.stream_url if resume_result else None,
        execution_generation=(resume_result.execution_generation if resume_result else None),
        reapprove_plan_id=_reapprove_plan_id,
        decision_id=(prepared.decision.context.decision_id if prepared is not None else None),
        decision_state=(prepared.decision.state.value if prepared is not None else None),
    )


# Sprint chat-v4-streaming Phase 4C step 6: rephrase-question dismiss path.
class DismissResultDTO(BaseModel):
    ok: bool
    request_id: str
    dismissed: bool
    original_user_text: str | None = None  # FE composer prefill source


@router.post("/{request_id}/dismiss", response_model=DismissResultDTO, dependencies=[Depends(require_not_readonly)])
async def dismiss_clarification(
    request_id: str,
    request: Request,
    clarify: ClarificationStore = Depends(_clarify_store),
) -> DismissResultDTO:
    """User clicked '换个问法 →' — mark clarification dismissed.

    Lead orchestrator will end the paused turn with status="clarification_dismissed".
    Returns original_user_text from resume_context so FE composer can pre-fill.
    """
    pre = await clarify.get(request_id)
    if pre is None:
        raise rule_clarification_http_error(404, "clarification_not_found")
    cur_uid = _current_user_id(request)
    if pre.user_id != cur_uid:
        raise rule_clarification_http_error(404, "clarification_not_found")
    await _require_clarification_lifecycle(
        pre,
        request,
        operation=SpaceOperation.WRITE,
    )

    terminal_factory, terminal_team_id, terminal_session_id = await _terminal_clarification_factory(
        request,
        pre,
        user_id=cur_uid,
    )
    if terminal_factory is None:
        requires_settlement = clarification_requires_legacy_resume_fence(pre)
        if pre.status == "pending" and requires_settlement:
            try:
                scheduled_origin = await scheduled_run_for_turn(
                    request.app,
                    pre.turn_id,
                )
            except RuntimeError as exc:
                raise rule_clarification_http_error(
                    503,
                    "clarification_store_unavailable",
                    nested_error=True,
                ) from exc
            if scheduled_origin is not None:
                if scheduled_origin.status != "awaiting_input":
                    raise rule_clarification_http_error(
                        409,
                        (
                            "clarification_not_resolvable"
                            if scheduled_origin.status in {"success", "failed", "skipped", "expired"}
                            else "clarification_resume_not_ready"
                        ),
                    )
            orchestrator = getattr(request.app.state, "orchestrator", None)
            session_store = getattr(orchestrator, "session_store", None)
            if session_store is None:
                raise rule_clarification_http_error(
                    503,
                    "clarification_orchestrator_unavailable",
                )
            if not await wait_for_legacy_turn_resume_ready(
                session_store,
                pre.turn_id,
            ):
                raise rule_clarification_http_error(
                    409,
                    "clarification_resume_not_ready",
                )
        invalidation = await clarify.invalidate_one(
            request_id,
            target_status="dismissed",
            reason="user_rephrase",
            actor="user",
        )
        req = invalidation.request
        if (
            req is not None
            and req.status == "dismissed"
            and isinstance(req.resume_context, dict)
            and ("reference_continuation" in req.resume_context or "continuation" in req.resume_context)
        ):
            # The durable dismissal is the cancellation intent. Common dispatch
            # recovery closes the paused execution and preserves accepted steer;
            # its periodic sweep also repairs a crash before this eager attempt.
            try:
                await request.app.state.orchestrator.session_store.recover_expired_chat_spawn_dispatches()
            except Exception:
                logger.exception("clarify.reference_dismiss_settlement_deferred request=%s turn=%s", request_id, pre.turn_id)
        if invalidation.invalidation is not None:
            from bowhead_gateway.clarification_invalidation import (
                publish_pending_clarification_invalidations,
            )

            try:
                await publish_pending_clarification_invalidations(
                    request.app,
                    request_ids=[request_id],
                )
            except Exception:
                # The state + intent transaction already committed.  Periodic
                # recovery owns delivery; returning an error would invite the
                # user to repeat an action that already won.
                logger.exception(
                    "clarify.dismiss_publish_deferred request=%s turn=%s",
                    request_id,
                    pre.turn_id,
                )
    else:
        from bowhead_core.terminal_protocol import (
            DecisionAction,
            DecisionPrincipalSet,
            DecisionSourceKind,
        )

        if terminal_team_id is None or terminal_session_id is None:
            raise rule_clarification_http_error(500, "decision_source_malformed", nested_error=True)
        try:
            from bowhead_gateway.terminal_decision_authorization import (
                TerminalDecisionAuthorizer,
            )

            authorizer = TerminalDecisionAuthorizer(app=request.app)
            principals = DecisionPrincipalSet(
                cur_uid,
                cur_uid,
                cur_uid,
            )
            existing = await terminal_factory.read_existing_source_decision(
                team_id=terminal_team_id,
                session_id=terminal_session_id,
                turn_id=pre.turn_id,
                source_kind=DecisionSourceKind.CLARIFICATION,
                source_id=request_id,
                action=DecisionAction.DISMISS,
                principals=principals,
            )
            if existing is None:
                await authorizer.authorize_pending_source(
                    team_id=terminal_team_id,
                    session_id=terminal_session_id,
                    workspace_id=pre.workspace_id,
                    requester_ref=cur_uid,
                )
            prepared = await terminal_factory.record_source_decision(
                team_id=terminal_team_id,
                session_id=terminal_session_id,
                turn_id=pre.turn_id,
                source_kind=DecisionSourceKind.CLARIFICATION,
                source_id=request_id,
                action=DecisionAction.DISMISS,
                principals=principals,
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import terminal_protocol_http_error

            raise terminal_protocol_http_error(exc) from exc
        try:
            from bowhead_core.terminal_protocol import DecisionState

            if prepared.decision.state is DecisionState.DECISION_RECORDED:
                await authorizer.authorize(
                    prepared,
                    workspace_id=pre.workspace_id,
                )
                await terminal_factory.commit_decision_control(
                    prepared,
                    reason_code="clarification_dismissed",
                    message_key="turn.clarification_dismissed",
                )
        except Exception as exc:
            from bowhead_core.terminal_protocol_store import (
                DecisionAccessRevokedError,
            )

            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
                terminal_protocol_http_error,
            )

            if isinstance(exc, DecisionAccessRevokedError):
                await terminal_factory.commit_decision_authorization_denial(
                    prepared,
                    denial_reason_code=getattr(
                        exc,
                        "reason_code",
                        "access_revoked",
                    ),
                )
                raise terminal_protocol_http_error(exc) from exc
            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        req = await clarify.get(request_id)
    if req is None:
        if terminal_factory is not None:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                RuntimeError("terminal_clarification_projection_missing"),
                decision_id=prepared.decision.context.decision_id,
            )
        raise rule_clarification_http_error(404, "clarification_not_found")

    original_text = None
    if req.resume_context and isinstance(req.resume_context, dict):
        original_text = req.resume_context.get("user_text")

    return DismissResultDTO(
        ok=True,
        request_id=request_id,
        dismissed=(req.status == "dismissed"),
        original_user_text=original_text,
    )


# Sprint chat-v4-streaming Phase 4C step 4 — HITL same-turn-resume orchestrator.
# FE calls this after /resolve returns action="turn_resume".
class ResumeTurnResultDTO(BaseModel):
    ok: bool
    request_id: str
    turn_id: str  # same as req.turn_id (Option A invariant)
    stream_url: str  # FE reconnect via EventSource
    execution_generation: int


@router.post(
    "/{request_id}/resume_turn",
    response_model=ResumeTurnResultDTO,
    dependencies=[Depends(require_not_readonly)],
)
async def resume_turn(
    request_id: str,
    request: Request,
    response: Response,
    clarify: ClarificationStore = Depends(_clarify_store),
) -> ResumeTurnResultDTO:
    """Resume a paused turn after user resolved its clarification.

    Phase 4C step 4: reconstructs TurnRequest from ClarificationRequest.resume_context,
    keeps the same turn_id (Option A: same-turn-resume), injects resume_glossary
    (token → chosen_value) so Lead skips the same HITL on second pass.

    Validation:
    - clarification exists + user owns it
    - status == "resolved" (otherwise nothing to resume)
    - resume_context present (sentinel emitted by Lead snapshotted it)

    Returns turn_id + stream_url — FE opens EventSource on stream_url to receive
    continuation events (turn_log_store replays past events + bus subscription
    yields new ones, dedupe by (event_type, occurred_at)).
    """
    pre = await clarify.get(request_id)
    if not pre:
        raise rule_clarification_http_error(404, "clarification_not_found")
    cur_uid = _current_user_id(request)
    if pre.user_id != cur_uid:
        raise rule_clarification_http_error(404, "clarification_not_found")
    await _require_clarification_lifecycle(
        pre,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    if pre.status != "resolved":
        raise rule_clarification_http_error(
            409,
            "clarification_resume_not_ready",
        )
    if not pre.resume_context or not resume_context_is_readable(pre.resume_context):
        raise rule_clarification_http_error(
            409,
            "clarification_resume_unavailable",
            clarification_stage="clarification_resume_preflight",
        )

    from bowhead_core.clarification_resume_context import DocumentResumeContext, OrdinaryLeadResumeContext, decode_clarification_context

    try:
        document_context = decode_clarification_context(pre)
    except ValueError as exc:
        raise rule_clarification_http_error(409, "clarification_resume_unavailable", clarification_stage="clarification_resume_preflight") from exc
    is_document_resume = isinstance(document_context, DocumentResumeContext)
    rc = pre.resume_context
    ordinary_checkpoint = None
    if isinstance(document_context, OrdinaryLeadResumeContext):
        from bowhead_core.ordinary_lead_continuation import ordinary_resume_request_material

        try:
            ordinary_checkpoint, rc = ordinary_resume_request_material(pre)
        except ValueError as exc:
            raise rule_clarification_http_error(
                409, "clarification_resume_unavailable", clarification_stage="clarification_resume_preflight"
            ) from exc
    if rc.get("plan_reapprove_plan_id") or rc.get("plan_midrun_plan_id"):
        # planning-mode-audit WP-2 / graph-mode batch 2 defense for old FE: plan-card clarifications
        # (preflight reapprove and mid-run shapes) cannot resume; doing so spawns an ordinary Lead turn
        # on the plan execution turn_id, double-spending tokens and overwriting terminal state.
        # Correct path: resolve returns plan_reapprove/plan_midrun_ack. Previously mid-run coincidentally returned
        # 409 because rc lacked user_text, with a misleading error code (review P0-2).
        raise rule_clarification_http_error(
            409,
            "clarification_plan_reapprove_required",
        )
    user_text = rc.get("user_text") or ""
    if not user_text:
        raise rule_clarification_http_error(
            409,
            "clarification_resume_unavailable",
            clarification_stage="clarification_resume_preflight",
        )
    # Sprint chat-v4-streaming Phase 4C CTO review fix — reject empty session_id.
    # Without this, TurnRequest.session_id falls back to "" and the resumed
    # turn is spawned against an unscoped session — session_store reads return
    # nothing, multi-turn context is lost, and the session sidebar can't link
    # the resume turn back to its origin. Sentinel emit MUST snapshot session_id;
    # if it didn't, the resume row is unrecoverable and FE should fall back to
    # starting a fresh turn via chat/send.
    rc_session_id = (rc.get("session_id") or "").strip()
    if not rc_session_id:
        raise rule_clarification_http_error(
            409,
            "clarification_resume_unavailable",
            clarification_stage="clarification_resume_preflight",
        )

    # One canonical interpretation is shared with restart recovery.  A page
    # close after the durable decision must not change skip wording,
    # multi-question answers, or tool-context routing instructions.
    from bowhead_gateway.turn_service import (
        build_clarification_resume_semantics,
    )

    if is_document_resume or ordinary_checkpoint is not None:
        user_text = document_context.user_text
        resume_glossary = {}
        skipped_clarification = False
    else:
        resume_semantics = build_clarification_resume_semantics(pre)
        user_text = resume_semantics.user_text
        resume_glossary = resume_semantics.resume_glossary
        skipped_clarification = resume_semantics.skipped

    # Orchestrator from app.state (same pattern as chat.py /send).
    orchestrator: Orchestrator | None = getattr(
        request.app.state,
        "orchestrator",
        None,
    )
    if orchestrator is None:
        raise rule_clarification_http_error(
            503,
            "clarification_orchestrator_unavailable",
        )

    # feedback-0713 F2: soft-deleted sessions (R1 report-deletion linkage) reject resume with 410 Gone.
    # Same boundary as chat/send (reject, never revive); see turn_service.is_session_deleted.
    from bowhead_gateway.turn_service import is_session_deleted

    if await is_session_deleted(orchestrator, rc_session_id):
        raise rule_clarification_http_error(
            410,
            "session_deleted",
            nested_error=True,
        )

    workspace_id = rc.get("workspace_id") or pre.workspace_id
    team_id: str | None = rc.get("team_id")
    access_context = rc.get("access_context") if isinstance(rc.get("access_context"), dict) else None
    team_id, access_context = await _revalidate_resume_access_context(
        request,
        user_id=cur_uid,
        workspace_id=workspace_id,
        team_id=team_id,
        access_context=access_context,
    )

    # Licensing: pre-flight entitlement gate (§3.3②) — BEFORE the idempotency early-return below, so every
    # resume (incl. idempotent retries) is gated. Above create_task → a denied resume costs zero LLM.
    from bowhead_gateway._entitlement_gate import gate_turn_http

    await gate_turn_http(request, team_id, response)

    terminal_factory, terminal_team_id, terminal_session_id = await _terminal_clarification_factory(
        request,
        pre,
        user_id=cur_uid,
    )

    # Batch 0b: a same-turn resume is still an authenticated physical model
    # invocation.  Re-resolve the current team's Router and fail closed before
    # creating a fresh bus; never pass None into Core's process-Router fallback.
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    _resume_model_runtime = None
    if team_id and terminal_factory is None:
        _resume_model_runtime = await resolve_team_model_runtime(
            request.app.state,
            team_id,
            entrypoint="clarification",
        )
        if not _resume_model_runtime.available:
            raise rule_clarification_http_error(
                503,
                "team_model_runtime_unavailable",
            )

    if is_document_resume:
        from bowhead_gateway.turn_service import build_resume_turn_request

        if terminal_factory is not None or _resume_model_runtime is None:
            raise rule_clarification_http_error(409, "clarification_resume_unavailable", clarification_stage="clarification_resume_preflight")
        turn_req = build_resume_turn_request(
            pre,
            team_router=_resume_model_runtime.router,
            team_kb_retriever=_resume_model_runtime.kb_retriever,
        )
        if turn_req is None:
            raise rule_clarification_http_error(409, "clarification_resume_unavailable", clarification_stage="clarification_resume_preflight")
        turn_req.access_context = access_context
    else:
        from bowhead_gateway.datarc_context import resolve_datarc_turn_context

        # scheduled-recipe-runs S3: did this turn originate from a scheduled job?
        #
        # Reverse-query scheduled_job_runs by turn_id, not session origin / turn prefix: fresh scheduled sessions
        # have origin='normal'; anchor sessions belong to the user. Neither heuristic is accurate;
        # reverse lookup also returns the job with its Datarc snapshot and bound workflow.
        #
        # Recognition matters: resume was designed for **interactive chat**, and every default conflicts with routines:
        # live data-scope resolution loses creation-time pin; context-switch injection is structurally forbidden in headless
        # and writes owner permanent preferences; config_lite follows global flag. Both run halves must share semantics,
        # or one user answer changes a fixed-workspace run into a current-preference run.
        _sched_job = None
        _sched_store = getattr(request.app.state, "scheduled_store", None)
        _sched_run = await scheduled_run_for_turn(request.app, pre.turn_id)
        if _sched_run is not None and _sched_store is not None:
            try:
                _sched_job = await _sched_store.get(_sched_run.job_id)
            except Exception as exc:
                logger.warning("clarify.scheduled_job_load_failed turn=%s err=%s", pre.turn_id, exc)

        _authoritative_datarc_project = (
            pre.chosen_value if pre.kind == "tool_context_select" and pre.chosen_value and not skipped_clarification else None
        )
        datarc_ctx = await resolve_datarc_turn_context(
            request,
            team_id=team_id,
            workspace_id=workspace_id,
            user_id=cur_uid,  # Power BI Scenario ②: per-user UPN impersonation
            session_id=rc_session_id,  # Batch 2: session-preference scope.
            # Priority: workspace **explicitly selected in this clarification** > job's creation snapshot > normal ladder.
            # Explicit selection wins because this answer names the workspace; current intent overrides snapshot. Snapshot fallback
            # preserves batch 2/c9 decision 10 (routines always query their creation workspace, unaffected by owner preferences).
            # Falling back to the ladder on resume would let an unrelated answer shift data scope to current personal preferences.
            pinned_project=(
                rc.get("active_datarc_project")
                if ordinary_checkpoint is not None
                else _authoritative_datarc_project or (getattr(_sched_job, "datarc_workspace_id", None) if _sched_job else None)
            ),
            log=logger,
        )

        # Batch 2 (PRD §3.3c, review P0): per-user member_uid must be freshly resolved for the workspace
        # resolved at resume. resume_context's member_uid snapshot is empty when the original turn had no
        # workspace (sentinel guidance); member_uid is workspace-scoped, so its snapshot may be invalid for
        # the just-selected workspace.
        _resume_member_uid = ""
        if datarc_ctx.datarc_project_for_calls:
            from bowhead_gateway.routes.chat import _resolve_optional_datarc_member
            from bowhead_gateway.ws_authz import ws_membership_enforce_enabled

            datarc_ctx, _resume_member_uid = await _resolve_optional_datarc_member(
                request,
                datarc_ctx,
                ws_enforce=ws_membership_enforce_enabled(),
                log=logger,
            )

        # BUG2 completion (hidden device-reproduced gap): resume tool_switch_context needs the switch commit closure.
        # skipped_clarification / let-the-model-decide makes Lead call tool_switch_context for a relevant workspace,
        # but resume TurnRequest never injected a switcher, leaving ctx.datarc_context_switcher=None and hitting
        # tool_registry's "本通道不支持会话内切换"; Lead gives up, an empty dead end (hidden BUG2-fix gap).
        # Ambiguous queries masked it via escape hatch; unambiguous queries exposed it. Inject from chat.py's normal path
        _resume_switcher = None
        # No switcher for scheduled-origin turns: scheduled entry already omits it (absent TurnRequest field = structured
        # rejection). Adding it on resume lets the workflow switch data scope or even write owner permanent default workspace.
        if _sched_job is None:
            from bowhead_gateway.routes.chat import (
                _make_datarc_context_switcher,
            )
            from bowhead_gateway.ws_authz import (
                ws_membership_enforce_enabled,
            )

            _resume_switcher = _make_datarc_context_switcher(
                request,
                cur_uid,
                rc_session_id,
                ws_enforce=ws_membership_enforce_enabled(),
                log=logger,
            )

        if terminal_factory is not None:
            from bowhead_gateway.terminal_pending_execution import (
                resolve_terminal_team_runtime,
            )

            try:
                _resume_team_router, _resume_team_kb = await resolve_terminal_team_runtime(
                    request.app.state,
                    team_id,
                )
            except Exception as exc:
                from bowhead_gateway.terminal_protocol_http import (
                    terminal_protocol_http_error,
                )

                raise terminal_protocol_http_error(exc) from exc
        else:
            _resume_team_router = _resume_model_runtime.router if _resume_model_runtime is not None else None
            _resume_team_kb = _resume_model_runtime.kb_retriever if _resume_model_runtime is not None else None
        if terminal_factory is None and _resume_team_router is None:
            # Batch 2-a site 10: preserve best effort here (hard failure interrupts clarification resume), but
            # make it visible. None makes this resume fall back to the **process-assembled router**,
            # which is MockAdapter echo on 116, yielding fake-response-derived results without warning.
            logger.error("clarification_resume.team_router_unavailable team=%s session=%s", team_id, rc_session_id)
            try:
                from bowhead_core import metrics

                metrics.inc(
                    "bowhead_team_router_miss_total",
                    labels={"reason": "best_effort_none", "site": "clarification_resume"},
                    help="Team router resolution misses, by cause and call site.",
                )
            except Exception:
                logger.warning("clarification_resume.miss_metric_failed", exc_info=True)

        # Batch 7-F: nonterminal branch previously always used default(), so an English user's clarification
        # resumed entirely in Chinese (README §6 gap 1). Resume is the **same turn** and must retain
        # its chosen language. Old snapshot without this key -> from_dict(None) falls back to default.
        # The terminal branch checks the snapshot against its durable presentation locale;
        # a failed locale read stays a hard error.
        _resume_language_context = LanguageContext.from_dict(rc.get(RESUME_LANGUAGE_KEY) if isinstance(rc, dict) else None)
        if terminal_factory is not None:
            protocol_store = getattr(
                request.app.state,
                "terminal_protocol_store",
                None,
            )
            if protocol_store is None:
                raise rule_clarification_http_error(
                    503,
                    "terminal_protocol_writer_not_ready",
                    nested_error=True,
                )
            try:
                durable_locale = await protocol_store.read_persisted_turn_presentation_locale(
                    team_id=team_id,
                    turn_id=pre.turn_id,
                )
                # Keep the admitted answer language when it agrees with the durable turn locale.
                _resume_language_context = resume_language_context(rc, presentation_locale=durable_locale)
            except Exception as exc:
                from bowhead_gateway.terminal_protocol_http import (
                    terminal_protocol_http_error,
                )

                raise terminal_protocol_http_error(exc) from exc
        if ordinary_checkpoint is not None:
            # The sealed checkpoint is the paused Turn's language, including
            # none when i18n is off; a default or locale fallback fails restore.
            _resume_language_context = ordinary_checkpoint.language_context()
        from bowhead_core.configuration_settings_target import unrestored_settings_target

        turn_req = TurnRequest(
            session_id=rc_session_id,
            configuration_settings_target=unrestored_settings_target(rc),
            workspace_id=workspace_id,
            active_datarc_project=datarc_ctx.active_datarc_project,
            # Batch 2/c6 (PRD §3.4): resume also carries switch commit callback; otherwise skip / let-model-decide
            # cannot use tool_switch_context (device-reproduced BUG2 gap fix).
            datarc_context_switcher=_resume_switcher,
            # Batch 2: carry snapshot fields from freshly resolved resume context (repeat sentinel guidance/provenance).
            active_datarc_workspace_name=getattr(datarc_ctx, "active_datarc_workspace_name", "") or "",
            accessible_datarc_workspaces=getattr(datarc_ctx, "accessible_datarc_workspaces", ()) or (),
            datarc_absence_reason=getattr(datarc_ctx, "datarc_absence_reason", "") or "",
            datarc_client=datarc_ctx.datarc_client,
            datarc_unavailable=datarc_ctx.datarc_unavailable,
            vector_client=datarc_ctx.vector_client,
            team_router=_resume_team_router,
            team_kb_retriever=_resume_team_kb,
            user_id=cur_uid,
            member_uid=_resume_member_uid,
            text=user_text,
            turn_id=pre.turn_id,  # CRITICAL: same turn_id
            resume_clarification_id=pre.request_id if "question_round" in rc or ordinary_checkpoint is not None else None,
            ordinary_clarification_id=pre.request_id if ordinary_checkpoint is not None else None,
            ordinary_lead_checkpoint=ordinary_checkpoint,
            reference_clarification_id=pre.request_id
            if isinstance(rc.get("reference_continuation"), dict)
            and rc["reference_continuation"].get("contract")
            in {"reference-bi-workspace.v1", "reference-lead-predispatch.v1", "reference-lead-postdispatch.v1"}
            else None,
            team_id=team_id,
            access_context=access_context,
            language_context=_resume_language_context,
            # Same Turn ⇒ same model clock, also when no language snapshot was saved.
            viewer_time_zone=resume_viewer_time_zone(rc),
            kb_role=rc.get("kb_role") or "user",
            paradigm_v2=bool(rc.get("paradigm_v2", False)),
            preferred_provider=rc.get("preferred_provider"),
            preferred_model=rc.get("preferred_model"),
            resume_turn_id=pre.turn_id,  # Phase 4C signal — Lead reads this
            resume_glossary=resume_glossary or None,
            # Config copilot batch 0.5 (tech design §5.2, review P1-1): Web clarification resume matches chat.py's main path;
            # omitting this makes resumed Lead propose tell Web users "请到网页端" (same BUG2 shape).
            # Scheduled entry always False by discipline (types.py: Feishu/scheduled/plans stay False);
            # resume must not add a configuration-copilot channel absent from the first half.
            config_lite_supported=_sched_job is None,
        )

        # Reinject the bound saved workflow for resume. **Read authoritative ref freshly from job row**, not potentially stale
        # resume_context. Reapply resolution/fingerprint/admission gates: workflow may change/disappear during pause.
        # Otherwise the half delivering the result is recipe-blind, with Lead improvising, yet reconciliation
        # delivers it as workflow-completed. Resolution failure only warns, without interruption; continuation still has value.
        if _sched_job is not None and getattr(_sched_job, "recipe_ref", None):
            from bowhead_gateway._scheduled_runner import _resolve_job_workflow

            try:
                _bw, _bw_err = await _resolve_job_workflow(request.app, _sched_job, team_id)
                if _bw is not None:
                    turn_req.bound_workflow = _bw
                else:
                    logger.warning(
                        "clarify.resume_workflow_unavailable job=%s reason=%s",
                        _sched_job.id,
                        _bw_err,
                    )
            except Exception as exc:
                logger.warning("clarify.resume_workflow_failed job=%s err=%s", _sched_job.id, exc)

    # Spawn the resume worker via the channel-neutral TurnService shared with the Feishu bridge.
    # The bus generation guard (close → get_or_create) lives
    # inside spawn_resume_turn.
    from bowhead_gateway.turn_service import (
        ResumeTurnConflictError,
        resume_already_in_flight,
        spawn_resume_turn,
    )

    current_turn = await orchestrator.session_store.get_turn(turn_req.turn_id)
    if current_turn is not None and getattr(current_turn, "status", None) in (
        "completed",
        "failed",
        "cancelled",
        "aborted",
    ):
        # The first resolve may have spawned and fully completed before its HTTP
        # response reached the browser. Treat a retry as replay attachment, not
        # as a request to spawn another generation.
        return ResumeTurnResultDTO(
            ok=True,
            request_id=request_id,
            turn_id=turn_req.turn_id,
            stream_url=f"/api/v1/chat/stream/{turn_req.turn_id}",
            execution_generation=max(
                1,
                int(getattr(current_turn, "execution_generation", 1) or 1),
            ),
        )

    # Sprint chat-v4-streaming Phase 4C CTO review fix — idempotency guard.
    # FE EventSource auto-reconnect can fire /resume_turn twice (network blip
    # between resolveClarification → setDone → onResumed); without a guard we'd
    # spawn 2 concurrent turn tasks against the same turn_id, double-charging
    # tokens and racing the bus. If a turn_resume task for this turn_id is already
    # running, short-circuit with the same response (FE reattaches to the stream).
    if resume_already_in_flight(turn_req.turn_id):
        logger.info(
            "clarify.resume_turn_idempotent_hit req=%s turn=%s — returning existing stream_url, not spawning duplicate",
            request_id,
            turn_req.turn_id,
        )
        return ResumeTurnResultDTO(
            ok=True,
            request_id=request_id,
            turn_id=turn_req.turn_id,
            # NOTE 2026-05-19: fixed from buggy /orchestrator/{id}/stream (route
            # never existed — FE EventSource would 404). Correct route is
            # /api/v1/chat/stream/{turn_id} (matches the success path at line 685).
            stream_url=f"/api/v1/chat/stream/{turn_req.turn_id}",
            execution_generation=max(
                1,
                int(getattr(current_turn, "execution_generation", 1) or 1),
            ),
        )

    # Resume is new model work, using chat/send credit admission. After both idempotent early returns: completed/in-flight
    # resume retry only reattaches the stream, never 402 because balance fell later. When blocked after /resolve consumes
    # the answer, row stays resolved; after recharge another click continues via /resolve's resolved-replay branch.
    credit_denial = await check_credit_admission(
        getattr(request.app.state, "user_store", None),
        team_id=team_id,
        user_id=cur_uid,
        workspace_id=workspace_id,
        metrics_log=getattr(request.app.state, "metrics_log", None),
        turn_id=turn_req.turn_id,
    )
    if credit_denial is not None:
        raise rule_clarification_http_error(402, credit_denial, nested_error=True)

    if terminal_factory is not None:
        from dataclasses import replace as _dc_replace

        from bowhead_core.terminal_protocol import (
            DecisionAction,
            DecisionPrincipalSet,
            DecisionSourceKind,
        )

        if terminal_team_id != team_id or terminal_session_id != rc_session_id:
            raise rule_clarification_http_error(409, "decision_source_malformed", nested_error=True)
        normalized = normalize_clarification_resolution(
            kind=pre.kind,
            chosen_value=pre.chosen_value,
            chosen_values=pre.chosen_values,
            user_directive=pre.user_directive,
            edited_subs=pre.edited_subs,
            answers=pre.answers,
        )
        prepared = None
        try:
            from bowhead_gateway.terminal_decision_authorization import (
                TerminalDecisionAuthorizer,
            )

            authorizer = TerminalDecisionAuthorizer(app=request.app)
            principals = DecisionPrincipalSet(
                cur_uid,
                cur_uid,
                cur_uid,
            )
            existing = await terminal_factory.read_existing_source_decision(
                team_id=team_id,
                session_id=rc_session_id,
                turn_id=pre.turn_id,
                source_kind=DecisionSourceKind.CLARIFICATION,
                source_id=request_id,
                action=DecisionAction.RESUME,
                principals=principals,
            )
            if existing is None:
                await authorizer.authorize_pending_source(
                    team_id=team_id,
                    session_id=rc_session_id,
                    workspace_id=workspace_id,
                    requester_ref=cur_uid,
                )
                prepared = await terminal_factory.record_source_decision(
                    team_id=team_id,
                    session_id=rc_session_id,
                    turn_id=pre.turn_id,
                    source_kind=DecisionSourceKind.CLARIFICATION,
                    source_id=request_id,
                    action=DecisionAction.RESUME,
                    principals=principals,
                    source_values=normalized,
                )
            else:
                # ``/resolve`` is the sole decision CAS and also binds its
                # projection-effect plan.  ``/resume_turn`` consumes that
                # durable winner; rebuilding a second request here without
                # the already-committed effect plan changes the canonical
                # decision digest and turns every normal resolve→resume into
                # ``decision_conflict``.
                prepared = existing
            from bowhead_core.terminal_protocol import DecisionState
            from bowhead_core.terminal_protocol_store import (
                DecisionAccessRevokedError,
            )

            if prepared.decision.state is DecisionState.DECISION_RECORDED:
                try:
                    await authorizer.authorize(
                        prepared,
                        workspace_id=workspace_id,
                    )
                except DecisionAccessRevokedError as denial:
                    await terminal_factory.commit_decision_authorization_denial(
                        prepared,
                        denial_reason_code=getattr(
                            denial,
                            "reason_code",
                            "access_revoked",
                        ),
                    )
                    raise
            terminal_runtime = await terminal_factory.open_decision_runtime(
                prepared,
                turn_req,
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
                terminal_protocol_http_error,
            )

            if prepared is None:
                raise terminal_protocol_http_error(exc) from exc
            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        turn_req = _dc_replace(
            turn_req,
            terminal_runtime=terminal_runtime,
            terminal_auth_fence_vector_digest=(prepared.preflight.authz_snapshot_digest),
        )

    user_store_for_deduct = getattr(request.app.state, "user_store", None) if team_id else None
    _notice_values = list(pre.chosen_values or ([pre.chosen_value] if pre.chosen_value else []))
    if terminal_factory is None:
        # Legacy compatibility buses need a persisted seq seed.  A v1 resume
        # uses its generation-local durable writer and must have no fallible
        # await between open/claim and sole worker spawn.
        from bowhead_core.agent_execution_binding import AgentExecutionBindingError

        from bowhead_gateway.turn_service import turn_seq_seed

        _resume_initial_seq = await turn_seq_seed(
            getattr(request.app.state, "turn_log_store", None),
            turn_req.turn_id,
        )
        try:
            spawned = await spawn_resume_turn(
                orchestrator,
                turn_req,
                user_store=user_store_for_deduct,
                team_id=team_id,
                entitlements=getattr(request.app.state, "entitlements", None),
                initial_seq=_resume_initial_seq,
                resume_notice={
                    "request_id": request_id,
                    "chosen_values": ([] if skipped_clarification else _notice_values),
                    "skipped": skipped_clarification,
                },
            )
        except ResumeTurnConflictError as exc:
            raise rule_clarification_http_error(
                409,
                "turn_already_terminal",
                nested_error=True,
            ) from exc
        except (ValueError, AgentExecutionBindingError) as exc:
            if (
                turn_req.reference_clarification_id is None
                and turn_req.resume_clarification_id is None
                and turn_req.document_clarification_id is None
            ):
                raise
            raise rule_clarification_http_error(
                409, "clarification_resume_unavailable", nested_error=True, clarification_stage="clarification_resume_preflight"
            ) from exc
        execution_generation = spawned.execution_generation
    else:
        from bowhead_gateway.turn_service import spawn_turn

        try:
            await spawn_turn(
                orchestrator,
                turn_req,
                user_store=user_store_for_deduct,
                team_id=team_id,
                entitlements=getattr(
                    request.app.state,
                    "entitlements",
                    None,
                ),
                name_prefix="turn_resume",
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        execution_generation = terminal_runtime.scope.generation

    logger.info(
        "clarify.resume_turn_spawned req=%s turn=%s glossary_keys=%d",
        request_id,
        turn_req.turn_id,
        len(resume_glossary),
    )

    return ResumeTurnResultDTO(
        ok=True,
        request_id=request_id,
        turn_id=turn_req.turn_id,
        stream_url=f"/api/v1/chat/stream/{turn_req.turn_id}",
        execution_generation=execution_generation,
    )


# CTO review #5 (2026-04-29): user changes mind; null out directive + edited_subs.
# Cancel only before injection (injected_at IS NULL); next turn's force_spawn already used injected values, so revocation is pointless.
class CancelDirectiveResultDTO(BaseModel):
    ok: bool
    request_id: str
    cancelled: bool


@router.post("/{request_id}/cancel-directive", response_model=CancelDirectiveResultDTO, dependencies=[Depends(require_not_readonly)])
async def cancel_directive_endpoint(
    request_id: str,
    request: Request,
    clarify: ClarificationStore = Depends(_clarify_store),
) -> CancelDirectiveResultDTO:
    pre = await clarify.get(request_id)
    if not pre:
        raise rule_clarification_http_error(404, "clarification_not_found")
    cur_uid = _current_user_id(request)
    if pre.user_id != cur_uid:
        raise rule_clarification_http_error(404, "clarification_not_found")
    await _require_clarification_lifecycle(
        pre,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    if pre.kind != "task_plan":
        raise rule_clarification_http_error(
            400,
            "clarification_cancel_directive_invalid",
        )
    cancelled = await clarify.cancel_directive(request_id, user_id=cur_uid)
    if cancelled:
        try:
            from bowhead_core import metrics as _m

            _m.inc(
                "bowhead_clarification_directive_cancelled_total",
                labels={"workspace_id": pre.workspace_id},
                help="P1 CTO review #5: 用户撤销 directive 的次数.",
            )
        except Exception:  # noqa: S110
            pass
        try:
            from bowhead_gateway.turn_service import turn_seq_seed

            registry = get_event_registry()
            _seed = await turn_seq_seed(
                getattr(request.app.state, "turn_log_store", None),
                pre.turn_id,
            )
            # dingsiyan #11 related boundary: post-pause receipt resend must be out-of-band (seq seeding + closed-bus fallback).
            await registry.emit_out_of_band(
                pre.turn_id,
                OrchestratorEvent(
                    event_type="clarification.directive_cancelled",
                    turn_id=pre.turn_id,
                    payload={"request_id": request_id},
                ),
                fallback_initial_seq=_seed,
            )
        except Exception as e:
            logger.debug("clarify.cancel_event_failed err=%s", e)
    return CancelDirectiveResultDTO(
        ok=True,
        request_id=request_id,
        cancelled=cancelled,
    )
