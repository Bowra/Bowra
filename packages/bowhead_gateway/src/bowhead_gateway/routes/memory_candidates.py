"""Memory candidate API (Dreaming Light, V3 PRD-B).

V3 changes:
  - User-self-approve (was admin-gate) — already true at infra level.
  - Cross-user ownership check on approve/reject (security fix, spike #4).
  - Audit log + telemetry on every decision.
  - status filter via list_for_user(status=).

GET  /api/v1/memory/candidates?status=pending|approved|rejected|all
POST /api/v1/memory/candidates/{cid}/approve   → write into user_memory
POST /api/v1/memory/candidates/{cid}/reject
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite
from bowhead_core.memory_auto_capture import (
    _CaptureRateLimiter,
    build_router_drafter,
    draft_personal_memory,
    reason_user_message,
)
from bowhead_core.memory_notes import NotesCharLimitError, NotesLockedError
from bowhead_core.personal_memory_governance import (
    ApprovalTarget,
    CandidateNotFoundError,
    CandidateTargetError,
    CandidateWriteGateError,
    GovernanceCandidate,
    PersonalMemoryGovernanceStore,
)
from bowhead_core.user_memory import (
    MemoryConfidenceConflictError,
    MemoryEntryLockedError,
    MemoryScopeConflictError,
)
from bowhead_memory.candidates import (
    ORIGIN_DREAMING,
    ORIGIN_FEEDBACK_DRAFT,
    REDRAFT_ACTOR,
    humanize_memory_ids,
    memory_ids_in,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from bowhead_gateway.dtos import (
    MemoryCandidateCapabilitiesDTO,
    MemoryCandidateDTO,
    MemoryCandidateListDTO,
    MemoryCandidateScopeDTO,
    MemoryCandidateSourceTurnDTO,
    MemoryCandidateSupersedesItemDTO,
)
from bowhead_gateway.routes._deps import resolve_team_router

if TYPE_CHECKING:
    from bowhead_core.metrics_log import MetricsLog

    from bowhead_memory import MemoryCandidate, MemoryCandidateStore

logger = logging.getLogger(__name__)
router = APIRouter()


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _store(request: Request) -> MemoryCandidateStore:
    s: MemoryCandidateStore | None = getattr(
        request.app.state,
        "memory_candidate_store",
        None,
    )
    if s is None:
        raise HTTPException(503, "memory candidate store not configured")
    return s


def _user_id(request: Request) -> str:
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


def _governance_store(request: Request) -> PersonalMemoryGovernanceStore:
    store = getattr(request.app.state, "personal_memory_governance_store", None)
    if store is not None:
        return store
    candidate_store = getattr(request.app.state, "memory_candidate_store", None)
    if candidate_store is None:
        raise HTTPException(503, "personal memory governance store not configured")
    return PersonalMemoryGovernanceStore(
        candidate_store.db_path,
        root_secret=getattr(request.app.state, "artifact_fetch_secret", None),
    )


def candidate_to_dto(
    c: MemoryCandidate | GovernanceCandidate,
    *,
    accessible_spaces: dict[str, str] | None = None,
    supersedes_items: dict[str, MemoryCandidateSupersedesItemDTO] | None = None,
    memory_texts: dict[str, str] | None = None,
) -> MemoryCandidateDTO:
    """`supersedes_items` 是 id→(key, 当前值) 的**预解析**结果（见 `_supersedes_item_map`）。

    ⚠ 解不出来的 id 会被**丢掉**，不是补一个空壳：解不出来＝那条记忆已经不在了＝
    保存这条建议不会取代任何东西。留着它，画面 D 的「保存后会取代 N 条」就会
    报一个不会发生的数 —— 那比不显示更糟。
    """
    source_space, default_target, allowed_targets, capability_reason = _candidate_scopes(
        c,
        accessible_spaces=accessible_spaces,
    )
    display_value = _display_candidate_value(
        kind=c.kind,
        value=c.value,
    )
    if display_value is None:
        default_target = None
        allowed_targets = []
        capability_reason = "candidate_policy_unavailable"
    return MemoryCandidateDTO(
        origin=str(getattr(c, "origin", ORIGIN_DREAMING) or ORIGIN_DREAMING),
        approved_entry_id=getattr(c, "approved_resource_id", None),
        id=c.id,
        userId=c.user_id,
        kind=c.kind,
        key=c.key,
        value=c.value,
        reason=c.reason,
        sourceTurnId=c.source_turn_id,
        status=c.status,
        decidedBy=c.decided_by,
        decidedAt=c.decided_at,
        createdAt=c.created_at,
        updatedAt=c.updated_at,
        source_space=source_space,
        source_turn=(
            MemoryCandidateSourceTurnDTO(
                availability="unavailable",
                label="来源对话暂不可用",
            )
            if c.source_turn_id
            else None
        ),
        default_target=default_target,
        allowed_targets=allowed_targets,
        display_value=display_value,
        # Never a memory id: older rows kept the id the model copied; name the memory by its text instead.
        display_reason=humanize_memory_ids(c.reason, memory_texts),
        expires_at=_candidate_expires_at(c.created_at),
        canonical_state=c.status,
        capabilities=MemoryCandidateCapabilitiesDTO(
            approve=c.status == "pending" and default_target is not None,
            reject=c.status == "pending",
            reason=capability_reason,
        ),
        version=str(getattr(c, "version", 1)),
        supersedes=[item for sid in (getattr(c, "supersedes", ()) or ()) if (item := (supersedes_items or {}).get(sid)) is not None],
    )


async def _reason_memory_texts(
    user_mem: Any,
    user_id: str,
    candidates: Sequence[MemoryCandidate | GovernanceCandidate],
) -> dict[str, str]:
    """Resolve memory ids mentioned in candidate reasons to the memories' current text (one lookup per page)."""
    ids = {mid for c in candidates for mid in memory_ids_in(c.reason)}
    if not ids or user_mem is None:
        return {}
    fetch = getattr(user_mem, "get_entries_by_ids", None)
    if fetch is None:  # Older stores (test doubles): ids are still removed, just not replaced by text.
        return {}
    try:
        entries = await fetch(user_id, sorted(ids))
    except Exception as exc:  # A reason is decoration; never fail the candidate list over it.
        logger.warning("memory_candidates.reason_lookup_failed user=%s err=%s", user_id, exc)
        return {}
    out: dict[str, str] = {}
    for mid, entry in entries.items():
        if getattr(entry, "deleted_at", None):
            continue  # A trashed memory is not shown again through a suggestion reason.
        value = entry.parsed_value()
        text = value if isinstance(value, str) else (entry.key or "")
        if text:
            out[mid] = text
    return out


