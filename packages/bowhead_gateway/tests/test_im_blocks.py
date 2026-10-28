"""Unit tests for im/blocks.py — marker resolution (R-1) + neutral blocks.

The R-1 gate: NO unresolved inline marker may survive into a card payload text field.
"""

from __future__ import annotations

from bowhead_core.source_linkage_presentation import SourceLinkagePresentation
from bowhead_gateway.im.blocks import (
    NeutralBlock,
    SourceLinkageLine,
    TextBlock,
    compute_report_trust,
    resolve_markers,
)

_MARKER_PREFIXES = ("[num:", "[cite:", "[ref:", "[chart:", "[data:", "[memory:")


def _no_markers(text: str) -> bool:
    return not any(p in text for p in _MARKER_PREFIXES)


# ─── individual marker kinds ─────────────────────────────────────────────────


def test_cite_becomes_bracket_n() -> None:
    assert resolve_markers("see [cite:3] for detail") == "see [3] for detail"


def test_verified_num_bakes_to_value_no_per_number_tick() -> None:
    """M3-2：gen2 `:src:` 形态 bake 成「值+单位」纯文本；**逐数字 ✓ 已随退役消失**
    （来源关联语义由聚合 SourceLinkageLine 承担，M1-5 换口径）。原契约意图「标记不泄露 + 值可见」保留。"""
    out = resolve_markers("营收 [num:347100:CNY:src:tl_abc]", locale="zh-CN")
    assert _no_markers(out)
    assert out == "营收 34.7万元"  # Currency-enum scaling (M3-1 production regression fix).
    assert "✓" not in out


def test_verified_num_highlight_variant_handled() -> None:
    out = resolve_markers("[num:0.123:percent:src:tl_1#highlight=sentence]")
    assert _no_markers(out)
    assert out == "12.3%"  # Fractional percent ×100; discard #highlight too.
    assert "✓" not in out


def test_plain_num_renders_value_without_tick() -> None:
    out = resolve_markers("增长 [num:0.123:percent]")
    assert "12.3%" in out
    assert "✓" not in out  # unverified → no per-number tick (aggregate trust line conveys it)
    assert _no_markers(out)


def test_plain_count_and_bare_number() -> None:
    assert resolve_markers("[num:42:count] rows") == "42 rows"
    assert resolve_markers("n=[num:42]") == "n=42"


def test_ref_chart_data_memory_are_stripped() -> None:
    """M3-2：strip_legacy 的 ref/chart/fact 族 pass **吃掉前导空格**（`\\s?\\[…\\]`），
    `[data:]`/`[figure:]` 不属退役族 → 仍由本模块 `_STRIP_RE` 清（留双空格）。"""
    assert resolve_markers("a [ref:chunk_abc123] b") == "a b"
    assert resolve_markers("a [chart:spec_1] b") == "a b"
    assert resolve_markers("a [memory:m_1] b") == "a b"
    assert resolve_markers("a [data:art_e17c76] b") == "a  b"
    assert resolve_markers("a [data:5] b") == "a  b"


def test_malformed_smushed_num_bakes_to_literal_value() -> None:
    """DeepSeek 会写「值+单位黏一起 + :src: 尾」；bake 取值文本原样（它本就是显示形）。"""
    out = resolve_markers("约 [num:2.41亿:src:tl_6d89]")
    assert out == "约 2.41亿"
    assert _no_markers(out)


def test_pathological_empty_num_is_swept() -> None:
    # neither INLINE_RE nor the salvage parse `[num:]`; the residual sweep removes it.
    assert _no_markers(resolve_markers("x [num:] y"))


def test_empty_and_markerless_text_unchanged() -> None:
    assert resolve_markers("") == ""
    assert resolve_markers("just plain prose, no markers.") == "just plain prose, no markers."


def test_sanitizes_card_injection_but_keeps_normal_markdown() -> None:
    # review P1: neutralize Feishu lark_md injection (mention tags + dangerous link schemes)
    out = resolve_markers('点 [x](javascript:alert(1)) 和 <at user_id="all">全体</at> 见 [报告](https://e.com) **粗**')
    assert "javascript:" not in out  # scheme neutralized
    assert "<at" not in out and "</at>" not in out  # mention tag stripped
    assert "(https://e.com)" in out  # normal link preserved
    assert "**粗**" in out  # normal markdown preserved
    assert "data:" not in resolve_markers("[y](data:text/html,<b>)")
    assert "vbscript:" not in resolve_markers("[z](vbscript:x)")


