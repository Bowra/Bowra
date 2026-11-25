"""In-place editing of PowerPoint content (pptx-inline-edit design §5).

The deck is generated here so every structure the engine must handle is present: a paragraph
that mixes run styles, a slide-number field, a text box inside a scaled group, a table with a
merged cell and a multi-paragraph cell, a chart with a number format, a picture, and two
shapes that share an ID.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from bowhead_core.presentation_edit import (
    FIELD_TOKEN,
    MAX_EDITS,
    EditRefused,
    apply_edits,
    build_inventory,
    edited_filename,
    paragraph_text,
)
from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.util import Emu

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _deck() -> bytes:
    prs = Presentation()
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)

    title = slide.shapes.add_textbox(Emu(457200), Emu(228600), Emu(6400800), Emu(600000))
    title.name = "标题"
    title.text_frame.text = "2025 年 Q1 季度销售表现"

    mixed = slide.shapes.add_textbox(Emu(457200), Emu(900000), Emu(6400800), Emu(400000))
    mixed.name = "副标题"
    paragraph = mixed.text_frame.paragraphs[0]
    for text, bold in (("季度销售额 ", False), ("2,905", True), (" 万元", False)):
        run = paragraph.add_run()
        run.text = text
        run.font.bold = bold
        run.font.size = Emu(177800)

    footer = slide.shapes.add_textbox(Emu(457200), Emu(6000000), Emu(3000000), Emu(300000))
    footer.name = "页码"
    p_el = footer.text_frame.paragraphs[0]._p
    p_el.append(parse_xml(f'<a:r {nsdecls("a")}><a:rPr lang="zh-CN"/><a:t>第 </a:t></a:r>'))
    p_el.append(
        parse_xml(f'<a:fld {nsdecls("a")} id="{{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}}" type="slidenum"><a:rPr lang="zh-CN"/><a:t>1</a:t></a:fld>')
    )
    p_el.append(parse_xml(f'<a:r {nsdecls("a")}><a:rPr lang="zh-CN"/><a:t> 页</a:t></a:r>'))

    group = slide.shapes.add_group_shape()
    child = group.shapes.add_textbox(Emu(400), Emu(400), Emu(800), Emu(800))
    child.name = "组内说明"
    child.text_frame.text = "关键成功因素"
    xfrm = group._element.grpSpPr.get_or_add_xfrm()
    for tag, attrs in (
        ("off", {"x": "1000", "y": "1000"}),
        ("ext", {"cx": "2000", "cy": "2000"}),
        ("chOff", {"x": "0", "y": "0"}),
        ("chExt", {"cx": "4000", "cy": "4000"}),
    ):
        element = xfrm.find(qn(f"a:{tag}"))
        if element is None:
            element = etree.SubElement(xfrm, qn(f"a:{tag}"))
        for key, value in attrs.items():
            element.set(key, value)

    table = slide.shapes.add_table(3, 3, Emu(4572000), Emu(2000000), Emu(3600000), Emu(1200000)).table
    table.cell(0, 0).text = "产品线"
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(1, 0).text = "产品线 B"
    table.cell(1, 1).text = "815"
    table.cell(2, 2).text = "第一段"
    table.cell(2, 2).text_frame.add_paragraph().text = "第二段"

    data = CategoryChartData(number_format="#,##0")
    data.categories = ["1 月", "2 月", "3 月"]
    data.add_series("销售额", (820, 905, 1180), number_format="#,##0")
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Emu(457200), Emu(2000000), Emu(3600000), Emu(2400000), data)
    chart.name = "月度销售额"

    slide.shapes.add_picture(io.BytesIO(_png()), Emu(8000000), Emu(6000000), Emu(300000), Emu(300000))

    second = prs.slides.add_slide(blank)
    first_box = second.shapes.add_textbox(Emu(100000), Emu(100000), Emu(1000000), Emu(300000))
    first_box.text_frame.text = "第二页"
    twin = second.shapes.add_textbox(Emu(100000), Emu(600000), Emu(1000000), Emu(300000))
    twin.text_frame.text = "重复编号"
    twin._element.nvSpPr.cNvPr.set("id", str(first_box.shape_id))

    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def deck() -> bytes:
    return _deck()


@pytest.fixture(scope="module")
def inventory(deck: bytes) -> dict:
    return build_inventory(deck)


def _element(inventory: dict, name: str) -> dict:
    for slide in inventory["slides"]:
        for element in slide["elements"]:
            if element["name"] == name:
                return element
    raise AssertionError(name)


def _runs(data: bytes, name: str) -> list[tuple[str, bool]]:
    prs = Presentation(io.BytesIO(data))
    for shape in prs.slides[0].shapes:
        if shape.name == name:
            return [(run.text, bool(run.font.bold)) for run in shape.text_frame.paragraphs[0].runs]
    raise AssertionError(name)


def _changed_parts(before: bytes, after: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(io.BytesIO(after)) as new:
        names = set(old.namelist()) | set(new.namelist())
        return {name for name in names if name not in old.namelist() or name not in new.namelist() or old.read(name) != new.read(name)}


def test_the_inventory_lists_what_can_be_edited_and_why_the_rest_cannot(inventory: dict) -> None:
    mixed = _element(inventory, "副标题")
    assert (
        mixed["kind"] == "text"
        and mixed["editable"]
        and mixed["paragraphs"][0] == {"text": "季度销售额 2,905 万元", "mixed_style": True, "fields": 0}
    )
    footer = _element(inventory, "页码")
    assert footer["paragraphs"][0]["text"] == f"第 {FIELD_TOKEN} 页" and footer["paragraphs"][0]["fields"] == 1
    table = next(element for element in inventory["slides"][0]["elements"] if element["kind"] == "table")
    assert table["rows"][0][1] == {"text": "", "editable": False, "reason": "covered_by_merge"}
    assert table["rows"][1][0]["text"] == "产品线 B" and table["rows"][1][0]["editable"]
    assert table["rows"][2][2]["reason"] == "multi_paragraph"
    chart = _element(inventory, "月度销售额")
    assert chart["chart"]["categories"] == ["1 月", "2 月", "3 月"]
    assert chart["chart"]["series"] == [{"name": "销售额", "values": [820.0, 905.0, 1180.0], "format": "#,##0"}]
    picture = next(element for element in inventory["slides"][0]["elements"] if element["kind"] == "picture")
    assert picture["editable"] is False and picture["reason"] == "picture"
    twins = list(inventory["slides"][1]["elements"])
    assert len(twins) == 2 and all(element["reason"] == "duplicate_shape_id" for element in twins)
    assert inventory["slides"][0]["slide_id"] != inventory["slides"][1]["slide_id"]


def test_the_inventory_names_each_slides_title_and_a_tables_grid(inventory: dict) -> None:
    table = next(element for element in inventory["slides"][0]["elements"] if element["kind"] == "table")
    assert table["col_widths"] == [0.33333] * 3 and table["row_heights"] == [0.33333] * 3
    assert inventory["slides"][0]["title"] is None
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "区域表现：\n5 个省份"
    buffer = io.BytesIO()
    prs.save(buffer)
    assert build_inventory(buffer.getvalue())["slides"][0]["title"] == "区域表现： 5 个省份"


def test_a_box_inside_a_scaled_group_is_placed_on_the_slide(inventory: dict) -> None:
    grouped = _element(inventory, "组内说明")
    width, height = inventory["slide_width"], inventory["slide_height"]
    # Child (400, 400, 800, 800) in a group that maps (0..4000) onto (1000..3000): half scale, offset 1000.
    assert grouped["box"] == [round(1200 / width, 5), round(1200 / height, 5), round(400 / width, 5), round(400 / height, 5)]


def test_changing_numbers_keeps_each_characters_own_style(deck: bytes, inventory: dict) -> None:
    mixed = _element(inventory, "副标题")
    output, meta = apply_edits(
        deck,
        [{"kind": "text", "key": mixed["key"], "paragraph": 0, "expect": "季度销售额 2,905 万元", "text": "季度销售额 3,050 万元（修订）"}],
    )
    assert _runs(output, "副标题") == [("季度销售额 ", False), ("3,050", True), (" 万元（修订）", False)]
    assert meta["changes"] == [
        {"index": 0, "key": mixed["key"], "kind": "text", "before": "季度销售额 2,905 万元", "after": "季度销售额 3,050 万元（修订）", "paragraph": 0}
    ]


def test_only_the_touched_slide_changes_in_the_package(deck: bytes, inventory: dict) -> None:
    title = _element(inventory, "标题")
    output, meta = apply_edits(
        deck, [{"kind": "text", "key": title["key"], "paragraph": 0, "expect": "2025 年 Q1 季度销售表现", "text": "2026 年 Q1 季度销售表现"}]
    )
    assert meta["touched_parts"] == ["ppt/slides/slide1.xml"]
    assert _changed_parts(deck, output) == {"ppt/slides/slide1.xml"}
    assert paragraph_text(Presentation(io.BytesIO(output)).slides[0].shapes[0].text_frame.paragraphs[0]._p) == "2026 年 Q1 季度销售表现"


def test_an_edit_on_text_that_changed_since_the_inventory_is_refused_and_nothing_is_written(deck: bytes, inventory: dict) -> None:
    title = _element(inventory, "标题")
    mixed = _element(inventory, "副标题")
    with pytest.raises(EditRefused) as refused:
        apply_edits(
            deck,
            [
                {"kind": "text", "key": mixed["key"], "paragraph": 0, "expect": "季度销售额 2,905 万元", "text": "季度销售额 3,050 万元"},
                {"kind": "text", "key": title["key"], "paragraph": 0, "expect": "旧标题", "text": "新标题"},
            ],
        )
    assert refused.value.refusals == [{"index": 1, "code": "target_changed"}]


def test_a_field_cannot_be_removed_but_the_text_around_it_can_change(deck: bytes, inventory: dict) -> None:
    footer = _element(inventory, "页码")
    with pytest.raises(EditRefused) as refused:
        apply_edits(deck, [{"kind": "text", "key": footer["key"], "paragraph": 0, "expect": f"第 {FIELD_TOKEN} 页", "text": "第 页"}])
    assert refused.value.refusals == [{"index": 0, "code": "field_changed"}]
    output, _ = apply_edits(
        deck, [{"kind": "text", "key": footer["key"], "paragraph": 0, "expect": f"第 {FIELD_TOKEN} 页", "text": f"Page {FIELD_TOKEN}"}]
    )
    prs = Presentation(io.BytesIO(output))
    p_el = next(shape for shape in prs.slides[0].shapes if shape.name == "页码").text_frame.paragraphs[0]._p
    assert p_el.find(f"{{{A}}}fld") is not None and paragraph_text(p_el) == f"Page {FIELD_TOKEN}"


def test_table_cells_follow_merge_and_paragraph_rules(deck: bytes, inventory: dict) -> None:
    table = next(element for element in inventory["slides"][0]["elements"] if element["kind"] == "table")
    output, meta = apply_edits(deck, [{"kind": "cell", "key": table["key"], "row": 1, "col": 0, "expect": "产品线 B", "text": "产品线 B（华东）"}])
    cell = next(shape for shape in Presentation(io.BytesIO(output)).slides[0].shapes if shape.has_table).table.cell(1, 0)
    assert cell.text == "产品线 B（华东）" and meta["touched_parts"] == ["ppt/slides/slide1.xml"]
    with pytest.raises(EditRefused) as refused:
        apply_edits(
            deck,
            [
                {"kind": "cell", "key": table["key"], "row": 0, "col": 1, "expect": "", "text": "x"},
                {"kind": "cell", "key": table["key"], "row": 2, "col": 2, "expect": "第一段\n第二段", "text": "y"},
            ],
        )
    assert refused.value.refusals == [{"index": 0, "code": "covered_by_merge"}, {"index": 1, "code": "multi_paragraph"}]


def test_chart_values_change_with_their_number_format_and_workbook(deck: bytes, inventory: dict) -> None:
    chart = _element(inventory, "月度销售额")
    expect = {"categories": chart["chart"]["categories"], "series": [{"name": "销售额", "values": [820, 905, 1180]}]}
    output, meta = apply_edits(
        deck, [{"kind": "chart", "key": chart["key"], "expect": expect, "series": [{"name": "销售额", "values": [820, 905, 1250]}]}]
    )
    assert meta["changes"][0]["values"] == [{"category": "3 月", "series": "销售额", "before": 1180.0, "after": 1250.0}]
    assert "ppt/slides/slide1.xml" not in meta["touched_parts"]
    assert any(part.startswith("ppt/charts/chart") and part.endswith(".xml") for part in meta["touched_parts"])
    assert any(part.startswith("ppt/embeddings/") for part in meta["touched_parts"])
    assert _changed_parts(deck, output) <= set(meta["touched_parts"]) | set(meta["added_parts"]) | {"[Content_Types].xml"}
    after = _element(build_inventory(output), "月度销售额")["chart"]["series"][0]
    assert after == {"name": "销售额", "values": [820.0, 905.0, 1250.0], "format": "#,##0"}
    with pytest.raises(EditRefused) as refused:
        apply_edits(deck, [{"kind": "chart", "key": chart["key"], "expect": expect, "series": [{"name": "销售额", "values": [820, 905, "91%"]}]}])
    assert refused.value.refusals == [{"index": 0, "code": "not_a_number"}]


def test_read_only_elements_and_malformed_requests_are_refused(deck: bytes, inventory: dict) -> None:
    picture = next(element for element in inventory["slides"][0]["elements"] if element["kind"] == "picture")
    twin = inventory["slides"][1]["elements"][0]
    title = _element(inventory, "标题")
    with pytest.raises(EditRefused) as refused:
        apply_edits(
            deck,
            [
                {"kind": "text", "key": picture["key"], "paragraph": 0, "expect": "", "text": "x"},
                {"kind": "text", "key": twin["key"], "paragraph": 0, "expect": "第二页", "text": "x"},
                {"kind": "text", "key": "999:999", "paragraph": 0, "expect": "", "text": "x"},
            ],
        )
    assert refused.value.refusals == [
        {"index": 0, "code": "picture"},
        {"index": 1, "code": "duplicate_shape_id"},
        {"index": 2, "code": "target_missing"},
    ]
    same = {"kind": "text", "key": title["key"], "paragraph": 0, "expect": "2025 年 Q1 季度销售表现", "text": "a"}
    with pytest.raises(EditRefused) as duplicated:
        apply_edits(deck, [same, {**same, "text": "b"}])
    assert duplicated.value.refusals == [{"index": 1, "code": "duplicate_edit"}]
    with pytest.raises(EditRefused) as control:
        apply_edits(deck, [{**same, "text": "a\x00b"}])
    assert control.value.refusals == [{"index": 0, "code": "control_character"}]
    with pytest.raises(EditRefused) as many:
        apply_edits(deck, [same] * (MAX_EDITS + 1))
    assert many.value.refusals == [{"index": None, "code": "too_many_edits"}]


def test_a_new_file_from_an_edited_upload_gets_a_distinct_name() -> None:
    assert edited_filename("季度复盘.pptx") == "季度复盘（已修改）.pptx"
    assert edited_filename("Deck.PPTX") == "Deck（已修改）.pptx"
    assert edited_filename("") == "演示文稿（已修改）.pptx"
    assert edited_filename("Deck.pptx", "en-US") == "Deck (edited).pptx"
    assert edited_filename("", "fr-FR") == "Présentation (modifié).pptx"
