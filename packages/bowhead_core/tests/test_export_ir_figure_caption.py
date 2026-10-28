"""批1 2.1 (chart-table-ux) — 导出侧图注吸附 + 剥前缀.

FIGURE_CAPTION_PREFIX_RE（PY）与 FE `extensions/figure-caption.ts` 的 CAPTION_RE
是跨语言双份常量。本文件只测 PY 侧行为；**跨语言一致性由
test_figure_caption_parity.py 强制**（本文件的 golden 会被它读去与 TS 侧比对）。
"""

from __future__ import annotations

import pytest
from bowhead_core.export_ir import (
    iter_export_blocks,
    normalize_export_snapshot,
    split_figure_caption,
)
from test_export_ir_embed_inline import _host_with_charts

# ⚠ Exact shared fixtures (identical to TS GOLDEN_HITS / GOLDEN_MISSES).
GOLDEN_HITS = [
    ("图1：营业额自 1 月高点一路走低", "营业额自 1 月高点一路走低"),
    ("**图1**：营业额自 1 月高点一路走低", "营业额自 1 月高点一路走低"),
    ("图 12：分渠道占比对比", "分渠道占比对比"),
    ("图3: 英文冒号也认", "英文冒号也认"),
    ("**图2** ：星号后空格再冒号", "星号后空格再冒号"),
]

GOLDEN_MISSES = [
    "图表1：图表开头不是图N",
    "营业额走低，无前缀",
    "如图1所示，营收下滑",
    "[1] 来源引用不是图注",
]


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "expected"), GOLDEN_HITS)
def test_caption_rule_golden_hits(raw: str, expected: str) -> None:
    caption, rest = split_figure_caption(raw)
    assert caption == expected
    assert rest == ""


@pytest.mark.unit
@pytest.mark.parametrize("raw", GOLDEN_MISSES)
def test_caption_rule_golden_misses(raw: str) -> None:
    caption, rest = split_figure_caption(raw)
    assert caption is None
    assert rest == raw


@pytest.mark.unit
def test_split_keeps_following_prose() -> None:
    caption, rest = split_figure_caption("\n**图1**：图注句\n\n后续正文段")
    assert caption == "图注句"
    assert rest == "后续正文段"


@pytest.mark.unit
def test_inline_pass_absorbs_caption_after_anchor() -> None:
    """锚后紧邻「图N：」段 → figure_caption 子块（剥前缀）紧跟图，后续正文独立段。"""
    ir = normalize_export_snapshot(
        _host_with_charts(
            "导语段\n\n[figure:sec_c1]\n**图1**：营业额自 1 月高点一路走低\n\n后续正文段",
            ["sec_c1"],
        ),
    )
    host = ir.blocks[0]
    shape = [(c.block_kind, c.content.get("role")) for c in host.children]
    assert shape == [
        ("visual", None),
        ("paragraph", "figure_caption"),
        ("paragraph", None),
    ]
    caption = host.children[1]
    assert caption.content["markdown"] == "营业额自 1 月高点一路走低"
    assert "图1" not in caption.content["markdown"]
    assert caption.content["caption_for"] == "sec_c1"
    assert host.children[2].content["markdown"].strip() == "后续正文段"
    # Fallback remains intact: no token contains a literal anchor.
    assert not any("[figure:" in t for b in iter_export_blocks(ir.blocks) for t in b.semantic_tokens)


@pytest.mark.unit
def test_inline_pass_leaves_non_caption_prose_untouched() -> None:
    ir = normalize_export_snapshot(
        _host_with_charts("[figure:sec_c1]\n普通段落，没有图注前缀", ["sec_c1"]),
    )
    host = ir.blocks[0]
    shape = [(c.block_kind, c.content.get("role")) for c in host.children]
    assert shape == [("visual", None), ("paragraph", None)]
    assert host.children[1].content["markdown"].strip() == "普通段落，没有图注前缀"


@pytest.mark.unit
def test_dangling_anchor_keeps_caption_text_verbatim() -> None:
    """悬空锚（图没内联成功）的"图注"没有图可依附——原文原样保留，不剥前缀。

    负对照：吸附只对真实渲出的图生效，否则会把用户正文悄悄改掉。
    """
    ir = normalize_export_snapshot(
        _host_with_charts("[figure:sec_ghost]\n**图1**：这段必须原样保留", ["sec_c1"]),
    )
    host = ir.blocks[0]
    texts = [c.content.get("markdown", "") for c in host.children if c.block_kind == "paragraph"]
    assert any("**图1**：这段必须原样保留" in t for t in texts)
    assert not any(c.content.get("role") == "figure_caption" for c in host.children)
