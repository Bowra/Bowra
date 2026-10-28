"""chat-inline-figure-2026-08 批 3 —— 把「可用图表清单」告诉 Lead。

判据见 `docs/sprints/chat-inline-figure-2026-08/tech-design.md` §7。
**不挂 feature flag**：这是补前端缺陷，不是可选形态。退化路径是天然的——
没画图的轮次 no-op，模型不写锚点时前端逐字等于改动前。
"""

from __future__ import annotations

import json as _json
from typing import Any


def json_dumps(o: object) -> str:
    return _json.dumps(o, ensure_ascii=False)


from bowhead_core.subagent import (
    _attach_available_figures,
    _canvas_sections_from_invocations,
)


class _Inv:
    """最小 tool_invocation 替身（只有被读到的三个属性）。"""

    def __init__(self, tool_name: str, result: Any, arguments: dict[str, Any] | None = None) -> None:
        self.tool_name = tool_name
        self.result = result
        self.arguments = arguments or {}


def _chart(sid: str, kind: str = "bar", title: str = "") -> _Inv:
    # ⚠ Actual production shape: add_chart returns `kind` as **chart type** (bar/line/pie/table),
    # not section kind; use the tool name as the criterion, not this field.
    return _Inv("add_chart", {"section_id": sid, "kind": kind, "title": title})


def test_attaches_ids_and_syntax() -> None:
    payload: dict[str, Any] = {"ok": True, "directive": "原有指令。"}
    _attach_available_figures(
        payload,
        [_chart("sec_a", title="营业额趋势"), _chart("sec_b", "line", "利润趋势")],
    )
    assert payload["available_figures"] == [
        {"id": "sec_a", "kind": "bar", "title": "营业额趋势"},
        {"id": "sec_b", "kind": "line", "title": "利润趋势"},
    ]
    d = payload["directive"]
    assert d.startswith("原有指令。")  # Must not overwrite the existing directive
    assert "[figure:<id>" in d and "整行只写" in d
    assert "绝不编造" in d
    # ★ The report-turn condition is load-bearing: the existing report directive prohibits conflict with canvas;
    # without this qualification, the directives contradict each other (frontend silently discards report-turn anchors, wasting and misleading model output).
    assert "仅当本轮以 chat 答案为最终交付" in d
    assert "已交画布报告" in d
    # Do **not** repeat IDs/titles in the directive: duplicating the same contextual data wastes tokens
    # and may contradict itself. The directive describes only syntax and points to `available_figures`.
    assert "sec_a" not in d
    assert "营业额趋势" not in d
    assert "available_figures" in d


def test_no_figures_attaches_nothing() -> None:
    """没画图的轮次不该被塞一段没用的语法说明（白烧 token + 诱导编 id）。"""
    payload: dict[str, Any] = {"ok": True, "directive": "原样"}
    _attach_available_figures(payload, [_Inv("bi_query", {"rows": []})])
    assert payload == {"ok": True, "directive": "原样"}


def test_only_inlineable_tools_are_advertised() -> None:
    """insight_card / recommendations 不是 chat 能内联的产物，不进清单。"""
    payload: dict[str, Any] = {"ok": True}
    _attach_available_figures(
        payload,
        [
            _chart("sec_chart"),
            _Inv("canvas_add_insight", {"section_id": "sec_ins", "title": "洞察"}),
            _Inv("canvas_add_recommendations", {"section_id": "sec_rec", "title": "建议"}),
            _Inv("canvas_add_kpi_row", {"section_id": "sec_kpi", "title": "关键指标"}),
        ],
    )
    ids = [f["id"] for f in payload["available_figures"]]
    assert ids == ["sec_chart", "sec_kpi"]


