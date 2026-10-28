"""交付面自决（compute-agent-optimize 批 5ⓐ，用户拍板 2026-08-20）—— 决定链纯函数。

原则：chat 还是报告由 **agent 决定**，后端按「谁明说了听谁的」挑一个，不判断
「这算不算报告」。每档成对测（命中 + 不命中），并对回滚绳 flag 留负对照。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.delivery_decision import (
    decide_delivery,
    peer_declared_surface,
    plan_kind_to_surface,
    render_chat_answer_markdown,
)

pytestmark = pytest.mark.unit


def _base(**kw):  # noqa: ANN003, ANN202
    args = {
        "user_directive": None,
        "plan_deliverable_kind": None,
        "lead_delivery": None,
        "peer_payloads": [],
        "scanner_pre_flight": None,
        "evidence_canvas": False,
        "heuristic_surface": "chat",
    }
    args.update(kw)
    return decide_delivery(**args)


# ── Tiers: paired matches and nonmatches ────────────────────────


def test_user_directive_wins_over_everything() -> None:
    d = _base(
        user_directive="chat",
        plan_deliverable_kind="report",
        lead_delivery="report",
        peer_payloads=[{"payload": {"delivery_surface": "report"}}],
        evidence_canvas=True,
    )
    assert (d.surface, d.tier) == ("chat", "user")


def test_plan_wins_over_lead_and_peer() -> None:
    d = _base(plan_deliverable_kind="answer", lead_delivery="report", peer_payloads=[{"payload": {"delivery_surface": "report"}}])
    assert (d.surface, d.tier) == ("chat", "plan")
    d2 = _base(plan_deliverable_kind="report", lead_delivery="chat")
    assert (d2.surface, d2.tier) == ("canvas", "plan")


def test_plan_kind_without_surface_meaning_does_not_take_the_tier() -> None:
    # none / actions_summary / absent do not occupy the tier; fall through to Lead.
    for kind in (None, "", "none", "actions_summary"):
        d = _base(plan_deliverable_kind=kind, lead_delivery="chat")
        assert (d.surface, d.tier) == ("chat", "lead"), kind


def test_lead_wins_over_peer() -> None:
    d = _base(lead_delivery="report", peer_payloads=[{"payload": {"delivery_surface": "chat", "exec_summary": "x"}}])
    assert (d.surface, d.tier) == ("canvas", "lead")


def test_peer_declaration_is_a_tier() -> None:
    d = _base(peer_payloads=[{"payload": {"delivery_surface": "chat", "exec_summary": "今年 GMV 5.2 亿"}}])
    assert (d.surface, d.tier) == ("chat", "peer")
    d2 = _base(peer_payloads=[{"payload": {"delivery_surface": "report", "title": "复盘"}}])
    assert (d2.surface, d2.tier) == ("canvas", "peer")


def test_peer_chat_declaration_survives_report_shaped_content() -> None:
    """★核心：peer 说 chat，payload 里带 insights/recommendations 也**不**翻成报告——
    那只是它想在聊天里说的内容（后端不判断算不算报告）。"""
    d = _base(
        peer_payloads=[
            {
                "payload": {
                    "delivery_surface": "chat",
                    "exec_summary": "结论",
                    "insights": [{"title": "a", "narrative": "b"}],
                    "recommendations": [{"priority": "P0", "action": "做"}],
                }
            }
        ],
        evidence_canvas=True,
    )
    assert (d.surface, d.tier) == ("chat", "peer")


def test_scanner_then_evidence_then_heuristic() -> None:
    assert _base(scanner_pre_flight="canvas").tier == "scanner"
    assert _base(evidence_canvas=True) == _base(evidence_canvas=True)
    d = _base(evidence_canvas=True)
    assert (d.surface, d.tier) == ("canvas", "evidence")
    d2 = _base(heuristic_surface="canvas")
    assert (d2.surface, d2.tier) == ("canvas", "heuristic")


# ── Peer aggregation rules ─────────────────────────────────────


def test_peer_mixed_declarations_report_wins() -> None:
    payloads = [
        {"payload": {"delivery_surface": "chat", "exec_summary": "x"}},
        {"payload": {"delivery_surface": "report", "title": "t"}},
    ]
    assert peer_declared_surface(payloads) == "canvas"


def test_peer_partial_declaration_does_not_claim_chat() -> None:
    """一个说 chat、另一个没说 → 不占档（不能拿一个 peer 的决定代表整轮）。"""
    payloads = [
        {"payload": {"delivery_surface": "chat", "exec_summary": "x"}},
        {"payload": {"exec_summary": "没声明"}},
    ]
    assert peer_declared_surface(payloads) is None


def test_peer_bail_and_deliverable_payloads_do_not_vote() -> None:
    assert peer_declared_surface([{"payload": {"error": {"kind": "domain_mismatch", "message": "m"}}}]) is None
    assert peer_declared_surface([{"payload": {"delivery_surface": "chat", "deliverables": [{"kind": "pptx"}]}}]) is None


def test_peer_payload_accepts_bare_dicts_too() -> None:
    assert peer_declared_surface([{"delivery_surface": "report"}]) == "canvas"


def test_plan_kind_mapping() -> None:
    assert plan_kind_to_surface("answer") == "chat"
    assert plan_kind_to_surface("report") == "canvas"
    assert plan_kind_to_surface("deck") == "canvas"
    assert plan_kind_to_surface("ARTIFACT") == "canvas"
    assert plan_kind_to_surface("none") is None


# ── flag ────────────────────────────────────────────────────────────────


# ── Chat body rendering ────────────────────────────────────────


def test_chat_render_keeps_everything_the_peer_wanted_to_say() -> None:
    md = render_chat_answer_markdown(
        {
            "delivery_surface": "chat",
            "exec_summary": "今年 GMV 5.2 亿，同比 +12%。",
            "kpis": [{"label": "GMV", "value": "5.2 亿", "delta": "+12%"}],
            "insights": [{"title": "华东领跑", "thesis": "占比 41%", "narrative": "主要由 A 品牌带动。"}],
            "recommendations": [{"priority": "P0", "action": "加投华东"}],
            "caveats": ["4 月数据未回补"],
        }
    )
    for frag in ("5.2 亿", "GMV", "+12%", "华东领跑", "加投华东", "4 月数据未回补"):
        assert frag in md


def test_chat_render_has_no_language_specific_glue(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ review-R6：渲染不许拼任何语言相关的粘合词——英文轮的 final_answer 混进中文小标题，
    而 Lead 的 directive 是「直接使用，不要改写」。"""
    md = render_chat_answer_markdown(
        {
            "exec_summary": "Q3 revenue was 5.2B CNY, up 12% YoY.",
            "recommendations": [{"priority": "P0", "action": "Increase East-China spend"}],
            "caveats": ["April data not backfilled"],
        }
    )
    for zh_glue in ("建议：", "注意：", "结论："):
        assert zh_glue not in md, f"渲染拼了中文粘合词 {zh_glue!r}"
    assert "Increase East-China spend" in md and "April data not backfilled" in md


