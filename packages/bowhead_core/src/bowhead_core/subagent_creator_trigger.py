"""Conversational ``subagent_creator`` trigger.

The trigger first collects a sufficient business purpose, then creates an owner/team-bound
review draft and emits only a durable draft pointer; when no draft runtime is available
for the turn it opens the manual card with a purpose hint. It never puts a full
definition, prompt, or tool reference into Chat events (the card may receive
only short identity hints the user stated: a slug-shaped name and a display name), and final creation still goes
through the gateway's authenticated team-subagent write boundary.

Gateway authorization, team writability, entitlement, catalog resolution,
validation, and final creation remain server-enforced.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Final

from bowhead_router.types import ToolSchema

from bowhead_core.events import make_event
from bowhead_core.subagent_authoring_rollout import (
    resolve_subagent_authoring_capabilities,
)
from bowhead_core.subagent_draft_service import (
    DraftServiceOutcome,
)
from bowhead_core.subagent_purpose import (
    PurposeEvidence,
    PurposeGateOutcome,
    evaluate_purpose_evidence,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "subagent_creator"
_EVENT: Final = "cca.subagent_creator_open"  # Final → type checker infers the Literal (valid EventType)
_MAX_HINT = 200  # the purpose hint only prefills the description field; bound it before the SSE stream
# Identity hints prefill the card's name (slug) / display-name fields with what the user
# explicitly said. Same slug grammar as the create API; display name capped like the API.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_MAX_DISPLAY_NAME = 40


def subagent_creator_enabled() -> bool:
    """Shared authoring capability used by the trigger, CRUD routes and team metadata."""
    return resolve_subagent_authoring_capabilities("").creator


def _schema() -> ToolSchema:
    return ToolSchema(
        name=TOOL_NAME,
        description=(
            "打开「新建智能体 (subagent)」卡片，让成员把团队已有的工具组装成一个专属、可被路由点名调用的"
            "智能体（设定 slug、人设、工具束、模型、输出结构）。**用户说『做一个 / 建一个 智能体 / agent / "
            "专属助手 / 分析 agent / 做一个专门做 X 的助手』时调用本工具**——即使措辞像『做一个 X』，只要 X 是"
            "一个 agent / 助手 / 智能体，就用本工具。⚠️ 不要和 skill 混淆、不要走 skill-creator / skill_read："
            "智能体 = 带专属工具束 + 人设、可路由的 agent；skill = 可复用的 SOP 文档，两者不同。本工具只打开创建"
            "卡片，不直接创建——真正的创建在卡片里完成。若用户描述了用途，把一句话用途放进 `purpose` 预填。"
            "若 schema 包含 `action` 和 `purpose_evidence`，必须按用户原话填写；只有创建意图而没有用途时，"
            "使用 action=continue 并将 evidence 三项都设为 false，不要猜测业务领域。"
            "若当前对话已绑定右侧创建草稿、用户要求修改它，明确的新字段值直接用 action=edit 的"
            " draft_edits；在文本字段末尾追加内容用 draft_text_operations 的 append。"
            "只有必须先了解现值才能决定改法时才 action=inspect；只有 applied=true 才说已修改。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["continue", "manual", "cancel", "inspect", "edit"],
                    "description": "继续创建、手动配置、取消，或读取/修改当前对话绑定的右侧草稿。",
                },
                "purpose": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "可选：用户想要的这个 Agent 的用途；没有就留空。",
                },
                "purpose_evidence": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "source_or_object": {"type": "boolean"},
                        "action": {"type": "boolean"},
                        "expected_result": {"type": "boolean"},
                    },
                    "required": [
                        "source_or_object",
                        "action",
                        "expected_result",
                    ],
                },
                "name": {
                    "type": "string",
                    "maxLength": 64,
                    "description": ("仅当用户明确说出这个 Agent 的名字/标识时，原样照抄填写；用户没说就留空，不要自己起名。"),
                },
                "display_name": {
                    "type": "string",
                    "maxLength": 64,
                    "description": ("仅当用户另外明确给出展示名称时填写；没有就留空。"),
                },
                "clarification_answer": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": ("仅当上一轮已询问 Agent 的业务澄清问题时填写用户本轮答案；不要把它写进 purpose。"),
                },
                "draft_edits": {
                    "type": "object",
                    "description": "action=edit 时填写用户明确要求改变的完整字段值；未提到的字段不填。",
                    "properties": {
                        "name": {"type": "string"},
                        "display_name": {"type": "string"},
                        "description": {"type": "string"},
                        "system_prompt": {"type": "string"},
                        "model": {"type": ["string", "null"]},
                        "max_iterations": {"type": "integer"},
                        "output_schema": {"type": ["object", "null"]},
                        "selected_tool_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
                "draft_text_operations": {
                    "type": "array",
                    "maxItems": 8,
                    "description": "可选：对当前已保存文本字段做原子追加。不要先猜原文再拼接；与 draft_edits 不可修改同一字段。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": ["name", "display_name", "description", "system_prompt"]},
                            "operation": {"type": "string", "enum": ["append"]},
                            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                        },
                        "required": ["field", "operation", "text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )


def _identity_hints(args: dict[str, Any]) -> tuple[str | None, str | None]:
    """Name hints the user stated explicitly, for the manual card's name fields.

    A slug-shaped ``name`` prefills the slug; any other stated name (e.g. Chinese) is a
    display name. Only short identity strings travel — never prompts or tool refs.
    """
    raw_name = args.get("name")
    raw_display = args.get("display_name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    display = raw_display.strip() if isinstance(raw_display, str) else ""
    slug: str | None = None
    if name:
        candidate = re.sub(r"\s+", "-", name).lower()
        if _NAME_RE.fullmatch(candidate):
            slug = candidate
        elif not display:
            display = name
    display_hint = display[:_MAX_DISPLAY_NAME] if display else None
    return slug, display_hint


async def _emit_open(
    *,
    bus: Any,
    turn_id: str,
    purpose_hint: str | None,
    draft_id: str | None = None,
    draft_status: str | None = None,
    draft_title: str | None = None,
    draft_version: int | None = None,
    name_hint: str | None = None,
    display_name_hint: str | None = None,
) -> bool:
    if bus is None:
        return False
    delivered = await bus.emit(
        make_event(
            _EVENT,
            turn_id,
            {
                "purpose_hint": (purpose_hint[:_MAX_HINT] if purpose_hint else None),
                "draft_id": draft_id,
                "draft_status": draft_status,
                "draft_title": (draft_title[:80] if draft_title else None),
                "draft_version": draft_version,
                "name_hint": name_hint,
                "display_name_hint": display_name_hint,
            },
            persist_before_delivery=draft_id is not None,
        )
    )
    return delivered is not False


def _purpose_evidence(value: Any) -> PurposeEvidence:
    raw = value if isinstance(value, dict) else {}
    return PurposeEvidence(
        source_or_object=raw.get("source_or_object") is True,
        action=raw.get("action") is True,
        expected_result=raw.get("expected_result") is True,
    )


async def _handler(
    args: dict[str, Any],
    ctx: ToolExecutionContext,
) -> dict[str, Any]:
    bus = getattr(ctx, "parent_bus", None)
    turn_id = getattr(ctx, "turn_id", "") or ""
    action = args.get("action", "continue")
    if action in {"inspect", "edit"}:
        editor = getattr(ctx, "subagent_draft_editor", None)
        if editor is None:
            if not resolve_subagent_authoring_capabilities(getattr(ctx, "team_id", "") or "").draft_chat_edit:
                return {
                    "ok": False,
                    "applied": False,
                    "outcome": "draft_chat_edit_unavailable",
                    "message": "Draft editing through Chat is unavailable. Edit the existing draft on the right; reopening it will not enable Chat editing. Do not claim any change was applied.",
                }
            return {"ok": False, "applied": False, "outcome": "draft_not_bound", "message": "当前对话没有连接可编辑的右侧草稿。"}
        if action == "inspect":
            return await editor.inspect()
        edits = args.get("draft_edits")
        operations = args.get("draft_text_operations")
        result = await editor.edit(
            edits if isinstance(edits, dict) else {},
            operations if isinstance(operations, list) else [],
        )
        if result.get("applied"):
            try:
                delivered = await _emit_open(
                    bus=bus,
                    turn_id=turn_id,
                    purpose_hint=None,
                    draft_id=result["draft_id"],
                    draft_status=result["draft_status"],
                    draft_title=result.get("draft_title"),
                    draft_version=result["version"],
                )
            except Exception as exc:
                logger.warning("subagent_creator.edit_event_failed turn=%s err=%s", turn_id, type(exc).__name__)
                delivered = False
            if not delivered:
                return {
                    "ok": False,
                    "applied": True,
                    "outcome": "draft_updated_refresh_required",
                    "message": "草稿已保存，但右侧未收到刷新事件。请刷新草稿后核对。",
                }
        return result
    if action not in {"continue", "manual", "cancel"}:
        return {
            "ok": False,
            "opened": False,
            "outcome": "failed",
            "message": "无法识别创建操作，请继续描述用途或选择手动配置。",
        }
    if action == "cancel":
        await _consume_context(ctx)
        return {
            "ok": True,
            "opened": False,
            "outcome": "cancelled",
            "message": "已取消创建 Agent。",
        }

    identity = _identity_hints(args)
    if action == "manual":
        await _consume_context(ctx)
        return await _open_manual(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=None,
            outcome="manual_opened",
            identity=identity,
        )

    context = await _authoring_context(ctx)
    if context is not None and context.state == "clarifying" and context.draft_id:
        answer = args.get("clarification_answer")
        if not isinstance(answer, str) or not answer.strip():
            answer = args.get("purpose")
        if not isinstance(answer, str) or not answer.strip():
            return {
                "ok": True,
                "opened": False,
                "outcome": "clarification_required",
                "message": "请回答上一轮关于这个 Agent 的问题；也可以选择手动配置。",
            }
        clarified = await _clarify_existing_draft(
            ctx=ctx,
            context=context,
            answer=answer,
            bus=bus,
            turn_id=turn_id,
            identity=identity,
        )
        if clarified is not None:
            return clarified

    raw_purpose = args.get("purpose")
    purpose = raw_purpose if isinstance(raw_purpose, str) else ""
    decision = evaluate_purpose_evidence(
        purpose,
        _purpose_evidence(args.get("purpose_evidence")),
    )
    if decision.outcome is PurposeGateOutcome.MANUAL:
        await _consume_context(ctx)
        return await _open_manual(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=None,
            outcome="manual_opened",
            identity=identity,
        )
    if decision.outcome is PurposeGateOutcome.CANCELLED:
        await _consume_context(ctx)
        return {
            "ok": True,
            "opened": False,
            "outcome": "cancelled",
            "message": "已取消创建 Agent。",
        }
    if decision.outcome is PurposeGateOutcome.PURPOSE_REQUIRED:
        recorded = await _record_purpose_prompt(ctx)
        if recorded is False:
            return _purpose_options()
        return {
            "ok": True,
            "opened": False,
            "outcome": "purpose_required",
            "message": "你希望这个 Agent 主要帮你完成什么工作？直接用日常语言描述就可以。",
        }
    if decision.outcome is PurposeGateOutcome.PURPOSE_INSUFFICIENT:
        recorded = await _record_insufficient_prompt(
            ctx,
            decision.normalized_purpose,
        )
        if recorded is False:
            return _purpose_options()
        return {
            "ok": True,
            "opened": False,
            "outcome": "purpose_insufficient",
            "message": "请再补充它要处理的业务对象、执行的动作或期望结果；也可以选择手动配置。",
        }
    if decision.outcome is PurposeGateOutcome.TOO_LONG:
        return {
            "ok": False,
            "opened": False,
            "outcome": "failed",
            "message": "用途描述不能超过 2,000 个字符，请缩短后重试。",
        }
    purpose_hint = decision.normalized_purpose

    generated = await _generate_draft(
        ctx=ctx,
        purpose=purpose_hint,
        bus=bus,
        turn_id=turn_id,
        identity=identity,
    )
    if generated is not None:
        return generated

    return await _open_manual(
        bus=bus,
        turn_id=turn_id,
        purpose_hint=purpose_hint,
        identity=identity,
    )


async def _open_manual(
    *,
    bus: Any,
    turn_id: str,
    purpose_hint: str | None,
    outcome: str = "fallback_opened",
    identity: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    if bus is None:
        return {
            "ok": False,
            "opened": False,
            "error": "无法打开创建卡片（当前会话不支持事件）。",
        }
    try:
        delivered = await _emit_open(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=purpose_hint,
            name_hint=identity[0],
            display_name_hint=identity[1],
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.emit_failed turn=%s err=%s",
            turn_id,
            type(exc).__name__,
        )
        delivered = False
    if not delivered:
        return {
            "ok": False,
            "opened": False,
            "outcome": "failed",
            "message": "打开创建卡片失败。",
        }
    return {
        "ok": True,
        "opened": True,
        "outcome": outcome,
        "message": "已打开「新建 Agent」卡片，请复核配置后再创建。",
    }


async def _authoring_context(ctx: ToolExecutionContext) -> Any | None:
    store = getattr(ctx, "subagent_authoring_context_store", None)
    team_id = getattr(ctx, "team_id", "") or ""
    user_id = getattr(ctx, "user_id", "") or ""
    session_id = getattr(ctx, "session_id", "") or ""
    if store is None or not team_id or not user_id or not session_id:
        return None
    try:
        return await store.get(
            team_id=team_id,
            owner_user_id=user_id,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.context_read_failed turn=%s err=%s",
            getattr(ctx, "turn_id", ""),
            type(exc).__name__,
        )
        return None


def _purpose_options() -> dict[str, Any]:
    return {
        "ok": True,
        "opened": False,
        "outcome": "purpose_options",
        "message": "用途仍不够具体。你可以继续描述、选择手动配置，或取消创建。",
        "actions": ["continue", "manual", "cancel"],
    }


async def _record_purpose_prompt(ctx: ToolExecutionContext) -> bool | None:
    store = getattr(ctx, "subagent_authoring_context_store", None)
    team_id = getattr(ctx, "team_id", "") or ""
    user_id = getattr(ctx, "user_id", "") or ""
    session_id = getattr(ctx, "session_id", "") or ""
    if store is None or not team_id or not user_id or not session_id:
        return None
    try:
        return (
            await store.record_purpose_prompt(
                team_id=team_id,
                owner_user_id=user_id,
                session_id=session_id,
            )
            is not None
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.context_write_failed turn=%s err=%s",
            getattr(ctx, "turn_id", ""),
            type(exc).__name__,
        )
        return None


async def _record_insufficient_prompt(
    ctx: ToolExecutionContext,
    purpose: str,
) -> bool | None:
    store = getattr(ctx, "subagent_authoring_context_store", None)
    team_id = getattr(ctx, "team_id", "") or ""
    user_id = getattr(ctx, "user_id", "") or ""
    session_id = getattr(ctx, "session_id", "") or ""
    if store is None or not team_id or not user_id or not session_id:
        return None
    try:
        return (
            await store.record_insufficient_prompt(
                team_id=team_id,
                owner_user_id=user_id,
                session_id=session_id,
                purpose_text=purpose,
            )
            is not None
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.context_write_failed turn=%s err=%s",
            getattr(ctx, "turn_id", ""),
            type(exc).__name__,
        )
        return None


async def _consume_context(ctx: ToolExecutionContext) -> None:
    context = await _authoring_context(ctx)
    store = getattr(ctx, "subagent_authoring_context_store", None)
    if context is None or store is None:
        return
    try:
        await store.consume(
            team_id=context.team_id,
            owner_user_id=context.owner_user_id,
            session_id=context.session_id,
            expected_version=context.row_version,
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.context_consume_failed turn=%s err=%s",
            getattr(ctx, "turn_id", ""),
            type(exc).__name__,
        )


async def _generate_draft(
    *,
    ctx: ToolExecutionContext,
    purpose: str,
    bus: Any,
    turn_id: str,
    identity: tuple[str | None, str | None],
) -> dict[str, Any] | None:
    service = getattr(ctx, "subagent_draft_service", None)
    team_id = getattr(ctx, "team_id", "") or ""
    user_id = getattr(ctx, "user_id", "") or ""
    session_id = getattr(ctx, "session_id", "") or ""
    if service is None or not team_id or not user_id or not session_id:
        return None
    idempotency_key = (
        "chatdraft-"
        + hashlib.sha256(
            f"{team_id}\0{user_id}\0{session_id}\0{turn_id}".encode(),
        ).hexdigest()
    )
    language = getattr(ctx, "language_context", None)
    locale = getattr(language, "ui_language", None) or getattr(language, "output_language", None) or "en-US"
    if resolve_subagent_authoring_capabilities(team_id).unified_draft:
        return await _generate_canvas_draft(
            ctx=ctx,
            service=service,
            purpose=purpose,
            locale=str(locale),
            idempotency_key=idempotency_key,
            bus=bus,
            turn_id=turn_id,
            identity=identity,
        )
    try:
        result = await service.create_intent_draft(
            team_id=team_id,
            owner_user_id=user_id,
            purpose=purpose,
            locale=str(locale),
            idempotency_key=idempotency_key,
            actor_active=True,
            writable=True,
            authoring_enabled=True,
            mcp_entitled=bool(
                getattr(ctx, "subagent_draft_mcp_entitled", False),
            ),
            source_turn_id=turn_id,
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.draft_failed turn=%s err=%s",
            turn_id,
            type(exc).__name__,
        )
        return None
    if result.outcome in {
        DraftServiceOutcome.PURPOSE_REQUIRED,
        DraftServiceOutcome.PURPOSE_INSUFFICIENT,
    }:
        recorded = await _record_insufficient_prompt(ctx, purpose)
        if recorded is False:
            return _purpose_options()
        return {
            "ok": True,
            "opened": False,
            "outcome": result.outcome.value,
            "message": ("请再补充它要处理的业务对象、执行的动作或期望结果；也可以选择手动配置。"),
        }
    if result.outcome is DraftServiceOutcome.REQUEST_IN_PROGRESS or result.draft is None:
        return {
            "ok": True,
            "opened": False,
            "outcome": "request_in_progress",
            "message": "Agent 草稿正在生成，请稍后重试。",
        }
    draft = result.draft
    if draft.status == "needs_clarification":
        context_store = getattr(
            ctx,
            "subagent_authoring_context_store",
            None,
        )
        if context_store is not None:
            try:
                await context_store.upsert_clarifying(
                    team_id=team_id,
                    owner_user_id=user_id,
                    session_id=session_id,
                    purpose_text=purpose,
                    draft_id=draft.id,
                )
            except Exception as exc:
                logger.warning(
                    "subagent_creator.context_clarifying_failed turn=%s err=%s",
                    turn_id,
                    type(exc).__name__,
                )
    else:
        await _consume_context(ctx)
    try:
        delivered = await _emit_open(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=purpose,
            draft_id=draft.id,
            draft_status=draft.status,
            draft_title=_draft_title(draft),
            draft_version=draft.row_version,
            name_hint=identity[0],
            display_name_hint=identity[1],
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.draft_event_failed turn=%s err=%s",
            turn_id,
            type(exc).__name__,
        )
        delivered = False
    if not delivered:
        await _discard_undelivered_draft(
            ctx=ctx,
            draft=draft,
            turn_id=turn_id,
        )
        await _consume_context(ctx)
        return await _open_manual(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=purpose,
            outcome="delivery_failed_fallback",
            identity=identity,
        )
    return {
        "ok": True,
        "opened": True,
        "outcome": ("clarification_required" if draft.status == "needs_clarification" else "draft_opened"),
        "draft_id": draft.id,
        "message": (
            "已生成 Agent 草稿，请先复核配置再创建。" if draft.status != "needs_clarification" else "Agent 草稿还需要你补充一个信息，请在卡片中回答。"
        ),
    }


async def _generate_canvas_draft(
    *,
    ctx: ToolExecutionContext,
    service: Any,
    purpose: str,
    locale: str,
    idempotency_key: str,
    bus: Any,
    turn_id: str,
    identity: tuple[str | None, str | None],
) -> dict[str, Any]:
    """Open the durable Canvas task before generation; never replace it on failure."""
    workspace_id = getattr(ctx, "workspace_id", None)
    if not workspace_id:
        return {"ok": False, "opened": False, "outcome": "draft_space_required", "message": "Select a Space before creating an Agent draft."}
    try:
        draft = await service.create_chat_canvas_draft(
            team_id=ctx.team_id,
            owner_user_id=ctx.user_id,
            workspace_id=workspace_id,
            source_turn_id=turn_id,
            purpose=purpose,
            locale=locale,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        logger.warning("subagent_creator.canvas_create_failed turn=%s err=%s", turn_id, type(exc).__name__)
        return {
            "ok": False,
            "opened": False,
            "outcome": "draft_create_unknown",
            "message": "The draft creation result is unavailable. Recover the original task; do not start another draft automatically.",
        }
    await _consume_context(ctx)
    try:
        delivered = await _emit_open(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=purpose,
            draft_id=draft.id,
            draft_status=draft.status,
            draft_title=_draft_title(draft),
            draft_version=draft.row_version,
            name_hint=identity[0],
            display_name_hint=identity[1],
        )
    except Exception as exc:
        logger.warning("subagent_creator.canvas_event_failed turn=%s err=%s", turn_id, type(exc).__name__)
        delivered = False
    if not delivered:
        return {
            "ok": False,
            "opened": False,
            "draft_id": draft.id,
            "outcome": "draft_saved_refresh_required",
            "message": "The draft was saved, but the editor could not open. Resume this draft from My Agents; no replacement draft was created.",
        }
    try:
        result = await service.generate_canvas_suggestion(
            # Keep the first generation anchored to creation when replaying a
            # receipt after the author has edited the same task.
            draft=draft,
            workspace_id=workspace_id,
            expected_version=1,
            purpose=purpose,
            locale=locale,
            idempotency_key=f"{idempotency_key}-suggestion",
            mcp_entitled=bool(getattr(ctx, "subagent_draft_mcp_entitled", False)),
        )
    except Exception as exc:
        logger.warning("subagent_creator.canvas_generation_failed turn=%s err=%s", turn_id, type(exc).__name__)
        return {
            "ok": False,
            "opened": True,
            "draft_id": draft.id,
            "outcome": "draft_generation_unavailable",
            "message": "The existing draft remains editable on the right. Generation did not return a confirmed suggestion; no replacement draft or Agent was created.",
        }
    return {
        "ok": True,
        "opened": True,
        "draft_id": draft.id,
        "outcome": "request_in_progress" if result.suggestion is None else "draft_opened",
        "message": "Review the suggestions in the existing draft on the right. They do not replace your edits or create an Agent until you confirm.",
    }


async def _clarify_existing_draft(
    *,
    ctx: ToolExecutionContext,
    context: Any,
    answer: str,
    bus: Any,
    turn_id: str,
    identity: tuple[str | None, str | None],
) -> dict[str, Any] | None:
    service = getattr(ctx, "subagent_draft_service", None)
    if service is None:
        return None
    try:
        result = await service.clarify_draft(
            draft_id=context.draft_id,
            team_id=context.team_id,
            owner_user_id=context.owner_user_id,
            expected_version=None,
            answer=answer,
            mcp_entitled=bool(
                getattr(ctx, "subagent_draft_mcp_entitled", False),
            ),
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.clarify_failed turn=%s err=%s",
            turn_id,
            type(exc).__name__,
        )
        return None
    await _consume_context(ctx)
    if result.draft is None:
        return None
    try:
        delivered = await _emit_open(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=context.purpose_text,
            draft_id=result.draft.id,
            draft_status=result.draft.status,
            draft_title=_draft_title(result.draft),
            draft_version=result.draft.row_version,
            name_hint=identity[0],
            display_name_hint=identity[1],
        )
    except Exception as exc:
        logger.warning(
            "subagent_creator.draft_event_failed turn=%s err=%s",
            turn_id,
            type(exc).__name__,
        )
        delivered = False
    if not delivered:
        await _discard_undelivered_draft(
            ctx=ctx,
            draft=result.draft,
            turn_id=turn_id,
        )
        return await _open_manual(
            bus=bus,
            turn_id=turn_id,
            purpose_hint=context.purpose_text,
            outcome="delivery_failed_fallback",
            identity=identity,
        )
    return {
        "ok": True,
        "opened": True,
        "outcome": "draft_opened",
        "draft_id": result.draft.id,
        "message": "已根据补充信息更新 Agent 草稿，请复核后创建。",
    }


async def _discard_undelivered_draft(
    *,
    ctx: ToolExecutionContext,
    draft: Any,
    turn_id: str,
) -> None:
    service = getattr(ctx, "subagent_draft_service", None)
    team_id = getattr(ctx, "team_id", "") or ""
    user_id = getattr(ctx, "user_id", "") or ""
    if service is None or not team_id or not user_id:
        return
    try:
        await service.discard_undelivered_draft(
            draft_id=draft.id,
            team_id=team_id,
            owner_user_id=user_id,
            expected_version=draft.row_version,
        )
    except Exception as exc:
        logger.error(
            "subagent_creator.undelivered_discard_failed turn=%s err=%s",
            turn_id,
            type(exc).__name__,
        )


def _draft_title(draft: Any) -> str:
    definition = draft.definition if isinstance(draft.definition, dict) else {}
    for value in (
        definition.get("display_name"),
        draft.goal_summary,
        "Agent 草稿",
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return "Agent 草稿"


def register_subagent_creator_trigger(registry: ToolRegistry) -> bool:
    """Flag-gated registration of the ``subagent_creator`` TRIGGER (Lead-only).

    Returns True if it was registered (flag ON), False if skipped (flag OFF)."""
    if not subagent_creator_enabled():
        return False
    registry.register(
        ToolSpec(
            schema=_schema(),
            handler=_handler,
            needs_approval=False,
            risk_level="low",
            allowed_for_roles=frozenset({"lead"}),
            visible_to_llm=True,
        )
    )
    logger.info("subagent_creator_trigger.registered (flag on)")
    return True


__all__ = ["TOOL_NAME", "register_subagent_creator_trigger", "subagent_creator_enabled"]
