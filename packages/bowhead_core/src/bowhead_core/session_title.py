"""Session title generator — LLM 总结 session 标题.

Orchestrator 在 turn 1 完成后异步调用；不阻塞主流程，失败降级为
`user_text[:40]`（session_store.touch_session 的默认行为）。

约束：
- 输入：第一条 user_text + final_answer preview
- 输出：≤ 16 字中文 / ≤ 32 字符英文的短 title
- Writes back via session_store.set_title, which never overwrites an owner-renamed title
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_router import LLMRequest, Message

if TYPE_CHECKING:
    from bowhead_core.session_store import SessionStore
    from bowhead_router import Router

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你给一次数据分析对话起一个短标题（像给文档起名）。

输入：用户第一条问题 + 系统回答的前几句。
输出：5-16 字的中文短标题。

要求：
- 概括问了什么（指标 + 时间 + 维度）
- 不含标点结尾；不加引号
- 不要以"关于"、"如何"这种虚词开头
- 纯中文（除非问题里就是英文）

示例：
  Q: 今年 GMV 是多少？ → "2026 GMV 概览"
  Q: 为什么双11销售下滑 → "双11销售下滑归因"
  Q: 最近经营怎么样 → "近30天业务复盘"
  Q: 近7天每日订单量 → "近7天订单趋势"

只输出标题一行，不要解释。"""


async def generate_title(
    router: Router,
    *,
    user_text: str,
    final_answer: str,
    agent_type: str = "echo",  # Tiered: title generation is a small internal task → fast tier (echo)
    trace_id: str = "",
    # i18n v1: None uses SYSTEM_PROMPT's default (Chinese only unless the query is English)
    language_context: Any | None = None,
) -> str | None:
    """调 LLM 生成标题；失败返 None（调用方保留现有 title）."""
    prompt = f"用户第一条问题：{user_text[:300]}\n回答摘要：{final_answer[:300]}\n\n输出标题："
    # i18n v1: append the output-language directive (keep SYSTEM_PROMPT intact for full cache reuse)
    system = SYSTEM_PROMPT
    if language_context is not None:
        from bowhead_core.language_directive import build_language_directive

        directive = build_language_directive(
            language_context,
            "lead",
            catalog=None,  # session_title needs no glossary
            include_tool_constraint=False,  # Plain-text generation; no tool calls
        )
        if directive:
            system = system + directive
    try:
        resp = await router.invoke(
            agent_type,
            LLMRequest(
                model="",
                messages=[Message(role="user", content=prompt)],
                system=system,
                temperature=0.5,
                max_output_tokens=40,
                trace_id=f"title:{trace_id}",
            ),
        )
    except Exception as e:
        logger.warning("title_gen.llm_failed trace=%s err=%s", trace_id, e)
        return None

    title = (resp.content or "").strip().replace("\n", " ")
    # Remove common prefixes and wrappers
    title = title.strip("\"'“”‘’`·:- ").strip()
    if len(title) > 32:
        title = title[:32]
    if len(title) < 2:
        return None
    return title


async def generate_and_store_title(
    *,
    router: Router,
    session_store: SessionStore,
    session_id: str,
    user_text: str,
    final_answer: str,
    agent_type: str = "echo",  # Tiered: title generation is a small internal task → fast tier (echo)
    trace_id: str = "",
    language_context: Any | None = None,
) -> str | None:
    """完整链路：生成 → 写回 session_store；成功返回新 title."""
    new_title = await generate_title(
        router,
        user_text=user_text,
        final_answer=final_answer,
        agent_type=agent_type,
        trace_id=trace_id,
        language_context=language_context,
    )
    if new_title:
        try:
            written = await session_store.set_title(session_id=session_id, title=new_title)
            if written is False:
                # The owner renamed the session meanwhile; a user-set title always wins.
                logger.info(
                    "title_gen.skipped_user_title session=%s trace=%s",
                    session_id,
                    trace_id,
                )
                return None
            logger.info(
                "title_gen.ok session=%s title=%r trace=%s",
                session_id,
                new_title,
                trace_id,
            )
        except Exception as e:
            logger.warning(
                "title_gen.store_failed session=%s err=%s",
                session_id,
                e,
            )
            return None
    return new_title
