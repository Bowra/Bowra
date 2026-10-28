"""Complexity assessor (sprint multi-agent-v1 §5.2 PR-#4).

WORK intent 进入后用此评估 query 复杂度 → 决定并行度 + 是否走 H2 approval.

3 档:
- LOW (signals=0): 1 agent (单 metric lookup, 简单 query)
- MED (signals 1-2): 2-3 agent parallel (multi-dim 对比, 复合 query)
- HIGH (signals ≥3): N agent + **必走 H2 approval (TaskPlanCard)** (尽调 / 全面)

借 IntelliVault complexity heuristics (full-review.md §1.4) + 微调阈值.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Complexity(StrEnum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


# +2 signal: comprehensive keywords (due diligence / comprehensive — automatically HIGH)
_COMPREHENSIVE_KEYWORDS = (
    "尽调",
    "全面",
    "完整",
    "整体",
    "comprehensive",
    "due-diligence",
    "due diligence",
)

# +1 signal: comparison keywords
_COMPARISON_KEYWORDS = (
    "对比",
    "比较",
    "vs",
    "v.s.",
    "差异",
    "区别",
)


@dataclass(frozen=True)
class ComplexitySignals:
    """6 个信号 → 总分."""

    multi_domain: bool  # Spans 2+ manifest domains (router output has agent_names ≥ 2)
    has_comparison: bool  # Contains 对比 / vs / 差异
    has_comprehensive: bool  # Contains 全面 / 尽调 / 整体
    multi_file_workspace: bool  # Workspace file types ≥ 3
    long_query: bool  # query ≥ 80 char
    many_skills: bool  # Matches ≥ 3 skills

    @property
    def score(self) -> int:
        # comprehensive +2, multi_domain +2, all others +1
        s = 0
        if self.multi_domain:
            s += 2
        if self.has_comparison:
            s += 1
        if self.has_comprehensive:
            s += 2
        if self.multi_file_workspace:
            s += 1
        if self.long_query:
            s += 1
        if self.many_skills:
            s += 1
        return s


def assess_complexity(
    query: str,
    *,
    matched_agent_count: int = 1,
    workspace_file_classifications_count: int = 0,
    matched_skill_count: int = 0,
) -> tuple[Complexity, ComplexitySignals]:
    """评估 query 复杂度.

    Args:
        query: 用户 query 文本.
        matched_agent_count: WORK router 匹配到的 agent 数 (≥1).
        workspace_file_classifications_count: workspace KB 内文件分类数.
        matched_skill_count: skill router 匹配到的 skill 数.

    Returns:
        (complexity, signals) tuple.
    """
    q = query or ""
    q_lower = q.lower()

    multi_domain = matched_agent_count >= 2
    has_comparison = any(kw in q for kw in _COMPARISON_KEYWORDS) or any(kw in q_lower for kw in _COMPARISON_KEYWORDS)
    has_comprehensive = any(kw in q for kw in _COMPREHENSIVE_KEYWORDS) or any(kw in q_lower for kw in _COMPREHENSIVE_KEYWORDS)
    multi_file_workspace = workspace_file_classifications_count >= 3
    long_query = len(q) >= 80
    many_skills = matched_skill_count >= 3

    signals = ComplexitySignals(
        multi_domain=multi_domain,
        has_comparison=has_comparison,
        has_comprehensive=has_comprehensive,
        multi_file_workspace=multi_file_workspace,
        long_query=long_query,
        many_skills=many_skills,
    )

    score = signals.score
    if score >= 3:
        return Complexity.HIGH, signals
    if score >= 1:
        return Complexity.MED, signals
    return Complexity.LOW, signals


def needs_h2_approval(
    complexity: Complexity,
    *,
    auto_approve_high: bool = False,
) -> bool:
    """是否触发 TaskPlanCard H2 approval flow.

    HIGH 默认走 approval, 除非 workspace 设 auto_approve_high (CTO-P1.4 fix).
    LOW / MED 不走.
    """
    if complexity != Complexity.HIGH:
        return False
    return not auto_approve_high
