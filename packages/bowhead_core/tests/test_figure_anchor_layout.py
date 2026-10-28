"""图锚位置归一的纯函数闸（`figure_anchor_layout`）。

判据来自 116 真机实测 + 前端解析器实跑（见
`docs/sprints/canvas-update-turn-2026-09-03/spike-figure-anchor.md` §2）：
画布 PM 只提升**段落首个文本节点开头**的锚，别的位置一律原样漏成字面量。
"""

from __future__ import annotations

import pytest
from bowhead_core.figure_anchor_layout import (
    FIGURE_KINDS,
    MARKDOWN_PAYLOAD_KINDS,
    figure_prose_title,
    figure_title,
    normalize_figure_anchors,
)

A = "sec_a"
B = "sec_b"
FIGS = {A: "各门店营业额排名", B: "月度趋势"}


# ── 1. Frontend-promotable shapes: no byte may change ────────────
@pytest.mark.parametrize(
    "md",
    [
        f"前文。\n\n[figure:{A}]\n\n后文。",  # Standalone paragraph.
        f"前文。\n\n[figure:{A}]\n**图1**：说明\n\n后文。",  # Standalone paragraph + adjacent caption.
        f"[figure:{A}] 显示，女装一家独大。",  # Leading anchor followed by body (PM splits into two paragraphs).
        f"前文。\n\n[figure:{B}]\n\n[figure:{A}]\n\n后文。",  # Stacked, blank-line-separated.
        f"```\n[figure:{A}]\n```",  # Fenced: no citation.
        "纯文本，没有任何锚。\n\n第二段。",
        f"行尾有空格的正文。  \n\n[figure:{A}]",  # Never incidentally strip user whitespace.
    ],
)
def test_already_liftable_is_byte_identical(md: str) -> None:
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == md
    assert stats == {"moved": 0, "stripped": 0, "titled": 0, "changed": 0}


# ── 2. Leaked shapes: normalize to standalone anchor paragraph ──
@pytest.mark.parametrize(
    ("label", "md", "expected"),
    [
        # Most common 116 shape (23/49), also old F4 output:
        # anchor alone on line without preceding blank ⇒ CommonMark merges prior paragraph ⇒ PM cannot promote.
        ("F4 产物", f"前文说明见图。\n[figure:{A}]", f"前文说明见图。\n\n[figure:{A}]"),
        ("行尾锚", f"前文说明见图。[figure:{A}]", f"前文说明见图。\n\n[figure:{A}]"),
        ("叠放无空行", f"前文。\n\n[figure:{B}]\n[figure:{A}]\n\n后文。", f"前文。\n\n[figure:{B}]\n\n[figure:{A}]\n\n后文。"),
        ("同行叠放", f"前文。\n\n[figure:{B}] [figure:{A}]\n\n后文。", f"前文。\n\n[figure:{B}]\n\n[figure:{A}]\n\n后文。"),
        # Mid-sentence anchor: replace token by title, move figure after block (R11 shape).
        ("句中锚", f"对照全年排名[figure:{A}]可见，后文。", f"对照全年排名「各门店营业额排名」可见，后文。\n\n[figure:{A}]"),
        ("句中锚·括注", f"P0 门店 13 家（见 [figure:{A}]）。其中：", f"P0 门店 13 家（见「各门店营业额排名」）。其中：\n\n[figure:{A}]"),
        # Never split list/heading blocks midway; splitting makes one list into two.
        ("列表内", f"- 条目一 [figure:{A}]\n- 条目二", f"- 条目一\n- 条目二\n\n[figure:{A}]"),
        ("标题行尾", f"## 标题 [figure:{A}]\n\n正文。", f"## 标题\n\n[figure:{A}]\n\n正文。"),
    ],
)
def test_misplaced_anchor_is_normalized(label: str, md: str, expected: str) -> None:
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == expected, label
    assert stats["changed"] == 1


