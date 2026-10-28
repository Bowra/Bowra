"""Unit tests for cross_turn_digest.build_cross_turn_summary (P0-A line 1)."""

from __future__ import annotations

from bowhead_core.cross_turn_digest import (
    build_cross_turn_summary,
    render_recent_turns_block,
)


def _report_payload() -> dict:
    return {
        "exec_summary": "2025年各产品线销售金额同比基本持平，约六成下滑。",
        "kpis": [
            {"label": "整体销售金额同比", "value": "基本持平", "delta": "+0.3%"},
            {"label": "呼吸系统用药增幅", "value": ">15%", "delta": "+15%"},
        ],
        "insights": [
            {"title": "呼吸/抗肿瘤量价齐升", "thesis": "..."},
            {"title": "抗生素/心血管显著下滑", "thesis": "..."},
        ],
        "sources": [{"artifact_id": "a"}, {"artifact_id": "b"}],
    }


def test_delegated_report_turn_pulls_from_payload() -> None:
    s = build_cross_turn_summary(
        turn_id="t_419",
        user_text="你看看去年各药品的销售变化，给我一个summary吧",
        intent="report",
        final_answer="2025 年各产品线销售变化的关键发现：…完整报告见画布。",
        aggregated_payload=_report_payload(),
        canvas_id="cv_abc",
    )
    assert s["turn_id"] == "t_419"
    assert s["intent"] == "report"
    # conclusion comes from exec_summary, not the chat prose
    assert s["conclusion"].startswith("2025年各产品线")
    assert s["canvas_id"] == "cv_abc"
    assert [k["label"] for k in s["key_facts"]] == ["整体销售金额同比", "呼吸系统用药增幅"]
    assert s["insights"] == ["呼吸/抗肿瘤量价齐升", "抗生素/心血管显著下滑"]
    assert s["produced"] == {"kind": "report", "sources_count": 2}


def test_zero_kpi_value_preserved() -> None:
    # review fix: a literal-0 KPI value is falsy but meaningful — must render.
    s = build_cross_turn_summary(
        turn_id="t",
        user_text="缺货",
        intent="metric",
        final_answer="缺货 0 次",
        aggregated_payload={"kpis": [{"label": "缺货次数", "value": 0}]},
    )
    assert s["key_facts"][0]["value"] == "0"


def test_self_answer_turn_no_payload_uses_final_answer() -> None:
    s = build_cross_turn_summary(
        turn_id="t_simple",
        user_text="今年销售额多少",
        intent="metric",
        final_answer="今年销售额 96.11 亿元。还有别的要看的吗？",
        aggregated_payload=None,
    )
    assert s["turn_id"] == "t_simple"
    # conclusion = first sentence of the chat answer
    assert s["conclusion"].startswith("今年销售额 96.11 亿元")
    assert "key_facts" not in s
    assert "insights" not in s
    assert "canvas_id" not in s
    assert "produced" not in s


def test_internal_markers_stripped() -> None:
    # [cite:N]/[ref:...]/[num:val:unit] are turn-scoped; must not leak into the
    # digest (→ next turn's prompt). The plain numbers/text must survive.
    s = build_cross_turn_summary(
        turn_id="t",
        user_text="销售情况",
        intent="report",
        final_answer="2025总销售额 96.11 亿 [num:96.11:亿元]，同比 5.1% [cite:1]。报告见画布 [ref:S]。",
        aggregated_payload={
            "exec_summary": "整体增长 5.1% [cite:1]，区域分化明显 [cite:2]。",
            "kpis": [{"label": "总销售额 [cite:1]", "value": "96.11 亿 [num:96.11:亿元]", "delta": "+5.1% [cite:1]"}],
            "insights": [{"title": "增长稳健 [cite:3]"}],
        },
    )
    for field in (
        s["conclusion"],
        s["final_answer_full"],
        s["insights"][0],
        s["key_facts"][0]["label"],
        s["key_facts"][0]["value"],
        s["key_facts"][0]["delta"],
    ):
        assert "[cite:" not in field and "[num:" not in field and "[ref:" not in field, field
    # plain content survived the strip
    assert "96.11" in s["key_facts"][0]["value"]
    assert s["conclusion"].startswith("整体增长 5.1%")
    assert "+5.1%" in s["key_facts"][0]["delta"]