def test_marker_bake_has_no_locale_dimension() -> None:
    """M3-2/§4.5：退役标记的 bake **无 locale 维度**——原「locale 影响货币渲染」
    由 i18n inline-numbers v1（`render_inline_numbers`）提供，该子系统随本批退役。

    口径定死 zh（落库是死文本，必须选一个；且 `_ASCII_UNIT_MAP` 的 USD→美元 早已
    选定 zh 侧）。卡片其余部分仍按 locale 组装，只有标记 bake 不再分叉。
    """
    zh = resolve_markers("[num:347100:CNY:src:tl_1]", locale="zh-CN")
    en = resolve_markers("[num:347100:CNY:src:tl_1]", locale="en-US")
    assert _no_markers(zh) and _no_markers(en)
    assert zh == en == "34.7万元"


def test_kitchen_sink_property_no_marker_survives() -> None:
    text = (
        "Q3 营收 [num:347100:CNY:src:tl_a]，同比 [num:0.123:percent]，"
        "共 [num:42:count] 单，详见 [cite:1] 与 [cite:2]。"
        "来源 [ref:chunk_x] [data:art_deadbeef] [data:9] 图 [chart:c1] 记忆 [memory:m1] "
        "畸形 [num:2.41亿:src:tl_z] 空 [num:]"
    )
    out = resolve_markers(text)
    assert _no_markers(out), out
    # cite refs survived as [N]
    assert "[1]" in out and "[2]" in out
    # M3-2: per-number ✓ retired (aggregate SourceLinkageLine handles it); invariant remains no surviving markers.
    assert "✓" not in out
    # Values + units must survive (bake never drops units).
    assert "34.7万元" in out and "12.3%" in out and "2.41亿" in out


def test_neutral_block_union_members_construct() -> None:
    blocks: list[NeutralBlock] = [
        TextBlock(markdown="hello"),
        SourceLinkageLine(
            presentation=SourceLinkagePresentation(citation_count=3, figure_review_count=1, source_count=2),
            locale="zh-CN",
        ),
    ]
    assert blocks[0].kind == "text"
    assert blocks[1].kind == "source_linkage" and blocks[1].presentation.figure_review_count == 1


# ─── compute_report_trust (T2) ───────────────────────────────────────────────


def test_trust_empty_is_silent() -> None:
    stats = compute_report_trust([])
    assert (stats.verified, stats.unverified, stats.sources) == (0, 0, 0)
    assert stats.verified_pct is None
    assert stats.silent is True


def test_trust_gen2_markers_not_counted_default() -> None:
    """citation-audit M1-4: 退役 gen2 标记不再计数（发射端停写; 未迁移存量
    → pct=None, SourceLinkageLine 不显误导性 0%）。"""
    stats = compute_report_trust([{"kind": "narrative", "markdown": "营收 [num:347100:CNY:src:tl_a] 增长 [num:5:count]"}])
    assert (stats.verified, stats.unverified, stats.verified_pct) == (0, 0, None)


def test_trust_mixed_numbered_and_field_rounds_pct() -> None:
    """新口径混合: 有效 [N] verified + unverified_numbers 字段 → 50%。"""
    stats = compute_report_trust(
        [
            {"kind": "narrative", "markdown": "合计 9627万元 [1]。", "unverified_numbers": ["150亿元"]},
            {"kind": "sources", "entries": [{"label": "bi_query: 全省合计"}]},
        ]
    )
    assert (stats.verified, stats.unverified, stats.verified_pct) == (1, 1, 50)


def test_trust_unverified_numbers_list_counts() -> None:
    stats = compute_report_trust([{"kind": "narrative", "markdown": "no markers here", "unverified_numbers": ["12.3%", "¥5"]}])
    # C-P2-2: verified==0 → pct=None (unmigrated data avoids misleading 0%; still show counts).
    assert (stats.verified, stats.unverified, stats.verified_pct) == (0, 2, None)


def test_trust_sources_section_and_numbered_refs() -> None:
    sections = [
        {"kind": "narrative", "markdown": "see [1] and [2]"},
        {"kind": "sources", "entries": [{"label": "a"}, {"label": "b"}]},
    ]
    stats = compute_report_trust(sections)
    # both [1],[2] have backing entries → verified; sources=2
    assert (stats.verified, stats.unverified, stats.sources, stats.verified_pct) == (2, 0, 2, 100)


def test_trust_numbered_ref_beyond_sources_ignored_but_not_silent() -> None:
    sections = [
        {"kind": "narrative", "markdown": "ref [5] has no entry"},
        {"kind": "sources", "entries": [{"label": "only one"}]},
    ]
    stats = compute_report_trust(sections)
    assert stats.verified == 0  # [5] > sources(1) → not counted
    assert stats.verified_pct is None  # no claims
    assert stats.sources == 1
    assert stats.silent is False  # sources>0 → strip still shows


def test_trust_recurses_into_section_body() -> None:
    sections = [
        {
            "kind": "section",
            "label": "x",
            "body": [
                {"kind": "narrative", "markdown": "合计 9627万元 [1]。"},
            ],
        },
        {"kind": "sources", "entries": [{"label": "bi_query: 全省合计"}]},
    ]
    stats = compute_report_trust(sections)
    assert stats.verified == 1