async def _supersedes_item_map(
    user_mem: Any,
    user_id: str,
    candidates: Sequence[MemoryCandidate | GovernanceCandidate],
) -> dict[str, MemoryCandidateSupersedesItemDTO]:
    """一页候选里所有 `supersedes` 的 id，一次解析成 key + 当前值（画面 D）。"""
    ids = {sid for c in candidates for sid in (getattr(c, "supersedes", ()) or ())}
    if not ids or user_mem is None:
        return {}
    fetch = getattr(user_mem, "get_entries_by_ids", None)
    if fetch is None:  # Older stores (test doubles) lack this method: degrade without breaking candidate lists.
        return {}
    entries = await fetch(user_id, sorted(ids))
    out: dict[str, MemoryCandidateSupersedesItemDTO] = {}
    for sid, entry in entries.items():
        out[sid] = MemoryCandidateSupersedesItemDTO(
            id=sid,
            key=entry.key,
            current_value=_display_candidate_value(kind=entry.kind, value=entry.parsed_value()),
        )
    return out


def _candidate_scopes(
    candidate: MemoryCandidate | GovernanceCandidate,
    *,
    accessible_spaces: dict[str, str] | None,
) -> tuple[
    MemoryCandidateScopeDTO | None,
    MemoryCandidateScopeDTO | None,
    list[MemoryCandidateScopeDTO],
    str | None,
]:
    global_scope = MemoryCandidateScopeDTO(
        type="global",
        label="仅供你使用 · 所有空间",
        requires_confirmation=True,
    )
    workspace_id = candidate.workspace_id
    if not workspace_id:
        return None, global_scope, [global_scope], None

    name = (accessible_spaces or {}).get(workspace_id)
    if name:
        space_scope = MemoryCandidateScopeDTO(
            type="space",
            space_id=workspace_id,
            space_name=name,
            availability="available",
            label=f"仅供你使用 · {name}",
        )
        allowed = [space_scope]
        if candidate.kind == "note":
            allowed.append(global_scope)
        return space_scope, space_scope, allowed, None

    unavailable = MemoryCandidateScopeDTO(
        type="space",
        availability="unavailable",
        label="仅供你使用 · 不可用的空间",
    )
    if candidate.kind == "note":
        return unavailable, None, [global_scope], "source_space_unavailable"
    return unavailable, None, [], "target_space_unavailable"