def test_image_sections_are_advertised_with_section_id() -> None:
    """★ 图片图（地图/雷达/甘特等原生画不了的）**也要能内联**。

    116 dogfood t_6787e8159e94 逮到两侧 id 对不上（清单 `sec_*` / 气泡 `art_*`），
    根因是 `_inline_image_artifact` 少带 `section_id`——**不是图片不该内联**。
    锚点命名空间统一为 canvas section id，这里必须登记 `sec_*`。
    """
    payload: dict[str, Any] = {"ok": True}
    _attach_available_figures(
        payload,
        [
            _Inv(
                "add_image",
                {"section_id": "sec_img", "kind": "image", "artifact_id": "art_xyz"},
                {"caption": "各省销售热力图"},
            ),
        ],
    )
    assert payload["available_figures"] == [
        {"id": "sec_img", "kind": "image", "title": "各省销售热力图"},
    ]
    # Never register artifact IDs; frontend anchors look up section IDs.
    assert "art_xyz" not in json_dumps(payload)


def test_image_title_falls_back_to_caption() -> None:
    """`add_image` 的返回没有 title，标题在参数 `caption` 里。

    不兜这层，清单里就是一行没有标题的 id，模型无从判断哪张图放哪段
    （dogfood 实测两张图的 title 全为空）。
    """
    payload: dict[str, Any] = {"ok": True}
    _attach_available_figures(
        payload,
        [_Inv("add_image", {"section_id": "sec_i"}, {"caption": "雷达图"})],
    )
    assert payload["available_figures"][0]["title"] == "雷达图"


def test_missing_directive_key_is_created() -> None:
    """error 路径的 payload 可能压根没有 directive 键（生产实测存在）。"""
    payload: dict[str, Any] = {"ok": False}
    _attach_available_figures(payload, [_chart("sec_a", title="图")])
    d = payload["directive"]
    assert "available_figures" in d
    # Without an existing directive, do not start with a space (a concatenation artifact).
    assert d == d.lstrip()
    assert payload["available_figures"][0]["id"] == "sec_a"


class TestSectionExtraction:
    """`_canvas_sections_from_invocations` —— W4.2 事件与批 3 清单的共用真源。"""

    def test_chart_kind_comes_from_result_not_toolname(self) -> None:
        got = _canvas_sections_from_invocations([_chart("sec_a", "dual_axis", "加权均价")])
        assert got == [
            {"id": "sec_a", "kind": "dual_axis", "title": "加权均价", "tool": "add_chart"},
        ]

    def test_kind_falls_back_to_toolname_when_absent(self) -> None:
        got = _canvas_sections_from_invocations(
            [_Inv("canvas_add_insight", {"section_id": "s1"}), _Inv("add_chart", {"id": "s2"})],
        )
        assert [(g["id"], g["kind"]) for g in got] == [("s1", "insight_card"), ("s2", "chart")]

    def test_title_falls_back_to_arguments(self) -> None:
        got = _canvas_sections_from_invocations(
            [_Inv("add_chart", {"section_id": "s1"}, {"title": "来自参数"})],
        )
        assert got[0]["title"] == "来自参数"

    def test_skips_non_canvas_and_shapeless_results(self) -> None:
        got = _canvas_sections_from_invocations(
            [
                _Inv("bi_query", {"section_id": "nope"}),  # Not a canvas tool
                _Inv("add_chart", "not-a-dict"),  # result is not a dict
                _Inv("add_chart", {"title": "无 id"}),  # No section_id
                _chart("sec_ok"),
            ],
        )
        assert [g["id"] for g in got] == ["sec_ok"]

    def test_empty_and_none(self) -> None:
        assert _canvas_sections_from_invocations(None) == []
        assert _canvas_sections_from_invocations([]) == []