def test_chat_render_null_kpi_value_is_skipped_not_rendered_empty() -> None:
    """★ review-R10 + review2-R4：value 为 null 的 KPI 直接跳过——渲染成 `- GMV:` 是噪音，
    渲染成 `- GMV: None` 更糟。"""
    md = render_chat_answer_markdown({"exec_summary": "s", "kpis": [{"label": "GMV", "value": None}]})
    assert "None" not in md
    assert "GMV" not in md, "空值 KPI 应整条跳过，而不是留一个没值的行"


def test_chat_render_uses_halfwidth_punctuation_for_kpis() -> None:
    """★ review2-R2：KPI 行的全角「：（）」在英文轮就是混排（`- Revenue：5.2B（+12%）`）。"""
    md = render_chat_answer_markdown({"exec_summary": "Q3 summary.", "kpis": [{"label": "Revenue", "value": "5.2B", "delta": "+12% YoY"}]})
    for fullwidth in ("：", "（", "）"):
        assert fullwidth not in md, f"KPI 行用了全角标点 {fullwidth!r}"
    assert "- Revenue: 5.2B (+12% YoY)" in md


def test_chat_render_separates_caveats_from_recommendations() -> None:
    """★ review2-R3：建议与局限渲成同款 bullet 会让「4月数据未回补」读成待办。
    用 markdown 引用块区分——语言无关，不用任何粘合词。"""
    md = render_chat_answer_markdown(
        {
            "exec_summary": "s",
            "recommendations": [{"action": "加投华东"}],
            "caveats": ["4月数据未回补"],
        }
    )
    assert "- 加投华东" in md
    assert "> 4月数据未回补" in md


def test_chat_render_summary_only_is_just_the_summary() -> None:
    assert render_chat_answer_markdown({"exec_summary": "551"}) == "551"
    assert render_chat_answer_markdown({}) == ""


def test_reference_chat_answer_keeps_requested_verified_substitutions_visible() -> None:
    from bowhead_core.agents.delivery_decision import requested_reference_formula_lines

    payload = {
        "exec_summary": "净利率 10%、总资产周转率 2 次、权益乘数 2 倍、ROE 40%。",
        "ground_facts": [
            {"label": "净利率", "value": 10, "unit": "%", "formula": "20/200*100"},
            {"label": "总资产周转率", "value": 2, "unit": "次", "formula": "200/100"},
            {"label": "权益乘数", "value": 2, "unit": "倍", "formula": "100/50"},
            {"label": "ROE", "value": 40, "unit": "%", "formula": "20/50*100"},
        ],
    }
    user_text = "请列出每项计算公式和结果；缺权益时先澄清。"
    delegated_task = "营业收入 200，净利润 20，总资产 100，已补股东权益 50。"
    answer = render_chat_answer_markdown(payload)
    lines = requested_reference_formula_lines(payload, user_text=user_text, delegated_task=delegated_task, answer=answer)
    rendered = render_chat_answer_markdown(payload, calculation_lines=lines)

    assert len(lines) == 4
    for formula in ("20/200*100", "200/100", "100/50", "20/50*100"):
        assert f"`{formula}`" in rendered
    assert rendered.index("净利率 10%") < rendered.index("`20/200*100`")
    assert requested_reference_formula_lines(payload, user_text="只要结果", delegated_task=delegated_task, answer=answer) == []
    assert requested_reference_formula_lines(payload, user_text="不要给公式", delegated_task=delegated_task, answer=answer) == []
    assert requested_reference_formula_lines(payload, user_text="No calculations, just results", delegated_task=delegated_task, answer=answer) == []


