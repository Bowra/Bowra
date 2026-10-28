"""FIX-2 (2026-06-25 prod incident) — _plan_report_gap_caveats.

A plan whose report was composed from partial upstream (a sibling timed out) produced a
report that LOOKED complete (the business-analyst leg silently missing). This pins the
"flag the gap" logic so a partial report is always honest about what's missing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents.lead_orchestrator import _plan_report_gap_caveats

# Pure functions, no I/O, previously **no markers**, so `-m unit` (sprint
# and CLAUDE.md gate) **never collected them**, including batch 2-c title-contract guards.
# Tests outside gate protect nothing; add module marker.
pytestmark = pytest.mark.unit


def _s(agent: str, status: str, error_kind: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(agent_name=agent, status=status, error_kind=error_kind)


def test_flags_failed_blocked_pending_not_done_or_skipped() -> None:
    steps = [
        _s("financial-analyst", "done"),  # done → not a gap
        _s("business-analyst", "failed", "Timeout"),  # the incident case → flag
        _s("kb-analyst", "blocked", "needs_corpus"),  # gate-blocked → flag
        _s("compute", "pending"),  # never ran → flag
        _s("compliance-analyst", "skipped"),  # user excluded (track A) → NOT a gap
    ]
    caveats = _plan_report_gap_caveats(steps)
    assert len(caveats) == 3, caveats
    joined = " ".join(caveats)
    # the exact prod incident: business-analyst timed out → must be flagged with the reason
    assert "business-analyst" in joined and "执行失败" in joined and "Timeout" in joined
    assert "kb-analyst" in joined and "受阻" in joined and "needs_corpus" in joined
    assert "compute" in joined and "未执行" in joined
    # NOT flagged:
    assert "financial-analyst" not in joined  # done
    assert "compliance-analyst" not in joined  # skipped = user excluded


def test_all_done_no_caveats() -> None:
    steps = [_s("financial-analyst", "done"), _s("business-analyst", "done")]
    assert _plan_report_gap_caveats(steps) == []


# ── 2026-07 planning-ux-reliability: partial-report fallback + categorized copy ──

import json

from bowhead_core.agents.lead_orchestrator import (
    _plan_fallback_report_payload,
)


def _sp(agent: str, status: str, payload: dict | None = None, error_kind: str | None = None, objective: str = "obj") -> SimpleNamespace:
    return SimpleNamespace(
        agent_name=agent,
        status=status,
        error_kind=error_kind,
        objective=objective,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else "",
    )


def test_fallback_payload_from_done_experts() -> None:
    """116 plan_f3245e9e63e3 场景: 兄弟腿 Timeout,唯一成功腿成果必须可见."""
    steps = [
        _sp("business-analyst", "done", {"title": "销量结构", "exec_summary": "长尾占比 38%", "kpis": [{"label": "SKU", "value": "1.2k"}]}),
        _sp("financial-analyst", "failed", None, "Timeout"),
    ]
    p = _plan_fallback_report_payload(steps)
    assert p is not None
    # Batch 2-c (decision #6.6, 2026-08-24): title **no longer uses plan_title**.
    # plans.title truncates first 80 user characters; 32 of 41 production multileg reports
    # inherited it, often mid-sentence. New chain: final DONE leg title (closing chapter
    # reads everything; its title suits report) →plan_title only if every leg lacks title.
    # Sole DONE leg "销量结构" supplies title here.
    assert p["title"] == "销量结构", p["title"]
    assert any("长尾占比 38%" in i["narrative"] for i in p["insights"])
    assert p["kpis"] and p["kpis"][0]["label"] == "SKU"
    # Merge normal aggregate inputs, no report-writing-step-incomplete notices; separate gap caveats disclose gaps.
    assert p["caveats"] == [] and p["exec_summary"] == ""


def test_fallback_payload_none_when_nothing_done() -> None:
    steps = [_sp("business-analyst", "failed", None, "Timeout"), _sp("compute", "pending")]
    assert _plan_fallback_report_payload(steps) is None


def test_fallback_payload_ignores_bad_json() -> None:
    steps = [
        SimpleNamespace(agent_name="compute", status="done", error_kind=None, objective="o", payload_json="{not json"),
    ]
    assert _plan_fallback_report_payload(steps) is None


def test_done_step_verify_unmet_flows_to_caveats() -> None:
    """graph-mode 批1(review C9): done 步的 verify unmet 是其唯一 gap 来源."""
    import json as _json

    vj_partial = _json.dumps({"verdict": "partial", "items": [{"text": "含季度数字", "status": "unmet", "note": "缺"}], "note": ""})
    vj_pass = _json.dumps({"verdict": "pass", "items": [{"text": "含信源", "status": "met", "note": ""}], "note": ""})
    steps = [
        SimpleNamespace(agent_name="compute", status="done", error_kind=None, verify_json=vj_partial),
        SimpleNamespace(agent_name="kb-analyst", status="done", error_kind=None, verify_json=vj_pass),
        SimpleNamespace(agent_name="financial-analyst", status="done", error_kind=None, verify_json=None),
    ]
    out = _plan_report_gap_caveats(steps)
    assert len(out) == 1
    assert "compute" in out[0] and "含季度数字" in out[0]


# ─────────────────────────────────────────────────────────────
# G3 (actual 116 t_c9853b490565): cover KPI row must come from **closing chapter**,
# never first N across chapters, explicitly called bug by decision #2.
# ─────────────────────────────────────────────────────────────


def _leg(profile: str, payload: dict) -> dict:
    return {"profile": profile, "payload": payload}


def test_closing_chapter_header_takes_title_and_kpis_from_the_last_titled_leg() -> None:
    """封面的标题与 KPI 带都取**收口章**那一条腿，且取的是同一条腿。

    决议 #2 原文把「抄了步 0+步 1 的行业数字当全文 KPI」直接称为 bug 形态（纯噪音），
    并定「封面直接用收口章的 KPI 带，不新造」。116 真机 t_c9853b490565 实测出的
    恰恰就是那个 bug：封面 8 张卡 = 第 1 章 4 张 + 第 2 章 4 张，标签与数值逐字相同，
    而收口章自己的「估值区间下限/基准/上限/退款率」一张都没上封面。
    """
    from bowhead_core.agents.lead_orchestrator import _closing_chapter_header

    legs = [
        _leg("compute", {"title": "行业宏观", "kpis": [{"label": "社零", "value": "51.2万亿"}]}),
        _leg("compute", {"title": "竞品对标", "kpis": [{"label": "永辉营收", "value": "133.67亿"}]}),
        _leg("consultant", {"title": "尽调结论与报价建议", "kpis": [{"label": "估值基准", "value": "290.7亿"}]}),
    ]
    title, kpis = _closing_chapter_header(legs)
    assert title == "尽调结论与报价建议", title
    assert [k["label"] for k in kpis] == ["估值基准"], kpis


def test_closing_chapter_header_skips_untitled_tail_legs() -> None:
    """收口章＝**最后一条有标题的**腿（交付面选 chat 的腿不出 title），

    且 KPI 必须跟着同一条腿走 —— 不能标题取这条、KPI 取那条。
    """
    from bowhead_core.agents.lead_orchestrator import _closing_chapter_header

    legs = [
        _leg("compute", {"title": "行业宏观", "kpis": [{"label": "社零", "value": "51.2万亿"}]}),
        _leg("consultant", {"title": "尽调结论", "kpis": [{"label": "估值基准", "value": "290.7亿"}]}),
        _leg("financial-analyst", {"exec_summary": "chat 面短答，没有 title", "kpis": [{"label": "噪音", "value": "1"}]}),
    ]
    title, kpis = _closing_chapter_header(legs)
    assert title == "尽调结论", title
    assert [k["label"] for k in kpis] == ["估值基准"], kpis


def test_closing_chapter_header_empty_when_no_leg_has_a_title() -> None:
    """负对照：没有任何腿出标题时返空 —— 上游据此走兜底链，别凭空造一个封面。

    KPI 也必须是空：决议 #2 定的是「不新造文档级 KPI 带」，收口章没有就没有，
    宁可不渲这条带，也不拿跨章聚合的噪音充数。
    """
    from bowhead_core.agents.lead_orchestrator import _closing_chapter_header

    legs = [_leg("compute", {"exec_summary": "只有短答", "kpis": [{"label": "噪音", "value": "1"}]})]
    title, kpis = _closing_chapter_header(legs)
    assert title == ""
    assert kpis == []


def test_fallback_payload_cover_kpis_come_from_the_closing_chapter_only() -> None:
    """降级路同口径（G3）：封面 KPI 只取收口章那条腿，不跨腿累积。

    原实现是每条腿各取 4 张往上堆 ⇒ 3 条腿就是 12 张卡的拼盘，
    正是决议 #2 点名的「抄了步 0+步 1 的行业数字当全文 KPI，纯噪音」。
    """
    steps = [
        _sp("compute", "done", {"title": "行业宏观", "exec_summary": "宏观面", "kpis": [{"label": "社零", "value": "51.2万亿"}]}),
        _sp("compute", "done", {"title": "竞品对标", "exec_summary": "竞品面", "kpis": [{"label": "永辉营收", "value": "133.67亿"}]}),
        _sp("consultant", "done", {"title": "尽调结论", "exec_summary": "结论面", "kpis": [{"label": "估值基准", "value": "290.7亿"}]}),
    ]
    p = _plan_fallback_report_payload(steps)
    assert p is not None
    # Closing-chapter title ⇒ KPIs from same leg, never a three-leg mixture.
    assert p["title"].startswith("尽调结论"), p["title"]
    assert [k["label"] for k in p["kpis"]] == ["估值基准"], p["kpis"]


def test_fallback_payload_still_renders_when_only_kpis_and_no_summary() -> None:
    """★正对照（G3 修复时差点自己踩的坑）：「有 KPI、没 exec_summary」必须**照旧出报告**。

    `if not insights and not kpis: return None` 是「只要有成果就有报告」那条 P0 兜底
    的判据。G3 把 kpis 收窄成「收口章的」时，若不留累积兜底，这类计划会从
    「出部分报告」变成**不出报告** —— 比原 bug 严重得多。
    """
    steps = [_sp("compute", "done", {"kpis": [{"label": "SKU", "value": "1.2k"}]})]
    p = _plan_fallback_report_payload(steps)
    assert p is not None, "有 KPI 就必须出报告，不许因为没有 exec_summary 直接返 None"
    assert [k["label"] for k in p["kpis"]] == ["SKU"], p["kpis"]


def test_fallback_payload_never_falls_back_to_the_users_own_words() -> None:
    """★收口 review 逮到的既存缺口：降级路仍会把 `plans.title` 当报告标题。

    `plans.title` ＝ 用户原话前 80 字截断。决议 #6.6 / TODO 2-c 写的是
    「**任何情况下**不再回落到 plans.title」，但实现里一直留着 ` or plan_title.strip()`，
    **且可复现**——「有 KPI 但没有任何腿出 exec_summary」时 insights 为空，
    标题就落回用户原话。实测产出过：
    「…请做一份完整尽调。要求分头取数再逐层收敛：\n① 先并行取三类数据：联网检索
    2025-2026 年中国商超行业宏观数据（市场规模、(部分)」——正是 discovery C5 的原样。

    修法是**把参数整个拿掉**而不是删掉那一项：没有入口就传不进来。
    """
    import inspect

    # ① Structural guarantee: this seam no longer exists.
    sig = inspect.signature(_plan_fallback_report_payload)
    assert "plan_title" not in sig.parameters, f"plan_title 又被加回来了: {list(sig.parameters)}"

    # ② Without any leg title, only "计划执行报告", never original user wording.
    steps = [_sp("compute", "done", {"kpis": [{"label": "SKU", "value": "1.2k"}]})]
    p = _plan_fallback_report_payload(steps)
    assert p is not None
    assert p["title"].startswith("计划执行报告"), p["title"]


@pytest.mark.parametrize("writer_status", ["pending", "blocked", "failed", "skipped", "done"])
def test_retired_writer_step_is_neither_a_gap_nor_a_chapter(writer_status: str) -> None:
    """老 plan 的 canvas-writer 步按 skipped 口径：不进缺口说明，它写过的综述也不再当一章合并。"""
    steps = [
        _sp("business-analyst", "done", {"title": "销量结构", "exec_summary": "长尾占比 38%"}),
        _sp("canvas-writer", writer_status, {"title": "综述", "exec_summary": "writer 当年的综述"}, "unknown_agent"),
    ]
    assert _plan_report_gap_caveats(steps) == []
    p = _plan_fallback_report_payload(steps)
    assert p is not None
    assert "writer 当年的综述" not in json.dumps(p, ensure_ascii=False)
    assert [i["title"] for i in p["insights"]] == ["销量结构"]