class TestInlineImageSectionAlias:
    """`_inline_image_artifact` 必须带 `section_id` 别名。

    ★ 没有它，图片图在清单里登记的 `sec_*` 与气泡里的 `art_*` 对不上，锚点必 miss
    ——116 dogfood `t_6787e8159e94` 实测过一次（清单 `sec_3b2fd54d1c2d` /
    气泡 `art_a25c9105b3cb`）。`id` 仍必须是 artifact id：前端 `ChatInlineImage`
    拿它调 `requestCanvasDataFocus(artifactId)` 打开 Results。
    """

    @staticmethod
    def _sec(sec_id: str, aid: str, caption: str = "热力图"):
        class _S:
            id = sec_id
            kind = "image"
            payload = {"artifact_id": aid, "caption": caption}

        return _S()

    def test_carries_both_ids(self) -> None:
        from bowhead_core.agents.lead_orchestrator import _inline_image_artifact

        out = _inline_image_artifact(self._sec("sec_img", "art_xyz"))
        assert out is not None
        assert out["id"] == "art_xyz", "id 必须留 artifact id（Results 跳转靠它）"
        assert out["section_id"] == "sec_img", "少了它锚点查不到图片图"
        assert out["download_url"] == "/api/v1/files/art_xyz"

    def test_no_artifact_id_still_returns_none(self) -> None:
        from bowhead_core.agents.lead_orchestrator import _inline_image_artifact

        class _S:
            id = "sec_x"
            payload: dict[str, Any] = {}

        assert _inline_image_artifact(_S()) is None


class TestFigureAnchorHintOnToolResult:
    """产图工具的返回必须带锚点提示 —— 这是 peer 唯一会读到的那一面。

    116 dogfood 实证：把清单发给 Lead 没用（chat 轮 `_chat_direct_delivery`
    让 Lead 就地短路、根本没有写作轮，t_6787e8159e94 / t_752998836ac7 锚点均为 0）。
    写 chat 正文的是 peer，而 peer 的提示词有三个来源 ⇒ 只有工具返回是共同面。
    """

    def test_chart_result_gets_hint_with_its_own_id(self) -> None:
        from bowhead_core.model_view import project_tool_result

        out = project_tool_result(
            "add_chart",
            {"section_id": "sec_abc", "kind": "line", "title": "走势"},
        )
        assert "[figure:sec_abc]" in out["inline_hint"]
        assert "独占一行" in out["inline_hint"]
        # Original keys must remain unchanged.
        assert out["section_id"] == "sec_abc" and out["kind"] == "line"

    def test_every_figure_tool_covered(self) -> None:
        from bowhead_core.model_view import _FIGURE_ANCHOR_TOOLS, project_tool_result

        for tool in _FIGURE_ANCHOR_TOOLS:
            out = project_tool_result(tool, {"section_id": "sec_x"})
            assert "inline_hint" in out, f"{tool} 少了锚点提示"

    def test_non_figure_tool_untouched(self) -> None:
        from bowhead_core.model_view import project_tool_result

        src = {"section_id": "sec_x", "rows": []}
        assert project_tool_result("bi_query", src) == src

    def test_error_or_missing_section_gets_no_hint(self) -> None:
        from bowhead_core.model_view import project_tool_result

        # Failed/skipped creation must not encourage citing a nonexistent chart.
        assert "inline_hint" not in project_tool_result("add_chart", {"error": "boom", "section_id": "sec_x"})
        assert "inline_hint" not in project_tool_result("add_chart", {"canvas_id": "cv_1"})

    def test_original_result_object_is_not_mutated(self) -> None:
        """投影只作用于送给模型的副本——后端/用户面还要读原字段。"""
        from bowhead_core.model_view import project_tool_result

        src = {"section_id": "sec_x"}
        project_tool_result("add_chart", src)
        assert src == {"section_id": "sec_x"}

    def test_idempotent(self) -> None:
        """重放/二次投影不该把提示叠两遍。"""
        from bowhead_core.model_view import project_tool_result

        once = project_tool_result("add_chart", {"section_id": "sec_x"})
        twice = project_tool_result("add_chart", once)
        assert twice["inline_hint"] == once["inline_hint"]


