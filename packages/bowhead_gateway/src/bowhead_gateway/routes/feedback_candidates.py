"""建议收件箱审批面（agent-feedback-loop 批 2b-i，tech-design §2.3b/§2.4）.

骨架刻度（用户拍板 2026-09-08）：只做列表/详情/决定/采纳/撤回，digest、批量、
筛选重面全不做——真语料空跑显示收件箱初期每团队每月个位数进货，产量主力
在批 3 教练。UI 挂团队管理页新标签（设计稿见 tech-design §2.7）。

权限矩阵（§2.4）：

- 列表/详情：team owner/team_admin 全量；proposer 只读自己提的（他人证据
  只见计数）。其余人对任何候选一律 404（反枚举，仿 ``_require_turn_access``）。
- 决定（reject / 不经预填的 accept）：owner/team_admin，store 内再叠禁自批
  （proposer ∪ contributors 不得裁决，``SelfApprovalError``）。
  **不用 can_manage_shares 之类带 owner 短路的门**（team_skills.py:106 教训：
  owner 短路＝作者自批）。
- 采纳（accept + 预填）：semantic → owner/team_admin（预填空间说明）；agent_def → 目标 agent
  的**资源 owner**（能打开 working copy 的人；PRD §5 agent_def 行），经
  ``AgentReleaseReadStore.working_copy`` 的所有权查询裁定。
- 撤回：store 裁定（仅 proposer 且证据只剩本人）。

采纳物化＝教练第一触发面（§2.3b）：agent_def 先起草后落决定——reroute 裁决
不落决定（candidate 留 open）；起草失败降级为「无预填」而决定照落。教练产出
已过消毒/记号/URL 闸（coach_drafting 内）。

裸 turn id 永不入列表/详情响应（§2.4）；来源对话经同一份解析器按读取者权限
求交，打开走 ``GET /{id}/sources/{index}`` 再验一次。

功能已正式发布；权限、作用域和存储就绪校验仍须执行。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

import aiosqlite
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore
from bowhead_core.agent_release_read_store import AgentReleaseReadStore
from bowhead_core.agent_revision_store import (
    AgentReleaseCutoverRequiredError,
    AgentRevisionError,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.coach_drafting import draft_agent_change_proposal, draft_config_edit, new_urls_in_draft
from bowhead_core.feedback_candidate_store import (
    ConcurrentWriteConflict,
    FeedbackCandidate,
    FeedbackCandidateStore,
    FeedbackLoopDisabled,
    InvalidStateError,
    QuotaExceeded,
    SelfApprovalError,
    SuppressedSignature,
)
from bowhead_core.feedback_casework_store import FeedbackCaseworkStore
from bowhead_core.feedback_loop_flags import KEY_TEAM_FEEDBACK_CASEWORK_ENABLED, parse_casework_enabled, team_casework_enabled
from bowhead_core.prompt_injection_defense import sanitize_for_injection
from bowhead_core.user_store import User, UserStore
from bowhead_router.usage import usage_context
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway.feedback_surfaces import COACH_KINDS, Surface, SurfaceError, base_matches, build_surface
from bowhead_gateway.routes._deps import team_router_or_409
from bowhead_gateway.routes.auth import _current_user, _store
from bowhead_gateway.routes.turns import _require_turn_access

logger = logging.getLogger(__name__)


def _asset_kind(cand: FeedbackCandidate) -> str:
    """候选指向的资产类型：retire 候选在 proposed.asset_kind 里说明退的是什么。"""

    if cand.target_kind != "retire":
        return cand.target_kind
    kind = cand.proposed.get("asset_kind")
    return kind if isinstance(kind, str) else ""


def _involved(cand: FeedbackCandidate) -> set[str]:
    """禁自批集合＝proposer ∪ 全部 contributor（与 store.decide 同口径）。"""

    return {cand.proposer_id} | {c for e in cand.evidence if isinstance(c := e.get("contributor_id"), str) and c}


def _is_due(cand: FeedbackCandidate) -> bool:
    """到期但尚未被惰性翻转的行：对外等同非 open。"""

    try:
        due = datetime.fromisoformat(cand.expires_at)
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return due <= datetime.now(UTC)


router = APIRouter()

_NOT_FOUND = HTTPException(status_code=404, detail={"code": "feedback_candidate_not_found"})


def _casework(request: Request) -> FeedbackCaseworkStore | None:
    return getattr(request.app.state, "feedback_casework_store", None)


def _fbc(request: Request) -> FeedbackCandidateStore:
    store = getattr(request.app.state, "feedback_candidate_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail={"code": "feedback_store_unavailable"})
    return store


async def _is_inbox_admin(store: UserStore, user: User) -> bool:
    return await store.is_team_admin(user.id, user.team_id)


async def _visible_candidate(
    request: Request,
    user: User,
    store: UserStore,
    candidate_id: str,
) -> tuple[FeedbackCandidate, str]:
    """取候选并裁读权。返回 (candidate, role)；无权/不存在统一 404。

    role ∈ {"admin", "proposer", "resource_owner"}——admin 与 resource_owner
    是审批人视角（判据/联署全可见），proposer 只读自己的份（§2.4）。
    """

    cand = await _fbc(request).get(candidate_id)
    if cand is None or cand.team_id != user.team_id:
        raise _NOT_FOUND
    if await _is_inbox_admin(store, user):
        return cand, "admin"
    if cand.proposer_id == user.id:
        return cand, "proposer"
    # PRD §5: agent_def reviewer is the resource owner (possibly an ordinary member), who can read candidates
    # targeting their Agent. Ownership follows working copy's COALESCE(owner,creator)
    # query (sole implementation: agent_revision_store.py:99). Failed lookup always 404.
    if _asset_kind(cand) == "agent_def":
        try:
            await AgentReleaseReadStore(store.db_path).working_copy(team_id=user.team_id, agent_id=cand.target_ref, actor_user_id=user.id)
        except AgentReleaseCutoverRequiredError:
            # Legacy-mode ownership still holds; only owners receive this error.
            return cand, "resource_owner"
        except AgentRevisionError:
            raise _NOT_FOUND from None
        return cand, "resource_owner"
    raise _NOT_FOUND


# Source resolution runs turn authorization (several queries) per ref. Write-side propose has no cap;
# cap reads so one candidate cannot stall the detail endpoint. Do not expose excess refs.
_SOURCE_REF_CAP = 20


def _iter_source_refs(cand: FeedbackCandidate) -> list[str]:
    refs: list[str] = []
    for entry in cand.evidence:
        for ref in entry.get("provenance_refs") or []:
            if isinstance(ref, str):
                refs.append(ref)
            if len(refs) >= _SOURCE_REF_CAP:
                return refs
    return refs


async def _source_spaces(store: UserStore, cand: FeedbackCandidate) -> list[dict[str, Any]]:
    """Active Spaces of this Team whose conversations backed the suggestion, most conversations first.

    A wording suggestion applies to the Space it was raised in; the adopter adds it to each Space
    separately. Unknown or foreign turns are skipped; ties keep first-seen order.
    """

    turn_ids = [ref for ref in _iter_source_refs(cand) if ref.startswith("t_")]
    if not turn_ids:
        return []
    marks = ",".join("?" * len(turn_ids))
    async with aiosqlite.connect(store.db_path) as db:
        rows = await (
            await db.execute(
                f"SELECT t.id, w.id, w.name, s.id FROM turns t JOIN sessions s ON s.id = t.session_id "
                f"JOIN workspaces w ON w.id = s.workspace_id "
                f"WHERE t.id IN ({marks}) AND w.team_id = ? AND w.status = 'active'",
                (*turn_ids, cand.team_id),
            )
        ).fetchall()
    by_turn = {turn_id: (workspace_id, name, session_id) for turn_id, workspace_id, name, session_id in rows}
    spaces: dict[str, dict[str, Any]] = {}
    sessions: dict[str, set[str]] = {}
    for turn_id in turn_ids:
        if turn_id in by_turn:
            workspace_id, name, session_id = by_turn[turn_id]
            spaces.setdefault(workspace_id, {"workspace_id": workspace_id, "name": name})
            sessions.setdefault(workspace_id, set()).add(session_id)
    for workspace_id, entry in spaces.items():
        entry["conversation_count"] = len(sessions[workspace_id])
    return sorted(spaces.values(), key=lambda entry: -entry["conversation_count"])


async def _space_instruction_targets(request: Request, store: UserStore, cand: FeedbackCandidate) -> list[dict[str, Any]]:
    """Source Spaces with their current instructions version and the version this suggestion was added in."""
    spaces = await _source_spaces(store, cand)
    instructions = getattr(request.app.state, "space_instructions_store", None)
    if instructions is None or not spaces:
        return spaces
    current = await instructions.current_versions([space["workspace_id"] for space in spaces])
    added = await instructions.versions_from_candidate(cand.id)
    names = await store.get_users_by_ids([version.created_by for version in added.values()])
    for space in spaces:
        space["instructions_version"] = current.get(space["workspace_id"], 0)
        version = added.get(space["workspace_id"])
        space["added"] = (
            {"version": version.version, "by_name": names.get(version.created_by, version.created_by), "at": version.created_at}
            if version is not None
            else None
        )
    return spaces


async def _viewable_source_flags(request: Request, user: User, cand: FeedbackCandidate) -> list[bool]:
    """§2.4 的「同一份解析器」：详情计数与来源打开都走这里的按人求交。"""

    flags: list[bool] = []
    for ref in _iter_source_refs(cand):
        if not ref.startswith("t_"):
            flags.append(False)
            continue
        try:
            await _require_turn_access(ref, request, user)
            flags.append(True)
        except HTTPException:
            flags.append(False)
    return flags


async def _label_for(store: UserStore, team_id: str, kind: str, ref: str) -> str | None:
    """(kind, ref) → 展示名；查不到返回 None。展示名是便利不是权限判定，永远服务端解析、不信模型输出。"""

    try:
        if kind == "agent_def":
            ag = await store.get_team_subagent(team_id, ref)
            return (ag.display_name or ag.name) if ag is not None else None
        if kind == "team_skill":
            sk = await store.get_team_skill(team_id, ref)
            return (sk.display_name or sk.name) if sk is not None else None
        if kind == "tool_source":
            src = await store.get_team_tool_source(team_id, ref)
            return src.name if src is not None else None
    except Exception:  # Failed display-name lookup must not affect approval UI.
        logger.exception("feedback candidate target label lookup failed kind=%s ref=%s", kind, ref)
    return None


async def _target_labels(store: UserStore, team_id: str, cands: list[FeedbackCandidate]) -> dict[str, str]:
    """候选 id → 目标资产展示名（116 dogfood：列表里裸 osa_/usr_ id 没人认得）。

    只解析 agent（团队 subagent 表）与技能；口径/新资产/工具的 target_ref 本身就是可读名。
    查不到的保持裸 id。
    """

    out: dict[str, str] = {}
    cache: dict[tuple[str, str], str | None] = {}
    for cand in cands:
        key = (_asset_kind(cand), cand.target_ref)
        if key not in cache:
            cache[key] = await _label_for(store, team_id, *key)
        if cache[key]:
            out[cand.id] = cache[key] or ""
    return out


def _recurrence_view(
    prior: FeedbackCandidate,
    prior_casework: Any,
    *,
    reviewer: bool,
    names: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """FR7 读面：这条上次批过——什么时候、谁批的、教练当时判在哪。

    只给结构事实，不下判断（「为什么又回来了」是审批人要查的，不是我们能替他断的）。

    **按视角裁**（与 `casework` 同门）：提议人只得到「此前已被采纳过 + 多久前」——这对他有用
    （省得重复提），而**上次谁批的、教练当时判在哪，都是另一条他看不到的候选上的信息**，
    只给审批视角。`decided_by` 的展示名由调用方按既有的 `get_users_by_ids` 解析。
    """

    days = None
    if prior.decided_at:
        try:
            decided = datetime.fromisoformat(prior.decided_at)
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=UTC)
            days = max(0, ((now or datetime.now(UTC)) - decided).days)
        except ValueError:
            days = None
    if not reviewer:
        return {"candidate_id": None, "decided_at": None, "decided_by": None, "decided_by_display": None, "days_ago": days, "prior_verdict": None}
    report = getattr(prior_casework, "report", None) if prior_casework is not None else None
    return {
        "candidate_id": prior.id,
        "decided_at": prior.decided_at,
        "decided_by": prior.decided_by,
        "decided_by_display": (names or {}).get(prior.decided_by or "") or prior.decided_by,
        "days_ago": days,
        # Previous coach target: reviewer considers whether the last fix failed because it targeted the wrong place.
        "prior_verdict": report.get("verdict") if isinstance(report, dict) else None,
    }


async def _casework_view(store: UserStore, team_id: str, cw: Any, *, reviewer: bool) -> dict[str, Any]:
    """`public()` + 改道目标展示名（服务端解析）。"""

    view = cw.public(reviewer=reviewer)
    target = view.get("reroute_target")
    if isinstance(target, dict) and target.get("target_kind") and target.get("target_ref"):
        target["label"] = await _label_for(store, team_id, str(target["target_kind"]), str(target["target_ref"]))
    return view


def _list_row(cand: FeedbackCandidate, *, viewer_is_admin: bool, viewer_id: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": cand.id,
        "target_kind": cand.target_kind,
        "target_ref": cand.target_ref,
        "proposed": cand.proposed,
        "evidence_count": cand.evidence_count,
        "state": cand.state,
        "created_at": cand.created_at,
        "expires_at": cand.expires_at,
        "mine": cand.proposer_id == viewer_id,
    }
    if viewer_is_admin:
        row["proposer_id"] = cand.proposer_id
    return row


def _evidence_view(cand: FeedbackCandidate, *, viewer_is_admin: bool, viewer_id: str) -> dict[str, Any]:
    """他人证据对 proposer 只见计数（§2.4）；provenance 裸 id 永不下发。"""

    entries: list[dict[str, Any]] = []
    others = 0
    for entry in cand.evidence:
        contributor = entry.get("contributor_id")
        if viewer_is_admin or contributor == viewer_id:
            entries.append(
                {
                    "contributor_id": contributor,
                    "rubric_text": entry.get("rubric_text") or "",
                    "disclosure_ts": entry.get("disclosure_ts"),
                    "source_count": len(entry.get("provenance_refs") or []),
                }
            )
        else:
            others += 1
    return {"entries": entries, "others_count": others}


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    note: str = Field(default="", max_length=2000)


async def _emit_receipts(request: Request, cand: FeedbackCandidate) -> None:
    """终态回执，best-effort：回执失败不回滚决定（通知层惯例）。"""

    service = getattr(request.app.state, "notification_service", None)
    if service is None:
        return
    recipients = sorted(_involved(cand))
    try:
        await service.emit_feedback_candidate_receipt(
            team_id=cand.team_id,
            candidate_id=cand.id,
            target_kind=cand.target_kind,
            final_state=cand.state,
            decided_at=cand.decided_at or cand.updated_at,
            recipient_ids=recipients,
        )
        await _fbc(request).mark_receipt(cand.id)
    except Exception:
        logger.exception("feedback candidate receipt emit failed id=%s", cand.id)


def _decide_or_http(exc: Exception) -> HTTPException:
    if isinstance(exc, SelfApprovalError):
        return HTTPException(status_code=403, detail={"code": "self_approval_forbidden"})
    if isinstance(exc, InvalidStateError):
        return HTTPException(status_code=409, detail={"code": "feedback_candidate_not_open"})
    if isinstance(exc, FeedbackLoopDisabled):
        return HTTPException(status_code=404, detail={"code": "not_found"})
    raise exc


@router.get("")
async def list_candidates(
    request: Request,
    view: Literal["open", "decided", "all"] = "open",
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Suggestion list: `open` (default) waits for a decision, `decided` holds the 50 most recently closed.

    An accepted Business-definition suggestion in the decided view carries `space_targets`, so administrators
    can come back to add its wording to the remaining source Spaces and see where it already landed.

    非 admin 有两条车道：自己提的；以及指向自己名下 agent 的 agent_def 候选
    （PRD §5：资源 owner 是审批人——没有列表车道他就永远发现不了案子）。
    """

    is_admin = await _is_inbox_admin(store, user)
    fbc = _fbc(request)
    rows = []
    if view in ("open", "all"):
        rows += await fbc.list_open(user.team_id)
    if view in ("decided", "all"):
        rows += await fbc.list_decided(user.team_id)
    owned: set[str] = set()
    if any(_asset_kind(r) == "agent_def" for r in rows):
        # Include admins: agent_def adoption still requires ownership; for_my_agent is the UI's sole signal
        # distinguishing can-adopt from reject-only.
        owned = await AgentReleaseReadStore(store.db_path).owned_agent_ids(team_id=user.team_id, actor_user_id=user.id)
    if not is_admin:
        rows = [r for r in rows if r.proposer_id == user.id or (_asset_kind(r) == "agent_def" and r.target_ref in owned)]
    labels = await _target_labels(store, user.team_id, rows)
    cw_store = _casework(request)
    casework = await cw_store.get_many([r.id for r in rows]) if cw_store is not None else {}
    # FR7: candidates recurring after prior approval. Same signature = same target, purely structural.
    # A closed suggestion is not a new recurrence of an earlier approval.
    recurrences = await fbc.find_recurrences(user.team_id, [r for r in rows if r.state == "open"])
    prior_casework = await cw_store.get_many([p.id for p in recurrences.values()]) if (cw_store is not None and recurrences) else {}
    # Resolve reviewer display names alongside proposers (recurrence must identify who approved, not just ID).
    names = await store.get_users_by_ids([r.proposer_id for r in rows] + [p.decided_by for p in recurrences.values() if p.decided_by])
    items = []
    for r in rows:
        for_my_agent = _asset_kind(r) == "agent_def" and r.target_ref in owned
        row = _list_row(r, viewer_is_admin=is_admin or for_my_agent, viewer_id=user.id)
        row["for_my_agent"] = for_my_agent
        row["target_label"] = labels.get(r.id)
        cw = casework.get(r.id)
        row["casework"] = await _casework_view(store, user.team_id, cw, reviewer=is_admin or for_my_agent) if cw is not None else None
        prior = recurrences.get(r.id)
        row["recurrence"] = (
            _recurrence_view(prior, prior_casework.get(prior.id), reviewer=is_admin or for_my_agent, names=names) if prior is not None else None
        )
        if "proposer_id" in row:
            row["proposer_display"] = names.get(r.proposer_id, r.proposer_id)
        if r.state != "open":
            row["decided_at"] = r.decided_at or r.updated_at
            if is_admin and r.state == "accepted" and r.target_kind == "semantic":
                row["space_targets"] = await _adopted_space_targets(request, store, r)
        items.append(row)
    out: dict[str, Any] = {"items": items, "viewer_is_admin": is_admin}
    if is_admin:
        # §2.8.3 ③: owner-visible usage (casework rows, not billing) + team-level background-coach switch.
        tss = getattr(request.app.state, "team_settings_store", None)
        out["coach"] = {
            "month_drafts": await cw_store.count_started_this_month(user.team_id) if cw_store is not None else 0,
            "casework_enabled": await team_casework_enabled(tss, user.team_id),
        }
    return out


