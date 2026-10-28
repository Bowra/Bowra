"""workflow-solidify S4 — team shared recipe CRUD（``/api/v1/team-skills``，clone team_subagents）.

team-scoped 共享 recipe（固化的可复用 SKILL.md）。member-open，team_id-keyed（每 read/write 都
AND team_id → IDOR-safe）。create 把 body_md+scripts 经 **skill_upload 校验栈** 写到盘
``.data/team_skills/<team_id>/<name>/``（reserved-floor 防撞 builtin + 格式 gate 在
``_validate_skill_dir``）+ ``create_team_skill`` 行。写路由挂 ``require_feature_licensed`` +
``require_not_readonly``。distill 草稿→保存 team recipe 走 create 路由（personal save 复用现有
``/skills/generate``）。flag-gated mount（main.py）。

skill-team-governance P1b（PRD §3.1 / tech-design §1）：广播面（visibility=team_wide）过 admin
审批。三扇门——①create 非 admin team_wide→pending（本文件）；②PATCH visibility 转移语义
（本文件）；③put_audience 的闸下沉在 ``share_service.set_audience``（admin grants 控制台同吃）。
状态双槽正交：``approval``=已批槽（L1 首提生命周期），``candidate_*``=候选槽（L2 更新生命
周期，approval 保持 approved → 审批期间团队继续用旧已批版）。approve 的盘/DB 一致性靠
per-skill asyncio.Lock（``_team_skill_locks``，单进程假设）+ 内存/DB 双层 CAS。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import aiosqlite
from bowhead_core.audit_actions import AuditAction
from bowhead_core.resource_file_quarantine import (
    finalize_cleanup,
    quarantine_paths,
    restore_quarantined,
    retry_pending_cleanup,
    write_cleanup_receipts,
)
from bowhead_core.resource_impact import (
    ResourceChangeIdempotencyConflict,
    ResourceImpactKeyUnavailable,
    ResourceImpactTokenInvalid,
    ResourceImpactTokenStale,
    issue_impact_token,
    read_change_receipt,
    resource_change_request_digest,
    verify_impact_token,
    write_change_receipt,
)
from bowhead_core.resource_usage_flags import resource_impact_guard_enabled
from bowhead_core.resource_usage_projection import (
    ResourceUsageProjectionService,
    ResourceUsageSelector,
    ResourceUsageUnavailableError,
)
from bowhead_core.share_service import can_manage_shares
from bowhead_core.skill_import.hook import apply_import_inventory
from bowhead_core.skill_import_provenance import public_import_provenance
from bowhead_core.skill_upload import (
    MAX_DESC_LEN,
    MAX_SKILL_SIZE,
    SkillUploadError,
    _validate_skill_dir,
    bundle_sha256,
    copy_validated_bundle,
    peek_skill_name,
    validate_and_save_generated,
)
from bowhead_core.user_store import TeamSkill, User, UserStore, _id, _now, _row_to_team_skill
from bowhead_skill.loader import load_skill
from bowhead_skill.script_langs import script_execution_mode
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bowhead_core import share_service
from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway._team_skill_locks import team_skill_lock
from bowhead_gateway.resource_impact_http import (
    affected_skill_schedule_ids,
    delete_has_live_dependencies,
    impact_http_error,
    pause_affected_schedules,
    require_impact_headers,
    usage_dependency_impact,
)
from bowhead_gateway.routes.auth import _current_user, _store
from bowhead_gateway.skill_bundle_files import (
    BundleFileChange,
    change_bundle_file,
    display_body,
    evals_summary,
    list_bundle_files,
    read_bundle_file_snapshot,
    read_bundle_snapshot,
    require_bundle_authoring,
    validate_file_ref,
    write_bundle_body,
    write_bundle_file,
)
from bowhead_gateway.skill_import_sources import build_resolution_sources
from bowhead_gateway.skill_workflow_errors import skill_workflow_http_error

logger = logging.getLogger(__name__)

router = APIRouter()

_VISIBILITY = {"team_wide", "creator_only"}
_STATUS = {"enabled", "disabled"}
_TEAM_SKILLS_ROOT = Path(".data") / "team_skills"
_USER_SKILLS_ROOT = Path(".data") / "user_skills"  # copy-to-personal destination (same as routes/skills).
# Reviewer gate (PRD §3.1): explicitly exclude resource-owner short-circuiting; never use can_manage_shares for approval
# (its first branch short-circuits for owners, permitting author self-approval — CTO review P0-3).
_ADMIN_ROLES = {"owner", "team_admin"}
_WRITE_DEPS = [Depends(require_not_readonly), Depends(require_feature_licensed("mcp"))]


def _public(sk: TeamSkill, *, include_review: bool = False) -> dict[str, Any]:
    """Curated view（不暴露 source_path/candidate_path 等内部盘路径）.

    ``approval`` 恒带——成员可见的行恒为 approved（pending/rejected 已被 ``_visible_to``
    滤掉），不泄露。审批/候选明细仅 ``include_review=True``（作者+admin 视角）时携带：
    成员永远看不到 pending/rejected/候选的任何痕迹（PRD §2.3）。"""
    d: dict[str, Any] = {
        "id": sk.id,
        "team_id": sk.team_id,
        "name": sk.name,
        "display_name": sk.display_name,
        "description": sk.description,
        "visibility": sk.visibility,
        "status": sk.status,
        "verdict": sk.verdict,
        "size_bytes": sk.size_bytes,
        "created_by": sk.created_by,
        "created_at": sk.created_at,
        "updated_at": sk.updated_at,
        "approval": sk.approval or "approved",
    }
    if include_review:
        # Track B batch 4 (⑨): provenance of promotion after authors enable despite blockers. Review view only:
        # evidence for reviewers, not a team-wide ingredient list. None = different entry path (all existing rows are None).
        d["import_provenance"] = public_import_provenance(sk.import_provenance_json)
        d["submitted_by"] = sk.submitted_by
        d["approved_by"] = sk.approved_by
        d["approved_at"] = sk.approved_at
        d["approved_sha256"] = sk.approved_sha256
        d["rejected_reason"] = sk.rejected_reason
        d["rejected_at"] = sk.rejected_at
        d["candidate_state"] = sk.candidate_state
        d["candidate_submitted_at"] = sk.candidate_submitted_at
        d["candidate_sha256"] = sk.candidate_sha256
        d["candidate_rejected_reason"] = sk.candidate_rejected_reason
        if (sk.approval or "approved") == "pending" and not sk.candidate_path:
            # L1 approve/reject CAS key = source disk hash (computed on reads, no added column — tech-design §1).
            d["source_sha256"] = _source_sha256(sk)
        # Approval-card risk chips must be visible while collapsed (P3 review P2: admins may approve without expanding;
        # chips inform approval). risk_report_json is a row column; including it in lists requires no disk reads.
        d["risk_report"] = None
        if sk.risk_report_json:
            with contextlib.suppress(json.JSONDecodeError):
                d["risk_report"] = json.loads(sk.risk_report_json)
        # An L2 update is reviewed against the candidate, whose risks can differ from the live row's.
        d["candidate_risk_report"] = _candidate_risk_report(sk) if sk.candidate_path else None
    return d


def _bundle_title(skill_dir: Path) -> str:
    try:
        return str(load_skill(skill_dir).title or "")
    except Exception:  # noqa: BLE001 - a missing or invalid bundle simply has no title to follow
        return ""


def _candidate_risk_report(sk: TeamSkill) -> dict[str, Any] | None:
    """Risk report of the pending L2 candidate, computed from its bundle (pending candidates are few)."""
    cand_dir = _team_dir(sk.team_id, sk.candidate_path)
    if cand_dir is None or not (cand_dir / "SKILL.md").is_file():
        return None
    try:
        return _validate_skill_dir(cand_dir).risk_report or None
    except SkillUploadError:
        return None


def _team_dir(team_id: str, raw: str | None) -> Path | None:
    """resolve + 强校验路径仍在本 team 目录树内（防历史脏数据/符号链把读写点带出去）."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        team_root = (_TEAM_SKILLS_ROOT / team_id).resolve()
        p = Path(raw).resolve()
        if not p.is_relative_to(team_root):
            return None
    except OSError:
        return None
    return p


def _read_body_md(sk: TeamSkill, path: str | None = None) -> str:
    """读 team skill 的 SKILL.md **全文**（含 frontmatter）；越界/缺文件返空串.

    source_path 由 create 路由的 validate_and_save_generated 写入
    （.data/team_skills/<team_id>/<name>/），resolve 后强校验仍在本 team 目录树内。
    ``path`` 缺省读 source_path；审批卡传 candidate_path 读候选内容。

    ⚠ 这份是**编辑面**要的形状（写回去的是整份 SKILL.md）。展示面要过 ``display_body``
    剥掉 frontmatter，否则 drawer 说明 tab 会把 ``name:/description:`` 当正文渲出来。
    """
    skill_dir = _team_dir(sk.team_id, path if path is not None else sk.source_path)
    if skill_dir is None:
        return ""
    try:
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            return ""
        return md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _bundle_files(sk: TeamSkill, path: str | None = None) -> dict[str, Any]:
    """team skill bundle 的附属文件读取面（references/scripts/evals），与个人侧同形.

    promote / copy_validated_bundle 是**整目录**拷贝，团队副本盘上带着 references——
    但团队 detail 此前只回 SKILL.md 正文，用户面看不到这些文件，表现为「发布到团队后
    关联的 skill 没了」。这里只补读取面：字段名与 ``/api/v1/skills/{name}`` 详情一致，
    FE 可复用同一套 Files 视图。越界/缺目录一律降级为空（与 ``_read_body_md`` 同策）。
    """
    skill_dir = _team_dir(sk.team_id, path if path is not None else sk.source_path)
    if skill_dir is None:
        return {"references": [], "scripts": [], "evals_count": 0, "has_evals": False}
    try:
        references = [{"name": n, "size": s} for n, s in list_bundle_files(skill_dir, "references")]
        scripts = [{"name": n, "size": s} for n, s in list_bundle_files(skill_dir, "scripts")]
        evals_count, has_evals = evals_summary(skill_dir)
        spec = load_skill(skill_dir)
    except (OSError, ValueError):
        return {"references": [], "scripts": [], "evals_count": 0, "has_evals": False}
    return {
        "authoring_enabled": True,
        "script_execution": script_execution_mode(has_scripts=bool(spec.scripts), knowledge=spec.kind == "knowledge"),
        "references": references,
        "eval_files": [{"name": n, "size": size} for n, size in list_bundle_files(skill_dir, "evals")],
        "scripts": scripts,
        "evals_count": evals_count,
        "has_evals": has_evals,
    }