def _display_candidate_value(*, kind: str, value: Any) -> str | None:
    if kind not in {"preference", "constraint", "note"}:
        return None
    if isinstance(value, str):
        return value
    if kind in {"preference", "constraint"} and isinstance(
        value,
        (bool, int, float),
    ):
        return str(value)
    return None


def _candidate_expires_at(created_at: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(days=30)).isoformat(timespec="seconds")


async def accessible_candidate_spaces(
    request: Request,
    user_id: str,
) -> dict[str, str]:
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        return {}
    try:
        rows = await store.list_workspaces_for_user(user_id)
    except Exception:
        logger.exception("memory_candidates.space_resolution_failed user=%s", user_id)
        return {}
    return {str(row["workspace_id"]): str(row["name"]) for row in rows if row.get("workspace_id") and row.get("name")}


async def _space_target_is_available(
    request: Request,
    *,
    user_id: str,
    workspace_id: str,
) -> bool:
    if getattr(request.app.state, "user_store", None) is None:
        settings = getattr(request.app.state, "settings", None)
        return getattr(settings, "auth_mode", None) == "dev"
    return workspace_id in await accessible_candidate_spaces(request, user_id)


async def _audit(
    request: Request,
    *,
    user_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return
    try:
        await user_store.log_audit(
            user_id=user_id,
            team_id=None,
            event_type=event_type,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            payload=payload,
        )
    except Exception as e:
        logger.debug("memory_candidates.audit_failed event=%s err=%s", event_type, e)


async def _track(
    request: Request,
    *,
    name: str,
    user_id: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    log: MetricsLog | None = getattr(request.app.state, "metrics_log", None)
    if log is None:
        return
    await log.track(name, user_id=user_id, attrs=attrs)


@router.get("", response_model=MemoryCandidateListDTO)
async def list_candidates(
    request: Request,
    response: Response,
    status: str = Query("pending", pattern="^(pending|approved|rejected|all)$"),
    limit: int = Query(50, ge=1, le=500),
    store: MemoryCandidateStore = Depends(_store),
) -> MemoryCandidateListDTO:
    """List the user's candidates. Per-turn chat cards read `GET /memory/turn-receipt` instead."""
    uid = _user_id(request)
    _mark_no_store(response)
    items = await store.list_for_user(
        user_id=uid,
        status=status,
        limit=limit,
    )
    accessible_spaces = await accessible_candidate_spaces(request, uid)
    user_mem = getattr(request.app.state, "user_memory_store", None)
    supersedes_items = await _supersedes_item_map(user_mem, uid, items)
    memory_texts = await _reason_memory_texts(user_mem, uid, items)
    return MemoryCandidateListDTO(
        items=[
            candidate_to_dto(
                c,
                accessible_spaces=accessible_spaces,
                supersedes_items=supersedes_items,
                memory_texts=memory_texts,
            )
            for c in items
        ],
        scope=status,
    )


class CandidateTargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    space_id: str | None = None


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    target: CandidateTargetBody | None = None


def _ensure_owner(candidate: MemoryCandidate | None, uid: str) -> MemoryCandidate:
    """Spike #4: cross-user ownership check. Treat foreign rows as 404.

    Security log: foreign-row attempts logged at WARN for forensics. The 404
    response is opaque so the caller can't distinguish "no such id" from
    "not yours".
    """
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    if candidate.user_id != uid:
        logger.warning(
            "memory_candidate.cross_user_access actor=%s candidate_owner=%s cid=%s",
            uid,
            candidate.user_id,
            candidate.id,
        )
        raise HTTPException(404, "candidate not found")
    return candidate


async def _resolve_badge_if_done(
    request: Request,
    store: MemoryCandidateStore,
    uid: str,
) -> None:
    """review-fix P1: 用户 pending 清零时解掉 memory.candidate 通知条目。

    兄弟 action 类型全有 resolve 闭环（approval 决策/scheduled recovered）,
    唯独 memory.candidate 缺——badge 会永久红（action 计数只看 state='open',
    与 read_at 无关）。best-effort:失败只记日志,不影响决策主流程。
    """
    try:
        svc = getattr(request.app.state, "notification_service", None)
        if svc is None:
            return
        remaining = await store.list_for_user(user_id=uid, status="pending", limit=1)
        if not remaining:
            await svc.resolve_memory_candidates(user_id=uid)
    except Exception as e:
        logger.debug("memory_candidates.badge_resolve_failed user=%s err=%s", uid, e)


@router.post("/{cid}/approve", response_model=MemoryCandidateDTO)
async def approve(
    cid: str,
    body: DecideBody,
    request: Request,
    response: Response,
    target: str | None = Query(None, pattern="^(structured|notes)$"),
    store: MemoryCandidateStore = Depends(_store),
    governance: PersonalMemoryGovernanceStore = Depends(_governance_store),
) -> MemoryCandidateDTO:
    """V3.5 PRD-C: ?target=structured|notes routes the approved candidate
    to memory_entries (default, PRD-B compat) or memory_notes (V3.5).

    Target write and candidate decision are committed in one shared-DB
    transaction. Any target conflict or write failure rolls the whole
    transaction back, leaving the candidate pending.
    """
    uid = _user_id(request)
    _mark_no_store(response)
    existing = _ensure_owner(await store.get(cid), uid)

    if existing.status != "pending":
        # Already decided (idempotent path) — return DTO without re-writing.
        return candidate_to_dto(existing)

    effective_target = await _resolve_effective_target(
        request,
        user_id=uid,
        candidate=existing,
        selected=body.target,
        legacy_hint=target,
    )

    try:
        c = await governance.approve_candidate(
            user_id=uid,
            candidate_id=cid,
            target=effective_target,
            decided_by=uid,
        )
    except CandidateNotFoundError:
        raise HTTPException(404, "candidate not found") from None
    except CandidateWriteGateError as exc:
        # Memory redesign batch 2: content is unsuitable for long-term memory (one-off/language settings/secrets).
        # 422, not 400: the request is valid; **content** failed the gate. Pass the reason unchanged to users.
        raise HTTPException(
            422,
            {"code": "memory_write_gate_refused", "reason": exc.reason, "message": exc.gate_message},
        ) from None
    except CandidateTargetError as exc:
        raise HTTPException(400, str(exc)) from None
    except MemoryScopeConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_scope_conflict",
                "entry_id": exc.entry_id,
                "existing_scope": exc.existing_workspace_id or "global",
                "requested_scope": exc.requested_workspace_id,
            },
        ) from None
    except MemoryConfidenceConflictError as exc:
        # Memory redesign batch 1 (tech-design §4.2 b): low cannot overwrite high. Entire transaction rolled back,
        # so **candidate stays pending** (a candidate instead of an overwrite); users can edit/delete old memory before approval.
        raise HTTPException(
            409,
            {
                "code": "memory_confidence_conflict",
                "entry_id": exc.entry_id,
                "existing_confidence": exc.existing_confidence,
            },
        ) from None
    except (MemoryEntryLockedError, NotesLockedError) as exc:
        raise HTTPException(423, str(exc)) from None
    except NotesCharLimitError as exc:
        raise HTTPException(409, f"notes_char_limit:{exc}") from None
    except aiosqlite.Error:
        logger.exception("memory_candidate.atomic_approve_failed cid=%s", cid)
        raise HTTPException(503, "candidate approval temporarily unavailable") from None

    await _resolve_badge_if_done(request, store, uid)
    await _audit(
        request,
        user_id=uid,
        event_type="memory.candidate.approve",
        payload={
            "cid": cid,
            "kind": c.kind,
            "target": effective_target,
            "result": "approved",
        },
    )
    await _track(
        request,
        name="memory.candidate.approve",
        user_id=uid,
        attrs={
            "cid": cid,
            "kind": c.kind,
            "target": effective_target,
        },
    )
    return candidate_to_dto(c)