@router.get("/pending-count")
async def pending_count(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, int]:
    """Number of open suggestions waiting for the caller's decision (sidebar row-end pill).

    Same team scoping and reviewer lanes as the list: team owners/admins decide any open candidate
    (agent_def ones by rejecting), a resource owner decides agent_def candidates targeting their own
    Agent. Candidates the caller proposed or co-signed are excluded because the store forbids
    self-approval; nothing else is disclosed (a count only, no ids).
    """

    is_admin = await _is_inbox_admin(store, user)
    rows = await _fbc(request).list_open(user.team_id)
    owned: set[str] = set()
    if not is_admin and any(_asset_kind(r) == "agent_def" for r in rows):
        owned = await AgentReleaseReadStore(store.db_path).owned_agent_ids(team_id=user.team_id, actor_user_id=user.id)
    count = 0
    for r in rows:
        if user.id in _involved(r) or _is_due(r):
            continue
        if is_admin or (_asset_kind(r) == "agent_def" and r.target_ref in owned):
            count += 1
    return {"count": count}


# ★ Declare **before** /{candidate_id}: FastAPI matches declaration order, otherwise this becomes
# a candidate ID named "receipts" (404 without an obvious cause).
@router.get("/receipts")
async def pending_receipts(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """本人未读的终态回执——给 **chat 内联送达面**取数（§2.4 送达，批 5）。

    为什么不让前端直接读通知中心的列表：116 实测通知中心 139 条通知 **0 次点击**
    （action 类 55 条同样 0 次），而同期记忆候选的 8 次人工决策**全部**发生在 chat
    内联卡上。回执只走通知＝提议人基本不会知道结果，闭环在最后一公里断掉。

    内容比通知**更全**是有意的：通知正文按 §2.4 最小化（它随会话分享/导出外泄），
    这里是认证 API，可以带目标资产展示名。权限判据＝调用者必须在该候选的
    `_involved` 集合里（proposer ∪ contributors，正是回执的收件人集合）——
    不复用 `_visible_candidate`：联署人不是 proposer 也不是资源 owner，
    那把尺子会把**收到过回执的人**挡在外面。

    字段只发卡片真正渲染的那几个。`target_kind`/`target_ref` 曾一并下发，review 指出
    没有任何消费者——而本端点的卖点正是「通知最小化、这里才给展示名」，顺手多发一个
    裸 `target_ref` 与那句话自相矛盾。`candidate_id` 留着：它是这条回执的身份，
    出问题时是唯一能对上库里哪一行的键。
    """

    # ★ Shares the receipt-read path's lifecycle: POST /notifications/{id}/read returns **404** when notification center is off
    # (routes/notifications.py::_store). Delivering anyway gives users an acknowledgment card that always returns
    # 404; FE retains it as retryable failure, permanently pinning it to **every chat turn**.
    if getattr(request.app.state, "notification_center_enabled", True) is False:
        return {"items": []}
    nstore = getattr(request.app.state, "notification_store", None)
    if nstore is None:
        return {"items": []}
    rows = await nstore.list_unread_by_type(user_id=user.id, type="feedback_candidate_receipt")
    fbc = _fbc(request)
    items: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = str((row.metadata or {}).get("candidateId") or row.subject_id or "")
        if not candidate_id:
            continue
        cand = await fbc.get(candidate_id)
        if cand is None or cand.team_id != user.team_id or user.id not in _involved(cand):
            # Candidate removed / cross-team / not a recipient: expose no content, and do not leave it dangling forever.
            continue
        kind = _asset_kind(cand)
        items.append(
            {
                "notification_id": row.id,
                "candidate_id": candidate_id,
                # ★ _label_for resolves only agent_def/team_skill/tool_source;
                # semantic/new_asset/retire always return None, with readable target_ref names themselves
                # (e.g. net-profit definition). Fallback is the **normal path for half the kinds**, not an edge case.
                "target_label": await _label_for(store, user.team_id, kind, cand.target_ref) or cand.target_ref,
                "final_state": cand.state,
                "decided_at": cand.decided_at or cand.updated_at,
                "mine": cand.proposer_id == user.id,
            }
        )
    return {"items": items}


@router.get("/{candidate_id}")
async def candidate_detail(
    candidate_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    cand, role = await _visible_candidate(request, user, store, candidate_id)
    # ★ _visible_candidate checks proposer before resource_owner, reducing users who proposed for their own Agent
    # to proposer in detail, while list uses is_admin or for_my_agent; same person sees different surfaces
    # (review P1-3). Align with list: resource owner is reviewer (PRD §5).
    owns_target = False
    if _asset_kind(cand) == "agent_def":
        owned_ids = await AgentReleaseReadStore(store.db_path).owned_agent_ids(team_id=user.team_id, actor_user_id=user.id)
        owns_target = cand.target_ref in owned_ids
    reviewer = role in ("admin", "resource_owner") or owns_target
    flags = await _viewable_source_flags(request, user, cand)
    body = _list_row(cand, viewer_is_admin=reviewer, viewer_id=user.id)
    labels = await _target_labels(store, user.team_id, [cand])
    contributor_ids = [c for e in cand.evidence if isinstance(c := e.get("contributor_id"), str)]
    cw_store = _casework(request)
    cw = await cw_store.get(cand.id) if cw_store is not None else None
    recurrences = await _fbc(request).find_recurrences(user.team_id, [cand])
    prior = recurrences.get(cand.id)
    prior_cw = await cw_store.get(prior.id) if (prior is not None and cw_store is not None) else None
    extra_names = [prior.decided_by] if (prior is not None and prior.decided_by) else []
    names = await store.get_users_by_ids([cand.proposer_id, *contributor_ids, *extra_names]) if reviewer else {}
    body.update(
        {
            "casework": await _casework_view(store, user.team_id, cw, reviewer=reviewer) if cw is not None else None,
            "recurrence": _recurrence_view(prior, prior_cw, reviewer=reviewer, names=names) if prior is not None else None,
            "target_label": labels.get(cand.id),
            "proposer_display": names.get(cand.proposer_id) if reviewer else None,
            "contributor_displays": {cid: names.get(cid, cid) for cid in contributor_ids} if reviewer else {},
            "viewer_role": role,
            "rubric_text": cand.rubric_text,  # Initial criteria for reviewer; proposer is its author.
            "evidence": _evidence_view(cand, viewer_is_admin=reviewer, viewer_id=user.id),
            "sources": {
                "total": len(flags),
                "viewable": sum(flags),
                "viewable_indexes": [i for i, ok in enumerate(flags) if ok],
            },
            "decided_by_me": cand.decided_by == user.id,
            "decide_note": cand.decide_note if reviewer else None,
        }
    )
    return body


@router.get("/{candidate_id}/sources/{index}")
async def open_candidate_source(
    candidate_id: str,
    index: int,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """来源对话打开：与详情计数共用同一份解析器，打开时刻再验一次权限。"""

    cand, _role = await _visible_candidate(request, user, store, candidate_id)
    refs = _iter_source_refs(cand)
    if index < 0 or index >= len(refs):
        raise _NOT_FOUND
    ref = refs[index]
    if not ref.startswith("t_"):
        raise _NOT_FOUND
    await _require_turn_access(ref, request, user)  # No access -> 404 (anti-enumeration).
    return {"turn_id": ref}


@router.post("/{candidate_id}/decision")
async def decide_candidate(
    candidate_id: str,
    body: DecisionBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    cand, role = await _visible_candidate(request, user, store, candidate_id)
    if role != "admin":
        # Proposer cannot decide (withdraw uses a dedicated endpoint); resource_owner accepts via
        # /adopt (ownership + coach); admin rejects, a known v1 narrowing.
        raise HTTPException(status_code=403, detail={"code": "feedback_decision_forbidden"})
    if body.decision == "accepted" and _asset_kind(cand) == "agent_def":
        # PRD §5: agent_def adoption belongs to resource owner and must use /adopt's coach draft.
        # Admin acceptance here closes the case and sends accepted receipts to cosigners, but owner can
        # never reopen it (state≠open): a dishonest receipt.
        raise HTTPException(status_code=409, detail={"code": "feedback_adopt_required"})
    try:
        decided = await _fbc(request).decide(cand.id, decision=body.decision, decided_by=user.id, note=body.note)
    except (SelfApprovalError, InvalidStateError, FeedbackLoopDisabled) as exc:
        raise _decide_or_http(exc) from exc
    await _emit_receipts(request, decided)
    return {"id": decided.id, "state": decided.state}


@router.post("/{candidate_id}/withdraw")
async def withdraw_candidate(
    candidate_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    cand, _role = await _visible_candidate(request, user, store, candidate_id)
    try:
        withdrawn = await _fbc(request).withdraw(cand.id, by=user.id)
    except InvalidStateError as exc:
        raise HTTPException(status_code=409, detail={"code": "feedback_withdraw_forbidden"}) from exc
    return {"id": withdrawn.id, "state": withdrawn.state}


async def _adopted_space_targets(request: Request, store: UserStore, cand: FeedbackCandidate) -> dict[str, Any] | None:
    """The adopted wording and its source Spaces, or None when the wording carries an address (never prefilled)."""
    text = sanitize_for_injection(_proposed_text(cand))
    if new_urls_in_draft("", text):
        return None
    return {"text": text, "spaces": await _space_instruction_targets(request, store, cand)}


def _proposed_text(cand: FeedbackCandidate) -> str:
    raw = cand.proposed.get("text")
    return raw if isinstance(raw, str) else ""


# Reroute only to kinds with actual team objects; new_asset/retire have different payloads and are not targets.
_REROUTE_KINDS = frozenset({"agent_def", "team_skill", "tool_source", "semantic"})


class RerouteBody(BaseModel):
    target_kind: str = Field(pattern="^(agent_def|team_skill|tool_source|semantic)$")
    target_ref: str = Field(min_length=1, max_length=200)


async def _reroute_target_exists(store: UserStore, user: User, kind: str, ref: str, *, is_admin: bool) -> bool:
    """目标存在且**能被采纳**：技能只认 team_wide（creator_only 对他人「不存在」，与 build_skill_surface 同门，
    否则转案端点就是私有技能 id 的枚举 oracle；对创建者本人也不认——team_skill 的采纳是 admin 门，转过去谁都采不了）；
    非 admin 只认已审批技能（pending/rejected 技能 id 不给非 admin 当存在性 oracle）。"""

    team_id = user.team_id
    if kind == "agent_def":
        return await store.get_team_subagent(team_id, ref) is not None
    if kind == "team_skill":
        sk = await store.get_team_skill(team_id, ref)
        if sk is None or (sk.visibility or "team_wide") != "team_wide":
            return False
        return is_admin or (getattr(sk, "approval", None) or "approved") == "approved"
    if kind == "tool_source":
        return await store.get_team_tool_source(team_id, ref) is not None
    return kind == "semantic" and bool(ref.strip())


@router.post("/{candidate_id}/reroute")
async def reroute_candidate(
    candidate_id: str,
    body: RerouteBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """转案（tech-design §2.8.4）：教练改道卡的「转案」按钮，或审批人手工改落点。

    admin 或该 agent 的资源 owner 可调；调用者 ∉ involved（store 判，联署人不能自己改道）。
    新候选按新目标重新裁定审批人——改道人不因此获得决定权。
    """

    cand, role = await _visible_candidate(request, user, store, candidate_id)
    if role not in ("admin", "resource_owner"):
        raise HTTPException(status_code=403, detail={"code": "feedback_reroute_forbidden"})
    if body.target_kind not in _REROUTE_KINDS or not await _reroute_target_exists(
        store, user, body.target_kind, body.target_ref, is_admin=role == "admin"
    ):
        raise HTTPException(status_code=404, detail={"code": "feedback_reroute_target_not_found"})
    try:
        old, new, outcome = await _fbc(request).retarget(
            cand.id,
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            sig_text=_proposed_text(cand) or cand.rubric_text,
            by=user.id,
        )
    except SelfApprovalError as exc:
        raise HTTPException(status_code=403, detail={"code": "self_approval_forbidden"}) from exc
    except SuppressedSignature as exc:
        raise HTTPException(status_code=409, detail={"code": "signature_suppressed"}) from exc
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail={"code": "quota_exceeded", "which": exc.which}) from exc
    except InvalidStateError as exc:
        raise HTTPException(status_code=409, detail={"code": "feedback_reroute_not_available"}) from exc
    except ConcurrentWriteConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "concurrent_write_conflict"}) from exc
    # Reassess new row visibility/lane for its new target. Caller may lack a lane there (admin rerouting to another's Agent stays visible;
    # resource_owner rerouting elsewhere loses visibility). Project the returned row for this caller.
    is_admin = role == "admin"
    owned: set[str] = set()
    if _asset_kind(new) == "agent_def":
        owned = await AgentReleaseReadStore(store.db_path).owned_agent_ids(team_id=user.team_id, actor_user_id=user.id)
    for_my_agent = _asset_kind(new) == "agent_def" and new.target_ref in owned
    visible = is_admin or for_my_agent or new.proposer_id == user.id
    labels = await _target_labels(store, user.team_id, [new])
    row = _list_row(new, viewer_is_admin=is_admin or for_my_agent, viewer_id=user.id) if visible else None
    if row is not None:
        row["for_my_agent"] = for_my_agent
        row["target_label"] = labels.get(new.id)
        cw_store = _casework(request)
        cw = await cw_store.get(new.id) if cw_store is not None else None  # Merged existing candidate may already have casework.
        row["casework"] = await _casework_view(store, user.team_id, cw, reviewer=is_admin or for_my_agent) if cw is not None else None
    # Caller without a lane on the new target cannot even learn which candidate it merged into (same-signature existence oracle).
    return {
        "superseded_id": old.id,
        "outcome": outcome if visible else None,
        "candidate": row,
        "candidate_id": new.id if visible else None,
    }


class CaseworkSettingsBody(BaseModel):
    enabled: bool


@router.get("/settings/casework")
async def get_casework_settings(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    if not await _is_inbox_admin(store, user):
        raise HTTPException(status_code=403, detail={"code": "feedback_settings_forbidden"})
    tss = getattr(request.app.state, "team_settings_store", None)
    return {"enabled": await team_casework_enabled(tss, user.team_id)}


@router.put("/settings/casework")
async def set_casework_settings(
    body: CaseworkSettingsBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """团队级后台教练开关（§2.8.3 ③）：后台烧的是客户自己的 provider key，必须给关的地方。只影响后台办案。"""

    if not await _is_inbox_admin(store, user):
        raise HTTPException(status_code=403, detail={"code": "feedback_settings_forbidden"})
    tss = getattr(request.app.state, "team_settings_store", None)
    if tss is None:
        raise HTTPException(status_code=503, detail={"code": "team_settings_unavailable"})
    value = "true" if body.enabled else "false"
    before = await tss.get(user.team_id, KEY_TEAM_FEEDBACK_CASEWORK_ENABLED)
    await tss.set(user.team_id, KEY_TEAM_FEEDBACK_CASEWORK_ENABLED, value, updated_by=user.id)
    try:
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=AuditAction.TEAM_SETTINGS_CHANGED,
            target_kind="team_settings",
            target_id=KEY_TEAM_FEEDBACK_CASEWORK_ENABLED,
            target_team_id=user.team_id,
            scope="team",
            payload={"setting_key": KEY_TEAM_FEEDBACK_CASEWORK_ENABLED, "operation": "put", "before": before, "after": value},
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:  # Audit failure never rolls back settings (like team_settings: audit is auxiliary).
        logger.exception("feedback casework settings audit failed team=%s", user.team_id)
    return {"enabled": parse_casework_enabled(value)}


async def _decide_accept(request: Request, cand: FeedbackCandidate, user: User, decide: bool) -> FeedbackCandidate:
    """采纳＝落 accepted + 回执；redraft（decide=False）＝候选已 accepted，只重新物化预填。"""

    if not decide:
        return cand
    try:
        decided = await _fbc(request).decide(cand.id, decision="accepted", decided_by=user.id, note="adopted")
    except (SelfApprovalError, InvalidStateError, FeedbackLoopDisabled) as exc:
        raise _decide_or_http(exc) from exc
    await _emit_receipts(request, decided)
    return decided


def _degraded(decided: FeedbackCandidate, banner: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": decided.id,
        "state": decided.state,
        "prefill": None,
        "degraded": True,
        "degrade_reason": reason,
        "banner": banner,
    }


async def _reroute_target_view(store: UserStore, team_id: str, kind: str | None, ref: str | None) -> dict[str, Any]:
    label = await _label_for(store, team_id, kind, ref) if kind and ref else None
    return {"target_kind": kind, "target_ref": ref, "label": label}


async def _coach_then_decide(
    request: Request,
    cand: FeedbackCandidate,
    user: User,
    store: UserStore,
    *,
    surface: Surface,
    banner: dict[str, Any],
    decide: bool,
) -> dict[str, Any]:
    """教练第一触发面（§2.3b）：先起草后落决定；reroute 不落决定；起草失败降级。

    批 3a：后台办案已把稿子存在 sidecar 行时，**重算基线指纹一致**才复用（0 等待）；
    否则照旧现跑教练。缓存 reroute 与现跑同语义（不落决定、返改道卡）。
    """

    cw_store = _casework(request)
    cached = await cw_store.get(cand.id) if cw_store is not None else None
    draft = None
    reroute_payload: dict[str, Any] | None = None
    if cached is not None and cached.status == "done" and base_matches(surface, cached.base):
        verdict = cached.report.get("verdict")
        if verdict == "reroute":
            rr = cached.report.get("reroute") or {}
            reroute_payload = {
                "reason": rr.get("reason"),
                "rationale": cached.report.get("rationale"),
                "target": await _reroute_target_view(store, user.team_id, rr.get("target_kind"), rr.get("target_ref")),
            }
        elif verdict == "edit" and cached.prefill and cached.prefill.get("edited_content"):
            draft = cached.prefill
    if draft is None and reroute_payload is None:
        llm_router = await team_router_or_409(request.app.state, user.team_id, site="feedback_adopt")
        with usage_context(team_id=user.team_id, user_id=user.id, reason="feedback_adopt"):
            live = await draft_config_edit(
                llm_router,
                target_kind=cand.target_kind,
                target_ref=cand.target_ref,
                config_surface=surface.config_surface,
                proposed=cand.proposed,
                rubric_text=cand.rubric_text,
                mounted_assets=surface.mounted_assets,
            )
        if live is not None and live.verdict == "reroute":
            reroute_payload = {
                "reason": live.reroute_reason,
                "rationale": live.rationale,
                "target": await _reroute_target_view(store, user.team_id, live.reroute_target_kind, live.reroute_target_ref),
            }
        elif live is not None:
            draft = {"kind": surface.prefill_kind, "edited_content": live.edited_content, "rationale": live.rationale}
            if cw_store is not None:
                await cw_store.replace_cache(
                    cand.id,
                    cand.team_id,
                    report={"verdict": "edit", "rationale": live.rationale, "trigger": "explicit"},
                    prefill=draft,
                    base=surface.base,
                )
    if reroute_payload is not None:
        # Coach rejected collector's guessed target: no decision; owner decides after reviewing reroute card.
        return {"id": cand.id, "state": cand.state, "reroute": reroute_payload, "banner": banner}
    decided = await _decide_accept(request, cand, user, decide)
    if draft is None:
        return _degraded(decided, banner, "coach_unavailable")
    return {
        "id": decided.id,
        "state": decided.state,
        "prefill": {"kind": surface.prefill_kind, "edited_content": draft.get("edited_content"), "rationale": draft.get("rationale")},
        "base": surface.base,
        "banner": banner,
    }


async def _adopt_coach_kind(request, cand, user, store, role, banner, *, decide: bool) -> dict[str, Any]:  # noqa: ANN001
    """agent_def / team_skill：配置面走 feedback_surfaces 唯一实现（路由与后台办案共用）。"""

    if cand.target_kind == "team_skill" and role != "admin":
        # PRD §5: team Skill reviewers = {owner, team_admin}; only admins can edit team_wide approved/pending Skill bodies.
        raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"})
    try:
        surface = await build_surface(store, cand, actor_user_id=user.id)
    except SurfaceError as exc:
        if exc.http_status == 404:
            raise _NOT_FOUND from exc
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from exc
    if not surface.fits:
        # Coach lacking the full body cannot produce a complete rewrite, or prefill would diff as large deletions.
        decided = await _decide_accept(request, cand, user, decide)
        return _degraded(decided, banner, "surface_too_large")
    if cand.target_kind == "agent_def":
        from bowhead_gateway.routes.agents import _revision_store

        team_router = await team_router_or_409(
            request.app.state,
            user.team_id,
            agent_type="lead",
            site="feedback_adopt_agent_proposal",
        )
        with usage_context(team_id=user.team_id, user_id=user.id, reason="feedback_adopt_agent_proposal"):
            drafted = await draft_agent_change_proposal(
                team_router,
                agent_id=cand.target_ref,
                config_surface=surface.config_surface,
                goal=_proposed_text(cand),
                evidence_excerpts=[cand.rubric_text],
                trace_id=cand.id,
            )
        if drafted.draft is None:
            decided = await _decide_accept(request, cand, user, decide)
            return _degraded(decided, banner, drafted.failure or "coach_unavailable")
        proposal_store = AgentChangeProposalStore(
            store.db_path,
            revision_store=await _revision_store(request, user=user, store=store),
        )
        proposal = await proposal_store.create(
            team_id=user.team_id,
            agent_id=cand.target_ref,
            effective_owner_user_id=user.id,
            source_type="feedback",
            source_ref=f"feedback:{cand.id}",
            operations=drafted.draft.operations,
            atomic_groups=drafted.draft.atomic_groups,
            suggested_cases=drafted.draft.suggested_cases,
            rationale=drafted.draft.rationale,
            provenance={"feedback_candidate_id": cand.id},
        )
        decided = await _decide_accept(request, cand, user, decide)
        return {
            "id": decided.id,
            "state": decided.state,
            "proposal": {
                "agent_id": cand.target_ref,
                "proposal_id": proposal.id,
                "status": proposal.status,
                "url": f"/agents/manage/?agent={cand.target_ref}&proposal={proposal.id}",
            },
            "banner": banner,
        }
    return await _coach_then_decide(request, cand, user, store, surface=surface, banner=banner, decide=decide)


def _tool_source_summary(src: Any) -> dict[str, Any]:
    """工具源的无密钥摘要：不带 auth_ref/openapi_spec_json/spec_url/base_url
    （base_url 可能含 userinfo；team_tool_sources._public 同口径，再收紧一档）。"""

    return {
        "id": src.id,
        "name": src.name,
        "kind": src.kind,
        "status": src.status,
        "auth_type": src.auth_type,
        "selected_op_ids": list(src.selected_op_ids or []),
        "updated_at": src.updated_at,  # Baseline fingerprint (§2.3 ⑤); FE compares before save, realigns on drift.
    }


async def _adopt_tool_source(request, cand, user, store, role, banner, decide: bool) -> dict[str, Any]:  # noqa: ANN001
    # Tool sources are structured settings (enabled operations/on-off), not free coach rewrites; prefill = original suggestion + secretless summary.
    # Admin edits manually on native editor and PATCHes (including Impact Guard confirmation).
    if role != "admin":
        raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"})
    src = await store.get_team_tool_source(user.team_id, cand.target_ref)
    if src is None:
        raise _NOT_FOUND
    text = sanitize_for_injection(_proposed_text(cand))
    blocked = bool(new_urls_in_draft("", text))
    decided = await _decide_accept(request, cand, user, decide)
    if blocked:
        return _degraded(decided, banner, "url_in_proposed")
    return {
        "id": decided.id,
        "state": decided.state,
        "prefill": {"kind": "tool_source_patch", "proposal_text": text, "current": _tool_source_summary(src)},
        "banner": banner,
    }


_CCA_PURPOSE_MAX = 2000  # team_subagent_drafts.CreateDraftBody.purpose cap.


async def _adopt_new_asset(request, cand, user, role, banner, decide: bool) -> dict[str, Any]:  # noqa: ANN001
    # new_asset: intent text enters creation wizard (§2.3 ④). Wizard wraps <untrusted_user_purpose> and gates
    # purpose evidence; this layer ensures sanitization, no outbound addresses, and wizard field limits.
    if role != "admin":
        raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"})
    text = sanitize_for_injection(_proposed_text(cand))
    blocked = bool(new_urls_in_draft("", text))
    decided = await _decide_accept(request, cand, user, decide)
    if blocked:
        return _degraded(decided, banner, "url_in_proposed")
    return {
        "id": decided.id,
        "state": decided.state,
        "prefill": {"kind": "cca_intent", "purpose": text[:_CCA_PURPOSE_MAX], "locale": "zh-CN"},
        "banner": banner,
    }


_RETIRE_NATIVE = {
    "agent_def": "agent_archive",
    "team_skill": "team_skill_disable",
    "tool_source": "tool_source_disable",
}


async def _adopt_retire(request, cand, user, store, role, banner, decide: bool) -> dict[str, Any]:  # noqa: ANN001
    # No retirement execution here: prefill identifies what/why; actual archive/disable stays on each asset's
    # native endpoint (Agent: resource owner; Skill/tool: admin, with Impact Guard confirmation).
    asset_kind = _asset_kind(cand)
    native = _RETIRE_NATIVE.get(asset_kind)
    if native is None:
        raise HTTPException(status_code=409, detail={"code": "feedback_adopt_kind_unsupported"})
    if asset_kind == "agent_def":
        if role not in ("resource_owner", "admin"):
            raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"})
        if role == "admin":
            # Even admins must own the Agent to archive (archive route owner gate).
            try:
                await AgentReleaseReadStore(store.db_path).working_copy(team_id=user.team_id, agent_id=cand.target_ref, actor_user_id=user.id)
            except AgentReleaseCutoverRequiredError:
                pass
            except AgentRevisionError as exc:
                raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"}) from exc
    elif role != "admin":
        raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"})
    reason = sanitize_for_injection(_proposed_text(cand))
    blocked = bool(new_urls_in_draft("", reason))
    decided = await _decide_accept(request, cand, user, decide)
    if blocked:
        return _degraded(decided, banner, "url_in_proposed")
    return {
        "id": decided.id,
        "state": decided.state,
        "prefill": {"kind": "retire", "asset_kind": asset_kind, "asset_ref": cand.target_ref, "native_action": native, "reason": reason},
        "banner": banner,
    }


@router.post("/{candidate_id}/adopt")
async def adopt_candidate(
    candidate_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """采纳并预填（六分流，tech-design §2.3/§2.3b）。

    agent_def / team_skill 走教练起草（先起草后落决定，reroute 不落决定，失败降级）；
    semantic / tool_source / new_asset / retire 不走教练，预填对方原生入口的载荷。
    全部 target_kind：预填前再过一次消毒 + URL 句法检测（§2.3 ③）。
    """

    cand, role = await _visible_candidate(request, user, store, candidate_id)
    if cand.state != "open" or _is_due(cand):
        raise HTTPException(status_code=409, detail={"code": "feedback_candidate_not_open"})
    if user.id in _involved(cand):
        # store.decide also blocks self-approval, but coach invokes paid LLM before decision; self-reviewers must get no draft,
        # and reroute can deliver output without any decision.
        raise HTTPException(status_code=403, detail={"code": "self_approval_forbidden"})

    return await _materialize(request, cand, user, store, role, decide=True)


@router.post("/{candidate_id}/prefill")
async def redraft_candidate(
    candidate_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """已采纳候选的预填重新物化（tech-design §2.3 ⑤「基于新基线重新对齐」）。

    候选状态不变（已 accepted）、不发回执；只有当初的采纳人可调（decided_by），
    起草对着**当前**基线重跑。教练 reroute 在这里只当作降级（决定早已落）。
    """

    cand, role = await _visible_candidate(request, user, store, candidate_id)
    if cand.state != "accepted" or cand.decided_by != user.id:
        raise HTTPException(status_code=409, detail={"code": "feedback_redraft_not_available"})
    cw_store = _casework(request)
    throttled_kind = cand.target_kind in COACH_KINDS and not (cand.target_kind == "team_skill" and role != "admin")
    if cw_store is not None and throttled_kind:
        # Only kinds that actually call coach consume redraft quota; _materialize permission denials do not (review P2-6).
        if not await cw_store.take_redraft_slot(cand.id, cand.team_id):
            # At most 3 redrafts per candidate per day (unthrottled paid LLM is registered debt in 2b-ii).
            raise HTTPException(status_code=429, detail={"code": "feedback_redraft_throttled"})
        # Explicit redraft runs freshly against current baseline, never reuses old cache; invalidate done rows only, retain attempts.
        await cw_store.invalidate(cand.id)
    result = await _materialize(request, cand, user, store, role, decide=False)
    if "reroute" in result:
        banner = result["banner"]
        return _degraded(cand, banner, "coach_rerouted")
    return result


async def _materialize(request, cand, user, store, role, *, decide: bool) -> dict[str, Any]:  # noqa: ANN001
    names = await store.get_users_by_ids([cand.proposer_id])
    banner = {
        "candidate_id": cand.id,
        "proposer_id": cand.proposer_id,
        "proposer_display": names.get(cand.proposer_id),
        "evidence_count": cand.evidence_count,
    }
    kind = cand.target_kind

    if kind == "semantic":
        if role != "admin":
            raise HTTPException(status_code=403, detail={"code": "feedback_adopt_forbidden"})
        # Space instructions enter every prompt in the Space; never prefill suggestion text's external addresses.
        # Pure function: compute before deciding; discover prefill rejection before sending the receipt.
        text = sanitize_for_injection(_proposed_text(cand))
        blocked = bool(new_urls_in_draft("", text))
        decided = await _decide_accept(request, cand, user, decide)
        if blocked:
            return _degraded(decided, banner, "url_in_proposed")
        return {
            "id": decided.id,
            "state": decided.state,
            "prefill": {"kind": "space_instructions", "text": text, "spaces": await _space_instruction_targets(request, store, cand)},
            "banner": banner,
        }
    if kind in ("agent_def", "team_skill"):
        return await _adopt_coach_kind(request, cand, user, store, role, banner, decide=decide)
    if kind == "tool_source":
        return await _adopt_tool_source(request, cand, user, store, role, banner, decide)
    if kind == "new_asset":
        return await _adopt_new_asset(request, cand, user, role, banner, decide)
    if kind == "retire":
        return await _adopt_retire(request, cand, user, store, role, banner, decide)
    raise HTTPException(status_code=409, detail={"code": "feedback_adopt_kind_unsupported"})