class TestAnchorSurvivesChatComposition:
    """peer 把锚点写进 payload ⇒ 必须原样出现在合成后的 chat 正文里。

    这是端到端链条上我此前没验过的一环：peer 写 → `render_chat_answer_markdown`
    合成 → Lead 直达提升为 final_answer → FE 渲染。中间任何一步吃掉锚点，
    前端做得再对也没用。
    """

    def test_anchor_in_exec_summary_survives(self) -> None:
        from bowhead_core.agents.delivery_decision import render_chat_answer_markdown

        md = render_chat_answer_markdown({"exec_summary": "结论。\n[figure:sec_a]\n**图1**：说明", "kpis": [], "insights": []})
        assert "[figure:sec_a]" in md
        # Still occupy a line alone (frontend anchor criterion requires the whole line to contain only the anchor).
        assert any(line.strip() == "[figure:sec_a]" for line in md.split("\n"))

    def test_anchor_inside_insight_narrative_survives_as_own_line(self) -> None:
        from bowhead_core.agents.delivery_decision import render_chat_answer_markdown

        md = render_chat_answer_markdown(
            {
                "exec_summary": "总览。",
                "insights": [
                    {
                        "title": "月度走低",
                        "thesis": "逐月下滑。",
                        "narrative": "从 19.9 亿降到 8.0 亿。\n[figure:sec_m]\n**图1**：月度走势",
                    }
                ],
            }
        )
        assert any(line.strip() == "[figure:sec_m]" for line in md.split("\n")), md


class TestInsightEvidencePlacesItsChart:
    """图跟着论点走：insight 的 evidence 点名的图放在这条论点后面（116 `s_f09fdd7b3f02`：三张图全堆在末尾）。"""

    @staticmethod
    def _ins(title: str, sid: str | None, narrative: str = "说明。") -> dict:
        ev = [{"kind": "chart", "caption": title, "chart_section_id": sid}] if sid is not None else [{"kind": "callout", "caption": "c"}]
        return {"title": title, "thesis": "论点。", "narrative": narrative, "evidence": ev}

    def test_each_insight_gets_its_own_chart_right_after_it(self) -> None:
        from bowhead_core.agents.delivery_decision import render_chat_answer_markdown

        md = render_chat_answer_markdown(
            {
                "exec_summary": "总览。",
                "insights": [self._ins("占比", "sec_pie"), self._ins("相关", "sec_scatter"), self._ins("结构", "sec_bar")],
                "caveats": ["9 月未过完"],
            }
        )
        lines = [ln.strip() for ln in md.split("\n") if ln.strip()]
        assert lines.index("[figure:sec_pie]") == lines.index(next(ln for ln in lines if ln.startswith("**占比**"))) + 1
        assert lines.index("[figure:sec_scatter]") == lines.index(next(ln for ln in lines if ln.startswith("**相关**"))) + 1
        assert lines.index("[figure:sec_bar]") < lines.index("> 9 月未过完")

    def test_chart_already_anchored_or_repeated_is_not_placed_twice(self) -> None:
        from bowhead_core.agents.delivery_decision import render_chat_answer_markdown

        md = render_chat_answer_markdown(
            {
                "exec_summary": "总览。\n[figure:sec_a]",
                "insights": [self._ins("甲", "sec_a"), self._ins("乙", "sec_b"), self._ins("丙", "sec_b")],
            }
        )
        assert md.count("[figure:sec_a]") == 1 and md.count("[figure:sec_b]") == 1

    def test_untranslated_or_missing_ids_place_nothing(self) -> None:
        """没翻成真 id 的清单号、非图证据 ⇒ 不放（图照旧落在末尾）。"""
        from bowhead_core.agents.delivery_decision import render_chat_answer_markdown

        md = render_chat_answer_markdown({"exec_summary": "总览。", "insights": [self._ins("甲", "3"), self._ins("乙", None)]})
        assert "[figure:" not in md


def test_placement_stats_counts_placed_and_tail() -> None:
    """日志口径：本轮三张图，两条论点点名了其中两张 ⇒ placed=2、tail=1。"""
    from bowhead_core.agents.delivery_decision import chat_figure_placement_stats, render_chat_answer_markdown

    ins = TestInsightEvidencePlacesItsChart._ins
    payload = {"exec_summary": "总览。", "insights": [ins("甲", "sec_a"), ins("乙", "sec_b"), ins("丙", None)]}
    st = chat_figure_placement_stats(payload, render_chat_answer_markdown(payload), ["sec_a", "sec_b", "sec_c", "sec_a"])
    assert st == {"figures": 3, "insights": 3, "insights_with_chart": 2, "placed": 2, "tail": 1}