async def _resolve_effective_target(
    request: Request,
    *,
    user_id: str,
    candidate: MemoryCandidate,
    selected: CandidateTargetBody | None,
    legacy_hint: str | None,
) -> ApprovalTarget:
    if (
        _display_candidate_value(
            kind=candidate.kind,
            value=candidate.value,
        )
        is None
    ):
        raise HTTPException(
            409,
            {
                "code": "memory_candidate_policy_unavailable",
                "reason": "candidate_kind_or_value_not_supported",
            },
        )
    source_workspace_id = candidate.workspace_id
    if selected is not None:
        if selected.type == "global":
            if selected.space_id is not None:
                raise HTTPException(422, "global target cannot include space_id")
            if source_workspace_id is not None and candidate.kind != "note":
                raise HTTPException(
                    409,
                    {
                        "code": "memory_target_not_allowed",
                        "reason": "structured_candidate_scope_is_fixed",
                    },
                )
            return "notes" if candidate.kind == "note" else "structured"
        if selected.type == "space":
            if not selected.space_id or selected.space_id != source_workspace_id:
                raise HTTPException(
                    409,
                    {
                        "code": "memory_target_not_allowed",
                        "reason": "target_space_mismatch",
                    },
                )
            if not await _space_target_is_available(
                request,
                user_id=user_id,
                workspace_id=selected.space_id,
            ):
                raise HTTPException(
                    409,
                    {
                        "code": "memory_target_unavailable",
                        "reason": "target_space_unavailable",
                    },
                )
            return "structured"
        raise HTTPException(422, "target.type must be global or space")

    if source_workspace_id is not None:
        if not await _space_target_is_available(
            request,
            user_id=user_id,
            workspace_id=source_workspace_id,
        ):
            raise HTTPException(
                409,
                {
                    "code": "memory_target_unavailable",
                    "reason": "target_space_unavailable",
                },
            )
        # Legacy `target=notes` is only a hint. A candidate with a source Space
        # must retain that scope; note candidates become product `kind=note`
        # entries instead of being silently promoted to global About you.
        return "structured"

    if legacy_hint == "notes" and candidate.kind != "note":
        raise HTTPException(400, "only kind 'note' can be approved to About you")
    if legacy_hint == "structured" and candidate.kind == "note":
        raise HTTPException(
            400,
            "global note candidate must use ?target=notes",
        )
    return "notes" if candidate.kind == "note" else "structured"