def _source_sha256(sk: TeamSkill) -> str | None:
    """source_path 盘 hash 现算（L1 pending 的 CAS 键载体；bundle ≤5MB、pending 行少，可承受）."""
    d = _team_dir(sk.team_id, sk.source_path)
    if d is None or not d.is_dir():
        return None
    try:
        return bundle_sha256(d)
    except OSError:
        return None


class CreateBody(BaseModel):
    body_md: str  # Entire SKILL.md (name comes from frontmatter).
    scripts: list[dict[str, str]] | None = None  # [{name(.py), content}].
    display_name: str = Field("", max_length=256)
    # Unbounded descriptions enter per-turn sanitization hot paths (Lead <user_team_skills> segment + skill_list);
    # match frontmatter's MAX_DESC_LEN write guard (security review P1).
    description: str = Field("", max_length=MAX_DESC_LEN)
    visibility: str = "team_wide"


class PatchBody(BaseModel):
    display_name: str | None = Field(None, max_length=256)
    description: str | None = Field(None, max_length=MAX_DESC_LEN)
    visibility: str | None = None
    status: str | None = None


class TeamSkillImpactPreviewBody(BaseModel):
    action: str
    proposed_change: dict[str, Any] = Field(default_factory=dict)


def _team_skill_state(sk: TeamSkill) -> dict[str, Any]:
    source_digest = _source_sha256(sk)
    candidate_digest: str | None = None
    candidate_dir = _team_dir(sk.team_id, sk.candidate_path)
    if candidate_dir is not None and candidate_dir.is_dir():
        with contextlib.suppress(OSError):
            candidate_digest = bundle_sha256(candidate_dir)
    return {
        "id": sk.id,
        "name": sk.name,
        "display_name": sk.display_name,
        "description": sk.description,
        "visibility": sk.visibility,
        "status": sk.status,
        "approval": sk.approval,
        "approved_sha256": sk.approved_sha256,
        "source_digest": source_digest,
        "candidate_state": sk.candidate_state,
        "candidate_submitted_at": sk.candidate_submitted_at,
        "candidate_sha256": sk.candidate_sha256,
        "candidate_digest": candidate_digest,
        "import_provenance": public_import_provenance(sk.import_provenance_json),
        "updated_at": sk.updated_at,
    }


def _team_skill_selector(sk: TeamSkill) -> ResourceUsageSelector:
    return ResourceUsageSelector(
        key=sk.id,
        kind="skill",
        resource_id=sk.id,
        display_name=sk.display_name or sk.name,
        resource_ref=f"team:{sk.id}",
        revision=sk.approved_sha256,
        available=(sk.status == "enabled" and (sk.approval or "approved") == "approved"),
    )


def _team_skill_command(action: str, proposed_change: dict[str, Any]) -> dict[str, Any]:
    if action == "delete":
        return {"action": "delete", "proposed_change": {}}
    change: dict[str, Any] = {}
    for key in ("display_name", "description", "visibility", "status"):
        if key in proposed_change and proposed_change[key] is not None:
            change[key] = proposed_change[key]
    return {"action": "patch", "proposed_change": change}


class ApprovalCasBody(BaseModel):
    """approve/reject 的 CAS 键（PRD §3.2：admin 批的是「那一份内容」不是「这个名字」）.

    L1（首提，无候选槽）带 ``source_sha256``；L2（更新候选）带候选双键。"""

    source_sha256: str | None = None
    candidate_submitted_at: str | None = None
    candidate_sha256: str | None = None


class RejectBody(ApprovalCasBody):
    reason: str = Field(min_length=1, max_length=2000)  # Required reason (author notification includes a summary).


def _coded_http(request: Request, status_code: int, code: str, **params: Any) -> HTTPException:
    """Preserve the nested team-skill envelope with locale-aware enrichment."""

    del request
    return skill_workflow_http_error(
        status_code,
        code,
        nested_error=True,
        **params,
    )


async def _is_team_admin(store: UserStore, user: User) -> bool:
    """审批人判定：``get_team_role ∈ {owner, team_admin}``（fail-closed：查询失败=非 admin）."""
    if not user.team_id:
        return False
    try:
        role = await store.get_team_role(user.id, user.team_id)
    except Exception:
        return False
    return role in _ADMIN_ROLES


async def _require_team_admin(store: UserStore, user: User) -> None:
    """审批人门。非 admin 作者 approve 自己的 pending 走到这也是 403（自批拒，PRD 验收 3③）."""
    if not await _is_team_admin(store, user):
        raise skill_workflow_http_error(403, "team_skill_admin_required")


def _create_lock_or_null(team_id: str, name: str | None):
    """create 的盘写取 per-skill 锁；name peek 不到（无效 SKILL.md）时用 nullcontext
    ——那条路 validate 必抛、不会成功落盘，无需与 promote 互斥（正确性 review P2）。"""
    if name:
        return team_skill_lock(team_id, name)
    return contextlib.nullcontext()


@router.post("", dependencies=[Depends(require_not_readonly)])
async def create_team_skill_route(
    body: CreateBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
    _feat: None = Depends(require_feature_licensed("mcp")),
) -> dict[str, Any]:
    if not user.team_id:
        raise skill_workflow_http_error(403, "team_required")
    if body.visibility not in _VISIBILITY:
        raise skill_workflow_http_error(422, "team_skill_visibility_invalid")
    dest_root = _TEAM_SKILLS_ROOT.resolve()
    is_admin = await _is_team_admin(store, user)
    # Correctness review P2: disk writes (validate_and_save_generated), DB create + rollback must all occur
    # **inside the per-skill lock**. Otherwise concurrent same-name create/promote lets copy_validated_bundle
    # (overwrite=True) remove this request's newly written directory; this request's rollback then removes the promoted bundle,
    # hiding corruption behind success. Lock keys need name: peek at frontmatter first; if unavailable (invalid SKILL.md),
    # validate without locking (guaranteed coded error, no successful disk write). Lock only paths that actually write files.
    peeked = peek_skill_name(body.body_md)
    async with _create_lock_or_null(user.team_id, peeked):
        try:
            vs = validate_and_save_generated(
                body_md=body.body_md,
                scripts=body.scripts,
                user_id=user.team_id,  # Second path segment = team_id (→ team_skills/<team_id>/).
                dest_root=dest_root,
                existing_user_skills={},  # Optional team quota (§4.2 note; not enforced, same as team_subagent).
                overwrite=False,
            )
        except SkillUploadError as e:
            code = e.code or "skill_bundle_invalid"
            raise _coded_http(request, 422, code, **(e.params if e.code else {})) from e
        # Gate 1 (PRD §3.1): non-admin + team_wide → L1 pending; admin → direct approval; creator_only →
        # default approved for personal use (no review, immediately usable).
        sk = TeamSkill(
            id=_id("oskill"),
            team_id=user.team_id,
            name=vs.name,
            display_name=(body.display_name or vs.name),
            description=body.description or vs.description,
            source_path=str(vs.source_dir),
            visibility=body.visibility,
            created_by=user.id,
            size_bytes=vs.size_bytes,
            # skill-multiruntime S1 (G5): persist team-recipe upload risk report; OFF→empty.
            risk_report_json=json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
            created_at=_now(),
            updated_at=_now(),
        )
        if body.visibility == "team_wide":
            sk.submitted_by = user.id
            if is_admin:
                sk.approved_sha256 = bundle_sha256(vs.source_dir)
                sk.approved_by = user.id
                sk.approved_at = _now()
            else:
                sk.approval = "pending"
        try:
            await store.create_team_skill(sk)
        except ValueError as e:  # (team_id, name) row collision (rare: file layer already blocks same-name final_dir).
            shutil.rmtree(vs.source_dir, ignore_errors=True)  # Roll back written files.
            raise skill_workflow_http_error(
                409,
                "team_skill_create_conflict",
            ) from e
        # content-permissions S4 write-through (tech-design §2.3): team_wide creation synchronizes team grants.
        # Best effort: failure does not break creation (visibility fast path provides fallback).
        # P1b: only admin direct approval writes through; pending skips set_audience (defer grants until approve,
        # and share_service approval gate raises for pending, structurally preventing leaks).
        if body.visibility == "team_wide" and is_admin:
            try:
                access = getattr(request.app.state, "access_control_store", None)
                if access is not None and share_service.content_sharing_enabled():
                    await share_service.set_audience(
                        access,
                        kind="team_skill",
                        resource_id=sk.id,
                        team_id=user.team_id,
                        target="team_wide",
                        actor_id=user.id,
                        user_store=store,
                    )
            except Exception as e:
                # Swallow with reconciliation evidence (correctness review P3: silent failure leaves observable inconsistency,
                # approved team_wide without a team grant row, though execution still works through visibility's fast path).
                logger.warning("team_skill.grant_writethrough_failed id=%s err=%s", sk.id, e)
    await _audit(store, user, AuditAction.TEAM_SKILL_CREATED, sk)
    got = await store.get_team_skill(user.team_id, sk.id)
    if sk.approval == "pending":
        await _audit(store, user, AuditAction.TEAM_SKILL_SUBMITTED, sk)
        await _notify_approval(request, store=store, actor=user, sk=got or sk, event="submitted")
    elif body.visibility == "team_wide":
        await _audit(store, user, AuditAction.TEAM_SKILL_APPROVED, sk, extra={"direct": True})
    return _public(got or sk, include_review=True)  # Creator view always includes review details.


