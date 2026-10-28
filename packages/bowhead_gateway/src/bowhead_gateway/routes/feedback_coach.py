"""教练直连面（agent-feedback-loop 批 3d，skill-import 线接口请求 §1/§1b）。

个人资产直办（拍板④「用户调→个人资产直办」）：不进候选、不审批、不落草稿对象。调用者对**自己**处在
`pending/overridden` 的导入 skill 请教练起草整份 SKILL.md；同步返回预填与正文基线指纹，对比页在本页比对，
保存走 skill-import 线既有的 `PUT /skills/{name}/body`（写即重跑清点；状态由他们的钩子重算）。

- 权限：只到本人（不存在与无权同 404）；团队 router；`usage_context(reason="feedback_coach_direct", user_id=本人)`。
- 节流：每人每 skill 每 UTC 日 ≤3 次，复用 casework sidecar 的 redraft 槽（行 id 命名空间 `usk_<sha16>`；
  该行永远不会进 worker 池——池只按 open 候选 JOIN）。模型未配置（409）、正文过长（degraded）在占槽之前判；
  教练零产出（超时/解析失败/上游全败）退槽——用户不该为三次空跑被锁一天。
- 产出后服务端再守三道结构闸：frontmatter `name` 不变（保存路径同一把尺子）、`allowed-tools` 只能来自
  本平台真实名单∪原有集合（不许编名字）、零 diff 不给预填。都不是词表判散文，全是结构事实。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from bowhead_core.coach_drafting import COACH_AGENT_TYPE, draft_config_edit_ex
from bowhead_core.feedback_casework_store import FeedbackCaseworkStore
from bowhead_core.user_store import User
from bowhead_router.usage import usage_context
from fastapi import APIRouter, Depends, HTTPException, Request

from bowhead_gateway.feedback_surfaces import (
    SurfaceError,
    allowed_tools_of,
    build_user_skill_surface,
    frontmatter_of,
    user_skill_evidence,
)
from bowhead_gateway.routes._deps import team_router_or_409
from bowhead_gateway.routes.auth import _current_user

logger = logging.getLogger(__name__)

# Same untrusted-content banner as §2.7's shared component, shown unchanged on comparison page.
BANNER_TEXT = "此内容由教练起草，来源不可信，保存前逐行审阅"
USER_SKILL_DRAFT_TIMEOUT_SEC = 240.0  # Entire SKILL.md rewrite + full fallback chain (116 observed 141.7s; 120s insufficient).

_PROPOSED = {"text": "把这份导入的 skill 改成在本平台能跑通的样子：清零红色阻断项，黄色只说清不删。"}
_RUBRIC = "红色阻断项清零；frontmatter 的 name 逐字不变；不改口径、数字与范围；黄色项不删；说明按「改了什么／没改什么为什么／你还需要做什么」三段写。"
_NO_PRODUCT_FAILURES = frozenset({"timeout", "invoke_failed", "parse_failed"})  # No coach output: refund slot.


router = APIRouter()


def _throttle_key(user_id: str, skill_name: str) -> str:
    return "usk_" + hashlib.sha256(f"{user_id}\x00{skill_name}".encode()).hexdigest()[:16]


def _degraded(reason: str, *, base: dict[str, Any], rationale: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"degraded": True, "degrade_reason": reason, "prefill": None, "base": base, "banner": {"text": BANNER_TEXT}}
    if rationale:
        out["rationale"] = rationale
    return out


async def _resolution_sources(request: Request, user: User) -> Any | None:
    """skill-import 线的解析源（本平台真实存在的工具/skill 名）；装不出来＝None，教练只能走「自然语言描述」那一档。"""

    try:
        from bowhead_gateway.skill_import_sources import build_resolution_sources

        return await build_resolution_sources(request, user)
    except Exception:
        logger.warning("feedback_coach.resolution_sources_unavailable", exc_info=True)
        return None


@router.post("/user-skills/{skill_name}/draft")
async def draft_user_skill(
    skill_name: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    sis = getattr(request.app.state, "skill_instance_store", None)
    if sis is None:
        raise HTTPException(status_code=503, detail={"code": "feedback_store_unavailable"})
    sources = await _resolution_sources(request, user)
    try:
        us = await build_user_skill_surface(sis, user_id=user.id, skill_name=skill_name, sources=sources)
    except SurfaceError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from exc
    surface = us.surface
    if not surface.fits:
        return _degraded("surface_too_large", base=surface.base)

    llm_router = await team_router_or_409(request.app.state, user.team_id, agent_type=COACH_AGENT_TYPE, site="feedback_coach_direct")
    cw_store: FeedbackCaseworkStore | None = getattr(request.app.state, "feedback_casework_store", None)
    slot = _throttle_key(user.id, skill_name)
    if cw_store is not None and not await cw_store.take_redraft_slot(slot, user.team_id):
        raise HTTPException(status_code=429, detail={"code": "feedback_redraft_throttled"})  # Unconfigured-model 409 consumes no slot.

    with usage_context(team_id=user.team_id, user_id=user.id, reason="feedback_coach_direct"):
        result = await draft_config_edit_ex(
            llm_router,
            target_kind="user_skill",
            target_ref=skill_name,
            config_surface=surface.config_surface,
            proposed=_PROPOSED,
            rubric_text=_RUBRIC,
            evidence_excerpts=user_skill_evidence(us),
            mounted_assets=[],
            timeout_sec=USER_SKILL_DRAFT_TIMEOUT_SEC,
        )
    draft = result.draft
    if draft is None:
        if result.failure in _NO_PRODUCT_FAILURES and cw_store is not None:
            await cw_store.release_redraft_slot(slot)
        reason = "url_guard" if result.failure == "url_guard" else "coach_unavailable"
        return _degraded(reason, base=surface.base)
    if draft.verdict != "edit" or not draft.edited_content:
        return _degraded("coach_rerouted", base=surface.base)

    edited = draft.edited_content.replace("\r\n", "\n")
    fm = frontmatter_of(edited)
    if str(fm.get("name") or "").strip() != skill_name:
        # Save rejects renamed/missing-frontmatter writes; do not return a draft doomed to fail saving.
        return _degraded("name_changed", base=surface.base, rationale=draft.rationale)
    invented = allowed_tools_of(fm) - us.original_allowed_tools - us.available_tools
    if invented and us.available_tools:
        # Structural gate: allowed-tools from real platform list ∪ original set only. Skip check if available cannot resolve (underreport, avoid false rejection).
        return {**_degraded("invented_tool", base=surface.base, rationale=draft.rationale), "invented": sorted(invented)}
    if edited.strip() == us.body_raw.strip():
        # Coach cannot change it: no zero-diff prefill implying useful work remains.
        return _degraded("no_change", base=surface.base, rationale=draft.rationale)
    addressed = [fid for fid in draft.findings_addressed if fid in us.finding_ids]  # Accept only IDs actually present in the report.
    return {
        "degraded": False,
        "prefill": {"kind": "user_skill_body", "edited_content": edited, "rationale": draft.rationale},
        "base": surface.base,
        "report": {
            "red_count": len(us.findings_red),
            "yellow_count": len(us.findings_yellow),
            "truncated": us.truncated,
            "findings_addressed": addressed,
        },
        "banner": {"text": BANNER_TEXT},
    }