def test_requested_reference_formulas_skip_unverified_or_already_visible_calculations() -> None:
    from bowhead_core.agents.delivery_decision import requested_reference_formula_lines

    payload = {
        "ground_facts": [
            {"label": "已显示", "value": 10, "unit": "%", "formula": "20/200*100"},
            {"label": "错误结果", "value": 50, "unit": "%", "formula": "20/200*100"},
            {"label": "无来源", "value": 10, "unit": "%", "formula": "30/300*100"},
            {"label": "混入无来源数字", "value": 6.67, "unit": "%", "formula": "20/300*100"},
            {"label": "非法表达式", "value": 10, "unit": "%", "formula": "20/200*100 # note"},
        ],
    }
    assert (
        requested_reference_formula_lines(
            payload,
            user_text="请列公式",
            delegated_task="营业收入 200，净利润 20",
            answer="已显示：20/200*100 = 10%",
        )
        == []
    )


def test_chat_contract_tells_the_author_the_arrays_get_appended() -> None:
    """Authors need the order in which summary, KPI, and insight fields are rendered.

    The tool contract must explain that arrays append after the summary so authors
    can avoid repeating the same answer in multiple fields.
    """
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    desc = registry.get("subagent_return").schema.description

    assert "appended VERBATIM below it" in desc
    assert "exactly one of the two places" in desc
    surface = registry.get("subagent_return").schema.input_schema["properties"]["payload"]["properties"]["delivery_surface"]["description"]
    assert "原样拼在 exec_summary 之后" in surface

    # Positive control: documented order matches rendering (summary → KPI → insights → recommendations → caveats).
    md = render_chat_answer_markdown(
        {
            "exec_summary": "总额 5.5 亿",
            "kpis": [{"label": "同比", "value": "+12%"}],
            "insights": [{"title": "华东领跑", "thesis": "占增量六成"}],
            "recommendations": [{"action": "加投华东"}],
            "caveats": ["4月数据未回补"],
        }
    )
    assert md.index("总额 5.5 亿") < md.index("- 同比: +12%") < md.index("**华东领跑**") < md.index("- 加投华东") < md.index("> 4月数据未回补")


def test_revised_turn_override_only_flips_non_user_chat() -> None:
    """canvas-update-turn 批 B0（116 1.2.66 t_78d2ee478a61）：canvas-writer 就地改了上一轮正文，Lead/peer 档却判 chat
    ⇒ compose 不进、修订收尾器不跑。改了上一轮正文的轮：非 user 档的 chat 一律覆盖成 canvas；user 明说 chat 不动。"""
    from bowhead_core.agents.delivery_decision import DeliveryDecision, revised_turn_override

    for tier in ("lead", "peer", "scanner", "heuristic", "evidence"):
        d = revised_turn_override(DeliveryDecision("chat", tier), revised_prior=True)
        assert (d.surface, d.tier) == ("canvas", "canvas_mode"), tier
    assert revised_turn_override(DeliveryDecision("chat", "user"), revised_prior=True).tier == "user"
    assert revised_turn_override(DeliveryDecision("canvas", "lead"), revised_prior=True).tier == "lead"
    assert revised_turn_override(DeliveryDecision("chat", "lead"), revised_prior=False).surface == "chat"


def test_team_agent_transport_describes_the_chat_assembly() -> None:
    """The Team Agent prompt must describe what the renderer shows, or models restate exec_summary in other fields."""
    from bowhead_core.subagent import _materialize_team_profile, _team_revision_projection

    profile = _materialize_team_profile(
        **_team_revision_projection(
            {"tool_refs": [], "skills": [], "system_prompt": "Write the weekly report.", "max_iterations": 3},
            {"name": "weekly", "description": "Summarize weekly regional sales."},
            expected_name="weekly",
        )
    )
    assert profile is not None
    transport = profile.system_prompt.split("## Final answer transport\n", 1)[1].split("\n\n", 1)[0]
    assert "Put the complete answer in exec_summary and leave" not in transport
    assert "State each fact exactly once" in transport
    assert "not a restatement of exec_summary" in transport
    # The order the prompt names is the order the renderer appends.
    named = ["exec_summary", "kpis", "insights", "recommendations", "caveats"]
    assert [transport.index(field) for field in named] == sorted(transport.index(field) for field in named)
    rendered = render_chat_answer_markdown(
        {
            "exec_summary": "S",
            "kpis": [{"label": "K", "value": "1"}],
            "insights": [{"title": "I", "narrative": "N"}],
            "recommendations": [{"action": "R"}],
            "caveats": ["C"],
        }
    )
    assert rendered == "S\n\n- K: 1\n\n**I** N\n\n- R\n\n> C"