# ── 3. Fixed point: normalized output unchanged on another pass ──
@pytest.mark.parametrize(
    "md",
    [
        f"前文说明见图。\n[figure:{A}]",
        f"对照全年排名[figure:{A}]可见，后文。",
        f"- 条目一 [figure:{A}]\n- 条目二",
        f"## 标题 [figure:{A}]\n\n正文。",
        f"前文。\n\n[figure:{B}]\n[figure:{A}]\n\n后文。",
    ],
)
def test_output_is_a_fixed_point(md: str) -> None:
    once, _ = normalize_figure_anchors(md, FIGS)
    twice, stats = normalize_figure_anchors(once, FIGS)
    assert twice == once
    assert stats["changed"] == 0


# ── 4. Invalid IDs ──────────────────────────────────────────────
def test_invalid_id_in_sentence_is_stripped() -> None:
    out, stats = normalize_figure_anchors("对照全年排名[figure:sec_zzz]可见。", FIGS)
    assert out == "对照全年排名可见。"
    assert stats["stripped"] == 1


def test_invalid_id_on_own_line_drops_orphan_caption() -> None:
    """锚行删了图注还留着 = 屏幕上一条没有图的「图1：…」。"""
    out, _ = normalize_figure_anchors("前文。\n\n[figure:sec_zzz]\n**图1**：说明\n\n后文。", FIGS)
    assert out == "前文。\n\n后文。"


def test_same_figure_referenced_twice_renders_once() -> None:
    """同一张图已经成块，句中那次只换标题——否则前端会渲两张一样的图。"""
    out, _ = normalize_figure_anchors(f"前文。\n\n[figure:{A}]\n\n又见[figure:{A}]可知。", FIGS)
    assert out == f"前文。\n\n[figure:{A}]\n\n又见「各门店营业额排名」可知。"
    assert out.count(f"[figure:{A}]") == 1


# ── 5. Actual 116 samples (spike §3: mid-sentence anchors mainly parenthetical or subjects) ──
@pytest.mark.parametrize(
    ("md", "expected_prose"),
    [
        (f"P2 20 家（见 [figure:{A}]）。P0 门店兼具高退款率", "P2 20 家（见「各门店营业额排名」）。P0 门店兼具高退款率"),
        (f"按省拆解（[figure:{A}]），业务高度集中于北京", "按省拆解（「各门店营业额排名」），业务高度集中于北京"),
        (f"结构依赖度较高。[figure:{A}] 展示了各品类的营业额占比分布。", "结构依赖度较高。「各门店营业额排名」展示了各品类的营业额占比分布。"),
        (f"**城市分布**：[figure:{A}] 静安区以3.62亿元利润居城市", "**城市分布**：「各门店营业额排名」静安区以3.62亿元利润居城市"),
        (f"对照全年排名[figure:{A}]可见，**头部门店地位稳固**", "对照全年排名「各门店营业额排名」可见，**头部门店地位稳固**"),
    ],
)
def test_real_116_samples(md: str, expected_prose: str) -> None:
    out, _ = normalize_figure_anchors(md, FIGS)
    assert out == f"{expected_prose}\n\n[figure:{A}]"


def test_english_title_uses_ascii_quotes() -> None:
    """引号只看标题本身的语言，不看上下文——中英混排时不会给英文标题套「」。"""
    out, _ = normalize_figure_anchors("As shown in [figure:sec_c] the trend holds.", {"sec_c": "Monthly GMV"})
    assert out == "As shown in “Monthly GMV” the trend holds.\n\n[figure:sec_c]"


# ── 6. Payload wrappers and constants ───────────────────────────
def test_figure_title_never_empty() -> None:
    """句中锚要拿标题替换 token，空标题会留下残句。"""
    assert figure_title({}, "chart") == "chart"
    assert figure_title({"spec": {"title": "T"}}, "chart") == "T"
    assert figure_title({"caption": "C"}, "image") == "C"


def test_kind_constants_are_the_single_source() -> None:
    """`report_tools` 与 `canvas_revise` 都别名到这里；再冒出第四份就会一边剥一边留。"""
    from bowhead_core.agents.report_tools import _FIGURE_KINDS
    from bowhead_core.canvas_revise import _FIGURE_KINDS_LOCAL

    assert _FIGURE_KINDS is FIGURE_KINDS
    assert _FIGURE_KINDS_LOCAL is FIGURE_KINDS
    assert "narrative" not in FIGURE_KINDS
    assert {"narrative", "deck", "callout"} == MARKDOWN_PAYLOAD_KINDS


