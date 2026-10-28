"""Lead 侧工具 `feedback_escalate`（agent-feedback-loop 批 3c，tech-design §2.8.1 触发面③「chat 点名」）。

用户在对话里**明确要求**把一条改进诉求提交给团队（「把这条提给管理员」「让教练查清楚为什么」）时，
Lead 调它。它是收件箱的第三个进料口，与成员侧确认卡（memory 线 1c）**走同一个写入口**
`FeedbackCandidateStore.propose`——一个概念一实现，采集侧永不碰内容。

拍板落地（memory-agent-prompt.md §一）：
- 升团队域必须「确认 + 签字」：签字＝判据（怎样算改对了）。没有判据 ⇒ 不写、返回 `needs_rubric`，
  Lead 只问一句（裁决 1「低置信时问一句」）。用户明说要提交＝确认。
- 目标只收用户说得出口的**名字**（agent/技能展示名、口径名），id 由服务端解析（`switch` 工具同款：
  模型不给 id、解析与授权全在服务端）；解析不到 ⇒ `target_not_found`，让 Lead 问用户。
- `deep_dive` 是结构事实（用户明确要求查清楚为什么），由 Lead 按用户原话置位，**不得用短语表**判。
- 同签名候选已存在 ⇒ 并入证据；本人已提过 ⇒ 只翻 `deep_dive`（`mark_deep_dive`），不重复计数。
- 结果不走新 SSE：回执由既有通知中心（accepted/rejected/expired）回流；本轮由 Lead 转述工具消息。

形制＝`config_copilot_chat_tools`：注册期读 flag（工具 schema 是进 LLM 的静态契约，不能一 turn 一形状）、
provider 闭包在调用期取 store（lifespan 顺序解耦）、`allowed_for_roles={"lead"}`。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from bowhead_router.types import ToolSchema

from bowhead_core.feedback_candidate_store import (
    FeedbackCandidateStore,
    FeedbackLoopDisabled,
    QuotaExceeded,
    RubricRejected,
    SuppressedSignature,
)
from bowhead_core.feedback_target_resolver import AmbiguousTarget  # Originally in this module; moved to resolver on 2026-09-13
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME: Final = "feedback_escalate"

# Target kinds: only named team assets. tool_source/retire use native entries, not chat (different payload shapes).
_TARGET_KINDS: Final = ("agent_def", "team_skill", "semantic", "new_asset")
_RESOLVED_KINDS: Final = frozenset({"agent_def", "team_skill"})
_REQUEST_MAX = 2000
_RUBRIC_MAX = 1000
_NAME_MAX = 200


# Resolver: (team_id, kind, name) -> target_ref | None; duplicate names raise AmbiguousTarget. Gateway resolves display names via team tables.
TargetResolver = Callable[[str, str, str], Awaitable[str | None]]

_store_provider: Callable[[], FeedbackCandidateStore | None] | None = None
_resolver_provider: Callable[[], TargetResolver | None] | None = None


def set_feedback_escalate_providers(
    *,
    store_provider: Callable[[], FeedbackCandidateStore | None] | None,
    resolver_provider: Callable[[], TargetResolver | None] | None,
) -> None:
    global _store_provider, _resolver_provider  # noqa: PLW0603 -- Module providers, same shape as config_copilot_chat_tools
    _store_provider = store_provider
    _resolver_provider = resolver_provider


_MSG_NEEDS_RUBRIC: Final = "还没有判据。先问用户一句「怎样算改对了？」（一句话即可），拿到后把它作为 rubric 再调用一次。不要自己替用户编判据。"
_MSG_NOT_EXPLICIT: Final = "只有用户明确要求提交/升级这条建议时才调用；否则请先确认用户是否要提交。"
_MSG_STORE_MISSING: Final = "建议收件箱当前不可用，如实告知用户，稍后可再试。"
_MSG_TARGET_NOT_FOUND: Final = (
    "团队里找不到这个名字的目标。问用户这条建议是针对哪个 Agent 或哪项技能（用他们叫得出的名字），或改为业务口径（semantic）/新建资产（new_asset）。"
)
_MSG_SUPPRESSED: Final = "同样的建议最近被团队拒绝过，冷却期内不再重复提交；如实告知用户，并说明若有更多同事支持可再提。"
_MSG_QUOTA: Final = "今日提交已达上限（或团队待审建议已满）；如实告知用户，明天可再提。"
_MSG_REJECTED_INPUT: Final = "这段内容含有系统保留的标记文本，无法原样提交；如实告知用户这条不能提交，不要替他改写。"
_MSG_AMBIGUOUS: Final = "团队里有多个同名目标：{names}。问用户指的是哪一个，再调用。"
_MSG_CHANNEL: Final = "这里不是网页对话（定时任务/IM 渠道），不能代用户提交建议；如实告知用户请在网页对话里提。"
_EVIDENCE_PRIOR_TURNS = 1  # Include the preceding N turns in this session too: the submission turn often omits the issue to change.


def _schema() -> ToolSchema:
    return ToolSchema(
        name=TOOL_NAME,
        description=(
            "把用户在对话里明确要求提交的改进建议送进团队「建议收件箱」，由有权者审批、教练先起草。"
            "只在用户明确说要提交/升级/让团队处理时调用；request 必须用用户原话；rubric 是用户说的「怎样算改对了」，"
            "没有就先问用户一句再调用；target 只填用户叫得出的名字，不要填 id；"
            "用户明确要求「查清楚为什么」时 deep_dive=true。调用后用一句话向用户转述结果，不承诺时间。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "用户原话：要改什么（不要改写、不要总结）", "maxLength": _REQUEST_MAX},
                "rubric": {"type": "string", "description": "用户说的判据：怎样算改对了。没有就留空并先问用户", "maxLength": _RUBRIC_MAX},
                "target": {
                    "type": "object",
                    "description": "建议指向的目标：kind + 用户叫得出的名字（Agent/技能展示名、口径名、新资产名）",
                    "properties": {
                        "kind": {"type": "string", "enum": list(_TARGET_KINDS)},
                        "name": {"type": "string", "maxLength": _NAME_MAX},
                    },
                    "required": ["kind", "name"],
                },
                "deep_dive": {"type": "boolean", "description": "用户明确要求查清楚为什么/彻底看一下时为 true", "default": False},
                "user_asked_to_submit": {"type": "boolean", "description": "用户是否明确要求提交（必须为 true 才会提交）"},
            },
            "required": ["request", "target", "user_asked_to_submit"],
        },
    )


def _fail(error: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message}


def _interactive_channel(ctx: ToolExecutionContext) -> bool:
    """今天仓里唯一「有人在网页对话里说话」的通道信号（config_propose 同门）：
    web chat / 澄清 resume 为 True；定时任务、飞书为 False。v1 窄化：IM 渠道暂不代提交。"""

    return bool(getattr(ctx, "config_lite_supported", False))


async def _evidence_refs(ctx: ToolExecutionContext, turn_id: str) -> list[str]:
    """本轮 + 同会话紧邻前几轮的 turn id。同会话＝同一用户，casework 的贡献者求交仍然成立。"""

    refs = [turn_id] if turn_id else []
    session_store = getattr(ctx, "session_store", None)
    session_id = str(getattr(ctx, "session_id", "") or "")
    if session_store is None or not session_id:
        return refs
    try:
        turns = await session_store.list_turns(session_id=session_id, limit=_EVIDENCE_PRIOR_TURNS + 1)
    except Exception:
        logger.warning("feedback_escalate.prior_turns_failed session=%s", session_id, exc_info=True)
        return refs
    for t in turns:
        tid = str(getattr(t, "id", "") or getattr(t, "turn_id", "") or "")
        if tid and tid not in refs:
            refs.append(tid)
    return refs[: _EVIDENCE_PRIOR_TURNS + 1]


async def _handler(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    if not _interactive_channel(ctx):
        return _fail("channel_not_supported", _MSG_CHANNEL)
    if not bool(arguments.get("user_asked_to_submit")):
        return _fail("not_explicit", _MSG_NOT_EXPLICIT)
    request = str(arguments.get("request") or "").strip()[:_REQUEST_MAX]
    rubric = str(arguments.get("rubric") or "").strip()[:_RUBRIC_MAX]
    if not request:
        return _fail("bad_arguments", "request 不能为空：用用户原话。")
    if not rubric:
        return _fail("needs_rubric", _MSG_NEEDS_RUBRIC)
    target = arguments.get("target") if isinstance(arguments.get("target"), dict) else {}
    kind = str(target.get("kind") or "")
    name = str(target.get("name") or "").strip()[:_NAME_MAX]
    if kind not in _TARGET_KINDS or not name:
        return _fail("bad_arguments", "target 需要 kind（agent_def/team_skill/semantic/new_asset）和用户叫得出的名字。")
    team_id = str(getattr(ctx, "team_id", "") or "")
    user_id = str(getattr(ctx, "user_id", "") or "")
    turn_id = str(getattr(ctx, "turn_id", "") or "")
    if not team_id or not user_id:
        return _fail("no_team_context", _MSG_STORE_MISSING)
    store = _store_provider() if _store_provider is not None else None
    if store is None:
        return _fail("store_unavailable", _MSG_STORE_MISSING)

    target_ref = name
    if kind in _RESOLVED_KINDS:
        resolver = _resolver_provider() if _resolver_provider is not None else None
        try:
            target_ref = await resolver(team_id, kind, name) if resolver is not None else None
        except AmbiguousTarget as exc:
            return _fail("target_ambiguous", _MSG_AMBIGUOUS.format(names="、".join(exc.names)))
        except Exception:
            logger.exception("feedback_escalate.resolve_failed team=%s kind=%s", team_id, kind)
            return _fail("propose_failed", _MSG_STORE_MISSING)
        if not target_ref:
            return _fail("target_not_found", _MSG_TARGET_NOT_FOUND)

    deep_dive = bool(arguments.get("deep_dive"))
    refs = await _evidence_refs(ctx, turn_id)
    # ★ Sign **server-held verbatim user text** (`ctx.user_text`), not model-filled `request`.
    # Observed on 116 (memory work, 2026-09-10): despite schema requiring verbatim text, Lead submitted a paraphrase,
    # stripping "把这条提给管理员。判据：…". The signature is both a **cooldown key** (no resubmission for 30 days after rejection)
    # and recurrence key: model wording cannot reliably determine safety keys; changing paraphrases of one original changes signatures, bypassing cooldown.
    # `user_text` is an exact copy of this turn's TurnRequest.text (tool_registry notes that Lead paraphrasing is a lossy channel).
    # If unavailable (old callers/tests), fall back to request, at least no worse than before.
    sig_source = str(getattr(ctx, "user_text", "") or "").strip() or request
    try:
        cand, outcome = await store.propose(
            team_id=team_id,
            target_kind=kind,
            target_ref=target_ref,
            proposed={"text": request, "deep_dive": deep_dive, "origin": "chat_escalate"},
            rubric_text=rubric,
            contributor_id=user_id,
            sig_text=sig_source,
            provenance_refs=refs or None,
        )
    except FeedbackLoopDisabled:
        return _fail("feature_off", _MSG_STORE_MISSING)
    except SuppressedSignature:
        return _fail("signature_suppressed", _MSG_SUPPRESSED)
    except QuotaExceeded as exc:
        return _fail(f"quota_{exc.which}", _MSG_QUOTA)
    except RubricRejected:
        return _fail("rejected_input", _MSG_REJECTED_INPUT)
    except Exception:
        logger.exception("feedback_escalate.propose_failed team=%s", team_id)
        return _fail("propose_failed", _MSG_STORE_MISSING)

    if deep_dive and outcome in ("merged", "duplicate") and cand.proposed.get("deep_dive") is not True:
        try:
            cand = await store.mark_deep_dive(cand.id, by=user_id)
        except Exception:
            logger.warning("feedback_escalate.mark_deep_dive_failed id=%s", cand.id, exc_info=True)

    flagged = cand.proposed.get("deep_dive") is True  # Report only persisted facts (review P1: no promises based on inputs).
    if outcome == "created":
        message = "已提交到团队建议收件箱，有权审批的人会看到；教练会先起草改法。用一句话向用户转述，说明结果会以通知回来，不承诺时间。"
    elif outcome == "merged":
        message = f"团队里已有同一条建议，已把用户的这次作为新证据并入（现在 {cand.evidence_count} 人背书）。向用户如实转述。"
    else:
        message = "用户之前已经提过同一条建议，本次不重复计数。"
        if deep_dive:
            # deep_dive sets sorting priority, not deeper investigation; do not promise users deeper coaching review.
            message += "已提高这条的处理优先级。" if flagged else "本次未能提高优先级（这条可能已不在待审状态）。"
        message += " 向用户如实转述。"
    return {
        "ok": True,
        "candidate_id": cand.id,
        "outcome": outcome,
        "target_kind": kind,
        "evidence_count": cand.evidence_count,
        "deep_dive": flagged,
        "message": message,
    }


def register_feedback_escalate_tool(
    registry: ToolRegistry,
    *,
    store_provider: Callable[[], FeedbackCandidateStore | None] | None = None,
    resolver_provider: Callable[[], TargetResolver | None] | None = None,
) -> bool:
    """功能已正式发布；权限、作用域和存储就绪校验仍须执行。"""

    set_feedback_escalate_providers(store_provider=store_provider, resolver_provider=resolver_provider)
    registry.register(
        ToolSpec(
            schema=_schema(),
            handler=_handler,
            needs_approval=False,
            risk_level="low",
            allowed_for_roles=frozenset({"lead"}),
            visible_to_llm=True,
        ),
    )
    return True