@router.post("/{cid}/reject", response_model=MemoryCandidateDTO)
async def reject(
    cid: str,
    body: DecideBody,
    request: Request,
    response: Response,
    store: MemoryCandidateStore = Depends(_store),
) -> MemoryCandidateDTO:
    uid = _user_id(request)
    _mark_no_store(response)
    _ensure_owner(await store.get(cid), uid)

    c = await store.set_decision(cid=cid, status="rejected", decided_by=uid)
    if c is None:
        raise HTTPException(404, "candidate not found")
    await _resolve_badge_if_done(request, store, uid)
    await _audit(
        request,
        user_id=uid,
        event_type="memory.candidate.reject",
        payload={"cid": cid, "kind": c.kind, "result": "rejected"},
    )
    await _track(
        request,
        name="memory.candidate.reject",
        user_id=uid,
        attrs={"cid": cid, "kind": c.kind},
    )
    return candidate_to_dto(c)


class RedraftBody(BaseModel):
    """用户点「改一下」时说的那句「哪里不对」。"""

    comment: str


#: User feedback length limit. ★ Feedback enters the drafting prompt; without a limit, one chat message
#: directly determines the size of an LLM call's input. Use the same order of magnitude as candidate text.
_MAX_REDRAFT_COMMENT_CHARS = 2000

