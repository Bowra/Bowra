"""教练办案（B 线批 3a，tech-design §2.8.3）——后台把候选物化成预填+报告。

硬规则：
- **证据按贡献者身份求交（P0）**：`provenance_refs` 是贡献者自报的 turn id；一条 evidence 的 ref
  只有在 `turn.session.user_id == 该条 contributor_id` 且 session.workspace→team_id == 候选 team_id
  时才读（turn→session→workspace 链，`sessions` 表没有 team_id）。不满足＝丢弃、不计数。
  教练永远只看到「贡献者本来就能看的那一轮」。
- 配置面走 `feedback_surfaces` 唯一实现，以资源 owner 身份跑可见性门；系统 actor 不比 owner 看得更多。
- 模型走 `resolve_team_router`（绝不回退进程级 router）；后台调用只计量不计费（`usage_context(reason=…)`）。
- 402 明示＝起草前显式查团队余额（router 没有额度异常）；模型未配置与余额为零都在 **claim 之前**判，不占互斥也不占日 cap。
- 改道目标由 coach_drafting 按清单做服务端集合判定；展示名永远不信模型。
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from typing import Any

from bowhead_core.coach_drafting import COACH_AGENT_TYPE, draft_config_edit_ex
from bowhead_core.feedback_candidate_store import FeedbackCandidate, scrub_deep
from bowhead_core.feedback_casework_store import FeedbackCaseworkStore
from bowhead_core.prompt_injection_defense import sanitize_for_injection
from bowhead_router.usage import usage_context

from bowhead_gateway.feedback_surfaces import SurfaceError, build_surface, resolve_casework_actor
from bowhead_gateway.routes._deps import resolve_team_router

logger = logging.getLogger(__name__)

EVIDENCE_REF_CAP = 5
EVIDENCE_CHAR_CAP = 4_000
_SANITIZER_MARK = "[[escaped:"

REASON_COACH_UNAVAILABLE = "coach_unavailable"
REASON_SURFACE_TOO_LARGE = "surface_too_large"
REASON_MODEL_UNCONFIGURED = "team_model_unconfigured"
REASON_CREDIT_BLOCKED = "credit_blocked"
REASON_URL_GUARD = "url_guard"
REASON_TARGET_UNAVAILABLE = "target_unavailable"


@dataclass(frozen=True)
class Evidence:
    excerpts: list[str]
    used: int
    dropped: int


async def load_evidence(session_store: Any, user_store: Any, cand: FeedbackCandidate) -> Evidence:
    """按贡献者身份求交读取来源对话；任何一环对不上就丢弃该条。"""

    excerpts: list[str] = []
    used = 0
    dropped = 0
    seen = 0
    for entry in cand.evidence:
        contributor = entry.get("contributor_id")
        for ref in entry.get("provenance_refs") or []:
            if seen >= EVIDENCE_REF_CAP:
                break
            seen += 1
            if not isinstance(ref, str) or not ref.startswith("t_") or not isinstance(contributor, str):
                dropped += 1
                continue
            try:
                turn = await session_store.get_turn(ref)
                session = await session_store.get_session(turn.session_id) if turn is not None else None
                workspace = await user_store.get_workspace(session.workspace_id) if session is not None else None
            except Exception:
                logger.exception("feedback casework evidence lookup failed ref=%s", ref)
                dropped += 1
                continue
            team_id = workspace.get("team_id") if isinstance(workspace, dict) else None
            if session is None or turn is None or session.user_id != contributor or team_id != cand.team_id:
                dropped += 1
                continue
            if workspace.get("status") == "archived" or not await user_store.workspace_in_team(cand.team_id, session.workspace_id):
                dropped += 1
                continue
            if await user_store.get_team_role(contributor, cand.team_id) is None:
                # Same chain as turns' `_require_turn_access`: departed members' conversations must no longer reach the coach (review P2-3).
                dropped += 1
                continue
            text = f"用户：{turn.user_text or ''}\n助手：{turn.final_answer or ''}"
            text = scrub_deep(text)[:EVIDENCE_CHAR_CAP]
            if _SANITIZER_MARK in unicodedata.normalize("NFKC", text):
                # Contributor inserted the marker in their turn: drop it so the marker gate does not reject the entire draft.
                dropped += 1
                continue
            excerpts.append(sanitize_for_injection(text))
            used += 1
    return Evidence(excerpts=excerpts, used=used, dropped=dropped)


@dataclass(frozen=True)
class CaseworkOutcome:
    status: str  # done / failed / lost
    reason_code: str | None = None


async def resolve_coach_router(app_state: Any, team_id: str) -> Any | None:
    """团队教练模型；None＝未配置（调用方在 claim 前就 block，不占 claim 也不占 cap）。"""

    router, miss = await resolve_team_router(app_state, team_id, agent_type=COACH_AGENT_TYPE, site="feedback_casework")
    if router is None:
        logger.info("feedback casework: team model unconfigured team=%s miss=%s", team_id, miss)
    return router


async def run_casework(
    *,
    router: Any,
    user_store: Any,
    session_store: Any,
    casework_store: FeedbackCaseworkStore,
    cand: FeedbackCandidate,
    trigger: str = "cron",
) -> CaseworkOutcome:
    """对一条已 claim 的候选跑一次办案；结果落 sidecar 行。调用方负责 claim、模型解析与疲劳闸。

    所有写回都 ``running_only``：采纳路由若在此期间 `replace_cache` 覆盖了行，后台结果作废（outcome=lost）。
    """

    actor = await resolve_casework_actor(user_store, cand)
    if not actor:
        await casework_store.fail(cand.id, reason_code=REASON_TARGET_UNAVAILABLE, running_only=True)
        return CaseworkOutcome("failed", REASON_TARGET_UNAVAILABLE)
    try:
        surface = await build_surface(user_store, cand, actor_user_id=actor)
    except SurfaceError as exc:
        await casework_store.fail(cand.id, reason_code=exc.code, running_only=True)
        return CaseworkOutcome("failed", exc.code)
    if not surface.fits:
        await casework_store.fail(cand.id, reason_code=REASON_SURFACE_TOO_LARGE, running_only=True)
        return CaseworkOutcome("failed", REASON_SURFACE_TOO_LARGE)

    evidence = await load_evidence(session_store, user_store, cand)
    await casework_store.mark_coaching(cand.id)  # Daily cap counts only calls reaching here.
    with usage_context(team_id=cand.team_id, reason="feedback_coach"):
        result = await draft_config_edit_ex(
            router,
            target_kind=cand.target_kind,
            target_ref=cand.target_ref,
            config_surface=surface.config_surface,
            proposed=cand.proposed,
            rubric_text=cand.rubric_text,
            evidence_excerpts=evidence.excerpts,
            mounted_assets=surface.mounted_assets,
        )
    report_base: dict[str, Any] = {"evidence_used": evidence.used, "evidence_dropped": evidence.dropped, "trigger": trigger}
    draft = result.draft
    if draft is None:
        reason = REASON_URL_GUARD if result.failure == "url_guard" else REASON_COACH_UNAVAILABLE
        written = await casework_store.fail(cand.id, reason_code=reason, report={**report_base, "failure": result.failure}, running_only=True)
        return CaseworkOutcome("failed", reason) if written else CaseworkOutcome("lost")
    if draft.verdict == "reroute":
        written = await casework_store.finish(
            cand.id,
            report={
                **report_base,
                "verdict": "reroute",
                "rationale": draft.rationale,
                "reroute": {
                    "reason": draft.reroute_reason,
                    "target_kind": draft.reroute_target_kind,
                    "target_ref": draft.reroute_target_ref,
                },
            },
            prefill=None,
            base=surface.base,
            running_only=True,
        )
        return CaseworkOutcome("done") if written else CaseworkOutcome("lost")
    written = await casework_store.finish(
        cand.id,
        report={**report_base, "verdict": "edit", "rationale": draft.rationale},
        prefill={"kind": surface.prefill_kind, "edited_content": draft.edited_content, "rationale": draft.rationale},
        base=surface.base,
        running_only=True,
    )
    return CaseworkOutcome("done") if written else CaseworkOutcome("lost")