# ── 7. Order ───────────────────────────────────────────────────
def test_cross_line_anchor_order_is_preserved() -> None:
    """同一块里跨行的多个锚，挪到块尾后必须保持原文先后。

    导出侧按锚的出现序决定内联图怎么排（`test_canvas_export_projection` 钉着这条），
    顺序反了就是两张图串位——实现第一版正是这么错的：行内从右往左改写时用
    `insert(0, …)` 收集，单行内对、跨行就把后一行插到了前一行之前。
    """
    md = f"第一句提到[figure:{A}]的排名。\n第二句提到[figure:{B}]的趋势。"
    out, _ = normalize_figure_anchors(md, FIGS)
    assert out.index(f"[figure:{A}]") < out.index(f"[figure:{B}]")


def test_same_line_anchor_order_is_preserved() -> None:
    md = f"同一行先[figure:{A}]后[figure:{B}]。"
    out, _ = normalize_figure_anchors(md, FIGS)
    assert out.index(f"[figure:{A}]") < out.index(f"[figure:{B}]")


# ── Independent review round 2 (reproduce then fix again) ────────
def test_fenced_code_is_not_padded_when_an_anchor_moves() -> None:
    """闭合围栏前不许被插空行——那个空行会落进**代码块内部**。

    根因是 `flush_pending()` 无条件置「下一行前要空一行」，而围栏分支是先 flush 再 emit。
    """
    md = f"正文[figure:{A}]。\n\n```sql\nSELECT 1\n```\n\n尾。"
    out, _ = normalize_figure_anchors(md, FIGS)
    assert "SELECT 1\n```" in out


@pytest.mark.parametrize(
    ("md", "bare"),
    [
        (f"- [figure:{A}]\n- 条目二", "-"),
        (f"1. [figure:{A}]\n2. 条目", "1."),
        (f"> [figure:{A}]", ">"),
        (f"## [figure:{A}]", "##"),
    ],
)
def test_block_marker_left_alone_is_dropped(md: str, bare: str) -> None:
    """锚是整个块的全部内容时，摘掉 token 不能留下一个空列表项/空引用/空标题。"""
    out, _ = normalize_figure_anchors(md, FIGS)
    assert bare not in [ln.strip() for ln in out.split("\n")]


def test_crlf_input_does_not_leave_stray_carriage_returns() -> None:
    out, _ = normalize_figure_anchors(f"前文见图。\r\n[figure:{A}]\r\n后文。\r\n", FIGS)
    assert "\r" not in out.replace("\r\n", "")


def test_no_op_returns_the_original_string() -> None:
    """没有任何处置落地时必须回**原串**。

    否则调用方会拿到一份「只被重排过空白」的文本，而 stats 还说什么都没做——
    今天三个调用点都靠 `changed` 把它丢掉，但这是个等人踩的坑。
    """
    md = "前文。\n\n\n[figure:sec_gone]\n**图1**：说明"
    out, stats = normalize_figure_anchors(md, FIGS, strip_invalid=False)
    assert out == md
    assert stats["changed"] == 0


def test_title_containing_an_anchor_stays_a_fixed_point() -> None:
    """标题是模型写的，真带了锚形 token 时不能让输出变成「下次还会再动一次」。"""
    figures = {A: f"看 [figure:{B}] 的图"}
    once, _ = normalize_figure_anchors(f"见[figure:{A}]。", figures)
    twice, stats = normalize_figure_anchors(once, figures)
    assert twice == once
    assert stats["changed"] == 0


# ── Independent-review findings (all reproduced before fixing) ──
def test_caption_travels_with_an_anchor_moved_out_of_a_list() -> None:
    """锚从列表块挪到块尾时，它的「图N：」图注要跟着走——早先被当成孤儿删掉了。"""
    out, _ = normalize_figure_anchors(f"- 条目一\n[figure:{A}]\n**图1**：说明", FIGS)
    assert out == f"- 条目一\n\n[figure:{A}]\n**图1**：说明"