def _visible_to(
    sk: TeamSkill,
    user: User,
    granted: frozenset[str] = frozenset(),
    *,
    is_admin: bool = False,
) -> bool:
    """P2（review）：creator_only 仅 created_by 可见，team_wide 全 team 成员可见。
    与 agent 路径 skill_resolver._visibility_ok 同语义，避免管理 HTTP 面泄漏 creator_only 元数据。
    S4: 第三条件 = "分享给我"预取集命中（grant 叠加,share_service 内部挂 content_sharing flag）。
    P1b: 已批槽非 approved（L1 pending/rejected）仅作者+admin 可见（PRD §3.1）——本函数保持
    sync 纯函数，``is_admin`` 由路由层预取 ``get_team_role`` 结果传入。"""
    if (sk.approval or "approved") != "approved" and sk.created_by != user.id and not is_admin:
        return False
    return (sk.visibility or "team_wide") == "team_wide" or sk.created_by == user.id or sk.id in granted


async def _my_disabled_names(request: Request, user: User) -> frozenset[str]:
    """请求者的 per-user 停用集（稳定 Team ID key；兼容旧 name key）.

    P3：settings 团队组开关的现值面——写路径在 /api/v1/skills/update PATCH 团队腿
    （team Skill ID 与 name 均在 body）。
    best-effort：store 缺失/异常 → 空集（全部视为启用）。"""
    sis = getattr(request.app.state, "skill_instance_store", None)
    if sis is None:
        return frozenset()
    try:
        rows = await sis.list_for_user(user_id=user.id)
        return frozenset(r.name for r in rows if getattr(r, "source_type", "") == "team_shared" and not r.enabled)
    except Exception:
        return frozenset()


async def _granted_skill_ids(request: Request, user: User) -> frozenset[str]:
    access = getattr(request.app.state, "access_control_store", None)
    if access is None:
        return frozenset()
    try:
        from bowhead_core.share_service import grants_for_viewer

        return await grants_for_viewer(
            access,
            team_id=user.team_id,
            user_id=user.id,
            kind="team_skill",
        )
    except Exception:
        return frozenset()


async def _roster_counts(request: Request, user: User) -> dict[str, int]:
    """该 team 下每个 team_skill 的 user/group allow grant 数(custom 徽标的"指定 N 人")。"""
    access = getattr(request.app.state, "access_control_store", None)
    if access is None:
        return {}
    try:
        grants = await access.list_resource_grants(user.team_id, resource_kind="team_skill")
        counts: dict[str, int] = {}
        for g in grants:
            if g.effect == "allow" and g.principal_type in ("user", "group"):
                counts[g.resource_id] = counts.get(g.resource_id, 0) + 1
        return counts
    except Exception:
        return {}


def _public_enriched(
    sk: TeamSkill,
    user: User,
    granted: frozenset[str],
    roster: dict[str, int],
    *,
    include_review: bool = False,
) -> dict[str, Any]:
    """列表契约(与 team_subagents._public_enriched 同款): + audience 徽标 + share_count
    + shared_by,喂 workflows 页的分享徽标/入口。pending 行 audience 仍按 visibility 派生
    ——FE 对 pending 抑制 audience 徽标、优先显示 approval 徽标（review P2-2，P3 落）。"""
    d = _public(sk, include_review=include_review)
    count = roster.get(sk.id, 0)
    if (sk.visibility or "team_wide") == "team_wide":
        d["audience"] = "team_wide"
    else:
        d["audience"] = "custom" if count > 0 else "private"
    d["share_count"] = count
    d["shared_by"] = sk.created_by if sk.created_by != user.id and sk.id in granted and d["audience"] != "team_wide" else None
    return d


