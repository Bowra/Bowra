"""ξ B7 — grounding density gate（M1-0 换锚：>80 字分析文本须有数据锚点；数字或引用皆可。

B5 viz gate 已于 chat-truth sprint 删除。M3-2：旧 `_validate_citation_density`（marker
强制）随 gen2 读端下线删除，本文件全部锁改指新闸——契约意图逐条保留，只换判据形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.subagent_return_tool import (
    _payload_has_analytical_content,
    _validate_grounding_density,
)


@dataclass
class _MockInv:
    tool_name: str
    arguments: dict[str, Any]


# ─── analytical content classifier (shared by cite + narrative gates) ────


def test_analytical_content_detect() -> None:
    """exec_summary > 100 chars OR insights / kpis / recs / insight_section_ids 触发 report gate."""
    assert _payload_has_analytical_content({"exec_summary": "x" * 101}) is True
    assert _payload_has_analytical_content({"kpis": [{"label": "A", "value": "1"}]}) is True
    assert _payload_has_analytical_content({"insights": [{"title": "i", "facts": []}]}) is True
    assert _payload_has_analytical_content({"recommendations": [{"text": "do x"}]}) is True
    assert _payload_has_analytical_content({"insight_section_ids": ["s1"]}) is True
    # chat-like minimal: no gate
    assert _payload_has_analytical_content({"exec_summary": "短答案"}) is False
    assert _payload_has_analytical_content({}) is False


# ─── B7 citation density gate ────────────────────────────────────────


def test_b7_exec_summary_long_no_marker_rejects() -> None:
    """exec_summary 长文本 0 marker → reject (gate by analytical content + len > 80)."""
    long_summary = "本月销售下降明显，主要原因是渠道客流下降导致单月营收较去年同期下降百分点。" * 2 + "结尾增强字数以触发 cite gate threshold 检查."
    payload = {
        "exec_summary": long_summary,
        "kpis": [{"label": "x", "value": "1"}],  # trigger analytical_content
    }
    ok, errs = _validate_grounding_density(payload)
    assert ok is False
    assert any("exec_summary" in e for e in errs)


def test_b7_exec_summary_with_marker_passes() -> None:
    """exec_summary 含 [num:V:U] OK."""
    payload = {
        "exec_summary": ("本月销售下降明显，主要原因是渠道 A 的客流下降导致单月营收较去年同期下降 [num:12.3:%]，建议加大数字营销投入。"),
        "insights": [],
    }
    ok, _errs = _validate_grounding_density(payload)
    assert ok is True


def test_b7_insight_fact_no_marker_rejects() -> None:
    """insight_card facts 长文本 0 marker → reject WHEN evidence has citable artifact.

    0.39.5 update: added evidence[].chart_section_id so the gate still enforces
    citations when there's something to cite. Insights with only callout
    evidence (no chart/comp/table id) now skip per the new
    `_insights_are_callout_only` check.
    """
    payload = {
        "insights": [
            {
                "title": "渠道分析",
                "evidence": [{"chart_section_id": "sec_real"}],  # citable
                "facts": [
                    # >50 Chinese characters to clear the threshold.
                    {"text": "渠道 A 客流下降明显，主要因为竞争对手促销活动力度远大于本品牌且我们投放预算低迷导致 GMV 落后明显"},
                ],
            },
        ],
    }
    ok, _errs = _validate_grounding_density(payload)
    assert ok is False


def test_b7_recommendations_not_grounding_gated() -> None:
    """FU-1（writer-payload-debt, 2026-07-19）：recommendations **不做 grounding**。

    建议是行动指令、天然可无数字；grounding 是给数据断言（exec_summary / insights.facts）
    防瞎编的。spike 实测 668 条真 rec：仅 1 条 >80 字无数字，且是合法英文建议（false-positive）
    ——闸对建议只会误伤、抓不到真瞎编。此前代码读恒空的 `rec["text"]`（真实字段是 `action`）
    是死 no-op；现改为**显式排除**。本测用真实字段 `action`、喂无数字长建议 → 仍应放行。
    """
    numberless = (
        "建议加大数字营销预算，重点投放短视频平台，预期可拉回相当规模的 GMV，"
        "投入产出比可观。同时建议优化老客私域运营策略，提升复购频次和客单价水平，"
        "并对渠道结构做一次系统性梳理与复盘。"
    )
    assert len(numberless) > 80, len(numberless)
    ok, errs = _validate_grounding_density({"recommendations": [{"action": numberless}]})
    assert ok is True, errs
    assert not any("recommendations" in e for e in errs)


def test_b7_short_text_skipped() -> None:
    """短文本 (< threshold) 不强制 marker — 配套小段落可以无 ref."""
    payload = {
        "exec_summary": "短结论。",
        "insights": [
            {"title": "x", "facts": [{"text": "短结论"}]},
        ],
    }
    ok, _errs = _validate_grounding_density(payload)
    assert ok is True


def test_b7_ref_marker_passes() -> None:
    """[ref:1] / [ref:chunk_xxx] marker 也满足."""
    payload = {
        "exec_summary": "x" * 100 + " 详见 [ref:1] 历史 SOP",
        "insights": [],
    }
    ok, _errs = _validate_grounding_density(payload)
    assert ok is True


def test_b7_bail_skips_citation_gate() -> None:
    """bail envelope skips citation gate too."""
    payload = {
        "kpis": [{"label": "x", "value": "y"}],
        "exec_summary": "x" * 100,
        "error": {"kind": "transient", "message": "tool 暂时失败"},
    }
    ok, _errs = _validate_grounding_density(payload)
    assert ok is True


def test_b7_cite_marker_passes() -> None:
    """prod-bug-bash-039 Phase 4 (0.39.3) — `[cite:N]` marker satisfies the
    density gate (parity with [num:V:U] and [ref:N])."""
    payload = {
        "exec_summary": "x" * 100 + " 来源 [cite:1]",
        "insights": [],
    }
    ok, errs = _validate_grounding_density(payload)
    assert ok is True, errs


def test_b7_callout_only_evidence_skips_gate() -> None:
    """0.39.5 prod walkthrough fix (turn t_af5d8dbe): when peer LLM emits
    insights with kind='computation' but missing artifact_id, payload_auto_fix
    R13 drops the bad evidence and R10 synthesizes callout-only replacement.

    With no citable artifact in evidence, the density gate should SKIP rather
    than reject — _auto_append_citations has nothing to inject, so enforcing
    the gate just triggers reject→retry→strip-and-pass and degrades UX.
    """
    long_text = (
        "线性回归模型表明客流与营业额呈强正相关 r=0.716 p<0.01 统计显著 "
        "建议在客流低位月加大营销投入提升客单价以巩固关系并优化结构 同时 "
        "需要关注 Q4 异常波动并复盘归因。"
    )
    assert len(long_text) > 80, len(long_text)
    payload = {
        "exec_summary": long_text,
        "insights": [
            {
                "title": "x",
                # all callout, no chart/comp/table id (R10-style synth)
                "evidence": [
                    {"kind": "callout", "callout_text": "客流低谷需关注 Q4 走势"},
                ],
                "facts": [{"text": long_text}],
            },
        ],
        "recommendations": [{"text": long_text}],
    }
    ok, errs = _validate_grounding_density(payload)
    assert ok is True, errs  # NO errors despite 0 citation markers


def test_b7_gate_still_fires_when_real_artifact_exists() -> None:
    """Regression guard for the callout-only skip — if ANY insight has a real
    chart_section_id / computation_artifact_id / table_artifact_id, the gate
    MUST still fire on substantive text.

    M3-2 换锚适配：原 fixture 里的「Q4」在新判据下算 grounded（数字即锚），会让
    这条 guard 假通过 → 文案换成真·无数字文本，测的仍是「有可引用 artifact 时闸
    不该被 skip 掉」这个原意。
    """
    long_text = (
        "客流量与营业额呈强正相关 营业额随客流量增长 建议加大营销投入提升 "
        "客单价以巩固该关系并扩大渠道覆盖率与会员复购率持续优化 同时 "
        "建议关注季度末异常波动并启动归因复盘工作。"
    )
    assert len(long_text) > 80, len(long_text)
    payload = {
        "exec_summary": long_text,
        "insights": [
            {
                "title": "x",
                # has citable artifact → gate enforces
                "evidence": [{"chart_section_id": "sec_real"}],
                "facts": [{"text": long_text}],
            },
        ],
    }
    ok, errs = _validate_grounding_density(payload)
    assert ok is False  # gate fires
    assert any("exec_summary" in e for e in errs)


def test_b7_grounding_judgement_is_broad_bare_year_counts() -> None:
    """**已知过宽面**（M1 impl review 记档为 accepted/deferred）：判据是「任一数字」，
    所以裸年份 / 季度号（`Q4`、`2026年`）也算 grounded —— 一段没有任何真数据的
    分析文本只要提一句年份就能过闸。

    这条测试把该行为**显式钉住**，不是认可它：`BOWHEAD_B7_GROUNDING=enforce`
    翻转前必须先收紧判据（review log「B7 enforce 前收紧判据」），否则 enforce 只
    是个摆设。现状 shadow 只记不拒，过宽=零用户影响。
    """
    numberless = (
        "客流量与营业额呈强正相关 营业额随客流量增长 建议加大营销投入提升 "
        "客单价以巩固该关系并扩大渠道覆盖率与会员复购率持续优化 同时 "
        "建议关注季度末异常波动并启动归因复盘工作。"
    )
    payload_no_num = {"exec_summary": numberless, "kpis": [{"label": "x", "value": "1"}]}
    assert _validate_grounding_density(payload_no_num)[0] is False
    # Same text + a bare quarter number → passes (evidence that the criterion is too broad).
    payload_bare_year = {"exec_summary": numberless.replace("季度末", "Q4"), "kpis": [{"label": "x", "value": "1"}]}
    assert _validate_grounding_density(payload_bare_year)[0] is True


# ─── prod-bug-bash-039 Phase 4 (0.39.3): _auto_append_citations ──────────


class _StubCtx:
    """Minimal stub for parent_ctx — supports setattr for lazy-init registry."""


def test_auto_append_skips_when_no_analytical_content() -> None:
    """No insights / no kpis / no recs / short exec_summary → no-op."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    payload: dict[str, Any] = {"exec_summary": "短", "insights": []}
    records = _auto_append_citations(payload, _StubCtx())
    assert records == []
    assert payload["exec_summary"] == "短"


