"""cca P2.1 (C1) — the conversational ``tool_creator`` TRIGGER.

The Lead calls this when an team-admin asks to add / create a tool. It does NOT do the create
itself: the structured create (pick endpoints, name) + the credential go through the gateway P1
endpoints from the FE create card — the secret MUST NEVER enter the chat / LLM path. This tool only
emits a dedicated ``cca.tool_create_open`` SSE event (mirroring ``memory.proposal.suggested``) so the
FE opens the inline create card, optionally pre-filled with a spec-URL hint the Lead extracted.

Registration is flag-gated (``BOWHEAD_CCA_TOOL_CREATOR``, default OFF) and advertised only to the
Lead role. Team-admin enforcement lives at the gateway (the create POST is ``Depends``-gated); the
trigger merely opens the card — a non-admin who opens it still cannot create (the POST 403s).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from bowhead_router.types import ToolSchema

from bowhead_core.events import make_event
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "tool_creator"
_EVENT: Final = "cca.tool_create_open"  # Final → the type checker infers the Literal (a valid EventType member)
_FLAG = "BOWHEAD_CCA_TOOL_CREATOR"
# external-capability-onboarding §3.2 — proactive routing (default ON; `=0` = kill switch).
# When ON, the tool_creator description advertises the full "connect external tools" intent (OpenAPI *and* hosted-MCP,
# honest boundary for local CLIs) instead of the OpenAPI-only wording, and lead.py injects the
# <tool_onboarding> guidance block. Requires _FLAG ON too (the tool only exists when _FLAG=1).
_PROACTIVE_FLAG = "BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE"

# Original OpenAPI-only wording; unchanged when the proactive flag is OFF (zero behavior change).
_DESC_LEGACY = (
    "打开「新建工具」卡片，让团队管理员把一个外部 API（OpenAPI 文档）变成 AI 可调用的工具。"
    "**仅当用户明确要『新建 / 添加一个工具 / 接入一个 API』时调用。** 本工具只负责打开创建卡片，"
    "不直接创建——真正的创建（选 endpoint、命名、填密钥）在卡片里完成，**密钥绝不进入对话**。"
    "若用户消息里给了接口文档链接，把它放进 `spec_url` 作为预填提示。"
)
# Proactive wording: expand from OpenAPI to connecting any external capability, offering both OpenAPI and hosted-MCP paths,
# with honest boundaries for local CLIs (review P1-4: fix Turn t_8894ac3's "OpenAPI only / talk without action" behavior).
_DESC_PROACTIVE = (
    "打开「新建工具」卡片，让团队管理员把一个**外部能力**接成 AI 可调用的工具。"
    "**当团队管理员表达『接入 / 新建一个工具、连接某个 API 或服务、把 X 接进来、装个 X 工具』时调用**"
    "（不限 OpenAPI）。外部能力有两条路，按其形态选：① 有 OpenAPI/REST 文档 → 贴文档；"
    "② 有 hosted MCP 端点 → 用 MCP 接（可 OAuth）。**本地命令行工具（CLI）本身当不了服务端工具，"
    "但它底下的 API 或 hosted MCP 通常可以**——这时引导用户走上面两条路，别答『装不了』、也别只提 OpenAPI。"
    "本工具只打开卡片，不直接创建（选 endpoint、命名、填密钥都在卡片里，**密钥绝不进入对话**）。"
    "若消息里给了接口/文档链接，放进 `spec_url` 预填。"
)


def _scrub_spec_url(url: str) -> str:
    """Reduce a user-supplied spec URL to ``scheme://host[:port]/path`` before it rides the SSE /
    persisted turn-log stream (SEC-1). Drops userinfo, the ENTIRE query string, and the fragment:
    the hint only prefills the source field, so no query is needed and a host+path-only hint cannot
    carry a credential (no blacklist to leak through — CORR-1/SEC-1). Returns "" for a URL without an
    http(s) scheme + host (the caller maps "" to a null hint); IPv6 host brackets are preserved."""
    try:
        sp = urlsplit(url)
    except ValueError:
        return ""
    if sp.scheme not in ("http", "https") or not sp.hostname:
        return ""
    netloc = sp.netloc.rsplit("@", 1)[-1]  # drop userinfo; keep host[:port] incl. IPv6 brackets
    return urlunsplit((sp.scheme, netloc, sp.path, "", ""))


def tool_creator_enabled() -> bool:
    """P2.1 rollout flag (default OFF; flip ON after dogfood)."""
    return os.environ.get(_FLAG, "0") == "1"


def tool_creator_proactive_enabled() -> bool:
    """external-capability-onboarding §3.2 proactive-routing flag (default ON — set `=0` to disable).

    Still gated by the base `BOWHEAD_CCA_TOOL_CREATOR` (the tool only registers when that is on), so
    on a deployment without tool_creator this stays inert. 116 has the base flag on → proactive live."""
    return os.environ.get(_PROACTIVE_FLAG, "1") != "0"


def _schema() -> ToolSchema:
    return ToolSchema(
        name=TOOL_NAME,
        description=_DESC_PROACTIVE if tool_creator_proactive_enabled() else _DESC_LEGACY,
        input_schema={
            "type": "object",
            "properties": {
                "spec_url": {
                    "type": "string",
                    "description": "可选：用户消息里出现的接口文档或 MCP 服务地址，用于预填创建卡片；没有就留空。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["openapi", "mcp"],
                    "description": (
                        "可选：你已判断出这个地址的形态时填——`mcp` = hosted MCP 端点，`openapi` = OpenAPI/REST 文档。卡片会直接落到对应的输入挡位。判断不了就留空。"
                    ),
                },
            },
            "required": [],
        },
    )


async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    raw = args.get("spec_url")
    spec_url = _scrub_spec_url(raw.strip()) or None if isinstance(raw, str) and raw.strip() else None
    # The Lead already classifies the address in its answer ("这是 hosted MCP 端点"); before this it
    # had nowhere to SAY so, and the card always opened on the OpenAPI tab — so a pasted MCP address
    # landed under a field labelled "接口文档 (OpenAPI)". Whitelist the value: it selects a tab, and
    # anything else is treated as "unknown" (card keeps its default) rather than trusted verbatim.
    raw_kind = args.get("kind")
    kind_hint = raw_kind if raw_kind in ("openapi", "mcp") else None
    bus = getattr(ctx, "parent_bus", None)
    turn_id = getattr(ctx, "turn_id", "") or ""
    if bus is None:
        return {"ok": False, "opened": False, "error": "无法打开创建卡片（当前会话不支持事件）。"}
    try:
        # payload carries ONLY a URL hint — never a secret, never structured create state.
        await bus.emit(make_event(_EVENT, turn_id, {"spec_url_hint": spec_url, "kind_hint": kind_hint}))
    except Exception as e:
        logger.warning("tool_creator.emit_failed turn=%s err=%s", turn_id, e)
        return {"ok": False, "opened": False, "error": "打开创建卡片失败。"}
    return {
        "ok": True,
        "opened": True,
        # The catalog reminder rides the TOOL RESULT, not the Lead prompt. Measured on 116: making
        # the <tool_onboarding> block emphatic enough to hit 4/4 on this reminder also flipped the
        # answer language to English (3/4) — density on that block trades against language
        # compliance. This channel costs no prompt density: the model reads it right before it
        # composes, which is exactly when the reminder is needed.
        "message": (
            "已打开「新建工具」卡片。卡片顶部可能有「一键连接器」——先请用户看那里有没有现成的（内容随部署而变，你看不到，别点名具体条目）；"
            "没有再请他贴地址。然后在卡片里选择要开放的接口、给工具命名，并（如该 API 需要）填写密钥；密钥会加密保存，不会进入对话、不会发给 AI。"
        ),
    }


def register_tool_creator_trigger(registry: ToolRegistry) -> bool:
    """Flag-gated registration of the ``tool_creator`` TRIGGER (Lead-only).

    Returns True if it was registered (flag ON), False if skipped (flag OFF)."""
    if not tool_creator_enabled():
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
    logger.info("tool_creator_trigger.registered (flag on)")
    return True


__all__ = [
    "TOOL_NAME",
    "register_tool_creator_trigger",
    "tool_creator_enabled",
    "tool_creator_proactive_enabled",
]
