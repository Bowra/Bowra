"""Translate numeric figure aliases and validate chat anchors deterministically.

Synthetic aliases exercise the boundary between model references and section IDs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core.chat_figure_anchors import (
    figures_from_invocations,
    resolve_anchors,
)

from bowhead_core import model_view

# Six actual section_ids from production t_22828efa670b, in chart-creation order.
PROD_FIGURES = [
    "sec_0dddee9305ef",
    "sec_2b2a47c986c5",
    "sec_23bcd1698aee",
    "sec_36fb48aea59d",
    "sec_1d2967878e6f",
    "sec_176b0d4af551",
]


def _inv(tool_name: str, result: Any, error: Any = None) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool_name, result=result, error=error)


def _anchors(text: str) -> list[str]:
    import re

    return re.findall(r"\[figure:([^\]]*)\]", text)


# ─────────────────────────── fix 1: ν1.5 translation ───────────────────────────


def test_simple_int_figure_anchor_translates_to_real_id() -> None:
    """生产形状：peer 写清单数字，翻译后必须是真 section id。"""
    al = RefAllowlist()
    n = al.add("chart", "sec_0dddee9305ef")
    out = al.translate_marker(f"品规层面高度集中。\n[figure:{n}]\n渠道层面……")
    assert out.count("[figure:sec_0dddee9305ef]") == 1
    assert f"[figure:{n}]" not in out


def test_real_section_id_anchor_passes_through_unchanged() -> None:
    """ν1.5 关掉时 `inline_hint` 给的就是真 id —— 这条路不能被翻坏。"""
    al = RefAllowlist()
    al.add("chart", "sec_abc123")
    text = "结论。\n[figure:sec_abc123]\n"
    assert al.translate_marker(text) == text


def test_figure_anchor_never_binds_to_a_non_visual_ref() -> None:
    """★ `resolve()` 不看 kind：data/doc 的数字同样能解出真 id。

    若不查 kind，`[figure:1]`（1 = 某个 data artifact）会被翻成 `[figure:art_x]`
    —— 把图锚绑到另一个命名空间上，比不翻更坏（前端拿它去找图，永远找不到，
    而我们却以为翻好了）。
    """
    al = RefAllowlist()
    data_id = al.add("data", "art_deadbeef")
    doc_id = al.add("doc", "chunk_cafe")
    text = f"[figure:{data_id}] 和 [figure:{doc_id}]"
    out = al.translate_marker(text)
    assert "art_deadbeef" not in out
    assert "chunk_cafe" not in out
    assert out == text


def test_image_kind_is_translatable_too() -> None:
    """matplotlib 出的图走 `add_image` 落 image section，同为视觉引用面。"""
    al = RefAllowlist()
    n = al.add("image", "sec_img0001")
    assert al.translate_marker(f"[figure:{n}]") == "[figure:sec_img0001]"


def test_unknown_number_is_left_for_the_gate() -> None:
    """清单里没有的号不翻——留给闸剥，别在这里假装解析成功。"""
    al = RefAllowlist()
    al.add("chart", "sec_x")
    assert al.translate_marker("[figure:987]") == "[figure:987]"


def test_translate_payload_still_walks_every_narrative_field() -> None:
    """字段清单抽成 `map_payload_texts` 后，既有 `[ref:]` 翻译不能少走字段。"""
    al = RefAllowlist()
    doc = al.add("doc", "chunk_doc1")
    chart = al.add("chart", "sec_c1")
    payload: dict[str, Any] = {
        "exec_summary": f"总述 [ref:{doc}]",
        "eyebrow": f"眉题 [ref:{doc}]",
        "insights": [
            {
                "title": f"标题 [ref:{doc}]",
                "narrative": f"叙述 [figure:{chart}]",
                "thesis": f"论点 [ref:{doc}]",
                "evidence": [{"callout_text": f"注 [ref:{doc}]", "chart_section_id": str(chart)}],
            }
        ],
        "recommendations": [{"rationale": f"理由 [ref:{doc}]", "action": "做", "expected_impact": "好"}],
        "kpis": [{"value": f"12 [ref:{doc}]", "delta": "+1"}],
        "caveats": [f"注意 [ref:{doc}]"],
    }
    al.translate_payload(payload)
    blob = repr(payload)
    assert f"[ref:{doc}]" not in blob
    # 8 = exec_summary / eyebrow / title / thesis / callout_text / rationale /
    # kpi.value / caveats; one missing means `map_payload_texts` skipped a field.
    assert blob.count("chunk_doc1") == 8
    assert "[figure:sec_c1]" in payload["insights"][0]["narrative"]
    # Evidence ref fields retain existing simple→real translation.
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "sec_c1"


# ─────────────────────── fix 2: deterministic gate (validate / repair / strip) ───────────────────────


def test_valid_anchors_are_left_alone() -> None:
    text = "一段。\n[figure:sec_0dddee9305ef]\n二段。\n[figure:sec_2b2a47c986c5]\n"
    out, st = resolve_anchors(text, figures=PROD_FIGURES, known_ids=set(PROD_FIGURES))
    assert out == text
    assert (st.total, st.kept, st.repaired, st.stripped) == (2, 2, 0, 0)


def test_clean_text_keeps_its_whitespace_byte_for_byte() -> None:
    """★ 没坏锚就**一个字节都不动**。

    剥用的 `_strip_invalid_figure_anchors` 带 `.strip()` 与「3+ 空行折叠」；无条件
    跑一遍就等于每轮悄悄重排用户看到的空白。闸只该处理坏锚，不该顺手排版。
    """
    text = "\n\n开头留白。\n\n\n[figure:sec_a]\n\n结尾留白。\n\n"
    out, st = resolve_anchors(text, figures=["sec_a"], known_ids={"sec_a"})
    assert out == text
    assert not st.changed


def test_positional_repair_reproduces_the_production_intent() -> None:
    """条数一一对应 ⇒ 按文档序 ↔ 建图序改写。用生产那 6 个数字锚点。"""
    text = "\n".join(f"论点{i}。\n[figure:{54 + i}]" for i in range(6))
    out, st = resolve_anchors(text, figures=PROD_FIGURES, known_ids=set(PROD_FIGURES))
    assert _anchors(out) == PROD_FIGURES
    assert (st.repaired, st.stripped) == (6, 0)


def test_count_mismatch_strips_instead_of_guessing() -> None:
    """★ 条数对不上就**不猜**：猜错等于把图绑到错误论点旁，比不显示更坏。"""
    text = "一。\n[figure:54]\n二。\n[figure:55]\n三。\n[figure:56]\n"
    out, st = resolve_anchors(text, figures=PROD_FIGURES[:2], known_ids=set(PROD_FIGURES[:2]))
    assert "[figure:" not in out
    assert (st.repaired, st.stripped) == (0, 3)


def test_orphan_caption_line_goes_with_the_stripped_anchor() -> None:
    text = "一段。\n[figure:999]\n**图1**：各省环比。\n后续正文。"
    # Evidence exists (the session has a chart), but 999 is not its id ⇒ strip.
    out, _ = resolve_anchors(text, figures=[], known_ids={"sec_real"})
    assert "[figure:" not in out
    assert "图1" not in out
    assert "后续正文。" in out


def test_fenced_code_is_never_touched() -> None:
    """代码块里的方括号不是引用。"""
    text = "说明。\n```python\nx = a[figure:54]\n```\n[figure:54]\n"
    out, st = resolve_anchors(text, figures=["sec_only"], known_ids={"sec_only"})
    assert "a[figure:54]" in out
    assert "[figure:sec_only]" in out
    assert st.total == 1  # The fenced one does not count as an anchor.


def test_repeated_bad_id_maps_to_two_different_figures() -> None:
    """★ 同一个坏 id 写两遍：必须按**出现位置**各绑一张图。

    按 id 建 dict 再全局替换会把两处塌缩成同一张 —— 读者会看到同一张图被
    当成两个不同论点的证据。
    """
    text = "一。\n[figure:7]\n二。\n[figure:7]\n"
    out, st = resolve_anchors(text, figures=["sec_a", "sec_b"], known_ids={"sec_a", "sec_b"})
    assert _anchors(out) == ["sec_a", "sec_b"]
    assert st.repaired == 2


def test_cross_turn_figure_reference_is_not_stripped() -> None:
    """引上一轮的旧图在 chat 里合法（allowlist 认得），不能当幻觉剥掉。"""
    text = "沿用上一轮那张图。\n[figure:sec_old_turn]\n"
    out, st = resolve_anchors(text, figures=["sec_new"], known_ids={"sec_new", "sec_old_turn"})
    assert out == text
    assert (st.kept, st.stripped) == (1, 0)


def test_zero_evidence_fails_open() -> None:
    """★ 两个证据源都空 = **不知道**本轮有哪些图，不是「本轮没有图」。

    此时剥锚点会把合法引用当幻觉删掉，比改动前更坏（今天的无效锚只是被前端静默
    丢弃，不伤有效的那些）。既有 `test_escaped_newlines_unescaped_even_with_the_soft_gate_off`
    用的正是无证据的桩 ctx —— 它逮到过这一条。
    """
    text = "结论。\n[figure:sec_a]\n"
    out, st = resolve_anchors(text, figures=[], known_ids=set())
    assert out == text
    assert not st.changed


def test_no_anchor_text_is_a_byte_for_byte_noop() -> None:
    text = "一段普通的答案，没有任何锚点。"
    out, st = resolve_anchors(text, figures=PROD_FIGURES, known_ids=set(PROD_FIGURES))
    assert out == text
    assert st.total == 0 and not st.changed


# ─────────────────────── Figure-list extraction ───────────────────────


def test_figures_from_invocations_keeps_creation_order_and_dedupes() -> None:
    invs = [
        _inv("bi_query", {"artifact_id": "art_1"}),
        _inv("add_chart", {"section_id": "sec_a"}),
        _inv("add_chart", {"section_id": "sec_b"}),
        _inv("add_chart", {"section_id": "sec_a"}),  # Retry / update same section
        _inv("add_image", {"section_id": "sec_c"}),
    ]
    assert figures_from_invocations(invs) == ["sec_a", "sec_b", "sec_c"]


def test_figures_from_invocations_skips_failures() -> None:
    invs = [
        _inv("add_chart", {"section_id": "sec_ok"}),
        _inv("add_chart", {"error": "boom"}),
        _inv("add_chart", {"section_id": "sec_err"}, error="tool failed"),
        _inv("add_chart", None),
    ]
    assert figures_from_invocations(invs) == ["sec_ok"]


def test_non_inlineable_canvas_tools_are_not_figures() -> None:
    """`write_report` / `canvas_patch_section` 建的段不是可内联产物。"""
    invs = [_inv("write_report", {"section_id": "sec_report"})]
    assert figures_from_invocations(invs) == []


# ─────────────────────── Id used by model_view hints ───────────────────────


def test_inline_hint_prefers_the_allowlist_number() -> None:
    """★ 这是本批的根因修复：提示必须与 `<allowlist_rules>` 说同一套 id。"""
    out = model_view.project_tool_result(
        "add_chart",
        {"section_id": "sec_0dddee9305ef", "kind": "bar"},
        figure_anchor_id="54",
    )
    assert "[figure:54]" in out["inline_hint"]
    assert "sec_0dddee9305ef" not in out["inline_hint"]


def test_inline_hint_falls_back_to_the_real_id() -> None:
    """ν1.5 关掉时没有数字可用 —— 回落到真 id（改动前的逐字行为）。"""
    out = model_view.project_tool_result("add_chart", {"section_id": "sec_x", "kind": "bar"})
    assert "[figure:sec_x]" in out["inline_hint"]


# ─────────────────── Wiring: _serialize → project_tool_result ───────────────────


def test_serialize_hands_the_model_the_allowlist_number() -> None:
    """★ 测的是**接线**不是函数：peer 真正读到的那串 JSON 里必须是清单号。

    只测 `project_tool_result` 会漏掉「`_serialize` 根本没把 allowlist 传下去」
    这一类接错（本仓已栽过不止一次）。
    """
    from bowhead_core.subagent import _serialize

    al = RefAllowlist()
    inv = SimpleNamespace(
        tool_name="add_chart",
        result={"section_id": "sec_0dddee9305ef", "kind": "bar"},
        error=None,
    )
    blob = _serialize(inv, ref_allowlist=al)
    n = al.add("chart", "sec_0dddee9305ef")  # Idempotent: retrieve the previously issued number.
    assert f"[figure:{n}]" in blob
    assert n == 1


def test_serialize_without_allowlist_keeps_the_real_id() -> None:
    from bowhead_core.subagent import _serialize

    inv = SimpleNamespace(tool_name="add_chart", result={"section_id": "sec_x", "kind": "bar"}, error=None)
    assert "[figure:sec_x]" in _serialize(inv)


def test_legacy_tool_name_still_gets_a_number() -> None:
    """`canvas_add_chart` 是改名前的旧名——按 tool_name 硬匹配会漏掉它。"""
    from bowhead_core.subagent import _figure_anchor_id

    al = RefAllowlist()
    inv = SimpleNamespace(tool_name="canvas_add_chart", result={"section_id": "sec_legacy"}, error=None)
    assert _figure_anchor_id(inv, al) == "1"
    assert al.kind_of(1) == "chart"


def test_failed_chart_call_gets_no_number() -> None:
    """失败的调用不该占号——占了就是清单里多一条指向不存在 section 的 id。"""
    from bowhead_core.subagent import _figure_anchor_id

    al = RefAllowlist()
    assert _figure_anchor_id(SimpleNamespace(tool_name="add_chart", result={"error": "x"}, error=None), al) is None
    assert _figure_anchor_id(SimpleNamespace(tool_name="bi_query", result={"section_id": "s"}, error=None), al) is None
    assert _figure_anchor_id(SimpleNamespace(tool_name="add_chart", result={"section_id": "s"}, error=None), None) is None


# ─────────────────── Two-pass cross-field resolution ───────────────────


def test_repair_counts_anchors_across_the_whole_payload() -> None:
    """★ 条数判据的分母是**本轮**的图，分子必须也是**整个 payload** 的锚。

    逐字段各判各的话：某字段 1 个锚 vs 本轮 3 张图 ⇒ 1 != 3 ⇒ 永远修不成、整批被剥。
    """
    from bowhead_core.chat_figure_anchors import PayloadAnchorResolver

    figs = ["sec_a", "sec_b", "sec_c"]
    payload: dict[str, Any] = {
        "exec_summary": "总述。\n[figure:11]\n",
        "insights": [
            {"narrative": "一。\n[figure:12]\n"},
            {"narrative": "二。\n[figure:13]\n"},
        ],
    }
    r = PayloadAnchorResolver(figures=figs, known_ids=set(figs))
    RefAllowlist.map_payload_texts(payload, r.observe)
    r.plan()
    RefAllowlist.map_payload_texts(payload, r.apply)

    assert _anchors(payload["exec_summary"]) == ["sec_a"]
    assert _anchors(payload["insights"][0]["narrative"]) == ["sec_b"]
    assert _anchors(payload["insights"][1]["narrative"]) == ["sec_c"]
    assert (r.stats.repaired, r.stats.stripped) == (3, 0)


def test_cross_field_mismatch_strips_every_field() -> None:
    from bowhead_core.chat_figure_anchors import PayloadAnchorResolver

    payload: dict[str, Any] = {
        "exec_summary": "总述。\n[figure:11]\n",
        "insights": [{"narrative": "一。\n[figure:12]\n"}],
    }
    r = PayloadAnchorResolver(figures=["sec_a"], known_ids={"sec_a"})
    RefAllowlist.map_payload_texts(payload, r.observe)
    r.plan()
    RefAllowlist.map_payload_texts(payload, r.apply)
    assert "[figure:" not in payload["exec_summary"]
    assert "[figure:" not in payload["insights"][0]["narrative"]
    assert (r.stats.repaired, r.stats.stripped) == (0, 2)


def test_cross_field_resolver_is_fail_open_without_evidence() -> None:
    from bowhead_core.chat_figure_anchors import PayloadAnchorResolver

    payload: dict[str, Any] = {"exec_summary": "总述。\n[figure:sec_a]\n"}
    r = PayloadAnchorResolver(figures=[], known_ids=set())
    RefAllowlist.map_payload_texts(payload, r.observe)
    r.plan()
    RefAllowlist.map_payload_texts(payload, r.apply)
    assert payload["exec_summary"] == "总述。\n[figure:sec_a]\n"
    assert not r.stats.changed


def test_legacy_tool_name_still_reaches_available_figures() -> None:
    """旧名 `canvas_add_chart` 建的图不能从「可内联清单」里掉出去。

    今天不可达（registry 在构造 invocation 前已归一），这条钉的是**两侧判据一致**：
    `is_canvas_tool()` 归一而成员判断不归一，就是一个等着被踩的不一致。
    """
    from bowhead_core.subagent import _attach_available_figures

    payload: dict[str, Any] = {}
    _attach_available_figures(
        payload,
        [_inv("canvas_add_chart", {"section_id": "sec_legacy", "title": "旧名建的图"})],
    )
    assert [f["id"] for f in payload.get("available_figures", [])] == ["sec_legacy"]


# ─────────────── Batch 5: run-level boundary (each agent uses its own ν1.5 table) ───────────────

# Two actual charts from 116 dogfood t_c83badd6c5ff
RUN_C1, RUN_C2 = "sec_794ec1303804", "sec_6997b0252e0b"


def _chart_inv(sid: str) -> SimpleNamespace:
    return SimpleNamespace(tool_name="add_chart", result={"section_id": sid}, error=None, nested=None)


def test_lead_numbering_resolves_with_the_lead_table() -> None:
    """★ Lead 自己没调 add_chart（图是 peer 画的），只能靠 allowlist 认号。

    116 实测：Lead 写 `[figure:2]`/`[figure:3]`，因为它那份表里 1 号被 data artifact 占了。
    """
    from bowhead_core.chat_figure_anchors import resolve_run_figure_anchors

    al = RefAllowlist()
    al.add("data", "art_seed")
    a, b = al.add("chart", RUN_C1), al.add("chart", RUN_C2)
    assert (a, b) == (2, 3)

    text = f"论点一。\n[figure:{a}]\n论点二。\n[figure:{b}]\n"
    out, st = resolve_run_figure_anchors(text, ref_allowlist=al, invocations=[])
    assert _anchors(out) == [RUN_C1, RUN_C2]
    assert (st.kept, st.stripped) == (2, 0)


def test_peer_numbering_resolves_with_the_peer_table() -> None:
    """同两张图在 peer 表里是 17/19——两套编号必须各翻各的。"""
    from bowhead_core.chat_figure_anchors import resolve_run_figure_anchors

    al = RefAllowlist()
    for i in range(16):
        al.add("data", f"art_{i}")
    a = al.add("chart", RUN_C1)
    al.add("code", "sec_code")
    b = al.add("chart", RUN_C2)
    assert (a, b) == (17, 19)

    text = f"分析一。\n[figure:{a}]\n分析二。\n[figure:{b}]\n"
    out, _ = resolve_run_figure_anchors(text, ref_allowlist=al, invocations=[_chart_inv(RUN_C1), _chart_inv(RUN_C2)])
    assert _anchors(out) == [RUN_C1, RUN_C2]


def test_wrong_table_would_bind_the_wrong_chart() -> None:
    """★★ 这条钉的是「为什么必须按 run 分表」，不是钉当前行为。

    A 的表里 2 号是**另一张图**；拿它去翻 B 写的 `[figure:2]` 会安静地绑错图 ——
    比不翻更坏（读者会照着错图读结论）。所以落点只能在各自 run 的收口，
    绝不能改成共用一份表。
    """
    from bowhead_core.chat_figure_anchors import resolve_run_figure_anchors

    other = RefAllowlist()
    other.add("chart", "sec_SOMEONE_ELSE")  # 1
    n = other.add("chart", "sec_WRONG")  # 2
    assert n == 2

    out, _ = resolve_run_figure_anchors(f"论点。\n[figure:{n}]\n", ref_allowlist=other, invocations=[])
    assert _anchors(out) == ["sec_WRONG"], "拿错表就是绑错图——本测试就是这条风险的书面记录"


def test_run_gate_without_allowlist_falls_back_to_positional_repair() -> None:
    """ν1.5 关掉时没有表可翻，靠闸按位置修。"""
    from bowhead_core.chat_figure_anchors import resolve_run_figure_anchors

    text = "一。\n[figure:41]\n二。\n[figure:42]\n"
    out, st = resolve_run_figure_anchors(text, ref_allowlist=None, invocations=[_chart_inv(RUN_C1), _chart_inv(RUN_C2)])
    assert _anchors(out) == [RUN_C1, RUN_C2]
    assert st.repaired == 2


def test_run_gate_reaches_charts_made_by_a_nested_peer() -> None:
    """Lead 的 invocations 里 add_chart 藏在 subagent_spawn 的 nested 下。"""
    from bowhead_core.chat_figure_anchors import resolve_run_figure_anchors

    spawn = SimpleNamespace(
        tool_name="subagent_spawn",
        result={"ok": True},
        error=None,
        nested=[_chart_inv(RUN_C1), _chart_inv(RUN_C2)],
    )
    out, st = resolve_run_figure_anchors("一。\n[figure:91]\n二。\n[figure:92]\n", ref_allowlist=None, invocations=[spawn])
    assert _anchors(out) == [RUN_C1, RUN_C2]
    assert st.repaired == 2


def test_run_gate_is_a_noop_without_anchors() -> None:
    from bowhead_core.chat_figure_anchors import resolve_run_figure_anchors

    text = "一段没有锚点的答案。"
    out, st = resolve_run_figure_anchors(text, ref_allowlist=RefAllowlist(), invocations=[])
    assert out == text and not st.changed


def test_translate_figure_markers_leaves_other_markers_alone() -> None:
    """★ run 收口只翻图锚——顺手翻 `[ref:]`/`[num:]` 会动到引用渲染，属无关改动。"""
    al = RefAllowlist()
    doc = al.add("doc", "chunk_1")
    ch = al.add("chart", "sec_c")
    text = f"叙述 [ref:{doc}] 与图 [figure:{ch}]"
    out = al.translate_figure_markers(text)
    assert "[figure:sec_c]" in out
    assert f"[ref:{doc}]" in out, "图锚入口不该碰 KB 引用"