#: Redraft cost guard. ★ **Each user click makes one LLM call**,
#: easier to trigger than direct writes (which at least have a coarse keyword filter; this only has a button).
#: Direct writes already had a 60s cooldown + daily cap 40; this initially had neither — caught in 2026-09-09 review.
#: Deliberately reuse the same structure/values (_CaptureRateLimiter), aligning cost guards across three paths.
_REDRAFT_LIMITER = _CaptureRateLimiter(min_interval_seconds=10.0, daily_cap=40)


@router.post("/{cid}/redraft", response_model=MemoryCandidateDTO)
async def redraft(
    cid: str,
    body: RedraftBody,
    request: Request,
    response: Response,
    store: MemoryCandidateStore = Depends(_store),
) -> MemoryCandidateDTO:
    """「改一下」：带用户意见重新起草，旧的让位、新的接着问。

    ★★ **不走 reject**。`reject` 会写 30 天逐字抑制——用户说「我要换个说法」
    却被记成「我不要这条」，下次他再说同一件事会被静默压掉。
    这里用 `REDRAFT_ACTOR` 走 `set_decision`，落 `decision_kind='feedback_redrafted'`
    且**不写抑制**（`candidates.py` 的白名单只让 dreaming 那一池写抑制）。

    ★ 只有起草档（`origin='feedback_draft'`）能重写：dreaming 蒸馏出来的候选
    不是「对用户某句话的一版改写」，没有「再写一遍」的语义。
    """
    uid = _user_id(request)
    _mark_no_store(response)
    cand = _ensure_owner(await store.get(cid), uid)

    if getattr(cand, "origin", ORIGIN_DREAMING) != ORIGIN_FEEDBACK_DRAFT:
        # 400, not 404: the candidate exists and is yours; this action simply does not apply to it.
        raise HTTPException(400, "candidate is not a feedback draft")
    if cand.status != "pending":
        raise HTTPException(409, "candidate already decided")

    # ★ Guard **after reading parameters, before the LLM call**: invalid requests must not consume quota
    # (otherwise repeated empty feedback exhausts users' quota despite incurring no cost).
    comment = str(body.comment or "").strip()
    if not comment:
        raise HTTPException(400, "comment is required")
    if len(comment) > _MAX_REDRAFT_COMMENT_CHARS:
        raise HTTPException(400, "comment too long")

    if not _REDRAFT_LIMITER.take(uid):
        # 429 instead of silence: users clicked a button and must know why nothing happened.
        raise HTTPException(429, {"code": "redraft_rate_limited"})

    team_id = await _team_id_for(request, uid)
    router_obj, miss = await resolve_team_router(
        request.app.state,
        team_id,
        agent_type="echo",
        site="memory_candidate_redraft",
    )
    if router_obj is None:
        # ★ 503, not 500: this is currently unavailable, not an incorrect user request.
        # Correct degradation skips the LLM (resolve_team_router discipline),
        # rather than invoking a router guaranteed to refuse.
        raise HTTPException(503, {"code": "model_unavailable", "reason": miss.value if miss else "unknown"})

    draft = await draft_personal_memory(
        user_text=_candidate_source_text(cand),
        drafter=build_router_drafter(router_obj, trace_id=f"redraft:{cid}"),
        kind=cand.kind,
        revision_comment=comment,
        trace=cid,
    )
    if draft.text is None:
        # Same rejection reasons as direct writes (generated by the same function), with the same copy table.
        raise HTTPException(
            422,
            {
                "code": "memory_redraft_failed",
                "reason": draft.reason,
                "message": reason_user_message(draft.reason) or "",
            },
        )

    # Old draft gives way; **not a rejection**, so write no suppression.
    await store.set_decision(cid=cid, status="rejected", decided_by=REDRAFT_ACTOR)
    fresh = await store.enqueue(
        user_id=uid,
        kind=cand.kind,
        key=cand.key,
        value=draft.text,
        reason=None,
        source_turn_id=cand.source_turn_id,
        workspace_id=cand.workspace_id,
        memory_type=getattr(cand, "memory_type", None),
        origin=ORIGIN_FEEDBACK_DRAFT,
    )
    await _audit(
        request,
        user_id=uid,
        event_type="memory.candidate.redraft",
        payload={"cid": cid, "new_cid": fresh.id, "kind": cand.kind},
    )
    await _track(request, name="memory.candidate.redraft", user_id=uid, attrs={"cid": cid, "kind": cand.kind})
    return candidate_to_dto(fresh)


