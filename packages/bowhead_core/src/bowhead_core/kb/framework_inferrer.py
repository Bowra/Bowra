"""Framework inferrer — LLM (qwen-plus JSON mode) 推 sections.

Tech design v3.6 §3.6. role schema 6 类: intro / data / analysis / appendix / glossary / sop.

V1 短文档 (< 10 page) 跳过 inferrer (paragraph splitter 够用).
长文档走 LLM, 失败 fallback 空 list (caller 走 paragraph splitter).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bowhead_core.kb.splitter.section_aware import KbSectionInferred

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)


# Skip short docs (<10 pages: paragraph splitter suffices).
SHORT_DOC_PAGE_THRESHOLD = 10
# Excerpt character cap (first 5K chars ~2 pages for framework_inferrer prompt).
EXCERPT_MAX_CHARS = 5000


INFERRER_PROMPT = """你正在分析一份文档的章节结构.

输入是文档前 2 页 + 目录 (TOC).

输出 JSON 数组, 每元素是一个 logical section, key 字段:
- title: section 标题 (从原文文字提取, 不要改写)
- char_start: section 在原文的 char offset (粗略估计, ±100 char OK)
- char_end: section 结束 char offset
- page: section 主要在哪页 (整数, 不知道填 0)
- role: 6 类之一
  - intro: 引言 / 摘要 / 概述
  - data: 数据表 / KPI / 数字汇总
  - analysis: 分析 / 结论 / 解读 / 建议
  - appendix: 附录 / 参考
  - glossary: 术语 / 定义 / 业务规则
  - sop: 标准操作流程 / 操作指南

仅返回 {{"sections": [...]}} JSON object, 不要解释.

# 文档片段
{doc_excerpt}

# TOC
{toc_text}
"""


# Type: caller-injected LLM call function, no hard bowhead_router dependency.
LLMCallFn = "Callable[..., Awaitable[str]]"


class FrameworkInferrer:
    """LLM 推 section 结构. caller 注入 llm_call_fn."""

    def __init__(self, llm_call_fn: Callable[..., object]):
        """llm_call_fn(messages, response_format) → returns JSON string content."""
        self._llm_call = llm_call_fn

    async def infer(
        self,
        full_text: str,
        *,
        toc_text: str = "",
        page_count: int = 0,
    ) -> list[KbSectionInferred]:
        """Returns list of KbSectionInferred, 短文档 / LLM 失败时返空 list."""
        if page_count < SHORT_DOC_PAGE_THRESHOLD:
            return []

        excerpt = full_text[:EXCERPT_MAX_CHARS]
        prompt = INFERRER_PROMPT.format(
            doc_excerpt=excerpt,
            toc_text=toc_text or "(无 TOC)",
        )

        try:
            content = await self._llm_call(
                model="qwen-plus",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            payload = json.loads(content) if isinstance(content, str) else content
            sections_raw = payload.get("sections", [])
            sections = [
                KbSectionInferred(
                    title=item["title"],
                    char_start=int(item.get("char_start", 0)),
                    char_end=int(item.get("char_end", 0)),
                    role=item.get("role", "intro"),
                    page=item.get("page") or None,
                )
                for item in sections_raw
                if isinstance(item, dict) and "title" in item
            ]
            return sections
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("framework infer parse failed: %s, fallback empty", exc)
            return []
        except Exception as exc:
            logger.warning("framework infer failed: %s, fallback empty", exc)
            return []