def test_mid_and_trailing_in_one_block_keep_document_order() -> None:
    """同一块里句中锚与行尾锚混排时的先后。

    早先行尾锚走「就地切段」、句中锚等到块尾，于是后写的那张图反而排在前面。
    """
    out, _ = normalize_figure_anchors(f"见[figure:{A}]的数据。[figure:{B}]", FIGS)
    assert out.index(f"[figure:{A}]") < out.index(f"[figure:{B}]")


def test_moved_figure_lands_after_the_whole_block() -> None:
    """独占行分支早先在 `i += 1` **之后**才判块尾，等于拿下一行去问，图被插进块中间。"""
    out, _ = normalize_figure_anchors(f"开头提到[figure:{B}]的事。\n[figure:{A}]\n结尾这一行。", FIGS)
    assert out.index("结尾这一行。") < out.index(f"[figure:{B}]")


def test_pre_existing_empty_brackets_are_not_eaten() -> None:
    """摘 token 后的空括号清理只收拾**紧包着它**的那一对，不扫全行。"""
    out, _ = normalize_figure_anchors("空括号（）在前，见[figure:sec_zzz]。", FIGS)
    assert out == "空括号（）在前，见。"


def test_untitled_figure_strips_instead_of_writing_a_kind_name() -> None:
    """无标题的图：句中锚只摘 token。绝不能把 `data_table` 这种 kind 名写进用户句子。"""
    assert figure_prose_title({}, "data_table") == ""
    assert figure_prose_title({"spec": {"title": "真标题"}}, "data_table") == "真标题"
    out, _ = normalize_figure_anchors("见[figure:sec_t]可知。", {"sec_t": ""})
    assert out == "见可知。\n\n[figure:sec_t]"
    assert "data_table" not in out


@pytest.mark.parametrize(
    "md",
    [
        '正文：\n\n    x = "[figure:sec_a]"\n\n后文。',  # Indented code.
        "见[链接](http://x/[figure:sec_a])。",  # Link target.
        "行内代码 `[figure:sec_a]` 不是引用。",  # Inline code.
    ],
)
def test_code_and_link_contexts_are_untouched(md: str) -> None:
    """方括号在代码和 URL 里不是引用——改写它们等于弄坏用户的代码/链接。"""
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == md
    assert stats["changed"] == 0


def test_invalid_anchor_survives_when_caller_did_not_touch_content() -> None:
    """`strip_invalid=False`：只挪不删。存储层据此保证一次 `_meta` 写入不会删正文。"""
    md = f"前文。\n\n[figure:sec_gone]\n**图1**：说明\n\n又见[figure:{A}]可知。"
    out, _ = normalize_figure_anchors(md, FIGS, strip_invalid=False)
    assert "[figure:sec_gone]" in out
    assert "**图1**：说明" in out
    assert out.rstrip().endswith(f"[figure:{A}]")  # Valid anchor still moves.


def test_repeated_id_keeps_the_first_occurrence() -> None:
    """同一个 id 出现两次时，成块的必须是**最左边**那次。

    实现第一版把去重方向做反了：改写要从右往左（span 偏移才不失效），而 `placed` 也跟着
    从右往左记，于是正文里最后一次被当成「首次」留下、最左那次反被判重复 —— 两张图在
    导出里就串了位（`test_canvas_export_projection::test_narrative_figure_anchors_project_as_embedded`
    逮到）。修法是决策按文档序、改写按逆序，分两趟。
    """
    md = f"开头[figure:{A}]中段[figure:{B}]又见[figure:{A}]尾"
    out, _ = normalize_figure_anchors(md, FIGS)
    assert out.count(f"[figure:{A}]") == 1  # Duplicate occurrence retains title only, no second figure.
    assert out.index(f"[figure:{A}]") < out.index(f"[figure:{B}]")


# ── Independent review round 3 ──────────────────────────────────
def test_caption_follows_a_trailing_or_mid_anchor() -> None:
    """图注属于**那张图**：锚从这一行挪走，紧随的「图N：」要跟着走。

    早先要求「整行被掏空」才搬图注，于是行尾锚/句中锚的图注被落在原地——
    图搬走了没有图注，图注又混进了上一段正文。
    """
    out, _ = normalize_figure_anchors(f"见下图。[figure:{A}]\n**图1**：门店排名\n\n后文。", FIGS)
    assert f"[figure:{A}]\n**图1**：门店排名" in out
    assert out.startswith("见下图。")

    out2, _ = normalize_figure_anchors(f"见[figure:{A}]的排名。\n**图1**：门店排名\n\n后文。", FIGS)
    assert f"[figure:{A}]\n**图1**：门店排名" in out2