def test_dirty_input_never_throws() -> None:
    # odd/empty inputs must degrade, not raise (caller wraps best-effort)
    s = build_cross_turn_summary(
        turn_id="t",
        user_text="",
        intent=None,
        final_answer="",
        aggregated_payload={"kpis": ["not-a-dict", {"no_label": 1}], "insights": [42]},
    )
    assert s["turn_id"] == "t"
    assert s["intent"] is None
    assert "key_facts" not in s  # all kpis filtered out (no valid label)
    assert "insights" not in s


def test_caps_applied() -> None:
    long = "句" * 5000
    s = build_cross_turn_summary(
        turn_id="t",
        user_text=long,
        intent="x",
        final_answer=long,
        aggregated_payload={"kpis": [{"label": "L", "value": "V"} for _ in range(20)]},
    )
    assert len(s["user_text"]) <= 241  # _MAX_USER_TEXT + ellipsis
    assert len(s["final_answer_full"]) <= 601
    assert len(s["key_facts"]) == 6  # _MAX_KEY_FACTS
    # kpi with no delta → delta omitted as None
    assert s["key_facts"][0]["delta"] is None


# ── read side: render_recent_turns_block ─────────────────────────────────────


def test_render_empty_is_empty_string() -> None:
    assert render_recent_turns_block([]) == ""
    assert render_recent_turns_block([None, "x", 3]) == ""  # all non-dicts dropped
    # the topic-continuity rule must NOT leak when there is no prior turn (guards a future
    # refactor that moves the rule outside the `if rows` block — review-fix).
    assert "Topic continuity" not in render_recent_turns_block([])


def test_render_includes_report_and_reuse_hint() -> None:
    digests = [
        {
            "user_text": "你看看去年各药品的销售变化，给我一个summary吧",
            "conclusion": "2025各产品线同比基本持平，约六成下滑。",
            "key_facts": [{"label": "整体同比", "value": "持平", "delta": "+0.3%"}],
            "insights": ["呼吸/抗肿瘤量价齐升"],
            "canvas_id": "cv_367b",
        },
    ]
    block = render_recent_turns_block(digests)
    # surfaces the prior question + conclusion + facts + insight
    assert "各药品的销售变化" in block
    assert "2025各产品线同比基本持平" in block
    assert "整体同比=持平(+0.3%)" in block
    assert "呼吸/抗肿瘤量价齐升" in block
    # the reuse-as-deck nudge (helps "做个ppt" reuse the report)
    assert "canvas cv_367b" in block
    assert "reuse" in block and "do not rerun the data" in block


def test_render_prose_askback_question_survives() -> None:
    # Regression (prod s_ae271982f3 seq2→seq3): a turn that ends by ASKING the
    # user a prose question must carry that question into the next turn's Lead
    # context. The old renderer emitted only `conclusion`
    # (= _first_sentence(final_answer)) which dropped the ask-back, so a one-word
    # follow-up ("北京") was misread as a fresh query instead of the awaited city.
    digests = [
        {
            "user_text": "那你可以联网搜索一下今天天气么",
            "conclusion": "好的，我帮你搜一下今天的天气。",  # first sentence only
            "final_answer_full": (
                "好的，我帮你搜一下今天的天气。不过我需要知道你在哪个城市——不同地方天气差别很大。你方便告诉我城市吗？我先不搜，等你确认城市再查。"
            ),
        },
    ]
    block = render_recent_turns_block(digests)
    # the pending question — the part dropped before the fix — must be present
    assert "我需要知道你在哪个城市" in block
    assert "等你确认城市再查" in block


