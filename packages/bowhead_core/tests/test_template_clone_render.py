"""V2.9 A5 · template_clone_render 单测 (用合成 .pptx fixture)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.template_clone_render import (
    FillResult,
    _match_sections_to_slides,
    render_filled_pptx,
)
from bowhead_core.template_clone_workflow import ComposedSection

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def synthetic_pptx(tmp_path: Path) -> Path:
    """生成一份 4-slide 测试 .pptx:
    - slide 0: 标题 "全产品业务回顾" + 1 个 table (2x3, 1 header + 1 data row 占位)
    - slide 1: 标题 "2025 YTD by 省份" + 1 个 table (2x3)
    - slide 2: 标题 "客观结论 Facts" 文字 frame
    - slide 3: 无标题, 用来当 unmatched 路径
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]  # blank layout

    # slide 0
    slide0 = prs.slides.add_slide(blank)
    tb = slide0.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6))
    tb.text_frame.text = "全产品业务回顾"
    tbl_shape = slide0.shapes.add_table(
        rows=2,
        cols=3,
        left=Inches(0.5),
        top=Inches(1.5),
        width=Inches(9),
        height=Inches(2),
    )
    tbl = tbl_shape.table
    for ci, h in enumerate(("Quarter", "Target", "Actual")):
        tbl.rows[0].cells[ci].text = h
    # Row 1 placeholders (will be filled)
    for ci in range(3):
        tbl.rows[1].cells[ci].text = "(placeholder)"

    # slide 1
    slide1 = prs.slides.add_slide(blank)
    tb = slide1.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6))
    tb.text_frame.text = "2025 YTD by 省份"
    tbl_shape = slide1.shapes.add_table(
        rows=3,
        cols=2,
        left=Inches(0.5),
        top=Inches(1.5),
        width=Inches(9),
        height=Inches(2),
    )
    tbl = tbl_shape.table
    tbl.rows[0].cells[0].text = "省份"
    tbl.rows[0].cells[1].text = "销售额"

    # slide 2
    slide2 = prs.slides.add_slide(blank)
    tb = slide2.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6))
    tb.text_frame.text = "客观结论 Facts"

    # slide 3 unmatched
    prs.slides.add_slide(blank)

    out = tmp_path / "synthetic.pptx"
    prs.save(str(out))
    return out


# ─────────────────────────────────────────────────────────────
# Matching algorithm
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_match_sections_by_title(synthetic_pptx: Path):
    from pptx import Presentation

    prs = Presentation(str(synthetic_pptx))

    sections = [
        ComposedSection(id="s1", title="全产品业务回顾", chapter_tag="全产品", kind="trend_with_compare", data=None, narrative=""),
        ComposedSection(id="s2", title="2025 YTD by 省份", chapter_tag="全产品", kind="breakdown", data=None, narrative=""),
    ]
    matches = _match_sections_to_slides(prs, sections)
    assert matches.get(0) is not None and matches[0].id == "s1"
    assert matches.get(1) is not None and matches[1].id == "s2"


@pytest.mark.unit
def test_match_uses_each_section_only_once(synthetic_pptx: Path):
    from pptx import Presentation

    prs = Presentation(str(synthetic_pptx))
    # Both section titles contain "全产品"; only slide 0's title truly matches; the second section's
    # title is absent from slide context → no match.
    sections = [
        ComposedSection(id="s1", title="全产品业务回顾", chapter_tag="全产品", kind="trend_with_compare", data=None, narrative=""),
        ComposedSection(id="s2", title="不在任何 slide 标题里", chapter_tag="全产品", kind="breakdown", data=None, narrative=""),
    ]
    matches = _match_sections_to_slides(prs, sections)
    assert matches.get(0).id == "s1"
    # s2 chapter_tag fallback matches an unmatched slide (1, 2, or 3).
    matched_ids = [v.id for v in matches.values() if v is not None]
    assert matched_ids.count("s1") == 1


# ─────────────────────────────────────────────────────────────
# Main rendering flow
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_render_fills_table_with_rows(synthetic_pptx: Path, tmp_path: Path):
    sections = [
        ComposedSection(
            id="s1",
            title="全产品业务回顾",
            chapter_tag="全产品",
            kind="trend_with_compare",
            data=[{"Quarter": "25Q3", "Target": "5.4", "Actual": "5.3"}],
            narrative="",
        ),
    ]
    out_path = tmp_path / "filled.pptx"
    res = render_filled_pptx(synthetic_pptx, sections, out_path)

    assert isinstance(res, FillResult)
    assert res.slides_filled >= 1
    assert out_path.exists()

    # Verify writes.
    from pptx import Presentation

    prs = Presentation(str(out_path))
    slide0 = next(iter(prs.slides))
    tbl = next((s.table for s in slide0.shapes if s.has_table), None)
    assert tbl is not None
    assert tbl.rows[1].cells[0].text == "25Q3"
    assert tbl.rows[1].cells[1].text == "5.4"


@pytest.mark.unit
def test_render_skips_deferred_section(synthetic_pptx: Path, tmp_path: Path):
    sections = [
        ComposedSection(
            id="s1",
            title="全产品业务回顾",
            chapter_tag="全产品",
            kind="trend_with_compare",
            data=None,
            narrative="",
            needs_user_input=True,
        ),
    ]
    out_path = tmp_path / "filled.pptx"
    res = render_filled_pptx(synthetic_pptx, sections, out_path)
    assert res.slides_filled == 0
    # Skip slide 0 (matched but needs_user_input)
    assert res.slides_skipped >= 1


@pytest.mark.unit
def test_render_unmatched_sections_listed(synthetic_pptx: Path, tmp_path: Path):
    sections = [
        ComposedSection(
            id="s1",
            title="完全不存在的标题",
            chapter_tag="不存在 tag",
            kind="trend_with_compare",
            data=[{"a": "1"}],
            narrative="",
        ),
    ]
    out_path = tmp_path / "filled.pptx"
    res = render_filled_pptx(synthetic_pptx, sections, out_path)
    assert "s1" in res.sections_unmatched


@pytest.mark.unit
def test_render_narrative_replaces_facts_block(synthetic_pptx: Path, tmp_path: Path):
    sections = [
        ComposedSection(
            id="s_facts",
            title="客观结论 Facts",
            chapter_tag="",
            kind="narrative",
            data=None,
            narrative="• 销售环比 +8%\n• 区域北京领跑",
        ),
    ]
    out_path = tmp_path / "filled.pptx"
    render_filled_pptx(synthetic_pptx, sections, out_path)

    from pptx import Presentation

    prs = Presentation(str(out_path))
    slide2 = list(prs.slides)[2]
    tf_texts = [s.text_frame.text for s in slide2.shapes if hasattr(s, "has_text_frame") and s.has_text_frame]
    assert any("销售环比" in t for t in tf_texts), tf_texts


@pytest.mark.unit
def test_render_empty_sections_skips_all(synthetic_pptx: Path, tmp_path: Path):
    out_path = tmp_path / "filled.pptx"
    res = render_filled_pptx(synthetic_pptx, [], out_path)
    assert res.slides_filled == 0
    assert res.slides_skipped == 4
    assert out_path.exists()