def test_auto_append_skips_bail_envelope() -> None:
    """bail envelope (error.kind set) → no auto-append."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    payload = {
        "exec_summary": "x" * 100,
        "insights": [{"title": "x", "evidence": [{"chart_section_id": "sec_1"}], "facts": []}],
        "error": {"kind": "transient", "message": "fail"},
    }
    records = _auto_append_citations(payload, _StubCtx())
    assert records == []


def test_auto_append_exec_summary_uses_first_insight_evidence() -> None:
    """exec_summary > 80 chars + no marker + insight with chart_section_id
    → backend appends `[cite:1]` to exec_summary."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    payload = {
        "exec_summary": "本月销售下降明显，主要原因是渠道客流下降，营收下降明显需立即关注 GMV 全线下滑。" * 3,
        "insights": [
            {
                "title": "客流",
                "evidence": [{"chart_section_id": "sec_abc"}],
                "facts": [],
            },
        ],
    }
    ctx = _StubCtx()
    records = _auto_append_citations(payload, ctx)
    assert any(r["field"] == "exec_summary" for r in records), records
    assert "[cite:1]" in payload["exec_summary"]


def test_auto_append_insight_fact_per_insight_evidence() -> None:
    """insights[i].facts[j].text > 50 + no marker + insights[i] has evidence
    → append [cite:N] using THAT insight's evidence."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    long_a = "渠道 A 客流下降明显主因竞争对手促销力度远大于本品牌且我们投放预算低迷导致 GMV 落后明显需立即处理 P0 优先级"
    long_b = "营收同比下滑显著主要受高客流时段优惠失效影响复购率有所下滑且新客增长动能严重不足建议加大投放 P1 优先级"
    assert len(long_a) > 50 and len(long_b) > 50, (len(long_a), len(long_b))
    payload = {
        "insights": [
            {
                "title": "渠道",
                "evidence": [{"computation_artifact_id": "art_aaa"}],
                "facts": [{"text": long_a}],
            },
            {
                "title": "营收",
                "evidence": [{"table_artifact_id": "tbl_bbb"}],
                "facts": [{"text": long_b}],
            },
        ],
    }
    records = _auto_append_citations(payload, _StubCtx())
    # Each fact gets its own insight's artifact (different artifacts → different N)
    fact_texts = [payload["insights"][i]["facts"][0]["text"] for i in (0, 1)]
    assert "[cite:1]" in fact_texts[0]
    assert "[cite:2]" in fact_texts[1]
    assert {r["field"] for r in records} == {"insights[0].facts[0]", "insights[1].facts[0]"}


def test_auto_append_exec_summary_lands_before_trailing_figure_anchor() -> None:
    """chat 行内图互作：exec_summary 以独占一行的 `[figure:sec_x]` 锚行收尾时，
    auto-append 的 `[cite:N]` 必须贴在散文行尾，不得粘上锚行——粘上 = FE 锚行
    判据（独占一行）失配 ⇒ 图静默掉回末尾堆。"""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    prose = "本月销售下降明显，主要原因是渠道客流下降，营收下降明显需立即关注 GMV 全线下滑。" * 3
    payload = {
        "exec_summary": prose + "\n[figure:sec_abc]",
        "insights": [
            {"title": "客流", "evidence": [{"chart_section_id": "sec_abc"}], "facts": []},
        ],
    }
    records = _auto_append_citations(payload, _StubCtx())
    assert any(r["field"] == "exec_summary" for r in records), records
    out_lines = payload["exec_summary"].split("\n")
    # Anchor remains on its own line (last line is still a bare anchor).
    assert out_lines[-1] == "[figure:sec_abc]", out_lines
    # Marker at the end of the prose line.
    assert out_lines[0].endswith("[cite:1]"), out_lines


def test_auto_append_exec_summary_inserts_before_line_end_anchor() -> None:
    """74d0e81c3 行尾锚形态（peer 真实写法 `…反弹。[figure:sec_a]`）：marker 必须
    插到尾部锚点串之前——贴行尾会粘到锚点后、FE 行尾判据失配 ⇒ 图掉回末尾。"""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    prose = "本月销售下降明显，主要原因是渠道客流下降，营收下降明显需立即关注 GMV 全线下滑。" * 3
    payload = {
        "exec_summary": prose + "未见明显反弹。[figure:sec_abc]",
        "insights": [
            {"title": "客流", "evidence": [{"chart_section_id": "sec_abc"}], "facts": []},
        ],
    }
    _auto_append_citations(payload, _StubCtx())
    out = payload["exec_summary"]
    # Line still ends with the anchor (frontend trailing-anchor parsing); marker stays in the preceding prose.
    assert out.endswith("[figure:sec_abc]"), out[-60:]
    assert "[cite:1] [figure:sec_abc]" in out or "[cite:1][figure:sec_abc]" in out, out[-80:]


def test_auto_append_skips_pure_anchor_run_line() -> None:
    """review C5：两个锚并排独占一行（纯锚点串）不是散文行——marker 必须贴到
    上一行散文尾，不得插到锚点串行首。"""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    prose = "本月销售下降明显，主要原因是渠道客流下降，营收下降明显需立即关注 GMV 全线下滑。" * 3
    payload = {
        "exec_summary": prose + "\n[figure:sec_a] [figure:sec_b]",
        "insights": [
            {"title": "客流", "evidence": [{"chart_section_id": "sec_a"}], "facts": []},
        ],
    }
    _auto_append_citations(payload, _StubCtx())
    out_lines = payload["exec_summary"].split("\n")
    assert out_lines[-1] == "[figure:sec_a] [figure:sec_b]", out_lines
    assert out_lines[0].endswith("[cite:1]"), out_lines


def test_auto_append_fact_lands_before_trailing_figure_anchor() -> None:
    """facts 同款：fact.text 以锚行收尾时 marker 贴散文行，锚行保持独占。"""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    long_fact = "渠道 A 客流下降明显主因竞争对手促销力度远大于本品牌且我们投放预算低迷导致 GMV 落后明显需立即处理 P0 优先级"
    assert len(long_fact) > 50
    payload = {
        "insights": [
            {
                "title": "渠道",
                "evidence": [{"chart_section_id": "sec_z"}],
                "facts": [{"text": long_fact + "\n[figure:sec_z]"}],
            },
        ],
    }
    _auto_append_citations(payload, _StubCtx())
    out_lines = payload["insights"][0]["facts"][0]["text"].split("\n")
    assert out_lines[-1] == "[figure:sec_z]", out_lines
    assert out_lines[0].endswith("[cite:1]"), out_lines


def test_auto_append_dedup_same_artifact_returns_same_seq() -> None:
    """Same artifact_id referenced from multiple fields → same `[cite:N]`."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    fact_long = "渠道流量大幅下滑主因竞品促销加剧且自家短视频投流预算被砍减导致 GMV 落后明显需立即处理 P0 优先级"
    assert len(fact_long) > 50, len(fact_long)
    payload = {
        "exec_summary": "本月营收同比下滑显著，渠道流量下降是主因，需立即优化营销策略以挽回市场份额 GMV 全线下滑。" * 3,
        "insights": [
            {
                "title": "x",
                "evidence": [{"chart_section_id": "sec_shared"}],
                "facts": [{"text": fact_long}],
            },
        ],
    }
    records = _auto_append_citations(payload, _StubCtx())
    # exec_summary + fact share an artifact_id → same seq (do not add recommendation citations, FU-1).
    assert "[cite:1]" in payload["exec_summary"]
    assert "[cite:1]" in payload["insights"][0]["facts"][0]["text"]
    assert len(records) == 2
    assert all(r["artifact_id"] == "sec_shared" for r in records)


