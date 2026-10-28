"""i18n v1 · 生成 prompt 末尾的"输出语言 + 术语 + tool 调用"约束段.

Shared output-language constraints for Agent and session-title prompts.

策略：
- prompt 主体（中文）保持不变，吃满现有 prompt cache
- 末尾追加新增段（一次 cache miss，~200-400 token）
- glossary 子集按 agent 类型筛，避免全表注入

输出语言 vs data_language 行为:
- output_language → LLM 给用户看的 prose
- data_language  → LLM 调 datarc 工具时 entity 应该用的语言
- 专有名词（医院名/产品名）保留 data_language 原文，不在 output prose 里二次翻译
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from bowhead_core.language_context import LanguageContext
    from bowhead_core.product_terms import (
        ProductTermsCatalog,
    )

# Language display names (help the LLM clearly understand the requested language in prompts)
_LANGUAGE_LABELS: Final[dict[str, dict[str, str]]] = {
    "zh-CN": {"zh": "中文（简体）", "en": "Simplified Chinese"},
    "en-US": {"zh": "英语（美式）", "en": "American English"},
    "de-DE": {"zh": "德语", "en": "German"},
    "fr-FR": {"zh": "法语", "en": "French"},
}


def build_language_directive(
    ctx: LanguageContext | None,
    agent: str,
    *,
    catalog: ProductTermsCatalog | None = None,
    include_tool_constraint: bool = True,
) -> str:
    """生成 prompt 末尾的输出语言 + 术语 + tool 调用约束段.

    Args:
        ctx: 当前 turn 的 LanguageContext. None 时返空字符串（兼容老路径）.
        agent: agent 名（"lead" / "analyst" / "compute"）。决定 glossary 子集.
        catalog: 产品术语 catalog. None 时跳过 glossary 段.
        include_tool_constraint: 是否包含 "调 tool 时 entity 用 data_language" 约束.
                                 仅 lead / analyst 这种会调 datarc 的 agent 需要;
                                 session_title 这种纯文本生成的不需要.

    Returns:
        以双换行开头、末尾不带换行的 prompt 段落; ctx=None 时返空串.
    """
    if ctx is None:
        return ""

    output_label = _label(ctx.output_language)
    data_label = _label(ctx.data_language)

    parts: list[str] = []
    parts.append("\n\n## 输出语言与术语规范")
    if agent == "lead":
        parts.append(
            "- If the current user request explicitly names an answer language, use that language for all visible prose "
            "and pass it on every Agent dispatch. Language examples in application instructions are not user requests. "
            f"Otherwise use the resolved output_language={ctx.output_language}."
        )
    else:
        parts.append(
            f"- 用 **{output_label}** 回答用户（output_language={ctx.output_language}）",
        )
    parts.append(
        f"- 数据库内容是 **{data_label}**（data_language={ctx.data_language}）；"
        f"当回答中需要引用数据库列值（机构名 / 产品名 / 部门名 / 人名 / 商品名等），"
        f"**保留原文不翻译**",
    )
    parts.append(
        f"- 数字、日期、货币按 {ctx.output_language} 习惯格式化（不在 prose 里手算单位换算）",
    )
    parts.append(
        f"- 时间分组的\u201c周\u201d以 **{ctx.week_start_day}** 为起点",
    )

    # Inject glossary subset.
    if catalog is not None:
        glossary_section = catalog.render_prompt_section(
            agent,
            output_language=ctx.output_language,
        )
        if glossary_section:
            parts.append(
                "- 产品术语严格使用以下对照表（左侧 stable key，右侧目标语言译法）：",
            )
            # Indent the glossary table for easier visual parsing by the LLM.
            indented = "\n".join("  " + line for line in glossary_section.split("\n"))
            parts.append(indented)

    # Tool-call constraints (only for datarc-calling agents such as lead / analyst)
    if include_tool_constraint:
        parts.append("")  # Blank line for visual separation
        parts.append("## Tool 调用约束（涉及 BI 查询时）")
        parts.append(
            f"- 调 datarc / BI 工具时，参数中的 entity（机构 / 产品 / 部门 / 人名）必须用 data_language={ctx.data_language} 的写法",
        )
        parts.append(
            '- 例：用户问"sales for Beijing Tongren Hospital"，data_language=zh-CN '
            '→ 你的 tool 调用应传 hospital_name="北京同仁医院"'
            '（不是 "Beijing Tongren Hospital"）',
        )
        parts.append(
            f"- If an entity's spelling in data_language={ctx.data_language} is uncertain, verify it with an available "
            "Tool that supports entity lookup before querying. If no such Tool is available, state the uncertainty instead of guessing.",
        )
        parts.append(
            # Separate source columns for different languages can return different entity
            # values. Preserve the requested identifiers while choosing the appropriate
            # configured column; translating a label is not equivalent to selecting that column.
            "- If the data source provides separate language variants of a column, select the configured column "
            f"matching output_language={ctx.output_language} and preserve its original values. This selects a data source; "
            "it does not translate returned values. Inspect column labels with an available schema Tool when needed. "
            "If that capability is unavailable, state the missing schema information instead of inventing columns.",
        )
        parts.append(
            "- 给用户的 prose 仍保持 data_language entity 原文，不在 output prose 里二次翻译",
        )

    # citation-audit M3-3 (§4.5 i18n inline-numbers v1 retirement): this previously taught
    # lead/analyst/compute to write KPI numbers as `[num:VALUE:UNIT_TYPE]`
    # (rendering chose scale to prevent model-selected off-by-10000 scaling). Removed with the subsystem:
    # marker syntax is retired; scale safety now uses plain-prose numbers plus backend [N] binding to tool truth.
    # If customers later need KPI locale/scale safety, use a structured replacement (separate kpis[] value
    # fields + structured numeric formatting at egress), without reviving marker syntax.

    return "\n".join(parts)


def _label(language: str) -> str:
    """生成给 LLM 看的语言显示名（用 LLM 当前 prompt 主体语言匹配，提高遵循率）."""
    # Prompt body is currently Chinese; Chinese labels are more intuitive for the LLM.
    return _LANGUAGE_LABELS.get(language, {}).get("zh", language)