def test_render_topic_continuity_fires_ungated_even_after_refusal() -> None:
    # Regression (prod s_c6887db3ad turn 2): turn 1 was a BI refusal ("天气不在 BI
    # 数据集里", open_world=false); the user then said "你联网搜索一下" and the Lead
    # re-asked "搜什么?" instead of continuing the weather topic. Dogfood: the lead
    # model (deepseek-v4-pro) re-clarified 11/11 because the only continuity hint
    # (source continuity) is gated on open_world and never fired. Fix = UNGATED topic continuity.
    # rule that inherits the prior turn's SUBJECT even after a no-tool/refusal turn.
    digests = [
        {
            "user_text": "帮我看看今天天气",
            "final_answer_full": "我的工作区是 BI 数据分析环境，没有接入天气服务。",
            "answered_via": {"source": "model", "open_world": False},
        },
    ]
    block = render_recent_turns_block(digests, source_continuity=True)
    # topic-continuity present even though the prior turn was a refusal (open_world=false)
    assert "Topic continuity" in block
    assert "do not ask back" in block
    # the over-fire carve-out (review-fix): an explicit topic-switch / ambiguous prior turn
    # must NOT be force-continued — so "不要反问" is not absolute.
    assert "explicitly changes the subject" in block and "别的" in block
    # the open_world-gated source-continuity clause stays OFF for a non-web prior turn
    assert "Source continuity" not in block


def test_render_topic_continuity_is_independent_of_source_continuity_flag() -> None:
    # Topic continuity is ungated: it renders regardless of source_continuity (which only
    # governs the open_world source-continuity clause). A bare-action follow-up like
    # "你联网搜索一下" must inherit the prior topic in both flag states.
    digests = [{"user_text": "上海各月销售对比", "conclusion": "基本持平"}]
    assert "Topic continuity" in render_recent_turns_block(digests, source_continuity=False)
    assert "Topic continuity" in render_recent_turns_block(digests, source_continuity=True)


def test_render_bi_turn_keeps_distinct_exec_summary() -> None:
    # When `conclusion` is a distinct exec_summary thesis (not contained in the
    # chat reply), both the full reply AND the thesis are rendered.
    digests = [
        {
            "user_text": "看看北京营业额",
            "conclusion": "北京近一年月均营业额 4000-5000 万，春节旺季冲 6000 万+。",
            "final_answer_full": "拿到了。北京累计营业额约 3.46 亿元，1 月最高 6036 万。",
            "key_facts": [{"label": "累计营业额", "value": "3.46亿"}],
        },
    ]
    block = render_recent_turns_block(digests)
    assert "拿到了。北京累计营业额约 3.46 亿元" in block  # full reply
    assert "春节旺季冲 6000 万+" in block  # distinct thesis kept
    assert "累计营业额=3.46亿" in block  # structured facts intact


def test_render_bounded_size_drops_oldest() -> None:
    big = "字" * 2000
    digests = [{"user_text": f"q{i}", "conclusion": big, "canvas_id": f"cv{i}"} for i in range(5)]
    block = render_recent_turns_block(digests)
    assert len(block) <= 3000 + 2  # _MAX_BLOCK_CHARS (+ ellipsis)
    # newest turn (q4) must survive the trim; oldest (q0) dropped
    assert "q4" in block


def test_render_canvas_mode_hint_only_with_section_revise() -> None:
    """canvas-update-turn 批 B0：「续轮档位」提示只在调用方说 section_revise=True 时追加（调用方按
    「flag 开 且 画布确有上一轮可见报告」传，不再看 digest 的 canvas_id——review 修）；整块只提一次；关时逐字节不变。"""
    d = [{"user_text": f"q{i}", "conclusion": "c", "canvas_id": "cv1"} for i in range(3)]
    off = render_recent_turns_block(d)
    on = render_recent_turns_block(d, section_revise=True)
    assert "Follow-up mode" not in off and "canvas_mode" not in off
    assert on.count("Follow-up mode") == 1 and "`revise`" in on and "`append`" in on and "`new_report`" in on
    assert "one mode per turn" in on and "compute" in on  # Routing: delegate revisions needing recalculation to compute.
    assert on.startswith(off)  # Append the hint at the end; preceding bytes unchanged.


def test_canvas_mode_hint_routes_only_to_registered_agents() -> None:
    """续轮档位提示里点名的 Agent 必须真能被派（点名一个不存在的 Agent，Lead 会照着去派）。"""
    import re

    from bowhead_core.agents import AgentRegistry
    from bowhead_core.cross_turn_digest import _SECTION_REVISE_HINT

    routed = set(re.findall(r"\b(?:compute|consultant|[a-z]+-(?:analyst|writer))\b", _SECTION_REVISE_HINT))
    assert "compute" in routed
    assert routed <= set(AgentRegistry.bootstrap().list_all()), routed