async def _team_id_for(request: Request, uid: str) -> str | None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return None
    try:
        user = await user_store.get_user(uid)
    except Exception:
        logger.warning("memory_candidates.team_lookup_failed uid=%s", uid, exc_info=True)
        return None
    return getattr(user, "team_id", None)


def _candidate_source_text(cand: MemoryCandidate) -> str:
    """重新起草的输入 = 上一版的正文。

    ⚠ 用的是**上一版草稿**而不是用户原话：原话没有存在候选行上
    （`source_turn_id` 只指向那一轮，正文没落）。这一点要写明，
    否则下一个人会以为这里拿到的是用户说的那句话。
    """
    raw = cand.value
    return raw if isinstance(raw, str) else str(raw)


# ─── bulk decide (PRD-B Day 6-7) ─────────────────────────


class BulkDecideRequest(BaseModel):
    cids: list[str]  # max 200
    action: str  # "approve" | "reject"


class BulkDecideResultItemDTO(BaseModel):
    cid: str
    ok: bool
    status: str | None = None
    error: str | None = None


class BulkDecideResultDTO(BaseModel):
    action: str
    total: int
    succeeded: int
    failed: int
    items: list[BulkDecideResultItemDTO]


_BULK_MAX = 200


async def _approve_candidate_inner(
    *,
    store: MemoryCandidateStore,
    governance: PersonalMemoryGovernanceStore,
    cid: str,
    uid: str,
) -> tuple[bool, str | None, str | None]:
    """Single approval helper. Returns (ok, status, error_msg).

    Encapsulates: ownership check + set_decision + write to user_memory.
    """
    cand = await store.get(cid)
    if cand is None or cand.user_id != uid:
        return False, None, "not_found"
    if cand.status != "pending":
        return True, cand.status, None  # idempotent: already decided
    target: ApprovalTarget = "structured" if cand.workspace_id is not None or cand.kind != "note" else "notes"
    try:
        c = await governance.approve_candidate(
            user_id=uid,
            candidate_id=cid,
            target=target,
            decided_by=uid,
        )
    except CandidateNotFoundError:
        return False, None, "not_found"
    except MemoryScopeConflictError:
        return False, cand.status, "memory_scope_conflict"
    except MemoryConfidenceConflictError:
        return False, cand.status, "memory_confidence_conflict"
    except (MemoryEntryLockedError, NotesLockedError):
        return False, cand.status, "target_locked"
    except NotesCharLimitError:
        return False, cand.status, "notes_char_limit"
    except CandidateWriteGateError as exc:
        return False, cand.status, f"write_gate:{exc.reason}"
    except (CandidateTargetError, aiosqlite.Error):
        logger.exception("memory_candidate.bulk_atomic_approve_failed cid=%s", cid)
        return False, cand.status, "write_failed"
    return True, c.status, None