def test_anchor_used_as_a_link_label_is_untouched() -> None:
    """`[figure:x](url)` 里锚是链接**文字**，改写它会把链接拆成散字加一个裸 (url)。"""
    md = f"见[figure:{A}](http://x) 后文。"
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == md
    assert stats["changed"] == 0


def test_html_block_is_untouched() -> None:
    """HTML 块靠空行终止：往中间插空行会让闭合标签渲成字面量。"""
    md = f"<div>\n[figure:{A}]\n</div>"
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == md
    assert stats["changed"] == 0


# ── Independent review round 4 ──────────────────────────────────
def test_multiline_indented_code_is_untouched() -> None:
    """缩进代码块要**整块**冻结：早先只冻结紧跟空行的那一行，第二行起照改、还插空行。"""
    md = '正文：\n\n    x = 1\n    y = "[figure:sec_a]"\n\n后文。'
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == md
    assert stats["changed"] == 0


def test_html_block_interrupting_a_paragraph_is_untouched() -> None:
    """CommonMark type-6 HTML 块**可以打断段落**，不能要求它前面有空行。

    早先按「前一行是空行」判，于是紧跟正文的 <div> 没被认出来，插进去的空行
    反而终止了 HTML 块，闭合标签渲成字面量——正是这条防线要防的事。
    """
    md = f"前文。\n<div>\n[figure:{A}]\n</div>"
    out, stats = normalize_figure_anchors(md, FIGS)
    assert out == md
    assert stats["changed"] == 0


def test_caption_carrying_its_own_anchor_stays_a_fixed_point() -> None:
    """图注是整行原样搬走的、不会再过一遍归一 ⇒ 自己带锚的行不能当图注。"""
    md = f"- 条目\n[figure:{A}]\n**图1**：另见[figure:{B}]"
    once, _ = normalize_figure_anchors(md, FIGS)
    twice, stats = normalize_figure_anchors(once, FIGS)
    assert twice == once
    assert stats["changed"] == 0


# ── Independent review round 5 ──────────────────────────────────
def test_stripping_a_token_keeps_english_word_spacing() -> None:
    """摘 token 不能把英文两侧黏成一个词。

    既有的剥除用例全是中文散文（中文不需要空格），所以这条一直没暴露——
    换标题那条分支早就补了空格，摘 token 那条漏了（review 五轮 #1）。
    """
    out, _ = normalize_figure_anchors("see [figure:sec_zzz] for details", FIGS)
    assert out == "see for details"

    out2, _ = normalize_figure_anchors("see [figure:sec_c] for details", {"sec_c": ""})
    assert out2.startswith("see for details")


def test_hard_line_break_survives_a_rewrite() -> None:
    """行尾两个空格是 markdown 的硬换行，改写这一行时不能把它 rstrip 掉。"""
    md = f"见[figure:{A}]的图。  \n续行内容"
    out, _ = normalize_figure_anchors(md, FIGS)
    assert "的图。  \n续行内容" in out


def test_prose_starting_with_angle_bracket_is_not_treated_as_html() -> None:
    """`<5% 的门店…` 是正文不是 HTML 块。

    早先按 `^<` 判，这种句子会被整块冻结 ⇒ 闸对它完全失效，锚原样漏出去——
    fail-open 成了本 sprint 要消灭的那类漏出（review 五轮 #3）。
    """
    md = f"<5% 的门店贡献了大头，见[figure:{A}]。"
    out, stats = normalize_figure_anchors(md, FIGS)
    assert stats["changed"] == 1
    assert out.endswith(f"[figure:{A}]")


def test_trailing_newline_is_preserved() -> None:
    md = f"见图[figure:{A}]。\n"
    out, _ = normalize_figure_anchors(md, FIGS)
    assert out.endswith("\n")