def test_auto_append_idempotent_skips_already_marked() -> None:
    """text already containing [num:V:U] / [ref:N] / [cite:N] → not touched."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    payload = {
        "exec_summary": "x" * 100 + " 来源 [num:12.3:%]",
        "insights": [
            {
                "title": "x",
                "evidence": [{"chart_section_id": "sec_xx"}],
                "facts": [
                    {"text": "x" * 60 + " [ref:1]"},
                ],
            },
        ],
    }
    before_exec = payload["exec_summary"]
    before_fact = payload["insights"][0]["facts"][0]["text"]
    records = _auto_append_citations(payload, _StubCtx())
    assert records == []
    assert payload["exec_summary"] == before_exec
    assert payload["insights"][0]["facts"][0]["text"] == before_fact


def test_auto_append_no_evidence_returns_empty() -> None:
    """insight has facts but no evidence → no artifact to cite → no-op."""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    payload = {
        "exec_summary": "本月销售下降明显，主要原因是渠道客流下降，营收下降明显需立即关注。" * 2,
        "insights": [
            {
                "title": "x",
                "evidence": [],  # no artifact slots
                "facts": [
                    {"text": "x" * 60},
                ],
            },
        ],
    }
    records = _auto_append_citations(payload, _StubCtx())
    assert records == []


def test_auto_append_evidence_priority_chart_over_computation() -> None:
    """When an insight has multiple evidence types, chart_section_id wins
    over computation_artifact_id over table_artifact_id, ACROSS the whole
    evidence list (priority outer, evidence inner).

    Fixed in 0.39.3 — CTO BE-Review #1 P0-1 (loop nesting was wrong; chart
    in evidence[1] lost to computation in evidence[0]). The right behavior:
    chart wins regardless of position because clicking a chart_section_id
    chip actually scrolls to a Canvas section, while computation_artifact_id
    has no DOM target.
    """
    from bowhead_core.subagent_return_tool import _auto_append_citations

    payload = {
        "insights": [
            {
                "title": "x",
                "evidence": [
                    {"computation_artifact_id": "art_comp"},
                    {"chart_section_id": "sec_chart"},  # WINS even though later
                    {"table_artifact_id": "tbl_tbl"},
                ],
                "facts": [
                    {"text": "x" * 60},
                ],
            },
        ],
    }
    records = _auto_append_citations(payload, _StubCtx())
    assert len(records) == 1
    # Chart wins across the whole evidence list — priority outer, evidence inner
    assert records[0]["artifact_id"] == "sec_chart"


def test_auto_append_recommendations_not_cited() -> None:
    """FU-1（writer-payload-debt）：recommendations **不自动补引用**（挂第一 insight 的
    artifact = 弱关联 / 假溯源）。本测用真实字段 `action`（非旧代码误读的 `text`）。"""
    from bowhead_core.subagent_return_tool import _auto_append_citations

    rec_text = (
        "建议加大数字营销预算 30%，重点投放短视频平台，预期可拉回约 800 万元 GMV "
        "投入产出比 1:2.5 短期内可见效请审批并落实由市场部协同执行避免再次降速"
    )
    assert len(rec_text) > 80, len(rec_text)
    payload = {
        "insights": [
            {"title": "客流", "evidence": [{"chart_section_id": "sec_q1"}], "facts": []},
        ],
        "recommendations": [{"action": rec_text}],
    }
    records = _auto_append_citations(payload, _StubCtx())
    assert not any(r["field"].startswith("recommendations") for r in records)
    assert "[cite:" not in payload["recommendations"][0]["action"]


# ── citation-audit M1-0: B7 anchor swap (grounding gate) ─────────────────────────


def test_grounding_gate_accepts_plain_prose_numbers() -> None:
    """新契约：纯散文数字即 grounded——marker 语法要求消失（backend 绑定 [N]）。"""
    from bowhead_core.subagent_return_tool import _validate_grounding_density

    payload = {
        "exec_summary": "全省合计 9627 万元，同比增长 4.18%，头部三省贡献超六成，结构性集中度持续上行。",
        "insights": [
            {
                "title": "集中度",
                "facts": [{"text": "CR3 达 76.25%，较去年提升 1.8pp，头部效应在全部区域市场同步强化。"}],
                "evidence": [{"kind": "chart", "chart_section_id": "sec_a"}],
            }
        ],
    }
    ok, errs = _validate_grounding_density(payload)
    assert ok and not errs


def test_grounding_gate_flags_numberless_analysis() -> None:
    """闸的意图保留：>80 字 0 数字 0 引用的分析文本 = 无出处结论。"""
    from bowhead_core.subagent_return_tool import _validate_grounding_density

    payload = {
        "exec_summary": (
            "整体表现相当不错，各个区域市场都有十分明显的进步，头部市场继续保持领先地位，"
            "腰部市场追赶势头良好，尾部市场也在逐步企稳，未来预计还会持续向好，"
            "建议保持当前策略不变并且加大资源投入力度，同时密切关注竞争格局变化。"
        ),
        "insights": [
            {
                "title": "走势",
                "facts": [],
                "evidence": [{"kind": "chart", "chart_section_id": "sec_a"}],
            }
        ],
    }
    ok, errs = _validate_grounding_density(payload)
    assert not ok
    assert any("0 数字" in e for e in errs)


def test_grounding_gate_marker_still_counts() -> None:
    """过渡期 peer 仍可能发 marker——marker 也算 grounded（不双重惩罚）。"""
    from bowhead_core.subagent_return_tool import _validate_grounding_density

    payload = {
        "exec_summary": (
            "增长情况详见引用来源标注的核心指标数值明细清单里的说明部分内容 [ref:2]，整体结构保持稳定且符合前期作出的判断与相应预期水平。"
        ),
    }
    ok, _ = _validate_grounding_density(payload)
    assert ok