@router.get("")
async def list_team_skills_route(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    # Management explicitly opts out (tech-design P1b): include pending/rejected rows so authors/admins see approval
    # status in lists. _visible_to filters members (store defaults close only agent/discovery consumers).
    items = await store.list_team_skills(user.team_id, only_enabled=False, only_approved=False)
    granted = await _granted_skill_ids(request, user)
    roster = await _roster_counts(request, user)
    is_admin = await _is_team_admin(store, user)
    visible = [s for s in items if _visible_to(s, user, granted, is_admin=is_admin)]
    # Skills-page IA redesign (2026-07-10): From <name> badges require creator display names;
    # batch reverse lookup (same get_users_by_ids as 0708 ⑤, avoids N+1); unresolved creators get null, never the raw id.
    names = await store.get_users_by_ids([s.created_by for s in visible if s.created_by])
    my_disabled = await _my_disabled_names(request, user)
    summaries = {}
    if visible:
        try:
            summaries = await ResourceUsageProjectionService(str(store.db_path)).summaries(
                team_id=user.team_id,
                actor_user_id=user.id,
                selectors=tuple(
                    ResourceUsageSelector(
                        key=s.id,
                        kind="skill",
                        resource_id=s.id,
                        display_name=s.display_name or s.name,
                        resource_ref=f"team:{s.id}",
                        available=(s.status == "enabled" and (s.approval or "approved") == "approved"),
                    )
                    for s in visible
                ),
                governance_action_allowed=is_admin,
            )
        except ResourceUsageUnavailableError as exc:
            raise skill_workflow_http_error(503, "resource_usage_unavailable") from exc
    out = []
    for s in visible:
        d = _public_enriched(s, user, granted, roster, include_review=is_admin or s.created_by == user.id)
        # Never echo the raw user id as a name: an unresolvable creator (deleted user,
        # no display name or email) is null and clients show a localized label.
        name = names.get(s.created_by or "")
        d["created_by_name"] = name if name and name != s.created_by else None
        d["my_enabled"] = f"team:{s.id}" not in my_disabled and s.name not in my_disabled
        if s.id in summaries:
            d["usage_summary"] = summaries[s.id].to_dict()
        out.append(d)
    return {
        "items": out,
        "impact_guard_enabled": resource_impact_guard_enabled(),
    }


@router.post("/{skill_id}/impact-preview", dependencies=_WRITE_DEPS)
async def preview_team_skill_impact(
    skill_id: str,
    body: TeamSkillImpactPreviewBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    sk, _is_admin = await _visible_or_404(request, store, user, skill_id)
    if not await can_manage_shares(store, user, sk.created_by):
        raise skill_workflow_http_error(403, "team_skill_modify_forbidden")
    if body.action == "patch":
        if set(body.proposed_change) != {"status"} or body.proposed_change.get("status") != "disabled":
            raise skill_workflow_http_error(422, "resource_command_invalid")
        command = _team_skill_command("patch", body.proposed_change)
    elif body.action == "delete":
        if body.proposed_change:
            raise skill_workflow_http_error(422, "resource_command_invalid")
        command = _team_skill_command("delete", {})
    else:
        raise skill_workflow_http_error(422, "resource_command_invalid")
    from bowhead_core._db import open_shared_ctx

    try:
        async with open_shared_ctx(store.db_path) as db:
            selector = _team_skill_selector(sk)
            views = await ResourceUsageProjectionService(str(store.db_path)).usage_many_in_connection(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                selectors=(selector,),
                # The permission check above authorizes a team-wide mutation for
                # both the resource creator and team admins.  Its preview must
                # therefore use the complete governance projection for either.
                governance_action_allowed=True,
                include_groups=True,
            )
            schedule_ids = await affected_skill_schedule_ids(
                db,
                team_id=user.team_id,
                resource_ref=f"team:{sk.id}",
            )
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_impact_projection_incomplete") from exc
    view = views[sk.id]
    dependency_impact = usage_dependency_impact(view, schedule_ids)
    blocking = view.summary.completeness != "complete" or (body.action == "delete" and delete_has_live_dependencies(view))
    token: str | None = None
    if not blocking:
        try:
            token = issue_impact_token(
                team_id=user.team_id,
                actor_user_id=user.id,
                resource_kind="team_skill",
                resource_id=sk.id,
                command=command,
                resource_state=_team_skill_state(sk),
                dependency_impact=dependency_impact,
            )
        except ResourceImpactKeyUnavailable as exc:
            raise impact_http_error(exc) from exc
    return {
        "kind": "resource_change_impact",
        "schema_version": 1,
        "action": body.action,
        "blocking": blocking,
        "summary": view.summary.to_dict(),
        "new_admission": "blocked_for_affected_agents",
        "running_execution": "future_dispatch_blocked",
        "already_sent_effect": "not_reversible",
        "required_follow_up": [{"kind": "schedule_reconfirm", "job_id": job_id} for job_id in schedule_ids],
        "impact_token": token,
        "expires_in_seconds": 300,
        "command": command,
        "reason_codes": [
            *view.summary.reason_codes,
            *(("resource_delete_has_live_dependencies",) if body.action == "delete" and delete_has_live_dependencies(view) else ()),
        ],
    }


async def _visible_or_404(
    request: Request,
    store: UserStore,
    user: User,
    skill_id: str,
) -> tuple[TeamSkill, bool]:
    """详情面共用的可见性前置：返回 ``(sk, is_admin)``，不可见/不存在一律 404（反枚举）."""
    sk = await store.get_team_skill(user.team_id, skill_id)
    granted = await _granted_skill_ids(request, user)
    is_admin = await _is_team_admin(store, user)
    if sk is None or not _visible_to(sk, user, granted, is_admin=is_admin):
        # Do not distinguish nonexistent from invisible (anti-enumeration).
        raise skill_workflow_http_error(404, "team_skill_not_found")
    return sk, is_admin


@router.get("/{skill_id}/usage")
async def get_team_skill_usage_route(
    skill_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    sk, is_admin = await _visible_or_404(request, store, user, skill_id)
    try:
        view = await ResourceUsageProjectionService(str(store.db_path)).usage(
            team_id=user.team_id,
            actor_user_id=user.id,
            selector=ResourceUsageSelector(
                key=sk.id,
                kind="skill",
                resource_id=sk.id,
                display_name=sk.display_name or sk.name,
                resource_ref=f"team:{sk.id}",
                revision=sk.approved_sha256,
                available=(sk.status == "enabled" and (sk.approval or "approved") == "approved"),
            ),
            governance_action_allowed=is_admin,
        )
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_usage_unavailable") from exc
    return view.to_dict()


@router.get("/{skill_id}")
async def get_team_skill_route(
    skill_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    initial, _ = await _visible_or_404(request, store, user, skill_id)
    async with team_skill_lock(initial.team_id, initial.name):
        sk, is_admin = await _visible_or_404(request, store, user, skill_id)
        include_review = is_admin or sk.created_by == user.id
        d = _public(sk, include_review=include_review)
        my_disabled = await _my_disabled_names(request, user)
        d["my_enabled"] = f"team:{sk.id}" not in my_disabled and sk.name not in my_disabled
        # Skills-page IA redesign (2026-07-10): detail drawer needs SKILL.md body preview.
        # Path guard: read SKILL.md only within the team's own tree (source_path comes from creation's
        # validate_and_save_generated; defensively recheck against historical bad data/symlink escapes).
        # Send display body_md (frontmatter stripped) separately from editing body_raw (entire SKILL.md);
        # mixing them makes edit-and-save submit a manifest without frontmatter, guaranteed to fail validation.
        d["body_raw"] = _read_body_md(sk)
        d["body_md"] = display_body(d["body_raw"])
        # Bundle attachments (references/scripts/evals), same field names as personal detail; see _bundle_files.
        d.update(_bundle_files(sk))
        if include_review and sk.candidate_path:
            # Approval-card candidate preview (L2: live=body_md, awaiting review=candidate_body_md).
            d["candidate_body_md"] = display_body(_read_body_md(sk, sk.candidate_path))
        # skill-multiruntime S1 (G5): expose risk report (languages/keywords/shebang) in details for human review.
        d["risk_report"] = None
        if sk.risk_report_json:
            with contextlib.suppress(json.JSONDecodeError):
                d["risk_report"] = json.loads(sk.risk_report_json)
        skill_dir = _team_dir(sk.team_id, sk.source_path)
        if skill_dir is not None:
            d.update(read_bundle_snapshot(skill_dir, editable=True))
        return d


@router.get("/{skill_id}/files/{kind}/{filename:path}")
async def get_team_skill_file_route(
    skill_id: str,
    kind: str,
    filename: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """读团队 skill bundle 里的单个 references/scripts/evals 文件（与个人侧 GET 对称）.

    可见性门与详情同一条（``_visible_or_404``）→ 不可见的行连文件名都探不到。读的是
    **已批 live 快照**（source_path）；候选槽内容不从这里出（审批卡走 candidate_body_md）。
    """
    validate_file_ref(kind, filename)  # Kind allowlist + path-traversal protection.
    initial, _ = await _visible_or_404(request, store, user, skill_id)
    async with team_skill_lock(initial.team_id, initial.name):
        sk, _is_admin = await _visible_or_404(request, store, user, skill_id)
        skill_dir = _team_dir(sk.team_id, sk.source_path)
        if skill_dir is None:
            raise skill_workflow_http_error(404, "skill_file_not_found")
        return read_bundle_file_snapshot(skill_dir, kind, filename, editable=True)


class FileWriteBody(BaseModel):
    """编辑面写体（与个人侧 SkillFileWriteRequest 同形）。细粒度上限在 bundle 校验里。"""

    content: str = Field(max_length=MAX_SKILL_SIZE)
    expected_content_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")


async def _assert_content_editable(store: UserStore, user: User, sk: TeamSkill, *, is_admin: bool) -> None:
    """团队 skill 的**内容**可编辑性门（两层，可见性已由 _visible_or_404 判过）.

    ① 能不能管 —— ``can_manage_shares``（owner ∨ team_admin ∨ 创建者），与 PATCH 同规则；
    ② 内容能不能改 —— 已提交/已批的 team_wide 广播面只有 admin 能改（PRD §3.1：admin 批的
      是「那一份内容」；作者单方面改已批 bundle = 全团队立刻跑没过审的内容）。非 admin 作者
      的正路是「复制为我的 → 改 → 重新提交」。creator_only 不进审批，创建者随便改。
    """
    if not await can_manage_shares(store, user, sk.created_by):
        raise skill_workflow_http_error(403, "team_skill_modify_forbidden")
    if (sk.visibility or "team_wide") == "team_wide" and sk.approval in ("pending", "approved") and not is_admin:
        raise skill_workflow_http_error(403, "team_skill_content_admin_required")


async def _restamp_team_bundle(store: UserStore, sk: TeamSkill, skill_dir: Path, validated: Any, *, body_changed: bool) -> None:
    """编辑落盘后刷新 size_bytes；改的是已批 live 快照时同步刷新已批指纹（否则指纹说谎）."""
    approved_sha = bundle_sha256(skill_dir) if (sk.approval or "approved") == "approved" else None
    await store.restamp_team_skill_bundle(
        sk.team_id,
        sk.id,
        size_bytes=validated.size_bytes,
        description=validated.description if body_changed else sk.description,
        risk_report_json=json.dumps(validated.risk_report, ensure_ascii=False) if validated.risk_report else None,
        approved_sha256=approved_sha,
    )


async def _edit_team_bundle(
    request: Request,
    store: UserStore,
    user: User,
    skill_id: str,
    *,
    label: str,
    write: Any,
) -> dict[str, Any]:
    """两条写路由的共壳：可编辑性门 → 锁内写盘 → 刷派生字段 → 审计.

    门判两次：锁外先判（不可编辑就别排队），拿到 per-skill 锁后**重读行再判一次**——
    与 PATCH 同一条纪律（锁外读到的 approval/visibility 可能已被并发的 approve/转移改掉，
    据陈旧值放行等于绕过治理门）。
    """
    sk, is_admin = await _visible_or_404(request, store, user, skill_id)
    await _assert_content_editable(store, user, sk, is_admin=is_admin)
    async with team_skill_lock(sk.team_id, sk.name):
        fresh = await store.get_team_skill(user.team_id, skill_id)
        if fresh is None:
            raise skill_workflow_http_error(404, "team_skill_not_found")
        await _assert_content_editable(store, user, fresh, is_admin=is_admin)
        skill_dir = _team_dir(fresh.team_id, fresh.source_path)
        if skill_dir is None or not (skill_dir / "SKILL.md").is_file():
            raise skill_workflow_http_error(409, "team_skill_bundle_incomplete")
        try:
            payload, validated = write(skill_dir, fresh)
        except SkillUploadError as e:
            raise _coded_http(request, 422, e.code or "skill_bundle_invalid", **(e.params if e.code else {})) from e
        await _restamp_team_bundle(store, fresh, skill_dir, validated, body_changed=label == "SKILL.md")
    await _audit(store, user, AuditAction.TEAM_SKILL_UPDATED, fresh, extra={"file": label})
    return payload


@router.post("/{skill_id}/files", dependencies=_WRITE_DEPS)
async def change_team_skill_file_route(
    skill_id: str,
    body: BundleFileChange,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    require_bundle_authoring()
    return await _edit_team_bundle(
        request,
        store,
        user,
        skill_id,
        label=f"{body.operation}:{body.kind}/{body.filename}",
        write=lambda skill_dir, _sk: change_bundle_file(skill_dir, **body.model_dump()),
    )


@router.put("/{skill_id}/files/{kind}/{filename:path}", dependencies=_WRITE_DEPS)
async def put_team_skill_file_route(
    skill_id: str,
    kind: str,
    filename: str,
    body: FileWriteBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """覆写团队 skill 的 references/scripts/evals 单文件（与 GET 对称）."""
    validate_file_ref(kind, filename)
    return await _edit_team_bundle(
        request,
        store,
        user,
        skill_id,
        label=f"{kind}/{filename}",
        write=lambda skill_dir, _sk: write_bundle_file(skill_dir, kind, filename, body.content, expected_content_sha256=body.expected_content_sha256),
    )


@router.put("/{skill_id}/body", dependencies=_WRITE_DEPS)
async def put_team_skill_body_route(
    skill_id: str,
    body: FileWriteBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """覆写团队 skill 的 SKILL.md 正文（配置页直接编辑；名字不可改）."""
    return await _edit_team_bundle(
        request,
        store,
        user,
        skill_id,
        label="SKILL.md",
        write=lambda skill_dir, sk: write_bundle_body(
            skill_dir, body.content, expected_name=sk.name, expected_content_sha256=body.expected_content_sha256
        ),
    )


async def _protected_disable_team_skill(
    *,
    skill_id: str,
    request: Request,
    user: User,
    store: UserStore,
    include_review: bool,
) -> dict[str, Any]:
    token, idempotency_key = require_impact_headers(request)
    command = _team_skill_command("patch", {"status": "disabled"})
    request_digest = resource_change_request_digest(
        resource_kind="team_skill",
        resource_id=skill_id,
        command=command,
    )
    from bowhead_core._db import open_shared_ctx

    try:
        async with open_shared_ctx(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cached = await read_change_receipt(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if cached is not None:
                await db.rollback()
                return cached
            async with db.execute(
                "SELECT * FROM team_skill WHERE team_id=? AND id=?",
                (user.team_id, skill_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise skill_workflow_http_error(404, "team_skill_not_found")
            current = _row_to_team_skill(row)
            selector = _team_skill_selector(current)
            views = await ResourceUsageProjectionService(str(store.db_path)).usage_many_in_connection(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                selectors=(selector,),
                governance_action_allowed=include_review,
                include_groups=True,
            )
            view = views[skill_id]
            if view.summary.completeness != "complete":
                raise ResourceUsageUnavailableError("resource_dependency_projection_incomplete")
            schedule_ids = await affected_skill_schedule_ids(
                db,
                team_id=user.team_id,
                resource_ref=f"team:{skill_id}",
            )
            dependency_impact = usage_dependency_impact(view, schedule_ids)
            verified = verify_impact_token(
                token,
                team_id=user.team_id,
                actor_user_id=user.id,
                resource_kind="team_skill",
                resource_id=skill_id,
                command=command,
                resource_state=_team_skill_state(current),
                dependency_impact=dependency_impact,
            )
            await pause_affected_schedules(db, schedule_ids)
            await db.execute(
                "UPDATE team_skill SET status='disabled',updated_at=? WHERE team_id=? AND id=?",
                (_now(), user.team_id, skill_id),
            )
            async with db.execute(
                "SELECT * FROM team_skill WHERE team_id=? AND id=?",
                (user.team_id, skill_id),
            ) as cursor:
                updated_row = await cursor.fetchone()
            if updated_row is None:  # pragma: no cover - same transaction invariant
                raise skill_workflow_http_error(404, "team_skill_not_found")
            response = _public(
                _row_to_team_skill(updated_row),
                include_review=include_review,
            )
            response["paused_schedule_ids"] = schedule_ids
            audit_id = (
                "aud_"
                + hashlib.sha256(
                    f"{user.team_id}:{user.id}:{idempotency_key}".encode(),
                ).hexdigest()[:24]
            )
            await store.write_admin_audit_log(
                actor_user_id=user.id,
                team_id=user.team_id,
                action=AuditAction.TEAM_SKILL_UPDATED,
                target_kind="team_skill",
                target_id=skill_id,
                target_team_id=user.team_id,
                scope="team",
                payload={"name": current.name, "changed": ["status"], "paused_schedule_count": len(schedule_ids)},
                conn=db,
                audit_id=audit_id,
            )
            await write_change_receipt(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                idempotency_key=idempotency_key,
                resource_kind="team_skill",
                resource_id=skill_id,
                action="patch",
                request_digest=request_digest,
                impact_digest=verified.dependency_impact_digest,
                response=response,
            )
            await db.commit()
            return response
    except (
        ResourceChangeIdempotencyConflict,
        ResourceImpactKeyUnavailable,
        ResourceImpactTokenInvalid,
        ResourceImpactTokenStale,
    ) as exc:
        raise impact_http_error(exc) from exc
    except ResourceUsageUnavailableError as exc:
        raise skill_workflow_http_error(503, "resource_impact_projection_incomplete") from exc


@router.patch("/{skill_id}", dependencies=_WRITE_DEPS)
async def patch_team_skill_route(
    skill_id: str,
    body: PatchBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    existing = await store.get_team_skill(user.team_id, skill_id)
    granted = await _granted_skill_ids(request, user)
    is_admin = await _is_team_admin(store, user)
    if existing is None or not _visible_to(existing, user, granted, is_admin=is_admin):
        raise skill_workflow_http_error(404, "team_skill_not_found")
    # B2 (content-permissions): previously every team member could edit anyone's skill. Restrict to
    # owner ∨ team_admin/creator, unconditionally, without a feature flag.
    if not await can_manage_shares(store, user, existing.created_by):
        raise skill_workflow_http_error(403, "team_skill_modify_forbidden")
    if body.visibility is not None and body.visibility not in _VISIBILITY:
        raise skill_workflow_http_error(422, "team_skill_visibility_invalid")
    if body.status is not None and body.status not in _STATUS:
        raise skill_workflow_http_error(422, "team_skill_status_invalid")
    has_guard_headers = bool(request.headers.get("X-Bowhead-Impact-Token") or request.headers.get("Idempotency-Key"))
    if resource_impact_guard_enabled() and ((body.status == "disabled" and existing.status != "disabled") or has_guard_headers):
        if any(value is not None for value in (body.display_name, body.description, body.visibility)) or body.status != "disabled":
            raise skill_workflow_http_error(422, "resource_command_invalid")
        response.headers["Cache-Control"] = "private, no-store"
        async with team_skill_lock(user.team_id, existing.name):
            return await _protected_disable_team_skill(
                skill_id=skill_id,
                request=request,
                user=user,
                store=store,
                include_review=is_admin or existing.created_by == user.id,
            )
    async with team_skill_lock(user.team_id, existing.name):
        existing = await store.get_team_skill(user.team_id, skill_id)  # Reread inside lock to avoid stale unlocked data.
        if existing is None:
            raise skill_workflow_http_error(404, "team_skill_not_found")
        # team_wide display_name/description feed Lead routing (advertising segment); authors cannot unilaterally change them
        # after approval (CTO review P2-14). **Frozen while pending too**: CAS covers bundles, not metadata,
        # or authors could swap descriptions between admins opening review cards and approving (security review P2:
        # routing-copy bait-and-switch). Authors must withdraw → edit → resubmit. Editing is allowed when rejected (review on resubmit).
        if (
            (existing.visibility or "team_wide") == "team_wide"
            and existing.approval in ("pending", "approved")
            and (body.display_name is not None or body.description is not None)
            and not is_admin
        ):
            raise skill_workflow_http_error(
                403,
                "team_skill_metadata_admin_required",
            )
        transition: str | None = None
        if body.visibility is not None and body.visibility != existing.visibility:
            transition = await _apply_visibility_transition(request, store, user, existing, body.visibility, is_admin=is_admin)
        if body.display_name is not None or body.description is not None or body.status is not None:
            await store.update_team_skill_meta(
                user.team_id,
                skill_id,
                display_name=body.display_name,
                description=body.description,
                status=body.status,
            )
    # All audit/notify outside locks (same as approve/reject/create; never hold locks across notification IO).
    got = await store.get_team_skill(user.team_id, skill_id)
    if transition == "submitted":
        await _audit(store, user, AuditAction.TEAM_SKILL_SUBMITTED, existing)
        await _notify_approval(request, store=store, actor=user, sk=got or existing, event="submitted")
    elif transition == "direct_approved":
        await _audit(store, user, AuditAction.TEAM_SKILL_APPROVED, existing, extra={"direct": True})
    await _audit(store, user, AuditAction.TEAM_SKILL_UPDATED, existing)
    return _public(got or existing, include_review=is_admin or existing.created_by == user.id)


async def _apply_visibility_transition(
    request: Request,
    store: UserStore,
    user: User,
    existing: TeamSkill,
    target: str,
    *,
    is_admin: bool,
) -> str | None:
    """PATCH 的 visibility 转移（§1 转移表；调用方已持 per-skill 锁）.

    P2-12：原「access store 缺失→回退裸写 visibility」降级路已删——visibility 转移必须经
    审批状态机；需要 grant 写穿的腿在 access store 缺失时 fail-closed 503，不留裸写后门。
    返回转移事件（'submitted' | 'direct_approved' | 'withdrawn' | 'unpublished'）——
    audit/notify 由调用方在锁外统一做。
    """
    access = getattr(request.app.state, "access_control_store", None)
    if target == "team_wide":
        # Gate 2: creator_only → team_wide. Non-admin = L1 submission (current content is candidate; skip
        # set_audience, deferring grant write-through until approve; pending must have no team grant);
        # admin = direct approval (submit+approve through the state machine, no bypass).
        if not await store.submit_team_skill(user.team_id, existing.id, submitted_by=user.id):
            raise skill_workflow_http_error(
                409,
                "team_skill_submit_state_conflict",
            )
        if not is_admin:
            return "submitted"
        sha = _source_sha256(existing)
        if sha is None:
            raise skill_workflow_http_error(
                409,
                "team_skill_bundle_incomplete",
            )
        if not await store.approve_team_skill(user.team_id, existing.id, approved_by=user.id, approved_sha256=sha):
            raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
        if access is None:
            raise skill_workflow_http_error(
                503,
                "access_control_store_unavailable",
            )
        await share_service.set_audience(
            access,
            kind="team_skill",
            resource_id=existing.id,
            team_id=user.team_id,
            target="team_wide",
            actor_id=user.id,
            user_store=store,
        )
        return "direct_approved"
    # → creator_only
    if existing.approval in ("pending", "rejected") and not existing.candidate_path:
        # L1 withdrawal / reclaim after rejection (author∨admin): no team grant (no pending write-through), so no set_audience needed.
        if existing.created_by != user.id and not is_admin:
            raise skill_workflow_http_error(
                403,
                "team_skill_withdraw_forbidden",
            )
        if await store.withdraw_team_skill(user.team_id, existing.id) != "submission":
            raise skill_workflow_http_error(
                409,
                "team_skill_withdraw_state_conflict",
            )
        return "withdrawn"
    # Unpublish approved team_wide = admin-only (review P1-1: authors may not unpublish approved assets).
    if not is_admin:
        raise skill_workflow_http_error(
            403,
            "team_skill_audience_admin_required",
        )
    if access is None:
        raise skill_workflow_http_error(
            503,
            "access_control_store_unavailable",
        )
    if existing.candidate_path:
        # Unpublishing also clears candidate slots/directories (correctness review P3: otherwise __candidate becomes orphaned;
        # creator_only rows structurally disallow candidates, see submit_team_skill_candidate's WHERE guard).
        await store.withdraw_team_skill(user.team_id, existing.id)
        _safe_rmtree(user.team_id, existing.candidate_path)
    # set_audience(custom) removes team grant + writes through creator_only visibility (one transaction, existing named grants retained).
    await share_service.set_audience(
        access,
        kind="team_skill",
        resource_id=existing.id,
        team_id=user.team_id,
        target="custom",
        actor_id=user.id,
        user_store=store,
    )
    return "unpublished"


@router.post("/{skill_id}/approve", dependencies=_WRITE_DEPS)
async def approve_team_skill_route(
    skill_id: str,
    body: ApprovalCasBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    sk = await _require_reviewable(request, store, user, skill_id)
    async with team_skill_lock(user.team_id, sk.name):
        sk = await store.get_team_skill(user.team_id, skill_id)  # Reread inside lock.
        if sk is None:
            raise skill_workflow_http_error(404, "team_skill_not_found")
        if sk.candidate_path and sk.candidate_state == "pending":
            await _approve_candidate(request, store, user, sk, body)
        elif sk.candidate_path:
            # Rejected candidates cannot be directly approved (L2 requires approved + **pending** candidate; correctness review:
            # rejection retains CAS keys, so without this gate admins could promote rejected candidates using old keys). Authors resubmit/overwrite first.
            raise skill_workflow_http_error(
                409,
                "team_skill_rejected_candidate",
            )
        elif sk.approval == "pending":
            await _approve_first_submission(request, store, user, sk, body)
        else:
            raise skill_workflow_http_error(
                409,
                "team_skill_nothing_to_approve",
            )
    await _audit(store, user, AuditAction.TEAM_SKILL_APPROVED, sk)
    got = await store.get_team_skill(user.team_id, skill_id)
    await _notify_approval(request, store=store, actor=user, sk=got or sk, event="decided", decision="approved")
    return _public(got or sk, include_review=True)


async def _approve_first_submission(
    request: Request,
    store: UserStore,
    user: User,
    sk: TeamSkill,
    body: ApprovalCasBody,
) -> None:
    """L1 批准：内容已在 source_path，approve 只翻已批槽（无盘替换）.

    CAS 载体=锁内对 source_path 实算 hash 与请求键比对（免加列方案，tech-design §1）
    + DB WHERE approval='pending' 二层。"""
    sha = _source_sha256(sk)
    if sha is None:
        raise skill_workflow_http_error(
            409,
            "team_skill_bundle_incomplete",
        )
    if not body.source_sha256 or body.source_sha256 != sha:
        raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
    if not await store.approve_team_skill(user.team_id, sk.id, approved_by=user.id, approved_sha256=sha):
        raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
    # Team-grant write-through moved from create (PRD §3.1: pending must have no team grant).
    # Same best effort as create (visibility fast-path fallback); flip approval first, naturally passing the gate.
    try:
        access = getattr(request.app.state, "access_control_store", None)
        if access is not None and share_service.content_sharing_enabled():
            await share_service.set_audience(
                access,
                kind="team_skill",
                resource_id=sk.id,
                team_id=sk.team_id,
                target="team_wide",
                actor_id=user.id,
                user_store=store,
            )
    except Exception as e:
        logger.warning("team_skill.grant_writethrough_failed id=%s err=%s", sk.id, e)


async def _approve_candidate(
    request: Request,
    store: UserStore,
    user: User,
    sk: TeamSkill,
    body: ApprovalCasBody,
) -> None:
    """L2 批准：候选目录原子替换已批目录（调用方已持 per-skill 锁）.

    盘/DB 顺序与 crash 窗口语义（tech-design P1b，单进程 + per-skill 锁前提）：
      1. 预清（顺序敏感）：先恢复检测（``__old`` 在 ∧ approved 缺 → 反向 rename 复原——
         该 ``__old`` 是 live 内容唯一副本，先删就没了）；然后才 rmtree 残留 ``__old``；
         再候选完整性校验。
      2. 内存 CAS 预检（行读出与请求键比对，不匹配 409）。
      3. 盘替换：rename(approved→__old) → rename(candidate→approved)。
      4. DB：approve_team_skill BEGIN IMMEDIATE 内候选双键二次 CAS → 翻字段；
         0 行命中=锁外理论竞态 → 回滚盘（反向 rename）+ 409。
      5. rmtree(__old)（best-effort，残留由下次预清收）。
    crash 窗口：3 前半后 crash → approved 缺、团队面 fail-closed 不可见（无害降级），
    下次预清反向 rename 复原；3 完成 4 前 crash → 盘=新内容而 DB 候选字段仍在，预清检测
    「candidate 目录缺 ∧ approved hash==candidate_sha256」幂等补放 DB 更新（approve 重放）；
    4 后 5 前 crash → 仅 ``__old`` 残留，预清收。"""
    team_root = (_TEAM_SKILLS_ROOT / sk.team_id).resolve()
    approved_dir = _team_dir(sk.team_id, sk.source_path)
    cand_dir = _team_dir(sk.team_id, sk.candidate_path)
    if approved_dir is None or cand_dir is None:
        raise skill_workflow_http_error(
            409,
            "team_skill_bundle_path_invalid",
        )
    old_dir = team_root / f"{sk.name}__old"
    # 1a. Crash recovery: __old present ∧ approved absent → reverse rename restores it (must precede 1b cleanup).
    if old_dir.exists() and not approved_dir.exists():
        old_dir.rename(approved_dir)
    # 1b. Clean __old residue (previous approve crashed after 4, before 5).
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)
    # 1c. Crash recovery (disk replaced, DB not updated): candidate renamed to approved → idempotently finish DB update.
    if not cand_dir.exists() and approved_dir.is_dir() and sk.candidate_sha256 and bundle_sha256(approved_dir) == sk.candidate_sha256:
        if not await store.approve_team_skill(
            sk.team_id,
            sk.id,
            approved_by=user.id,
            approved_sha256=sk.candidate_sha256,
            expected_candidate_submitted_at=sk.candidate_submitted_at,
            expected_candidate_sha256=sk.candidate_sha256,
        ):
            raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
        return
    # 1d. Prevalidate candidate integrity (PRD §2.4 disk-corruption boundary: preempt 500 with 409 so authors resubmit).
    if not (cand_dir / "SKILL.md").is_file():
        raise skill_workflow_http_error(
            409,
            "team_skill_candidate_incomplete",
        )
    try:
        vs = _validate_skill_dir(cand_dir)
    except SkillUploadError as e:
        raise skill_workflow_http_error(
            409,
            "team_skill_candidate_invalid",
        ) from e
    # 2. In-memory CAS precheck.
    if body.candidate_submitted_at != sk.candidate_submitted_at or body.candidate_sha256 != sk.candidate_sha256:
        raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
    # Titles before the swap: the row's display name follows the approved title unless an admin renamed it.
    live_title = _bundle_title(approved_dir)
    candidate_title = _bundle_title(cand_dir)
    # 3. Disk replacement (approved missing from corruption: skip 3a, directly promote candidate).
    had_old = False
    if approved_dir.exists():
        approved_dir.rename(old_dir)
        had_old = True
    cand_dir.rename(approved_dir)
    # 4. DB CAS + field updates; zero rows → disk rollback.
    if not await store.approve_team_skill(
        sk.team_id,
        sk.id,
        approved_by=user.id,
        approved_sha256=sk.candidate_sha256 or "",
        expected_candidate_submitted_at=sk.candidate_submitted_at,
        expected_candidate_sha256=sk.candidate_sha256,
    ):
        approved_dir.rename(cand_dir)
        if had_old:
            old_dir.rename(approved_dir)
        raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
    # 4b. Refresh row description from approved content (Lead routing; admins approve entire candidates including frontmatter;
    # otherwise advertising retains old descriptions). Best effort: failure does not roll back approved content.
    # The risk chips and title must also describe the approved content rather than the first submission.
    meta: dict[str, Any] = {
        "description": vs.description,
        "risk_report_json": json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
    }
    if candidate_title and sk.display_name in {live_title, sk.name}:
        meta["display_name"] = candidate_title
    with contextlib.suppress(Exception):
        await store.update_team_skill_meta(sk.team_id, sk.id, **meta)
    # 5. Best-effort cleanup.
    if had_old:
        shutil.rmtree(old_dir, ignore_errors=True)


@router.post("/{skill_id}/reject", dependencies=_WRITE_DEPS)
async def reject_team_skill_route(
    skill_id: str,
    body: RejectBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    sk = await _require_reviewable(request, store, user, skill_id)
    async with team_skill_lock(user.team_id, sk.name):
        sk = await store.get_team_skill(user.team_id, skill_id)  # Reread inside lock.
        if sk is None:
            raise skill_workflow_http_error(404, "team_skill_not_found")
        if sk.candidate_state == "pending":
            # L2 rejection: candidate_state=rejected + reason; live stays unchanged/usable. Retain candidate for author inspection.
            if body.candidate_submitted_at != sk.candidate_submitted_at or body.candidate_sha256 != sk.candidate_sha256:
                raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
            ok = await store.reject_team_skill(
                user.team_id,
                skill_id,
                reason=body.reason,
                expected_candidate_submitted_at=sk.candidate_submitted_at,
                expected_candidate_sha256=sk.candidate_sha256,
            )
        elif sk.approval == "pending":
            # L1 rejection: approval=rejected + reason; content stays in source_path, visible only to author.
            sha = _source_sha256(sk)
            if sha is not None and (not body.source_sha256 or body.source_sha256 != sha):
                raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
            ok = await store.reject_team_skill(user.team_id, skill_id, reason=body.reason)
        else:
            raise skill_workflow_http_error(
                409,
                "team_skill_nothing_to_reject",
            )
        if not ok:
            raise _coded_http(request, 409, "E_SKILL_CANDIDATE_STALE")
    await _audit(store, user, AuditAction.TEAM_SKILL_REJECTED, sk, extra={"reason": body.reason[:200]})
    got = await store.get_team_skill(user.team_id, skill_id)
    await _notify_approval(
        request,
        store=store,
        actor=user,
        sk=got or sk,
        event="decided",
        decision="rejected",
        reason=body.reason,
    )
    return _public(got or sk, include_review=True)


@router.post("/{skill_id}/withdraw", dependencies=_WRITE_DEPS)
async def withdraw_team_skill_route(
    skill_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """作者撤回（PRD §3.4）：L1 pending→creator_only approved 自用态；L2 只清候选槽
    （live/visibility 不动——不得下架已批资产，review P1-1）。"""
    sk = await store.get_team_skill(user.team_id, skill_id)
    granted = await _granted_skill_ids(request, user)
    is_admin = await _is_team_admin(store, user)
    if sk is None or not _visible_to(sk, user, granted, is_admin=is_admin):
        raise skill_workflow_http_error(404, "team_skill_not_found")
    if user.id not in {sk.created_by, sk.submitted_by}:
        raise skill_workflow_http_error(
            403,
            "team_skill_withdraw_forbidden",
        )
    async with team_skill_lock(user.team_id, sk.name):
        sk = await store.get_team_skill(user.team_id, skill_id)  # Reread inside lock.
        if sk is None:
            raise skill_workflow_http_error(404, "team_skill_not_found")
        leg = await store.withdraw_team_skill(user.team_id, skill_id)
        if leg is None:
            raise skill_workflow_http_error(
                409,
                "team_skill_withdraw_state_conflict",
            )
        if leg == "candidate":
            _safe_rmtree(user.team_id, sk.candidate_path)  # Clean candidate directory (row slot already cleared).
    await _audit(store, user, AuditAction.TEAM_SKILL_UPDATED, sk, extra={"withdrawn": leg})
    got = await store.get_team_skill(user.team_id, skill_id)
    return _public(got or sk, include_review=True)


class CopyToPersonalBody(BaseModel):
    overwrite: bool = False  # Explicit confirmation for existing same-name personal skills (E_SKILL_NAME_EXISTS triggers FE overwrite flow).


@router.post("/{skill_id}/copy-to-personal", dependencies=_WRITE_DEPS)
async def copy_team_skill_to_personal_route(
    skill_id: str,
    body: CopyToPersonalBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """团队 skill 复制为个人副本（PRD §3.4/§2.3：成员改良入口 + 无个人版作者的反向 CTA）.

    默认只放行**已批**快照（读 source_path=live 内容；候选在审不影响——拷的仍是 live 版）。
    gate 例外（二轮 P2-8）：作者对自己的 L1 pending/rejected 行放行——「固化直选团队→被拒」
    的作者无个人版、copy 是唯一编辑起点。个人侧走上传同款配额闸；同名个人 skill 已存在
    → 422 E_SKILL_NAME_EXISTS（带 overwrite 确认参数重试）。
    """
    sk = await store.get_team_skill(user.team_id, skill_id)
    granted = await _granted_skill_ids(request, user)
    is_admin = await _is_team_admin(store, user)
    if sk is None or not _visible_to(sk, user, granted, is_admin=is_admin):
        raise skill_workflow_http_error(404, "team_skill_not_found")
    if (sk.approval or "approved") != "approved" and sk.created_by != user.id:
        # Only admins see unapproved rows from other authors; admins cannot copy unapproved content either (approve first).
        raise skill_workflow_http_error(
            403,
            "team_skill_copy_requires_approval",
        )
    sis = getattr(request.app.state, "skill_instance_store", None)
    if sis is None:
        raise skill_workflow_http_error(503, "skill_store_unavailable")
    src = _team_dir(user.team_id, sk.source_path)
    if src is None or not (src / "SKILL.md").is_file():
        raise skill_workflow_http_error(
            409,
            "team_skill_bundle_incomplete",
        )
    from bowhead_gateway.routes.skills import _require_skill_capability_for_request, _require_skill_grant_for_request

    await _require_skill_capability_for_request(request, user, permission="write")
    await _require_skill_grant_for_request(request, user, sk.name, permission="write")
    existing = await sis.list_for_user(user_id=user.id)
    user_skills = {i.name: (i.size_bytes or 0) for i in existing if i.source_type in ("user_upload", "user_generated")}
    # Treat DB name occupancy with missing disk directory as same-name too, preventing silent row-metadata overwrites.
    if sk.name in user_skills and not body.overwrite:
        raise _coded_http(request, 422, "E_SKILL_NAME_EXISTS", name=sk.name)
    # Copy inside lock, excluding approve's disk-replacement window (otherwise could copy a partially renamed directory).
    async with team_skill_lock(user.team_id, sk.name):
        dest = _USER_SKILLS_ROOT / user.id / sk.name
        try:
            vs = copy_validated_bundle(
                src,
                dest,
                expected_name=sk.name,
                overwrite=body.overwrite,
                existing_user_skills=user_skills,
            )
        except SkillUploadError as e:
            if e.code:
                raise _coded_http(request, 422, e.code, **e.params) from e
            raise skill_workflow_http_error(
                422,
                "skill_copy_failed",
            ) from e
    await sis.upsert(
        user_id=user.id,
        name=vs.name,
        source_type="user_upload",  # Copy = personal-library import (USER_SKILL_SOURCE_TYPES; resolve user>team applies across both channels).
        source_path=str(vs.source_dir),
        version=vs.version,
        description=vs.description,
        enabled=True,
        size_bytes=vs.size_bytes,
        expected_content_hash=vs.content_hash,
        risk_report_json=json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
        reset_verdict=True,
    )
    # skill-import batch 1: personal-library copies use the same write-path hook (rerun content-based inventory).
    try:
        await apply_import_inventory(
            sis, user_id=user.id, name=vs.name, skill_dir=Path(str(vs.source_dir)), sources=await build_resolution_sources(request, user)
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).error("skill_import.inventory_failed skill=%s err=%s", vs.name, type(e).__name__)
    catalog = getattr(request.app.state, "skill_catalog", None)
    if catalog is not None:
        with contextlib.suppress(Exception):
            catalog.refresh()
    return {
        "name": vs.name,
        "source_type": "user_upload",
        "description": vs.description,
        "size_bytes": vs.size_bytes,
        "enabled": True,
    }


async def _require_reviewable(
    request: Request,
    store: UserStore,
    user: User,
    skill_id: str,
) -> TeamSkill:
    """approve/reject 共用前置：行存在 + 审批人门。非 admin：行对其可见→403（含作者自批），
    不可见→404（反枚举，不泄露存在性）。"""
    sk = await store.get_team_skill(user.team_id, skill_id)
    is_admin = await _is_team_admin(store, user)
    if sk is None or (not is_admin and not _visible_to(sk, user, await _granted_skill_ids(request, user), is_admin=False)):
        raise skill_workflow_http_error(404, "team_skill_not_found")
    await _require_team_admin(store, user)
    return sk


async def _protected_delete_team_skill(
    *,
    skill_id: str,
    request: Request,
    user: User,
    store: UserStore,
) -> None:
    token, idempotency_key = require_impact_headers(request)
    command = _team_skill_command("delete", {})
    request_digest = resource_change_request_digest(
        resource_kind="team_skill",
        resource_id=skill_id,
        command=command,
    )
    from bowhead_core._db import open_shared_ctx

    moved: list[Any] = []
    committed = False
    replayed = False
    try:
        async with open_shared_ctx(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cached = await read_change_receipt(
                db,
                team_id=user.team_id,
                actor_user_id=user.id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if cached is not None:
                replayed = True
                await db.rollback()
            else:
                async with db.execute(
                    "SELECT * FROM team_skill WHERE team_id=? AND id=?",
                    (user.team_id, skill_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise skill_workflow_http_error(404, "team_skill_not_found")
                current = _row_to_team_skill(row)
                selector = _team_skill_selector(current)
                views = await ResourceUsageProjectionService(str(store.db_path)).usage_many_in_connection(
                    db,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    selectors=(selector,),
                    governance_action_allowed=True,
                    include_groups=True,
                )
                view = views[skill_id]
                if view.summary.completeness != "complete":
                    raise ResourceUsageUnavailableError("resource_dependency_projection_incomplete")
                if delete_has_live_dependencies(view):
                    raise skill_workflow_http_error(
                        409,
                        "resource_delete_has_live_dependencies",
                        nested_error=True,
                    )
                schedule_ids = await affected_skill_schedule_ids(
                    db,
                    team_id=user.team_id,
                    resource_ref=f"team:{skill_id}",
                )
                dependency_impact = usage_dependency_impact(view, schedule_ids)
                verified = verify_impact_token(
                    token,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    resource_kind="team_skill",
                    resource_id=skill_id,
                    command=command,
                    resource_state=_team_skill_state(current),
                    dependency_impact=dependency_impact,
                )
                root = (_TEAM_SKILLS_ROOT / user.team_id).resolve()
                paths = [
                    path
                    for path in (
                        _team_dir(user.team_id, current.source_path),
                        _team_dir(user.team_id, current.candidate_path),
                        _team_dir(
                            user.team_id,
                            str(root / f"{current.name}__old"),
                        ),
                    )
                    if path is not None
                ]
                moved = quarantine_paths(
                    paths,
                    root=root,
                    command_identity=f"{user.team_id}:{user.id}:{idempotency_key}",
                )
                await pause_affected_schedules(db, schedule_ids)
                access = getattr(request.app.state, "access_control_store", None)
                if access is not None:
                    revoked = await access._revoke_grants_for_resource_conn(
                        db,
                        user.team_id,
                        "team_skill",
                        skill_id,
                    )
                    if revoked:
                        await access._bump_policy_epoch_conn(db, user.team_id)
                await db.execute(
                    "DELETE FROM team_skill WHERE team_id=? AND id=?",
                    (user.team_id, skill_id),
                )
                receipt_response = {
                    "ok": True,
                    "deleted": skill_id,
                    "paused_schedule_ids": schedule_ids,
                }
                await write_change_receipt(
                    db,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    idempotency_key=idempotency_key,
                    resource_kind="team_skill",
                    resource_id=skill_id,
                    action="delete",
                    request_digest=request_digest,
                    impact_digest=verified.dependency_impact_digest,
                    response=receipt_response,
                )
                await write_cleanup_receipts(
                    db,
                    entries=moved,
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    idempotency_key=idempotency_key,
                    resource_kind="team_skill",
                    resource_id=skill_id,
                )
                audit_id = (
                    "aud_"
                    + hashlib.sha256(
                        f"{user.team_id}:{user.id}:{idempotency_key}".encode(),
                    ).hexdigest()[:24]
                )
                await store.write_admin_audit_log(
                    actor_user_id=user.id,
                    team_id=user.team_id,
                    action=AuditAction.TEAM_SKILL_DELETED,
                    target_kind="team_skill",
                    target_id=skill_id,
                    target_team_id=user.team_id,
                    scope="team",
                    payload={"name": current.name, "paused_schedule_count": len(schedule_ids)},
                    conn=db,
                    audit_id=audit_id,
                )
                await db.commit()
                committed = True
    except (
        ResourceChangeIdempotencyConflict,
        ResourceImpactKeyUnavailable,
        ResourceImpactTokenInvalid,
        ResourceImpactTokenStale,
    ) as exc:
        if moved and not committed:
            restore_quarantined(moved)
        raise impact_http_error(exc) from exc
    except ResourceUsageUnavailableError as exc:
        if moved and not committed:
            restore_quarantined(moved)
        raise skill_workflow_http_error(503, "resource_impact_projection_incomplete") from exc
    except Exception:
        if moved and not committed:
            restore_quarantined(moved)
        raise
    if committed:
        await finalize_cleanup(store.db_path, moved)
    elif replayed:
        with contextlib.suppress(Exception):
            await retry_pending_cleanup(store.db_path)


@router.delete("/{skill_id}", status_code=204, dependencies=_WRITE_DEPS)
async def delete_team_skill_route(
    skill_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> None:
    existing = await store.get_team_skill(user.team_id, skill_id)
    if resource_impact_guard_enabled() and existing is None:
        response.headers["Cache-Control"] = "private, no-store"
        return await _protected_delete_team_skill(
            skill_id=skill_id,
            request=request,
            user=user,
            store=store,
        )
    granted = await _granted_skill_ids(request, user)
    is_admin = await _is_team_admin(store, user)
    if existing is None or not _visible_to(existing, user, granted, is_admin=is_admin):
        raise skill_workflow_http_error(404, "team_skill_not_found")
    # B2 (content-permissions): deletion requires owner ∨ team_admin/creator; same as patch comment.
    if not await can_manage_shares(store, user, existing.created_by):
        raise skill_workflow_http_error(403, "team_skill_delete_forbidden")
    if resource_impact_guard_enabled():
        response.headers["Cache-Control"] = "private, no-store"
        async with team_skill_lock(user.team_id, existing.name):
            return await _protected_delete_team_skill(
                skill_id=skill_id,
                request=request,
                user=user,
                store=store,
            )
    # Lock DELETE (tech-design P1b): without it, DELETE races approve's disk replacement; DB CAS rollback's
    # reverse rename revives deleted directories as orphans.
    async with team_skill_lock(user.team_id, existing.name):
        ok = await store.delete_team_skill(user.team_id, skill_id)
        if not ok:
            raise skill_workflow_http_error(404, "team_skill_not_found")
        _safe_rmtree(user.team_id, existing.source_path)  # Delete SKILL.md + scripts (best effort, path-escape protection).
        _safe_rmtree(user.team_id, existing.candidate_path)  # Candidate directory (pending/rejected).
        _safe_rmtree(user.team_id, str(_TEAM_SKILLS_ROOT / user.team_id / f"{existing.name}__old"))  # Approve residue.
    # content-permissions S4 cascade (PRD §4.7): deletion revokes all allow grants. Best effort.
    try:
        access = getattr(request.app.state, "access_control_store", None)
        if access is not None:
            await access.revoke_grants_for_resource(user.team_id, "team_skill", skill_id)
    except Exception:  # noqa: S110
        pass
    await _audit(store, user, AuditAction.TEAM_SKILL_DELETED, existing)
    return None


def _safe_rmtree(team_id: str, source_path: str | None) -> None:
    """只删本 team 目录树（``_TEAM_SKILLS_ROOT/<team_id>``）下的目录.

    source_path 服务端 set，此为 path-escape 防护兜底——安全 review P3：原先只锁到
    team_skills 根，DB 被污染时可跨 team 删；现与读侧 ``_team_dir`` 同域。"""
    if not source_path:
        return
    try:
        p = Path(source_path).resolve()
        root = (_TEAM_SKILLS_ROOT / team_id).resolve()
        if root == p or root in p.parents:
            shutil.rmtree(p, ignore_errors=True)
    except Exception:  # noqa: S110
        pass


async def _audit(
    store: UserStore,
    user: User,
    action: str,
    sk: TeamSkill,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort OPS audit（inline-await，mirror team_subagents）—— never block the op；NO secret."""
    with contextlib.suppress(Exception):
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=action,
            target_kind="team_skill",
            target_id=sk.id,
            target_team_id=user.team_id,
            scope="team",
            payload={"name": sk.name, **(extra or {})},
        )


async def _notify_approval(
    request: Request,
    *,
    store: UserStore,
    actor: User,
    sk: TeamSkill,
    event: str,  # "submitted" | "decided"
    decision: str | None = None,  # approved | rejected (when event=decided).
    reason: str | None = None,
) -> None:
    """审批通知（沿 shares._notify_share 先例：upsert_item + add_recipients 定向机制）.

    dedupe_key 按事件类分键（review P1-3：单键会让裁决 upsert 改写 admin 手里的待审 item）：
    提交=``skill_approval:submitted:<id>:<ts>``（→admins）；裁决=``skill_approval:decided:
    <id>:<ts>``（→作者）。best-effort，失败吞掉（与审计同策略）；作者退团/查无收件人则
    静默跳过（PRD §2.4）。文案 zh 硬编码同 _notify_share 先例，i18n 随 P3（§3.7）。"""
    try:
        ns = getattr(request.app.state, "notification_store", None)
        if ns is None:
            return
        label = sk.display_name or sk.name
        if event == "submitted":
            recipients = await store.list_team_users_by_roles(sk.team_id, {"owner", "team_admin"})
            recipients = [r for r in recipients if r != actor.id]
            title = f"{actor.display_name or actor.email} 提交了 skill「{label}」待审批"
            action_url = "/team/settings/workflows"
            ts = sk.candidate_submitted_at or sk.updated_at or _now()
            dedupe_key = f"skill_approval:submitted:{sk.id}:{ts}"
            ntype = "skill_approval_submitted"
        else:
            author = sk.submitted_by or sk.created_by
            recipients = [author] if author and author != actor.id else []
            if decision == "approved":
                title = f"你的 skill「{label}」已通过审批"
            else:
                title = f"你的 skill「{label}」被拒绝：{(reason or '').strip()[:80]}"
            action_url = "/skills/"
            dedupe_key = f"skill_approval:decided:{sk.id}:{_now()}"
            ntype = "skill_approval_decided"
        if not recipients:
            return
        item = await ns.upsert_item(
            team_id=sk.team_id,
            category="share",
            type=ntype,
            priority="normal",
            title=title,
            body=None,
            subject_type="team_skill",
            subject_id=sk.id,
            actor_user_id=actor.id,
            action_url=action_url,
            actions=None,
            metadata={
                "kind": "team_skill",
                "resource_id": sk.id,
                "event": event,
                "decision": decision,
                "actorName": actor.display_name or actor.email,
                "skillLabel": label,
                "reason": (reason or "").strip()[:80],
            },
            dedupe_key=dedupe_key,
        )
        await ns.add_recipients(item.id, [(r, None) for r in recipients])
    except Exception:  # Notification failures do not block approval operations.
        return