@router.post("/bulk-decide", response_model=BulkDecideResultDTO)
async def bulk_decide(
    body: BulkDecideRequest,
    request: Request,
    response: Response,
    store: MemoryCandidateStore = Depends(_store),
    governance: PersonalMemoryGovernanceStore = Depends(_governance_store),
) -> BulkDecideResultDTO:
    """V3 PRD-B Day 6-7: bulk approve / reject up to 200 candidates.

    Single audit row summarizing result. Partial failures returned per-item.
    Cross-user candidates silently skipped (404 → ok=False, error='not_found')
    rather than aborting the whole batch.
    """
    uid = _user_id(request)
    _mark_no_store(response)
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if len(body.cids) == 0:
        raise HTTPException(400, "cids cannot be empty")
    if len(body.cids) > _BULK_MAX:
        raise HTTPException(400, f"max {_BULK_MAX} cids per call")
    # de-dup while preserving order
    seen: set[str] = set()
    unique_cids: list[str] = []
    for c in body.cids:
        if c not in seen:
            seen.add(c)
            unique_cids.append(c)

    items: list[BulkDecideResultItemDTO] = []
    succeeded = 0
    failed = 0

    if body.action == "approve":
        for cid in unique_cids:
            ok, st, err = await _approve_candidate_inner(
                store=store,
                governance=governance,
                cid=cid,
                uid=uid,
            )
            if ok:
                succeeded += 1
            else:
                failed += 1
            items.append(
                BulkDecideResultItemDTO(
                    cid=cid,
                    ok=ok,
                    status=st,
                    error=err,
                )
            )
    else:
        for cid in unique_cids:
            cand = await store.get(cid)
            if cand is None or cand.user_id != uid:
                failed += 1
                items.append(
                    BulkDecideResultItemDTO(
                        cid=cid,
                        ok=False,
                        error="not_found",
                    )
                )
                continue
            c = await store.set_decision(
                cid=cid,
                status="rejected",
                decided_by=uid,
            )
            if c is None:
                failed += 1
                items.append(
                    BulkDecideResultItemDTO(
                        cid=cid,
                        ok=False,
                        error="not_found",
                    )
                )
            else:
                succeeded += 1
                items.append(
                    BulkDecideResultItemDTO(
                        cid=cid,
                        ok=True,
                        status=c.status,
                    )
                )

    await _resolve_badge_if_done(request, store, uid)
    await _audit(
        request,
        user_id=uid,
        event_type=f"memory.candidate.bulk_{body.action}",
        payload={
            "total": len(unique_cids),
            "succeeded": succeeded,
            "failed": failed,
        },
    )
    await _track(
        request,
        name=f"memory.candidate.bulk_{body.action}",
        user_id=uid,
        attrs={"total": len(unique_cids), "succeeded": succeeded, "failed": failed},
    )
    return BulkDecideResultDTO(
        action=body.action,
        total=len(unique_cids),
        succeeded=succeeded,
        failed=failed,
        items=items,
    )
