"""delivery-surface C3 —— plan 交付形态（deliverable_kind）接线。

三层各测各的：
1. `_parse_plan_deliverable`：保守判定（明确 "answer" 才 answer，其余一律 report）。
2. `author_plan(plan_meta_out=...)`：out-param 通道回填。
3. `is_report_worthy(plan_deliverable_kind=...)`：ANSWER plan 走 section 证据判定，
   REPORT / 缺失维持无条件 True（老 plan 兼容，R3 偏向出报告）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from bowhead_core.agents.lead_orchestrator import (
    _materialized_response_kind,
    is_report_worthy,
)
from bowhead_core.plan_authoring import (
    _parse_plan_deliverable,
    author_plan,
)

# ─── 1. Conservative _parse_plan_deliverable decision ────────────

_ANSWER_JSON = '{"deliverable": "answer", "steps": [{"id": "q", "agent_name": "compute", "objective": "查今年利润前三的品牌", "depends_on": []}]}'


def test_parse_deliverable_answer() -> None:
    assert _parse_plan_deliverable(_ANSWER_JSON) == "answer"


def test_parse_deliverable_answer_case_insensitive() -> None:
    assert _parse_plan_deliverable('{"deliverable": "Answer", "steps": []}') == "answer"


@pytest.mark.parametrize(
    "raw",
    [
        '{"deliverable": "report", "steps": []}',
        '{"steps": []}',  # Omitted.
        '{"deliverable": "deck", "steps": []}',  # Planner cannot choose deck.
        '{"deliverable": "none", "steps": []}',  # Reject non-answer variants.
        '{"deliverable": 42, "steps": []}',
        "not json at all",
    ],
)
def test_parse_deliverable_everything_else_is_report(raw: str) -> None:
    """解析兜底仍是 REPORT（LLM 漏填/非法值时的格式兜底）；交付面自决（批 5ⓐ）改的是 prompt——
    planner 按任务性质判 answer/report, 不再「其余一律 report」——解析器不变。"""
    assert _parse_plan_deliverable(raw) == "report"


def test_parse_deliverable_strips_code_fence() -> None:
    assert _parse_plan_deliverable("```json\n" + _ANSWER_JSON + "\n```") == "answer"


# ─── 2. author_plan plan_meta_out channel ───────────────────────


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Resp:
    content: str
    usage: object = None
    finish_reason: str = "stop"


class _FakeRouter:
    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, agent_type, req):
        return _Resp(self._content, usage=_Usage(11, 22))


@pytest.mark.asyncio
async def test_author_plan_fills_plan_meta_out() -> None:
    meta: dict = {}
    steps = await author_plan(
        _FakeRouter(_ANSWER_JSON),
        user_text="分两步：①用 BI 查今年利润前三的品牌 ②一句话总结。不要写报告。",
        agent_roster=[("compute", "分析师")],
        plan_meta_out=meta,
    )
    assert [s.llm_id for s in steps] == ["q"]
    assert meta["deliverable"] == "answer"


@pytest.mark.asyncio
async def test_author_plan_meta_out_defaults_report() -> None:
    meta: dict = {}
    await author_plan(
        _FakeRouter('{"steps": [{"id": "a", "agent_name": "compute", "objective": "x"}]}'),
        user_text="做份经营分析报告",
        agent_roster=[("compute", "分析师")],
        plan_meta_out=meta,
    )
    assert meta["deliverable"] == "report"


@pytest.mark.asyncio
async def test_author_plan_without_meta_out_unchanged() -> None:
    """不传 out-param 的调用方（amend/replan wrapper）行为逐字节不变。"""
    steps = await author_plan(
        _FakeRouter(_ANSWER_JSON),
        user_text="q",
        agent_roster=[("compute", "分析师")],
    )
    assert len(steps) == 1


# ─── 3. is_report_worthy plan branch ────────────────────────────


def _sec(kind: str, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, payload=payload or {})


def test_plan_report_kind_keeps() -> None:
    """REPORT plan：planner 明说 report ⇒ 保画布（批 5ⓐ 后这是「声明」而非「plan 轮无条件」）。"""
    keep, rationale = is_report_worthy(
        [_sec("code_block")],
        is_plan_execution=True,
        plan_deliverable_kind="report",
    )
    assert keep is True
    assert "declared: canvas" in rationale


def test_plan_missing_kind_falls_through_to_evidence() -> None:
    """老 plan 行 / bus attr 没挂：**不再**无条件 canvas——没人声明就按证据判（批 5ⓐ）。
    负对照：flag 关时仍是老行为（plan 轮无条件 True）。"""
    keep, rationale = is_report_worthy([_sec("code_block")], is_plan_execution=True, plan_deliverable_kind=None)
    assert keep is False
    assert "chat-mode" in rationale


def test_plan_answer_kind_is_chat_even_with_charts() -> None:
    """ANSWER plan = planner 说了「答案在聊天里」⇒ chat，**不再**被「画了 ≥2 图」这类
    后端猜测顶回报告（用户 2026-08-20 拍板：是不是报告不由后端判）。"""
    keep, rationale = is_report_worthy(
        [_sec("chart"), _sec("chart")],
        is_plan_execution=True,
        plan_deliverable_kind="answer",
    )
    assert keep is False
    assert "declared: chat" in rationale


def test_plan_answer_kind_shell_sections_chat() -> None:
    keep, rationale = is_report_worthy(
        [_sec("h1"), _sec("meta"), _sec("sources")],
        is_plan_execution=True,
        plan_deliverable_kind="answer",
    )
    assert keep is False
    assert "declared: chat" in rationale


def test_materialized_kind_passes_plan_deliverable_through() -> None:
    kind, _ = _materialized_response_kind(
        [_sec("h1"), _sec("narrative", {"style": "plain"})],
        is_plan_execution=True,
        plan_deliverable_kind="answer",
    )
    assert kind == "chat"
    kind2, _ = _materialized_response_kind(
        [_sec("h1")],
        is_plan_execution=True,
        plan_deliverable_kind="report",
    )
    assert kind2 == "canvas"
