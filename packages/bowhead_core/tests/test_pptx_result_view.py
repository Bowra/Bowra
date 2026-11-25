"""A written presentation comes back with facts read from the saved file."""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.pptx_result_view import LAYOUT_NOT_CHECKED, PPTX_MIME, attach_result_views, inspect_pptx
from bowhead_core.untrusted_parse import run_worker_json
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from bowhead_core import pptx_result_view

pytestmark = [pytest.mark.unit]


def _deck(
    *,
    values=(1.0, 2.0),
    label="Sample title",
    table_over_chart=False,
    percent=False,
    second_series=None,
    slides=1,
    chart_title=None,
    stored_axis_percent=False,
) -> bytes:
    presentation = Presentation()
    for _ in range(slides):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(4), Inches(0.6))
        box.text_frame.text = label
        data = CategoryChartData()
        data.categories = ["A", "B"]
        data.add_series("Sales", values)
        if second_series is not None:
            data.add_series("Share", second_series)
        frame = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1.2), Inches(5), Inches(3), data)
        frame.chart.plots[0].has_data_labels = True
        if percent:
            frame.chart.plots[0].data_labels.number_format = "0%"
            frame.chart.plots[0].data_labels.number_format_is_linked = False
        if chart_title is not None:
            frame.chart.has_title = True
            frame.chart.chart_title.text_frame.text = chart_title
        if stored_axis_percent:
            # As a filled template leaves it: the axis keeps its percent code but follows its source.
            frame.chart.value_axis.tick_labels.number_format = "0%"
            frame.chart.value_axis.tick_labels.number_format_is_linked = True
        if table_over_chart:
            slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(3), Inches(1))
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _kinds(view) -> list[str]:
    return [fact["kind"] for fact in view["facts"]]


def _details(view) -> str:
    return "\n".join(fact["detail"] for fact in view["facts"])


def test_view_states_chart_facts_without_a_template():
    view = inspect_pptx(_deck(values=(1.23456789, 2.0)))
    assert view["status"] == "checked" and view["summary"] == "1 slides; 1 charts, 0 tables, 0 pictures."
    assert _kinds(view) == ["chart"] and "General format" in _details(view)
    assert view["template"]["compared"] is False
    # The view lists facts and what it did not check; it echoes no slide text.
    assert "Sample title" not in str(view) and view["not_checked"]


def test_empty_chart_percent_format_and_series_of_different_size():
    assert "no values in any series" in _details(inspect_pptx(_deck(values=(None, None))))
    assert "displays as 5e+06%" in _details(inspect_pptx(_deck(values=(50000.0, 3.0), percent=True)))
    assert "over 100 times larger" in _details(inspect_pptx(_deck(values=(50000.0, 30000.0), second_series=(0.2, 0.3))))
    assert "times larger" not in _details(inspect_pptx(_deck(values=(5.0, 3.0), second_series=(2.0, 3.0))))


def test_a_kept_chart_title_and_a_stored_percent_axis_are_reported():
    template = _deck(values=(None, None), chart_title="Sales % by week", stored_axis_percent=True)
    filled = inspect_pptx(_deck(values=(855.0, 38.0), label="Brand growth", chart_title="Sales % by week", stored_axis_percent=True), template)
    details = _details(filled)
    assert 'title "Sales % by week"' in details
    assert "stores the format '0%'" in details and "largest value is 855" in details
    retitled = inspect_pptx(_deck(values=(0.4, 0.6), label="Brand growth", chart_title="Share by brand", stored_axis_percent=True), template)
    # A new title and values that fit a percent axis leave nothing to report.
    assert _kinds(retitled) == []


def test_template_comparison_reports_same_text_and_an_added_shape_over_a_chart():
    template = _deck()
    moved = Presentation(io.BytesIO(template))
    moved.slides[0].shapes[1].left += 12700
    buffer = io.BytesIO()
    moved.save(buffer)
    unchanged_text = inspect_pptx(buffer.getvalue(), template)
    assert _kinds(unchanged_text) == ["same_text_as_template"] and "Sample title" in _details(unchanged_text)
    assert unchanged_text["template"] == {"compared": True, "matched_shapes": 2, "shapes": 2}

    edited = Presentation(io.BytesIO(template))
    slide = edited.slides[0]
    slide.shapes[0].text_frame.text = "October revenue"
    slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(3), Inches(1))
    buffer = io.BytesIO()
    edited.save(buffer)
    view = inspect_pptx(buffer.getvalue(), template)
    assert _kinds(view) == ["overlap"] and "added) overlaps" in _details(view)
    assert view["template"]["matched_shapes"] == 2 and view["template"]["shapes"] == 3


def test_each_kind_of_fact_has_its_own_cap():
    template = _deck(values=(1.23456789, 2.0), slides=12)
    # Every chart moved by a point: no slide is identical to the template, every label still is.
    moved = Presentation(io.BytesIO(template))
    for slide in moved.slides:
        slide.shapes[1].left += 12700
    buffer = io.BytesIO()
    moved.save(buffer)
    view = inspect_pptx(buffer.getvalue(), template)
    assert _kinds(view).count("chart") == 8 and _kinds(view).count("same_text_as_template") == 6
    assert view["facts_omitted"] == {"chart": 4, "same_text_as_template_slides": 6}
    assert all(len(fact["detail"]) <= 320 for fact in view["facts"])


def _saved(presentation) -> bytes:
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def test_slides_identical_to_the_template_are_listed_once_and_a_chart_data_change_counts():
    """1.7.50 on 116 delivered slides 2-4 byte for byte as in the template; per-shape facts did not show it."""
    template = _deck(slides=3)
    filled = Presentation(io.BytesIO(template))
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("Sales", (7.0, 9.0))
    # Only the chart part changes; the slide XML stays the same.
    filled.slides[1].shapes[1].chart.replace_data(data)
    view = inspect_pptx(_saved(filled), template)
    assert view["summary"] == "3 slides; 3 charts, 0 tables, 0 pictures; slides 1, 3 are identical to the template."
    assert view["facts"][0] == {
        "kind": "slides_same_as_template",
        "detail": "slides 1, 3 are identical to the template (text, tables, chart data and pictures).",
    }
    # The per-shape listing stays for the changed slide only.
    assert [fact["detail"][:8] for fact in view["facts"] if fact["kind"] == "same_text_as_template"] == ["slide 2:"]
    slide_ids = [slide.slide_id for slide in filled.slides]
    assert view["_slide_state"] == {"identical": [[1, slide_ids[0]], [3, slide_ids[2]]], "changed": [slide_ids[1]]}
    assert "_slide_state" not in inspect_pptx(template)


def test_slides_an_earlier_file_of_the_turn_had_changed_are_stated_when_they_return_to_the_template(monkeypatch):
    monkeypatch.setattr(pptx_result_view, "_TURN_WRITES", {})
    template = _deck(slides=3)
    first = Presentation(io.BytesIO(template))
    first.slides[1].shapes[0].text_frame.text = "Province review"
    first_view = inspect_pptx(_saved(first), template)
    pptx_result_view._note_slides_back_to_template(first_view, "t_fill", "q1.pptx")
    assert "slides_back_to_template" not in _kinds(first_view) and "_slide_state" not in first_view

    # The next file starts again from the template, so slide 2 is back to it.
    second_view = inspect_pptx(template, template)
    pptx_result_view._note_slides_back_to_template(second_view, "t_fill", "q2.pptx")
    assert _kinds(second_view)[:2] == ["slides_same_as_template", "slides_back_to_template"]
    assert second_view["facts"][1]["detail"] == (
        "slide 2 differed from the template in 'q1.pptx', written earlier in this Turn, and is identical to it here."
    )
    assert "_slide_state" not in second_view

    other_turn = inspect_pptx(template, template)
    pptx_result_view._note_slides_back_to_template(other_turn, "t_other", "q2.pptx")
    assert "slides_back_to_template" not in _kinds(other_turn)


async def test_written_files_of_one_turn_carry_the_slide_facts_through_the_view(monkeypatch):
    monkeypatch.setattr(pptx_result_view, "_TURN_WRITES", {})
    template = _deck(slides=2)
    edited = Presentation(io.BytesIO(template))
    edited.slides[0].shapes[0].text_frame.text = "National review"
    first, second = _saved(edited), template
    store = _Store(
        {
            "art_q1": (_artifact("art_q1", "q1.pptx", PPTX_MIME, first, turn="t_fill"), first),
            "art_q2": (_artifact("art_q2", "q2.pptx", PPTX_MIME, second, turn="t_fill"), second),
        }
    )
    ctx = SimpleNamespace(artifact_store=store, user_id="u1", workspace_id="ws", turn_id="t_fill", session_store=None)

    async def attached_template(_ctx, _artifact):
        return template

    async def no_render(_data):
        return None, "renderer_unavailable"

    monkeypatch.setattr(pptx_result_view, "_template_bytes", attached_template)
    monkeypatch.setattr(pptx_result_view, "_render_pdf", no_render)
    views = [await pptx_result_view.pptx_result_view(ctx, store.artifacts[key][0]) for key in ("art_q1", "art_q2")]
    assert all("_slide_state" not in view for view in views)
    assert views[0]["summary"].endswith("slide 2 is identical to the template.")
    assert "slide 1 differed from the template in 'q1.pptx'" in _details(views[1])


async def test_the_bounded_parse_child_builds_the_view_and_reports_an_unreadable_file():
    template = _deck()
    view, outcome = await run_worker_json("pptx_result_view", {}, data=template, extra_files={"template": template})
    assert outcome == "ok" and view["template"]["compared"] is True
    assert await run_worker_json("pptx_result_view", {}, data=b"not a presentation") == (None, "failed")


class _Store:
    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.backend = SimpleNamespace(read_bounded=self._read)

    async def get(self, artifact_id, **_kwargs):
        return self.artifacts.get(artifact_id, (None, None))[0]

    async def _read(self, path, *, limit):
        return next(data for artifact, data in self.artifacts.values() if artifact.path == path)[:limit]


def _artifact(artifact_id, name, mime, data, *, turn="t_view"):
    return SimpleNamespace(id=artifact_id, filename=name, mime=mime, size=len(data), path="/" + artifact_id, created_by_turn=turn)


async def test_every_written_presentation_and_html_file_gets_a_view_including_work_files():
    deck = _deck(values=(1.23456789, 2.0))
    page = b"<!doctype html><html><body><p>ok</p></body></html>"
    store = _Store(
        {
            "art_work": (_artifact("art_work", "step.pptx", PPTX_MIME, deck), deck),
            "art_bad": (_artifact("art_bad", "broken.pptx", PPTX_MIME, b"broken"), b"broken"),
            "art_html": (_artifact("art_html", "page.html", "text/html", page), page),
            "art_other": (_artifact("art_other", "old.pptx", PPTX_MIME, deck, turn="t_earlier"), deck),
        }
    )
    ctx = SimpleNamespace(artifact_store=store, user_id="u1", workspace_id="ws", turn_id="t_view", session_store=None)
    result = {
        "ok": True,
        "output_artifacts": [
            {"artifact_id": "art_work", "name": "step.pptx", "mime": PPTX_MIME, "size": len(deck)},
            {"artifact_id": "art_bad", "name": "broken.pptx", "mime": PPTX_MIME, "size": 6},
            {"artifact_id": "art_html", "name": "page.html", "mime": "text/html", "size": len(page)},
            {"artifact_id": "art_other", "name": "old.pptx", "mime": PPTX_MIME, "size": len(deck)},
            {"artifact_id": "art_csv", "name": "rows.csv", "mime": "text/csv", "size": 3},
        ],
    }
    work, bad, html, other, rows = (await attach_result_views(result, ctx))["output_artifacts"]
    assert work["result_view"]["status"] == "checked" and work["result_view"]["facts"][0]["kind"] == "chart"
    # An unreadable file is still recorded; only its view is unavailable.
    assert bad["result_view"] == {"status": "not_available", "reason": "file_not_readable"}
    assert "html_static_check" in html and "result_view" not in html
    assert "result_view" not in other and "result_view" not in rows


_LAYOUT = Path(__file__).parent / "fixtures" / "pptx_layout"


def _layout(name: str) -> bytes:
    return (_LAYOUT / name).read_bytes()


def _layout_kinds(view) -> list[str]:
    return [fact["kind"] for fact in view["layout"]["facts"]]


def test_a_render_adds_where_the_text_was_drawn_relative_to_the_template():
    view = inspect_pptx(_layout("filled.pptx"), _layout("template.pptx"), _layout("filled.pdf"), _layout("template.pdf"))
    layout = view["layout"]
    assert layout["status"] == "rendered" and layout["compared_with_template"] is True
    assert _layout_kinds(view) == [
        "outside_page",
        "table_larger_than_frame",
        "text_over_text",
        "text_outside_box",
        "text_outside_box",
        "smaller_text",
    ]
    details = " ".join(fact["detail"] for fact in layout["facts"])
    assert "table 'Sales table' is drawn" in details and "taller than its frame (288x86 pt); in the template 0 pt" in details
    assert "text of 'Footer' is drawn" in details and "below the page (page is 720x540 pt)" in details
    assert "text of 'Note A' overlaps the text of 2 other shapes ('Caption', 'Note B')" in details
    assert "text of 'Caption' is drawn at 8 pt; in the template at 18 pt." in details
    # The rotated box is not measured, and the view says so instead of refusing the file.
    assert layout["shapes_not_measured"] == 1
    assert LAYOUT_NOT_CHECKED in view["not_checked"] and not any("not rendered" in line for line in view["not_checked"])


def test_the_template_own_render_states_no_layout_fact():
    view = inspect_pptx(_layout("template.pptx"), _layout("template.pptx"), _layout("template.pdf"), _layout("template.pdf"))
    assert view["layout"]["status"] == "rendered" and view["layout"]["facts"] == []
    # An excess the template already has is not attributed to the written file.
    same = inspect_pptx(_layout("filled.pptx"), _layout("filled.pptx"), _layout("filled.pdf"), _layout("filled.pdf"))
    assert same["layout"]["facts"] == []


def test_without_a_template_layout_facts_are_absolute_and_sizes_are_not_compared():
    view = inspect_pptx(_layout("filled.pptx"), None, _layout("filled.pdf"), None)
    assert view["layout"]["compared_with_template"] is False
    assert "smaller_text" not in _layout_kinds(view) and "table_larger_than_frame" in _layout_kinds(view)
    assert all("template" not in fact["detail"] for fact in view["layout"]["facts"])


def test_text_drawn_in_the_colour_behind_it_is_stated_relative_to_the_template():
    """an observed execution: a cover title was set to the cover's own purple and could not be seen."""
    view = inspect_pptx(
        _layout("contrast-filled.pptx"), _layout("contrast-template.pptx"), _layout("contrast-filled.pdf"), _layout("contrast-template.pdf")
    )
    faint = [fact["detail"] for fact in view["layout"]["facts"] if fact["kind"] == "low_contrast_text"]
    assert faint == [
        "slide 1: text of 'Cover title' is drawn in #503391 on #503291 behind it (contrast 1.0:1), so it barely shows; "
        "in the template it is readable."
    ]
    # Readable text (white on the same purple, black on white) and the template's own render state nothing.
    template = inspect_pptx(_layout("contrast-template.pptx"), None, _layout("contrast-template.pdf"), None)
    assert template["layout"]["facts"] == []
    alone = inspect_pptx(_layout("contrast-filled.pptx"), None, _layout("contrast-filled.pdf"), None)
    assert [fact["kind"] for fact in alone["layout"]["facts"]] == ["low_contrast_text"]
    assert "template" not in alone["layout"]["facts"][0]["detail"]


def test_slides_whose_master_the_presentation_does_not_list_are_stated():
    """an observed execution: a merge copied another deck's master into the package without listing it."""
    import re
    import zipfile

    source = io.BytesIO(_layout("contrast-filled.pptx"))
    out = io.BytesIO()
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as after:
        for item in before.infolist():
            data = before.read(item.filename)
            if item.filename == "ppt/presentation.xml":
                data = re.sub(rb"<p:sldMasterIdLst>.*?</p:sldMasterIdLst>", b"<p:sldMasterIdLst/>", data, flags=re.S)
            after.writestr(item, data)
    facts = inspect_pptx(out.getvalue(), None, None, None)["facts"]
    assert {
        "kind": "unlisted_master",
        "detail": (
            "slides 1, 2 use a layout of the slide master ppt/slideMasters/slideMaster1.xml, which ppt/presentation.xml does not list "
            "among its slide masters (p:sldMasterIdLst)."
        ),
    } in facts
    assert not [fact for fact in inspect_pptx(_layout("contrast-filled.pptx"), None, None, None)["facts"] if fact["kind"] == "unlisted_master"]


def test_layout_facts_are_capped_and_an_unreadable_render_keeps_the_file_facts(monkeypatch):
    monkeypatch.setattr(pptx_result_view, "_MAX_LAYOUT_FACTS", 2)
    capped = inspect_pptx(_layout("filled.pptx"), None, _layout("filled.pdf"), None)["layout"]
    assert len(capped["facts"]) == 2 and capped["facts_omitted"] == {"text_over_text": 1, "text_outside_box": 2}
    broken = inspect_pptx(_layout("filled.pptx"), None, b"%PDF-broken", None)
    assert broken["status"] == "checked" and broken["layout"]["status"] == "not_available"
    assert any("not rendered" in line for line in broken["not_checked"])


async def test_the_view_renders_through_the_executor_and_stays_whole_when_it_cannot(monkeypatch):
    deck = _layout("filled.pptx")
    store = _Store({"art_deck": (_artifact("art_deck", "deck.pptx", PPTX_MIME, deck), deck)})
    ctx = SimpleNamespace(artifact_store=store, user_id="u1", workspace_id="ws", turn_id="t_view", session_store=None)
    artifact = store.artifacts["art_deck"][0]

    # No executor configured: file facts are returned and the layout block names the reason.
    for name in ("BOWHEAD_SKILL_WORKDIR_ROOT", "BOWHEAD_SANDBOX_EXECUTOR_URL", "BOWHEAD_SANDBOX_EXECUTOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    view = await pptx_result_view.pptx_result_view(ctx, artifact)
    assert view["status"] == "checked" and view["layout"] == {"status": "not_available", "reason": "renderer_unavailable"}

    rendered: list[bytes] = []

    async def render(data):
        rendered.append(data)
        return _layout("filled.pdf"), "ok"

    monkeypatch.setattr(pptx_result_view, "_render_pdf", render)
    view = await pptx_result_view.pptx_result_view(ctx, artifact)
    assert rendered == [deck] and view["layout"]["status"] == "rendered" and "table_larger_than_frame" in _layout_kinds(view)

    async def slow(_data):
        return None, "time_limit"

    monkeypatch.setattr(pptx_result_view, "_render_pdf", slow)
    view = await pptx_result_view.pptx_result_view(ctx, artifact)
    assert view["status"] == "checked" and view["layout"] == {"status": "not_available", "reason": "time_limit"}


async def test_a_template_is_rendered_once_for_the_files_written_from_it(monkeypatch):
    calls: list[int] = []

    async def render(data):
        calls.append(len(data))
        return _layout("template.pdf"), "ok"

    monkeypatch.setattr(pptx_result_view, "_render_pdf", render)
    monkeypatch.setattr(pptx_result_view, "_TEMPLATE_RENDERS", {})
    template = _layout("template.pptx")
    assert await pptx_result_view._template_render(template) == await pptx_result_view._template_render(template)
    assert calls == [len(template)]


def _filled(slide, name, left, top, width, height, color, *, shape_type=None, alpha=None):
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.oxml.ns import qn

    shape = slide.shapes.add_shape(shape_type or MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    shape.name = name
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color)
    if alpha is not None:
        from lxml import etree

        etree.SubElement(shape._element.spPr.find(qn("a:solidFill"))[0], qn("a:alpha"), val=str(alpha))
    return shape


def _covered(view) -> list[str]:
    return [fact["detail"] for fact in view["facts"] if fact["kind"] == "covered"]


def test_shapes_under_an_opaque_rectangle_drawn_after_them_are_stated():
    """an observed execution: two colour bands inserted below a full-slide cream rectangle on a cover."""
    from pptx.enum.shapes import MSO_SHAPE

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    _filled(slide, "Band", 0, 7.0, 10, 0.4, "E08A4E")
    _filled(slide, "Bar", 0, 0, 0.5, 7.5, "C8622F")
    _filled(slide, "Cream", 0, 0, 10, 7.5, "FBF3E4")
    label = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    label.text_frame.text = "Title"
    # Neither an oval nor a translucent rectangle hides what lies inside its box.
    hint = slide.shapes.add_textbox(Inches(6), Inches(3), Inches(1), Inches(0.4))
    hint.text_frame.text = "Note"
    _filled(slide, "Circle", 5.5, 2.5, 2, 1.5, "FFAA00", shape_type=MSO_SHAPE.OVAL)
    _filled(slide, "Glass", 5.5, 2.5, 2, 1.5, "FFFFFF", alpha=40000)
    assert _covered(inspect_pptx(_saved(presentation))) == [
        "slide 1: 'Band' (id 2) and 'Bar' (id 3) lie entirely under 'Cream' (id 4), which is drawn after them with an opaque fill, "
        "so they cannot be seen."
    ]
    # Moving the cream rectangle to the back leaves nothing covered.
    tree = slide.shapes._spTree
    cream = next(shape._element for shape in slide.shapes if shape.name == "Cream")
    tree.remove(cream)
    tree.insert(2, cream)
    assert _covered(inspect_pptx(_saved(presentation))) == []


def test_with_a_template_only_cover_pairs_it_does_not_have_are_stated():
    template_deck = Presentation()
    slide = template_deck.slides.add_slide(template_deck.slide_layouts[6])
    _filled(slide, "Track", 1, 1, 4, 0.3, "E4E8EF")
    _filled(slide, "Progress", 1, 1, 4, 0.3, "2A9D8F")
    template = _saved(template_deck)
    assert _covered(inspect_pptx(template)) == [
        "slide 1: 'Track' (id 2) lies entirely under 'Progress' (id 3), which is drawn after it with an opaque fill, so it cannot be seen."
    ]
    edited = Presentation(io.BytesIO(template))
    slide = edited.slides[0]
    _filled(slide, "Accent", 6, 1, 3, 0.1, "1F4E79")
    _filled(slide, "Card", 6, 1, 3, 4, "F4F6F8")
    assert _covered(inspect_pptx(_saved(edited), template)) == [
        "slide 1: 'Accent' (id 4, added) lies entirely under 'Card' (id 5, added), which is drawn after it with an opaque fill, so it cannot be seen."
    ]


def test_a_rectangle_over_the_whole_slide_names_the_layout_pictures_it_hides():
    from PIL import Image
    from pptx.opc.constants import RELATIONSHIP_TYPE
    from pptx.oxml.ns import qn

    presentation = Presentation()
    layout = presentation.slide_layouts[6]
    slide = presentation.slides.add_slide(layout)
    png = io.BytesIO()
    Image.new("RGB", (4, 4), "purple").save(png, format="PNG")
    picture = slide.shapes.add_picture(png, Inches(0.2), Inches(6.5), Inches(1), Inches(0.5))
    picture.name = "Logo"
    # Move the picture onto the layout, as a brand mark of the template.
    image_part = slide.part.related_part(picture._element.blip_rId)
    element = picture._element
    element.getparent().remove(element)
    element.blipFill.find(qn("a:blip")).set(qn("r:embed"), layout.part.relate_to(image_part, RELATIONSHIP_TYPE.IMAGE))
    layout.shapes._spTree.append(element)
    _filled(slide, "Cream", 0, 0, 10, 7.5, "FBF3E4")
    assert _covered(inspect_pptx(_saved(presentation))) == [
        "slide 1: 'Cream' (id 2) covers the whole slide with an opaque fill, so the picture of its layout and slide master ('Logo') "
        "does not show on this slide."
    ]
    slide._element.set("showMasterSp", "0")
    assert _covered(inspect_pptx(_saved(presentation))) == []


def test_a_chart_cache_with_points_past_its_declared_count_is_stated():
    """A script appended cached points by hand and left c:ptCount; LibreOffice then refuses the whole file."""
    presentation = Presentation(io.BytesIO(_deck(values=(1.0, 2.0))))
    chart = next(shape.chart for shape in presentation.slides[0].shapes if getattr(shape, "has_chart", False))
    for count in chart._chartSpace.iter("{http://schemas.openxmlformats.org/drawingml/2006/chart}ptCount"):
        count.set("val", "1")
    buffer = io.BytesIO()
    presentation.save(buffer)
    view = inspect_pptx(buffer.getvalue())
    assert any("list up to 2 points but declare 1 (c:ptCount)" in fact["detail"] for fact in view["facts"])
    assert not any("ptCount" in fact["detail"] for fact in inspect_pptx(_deck())["facts"])


def test_a_scatter_chart_does_not_stop_the_view():
    from pptx.chart.data import XyChartData

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    data = XyChartData()
    series = data.add_series("Stores")
    series.add_data_point(1.0, 2.0)
    series.add_data_point(3.0, 4.0)
    slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(1), Inches(1), Inches(4), Inches(3), data)
    buffer = io.BytesIO()
    presentation.save(buffer)
    view = inspect_pptx(buffer.getvalue())
    assert view["status"] == "checked" and view["summary"].startswith("1 slides; 1 charts")


async def test_a_file_the_renderer_cannot_open_is_stated_as_not_rendered(monkeypatch):
    deck = _deck()
    store = _Store({"art_deck": (_artifact("art_deck", "deck.pptx", PPTX_MIME, deck), deck)})
    ctx = SimpleNamespace(artifact_store=store, user_id="u1", workspace_id="ws", turn_id="t_view", session_store=None)

    class _Renderer:
        def __init__(self, **_kwargs):
            pass

        async def view_presentation(self, _data):
            error = ValueError("render")
            error.code = "document_pdf_invalid"
            raise error

    from bowhead_core.documents import authoring_executor

    monkeypatch.setattr(authoring_executor, "AuthoringExecutor", _Renderer)
    for name in ("BOWHEAD_SKILL_WORKDIR_ROOT", "BOWHEAD_SANDBOX_EXECUTOR_URL", "BOWHEAD_SANDBOX_EXECUTOR_TOKEN"):
        monkeypatch.setenv(name, "/tmp" if name.endswith("ROOT") else "x")
    view = await pptx_result_view.pptx_result_view(ctx, store.artifacts["art_deck"][0])
    assert view["status"] == "checked" and view["layout"] == {"status": "not_rendered", "detail": pptx_result_view.FILE_NOT_RENDERED}


async def test_the_renderer_gets_a_copy_whose_chart_caches_declare_the_points_they_list():
    """PowerPoint opens such a file and LibreOffice refuses it; only the copy for rendering is changed."""
    import zipfile

    from bowhead_core.pptx_result_view import chart_point_count_copy, render_source

    clean = _deck(values=(1.0, 2.0))
    assert chart_point_count_copy(clean) is None and await render_source(clean) == clean

    presentation = Presentation(io.BytesIO(clean))
    chart = next(shape.chart for shape in presentation.slides[0].shapes if getattr(shape, "has_chart", False))
    for count in chart._chartSpace.iter("{http://schemas.openxmlformats.org/drawingml/2006/chart}ptCount"):
        count.set("val", "1")
    buffer = io.BytesIO()
    presentation.save(buffer)
    written = buffer.getvalue()

    copy = await render_source(written)
    assert copy != written
    assert not any("ptCount" in fact["detail"] for fact in inspect_pptx(copy)["facts"])
    # The file itself still carries what the script wrote, and every other part is byte-identical.
    assert any("ptCount" in fact["detail"] for fact in inspect_pptx(written)["facts"])
    with zipfile.ZipFile(io.BytesIO(written)) as before, zipfile.ZipFile(io.BytesIO(copy)) as after:
        assert before.namelist() == after.namelist()
        assert [name for name in before.namelist() if before.read(name) != after.read(name)] == ["ppt/charts/chart1.xml"]
    # A file that cannot be read is handed over unchanged; the renderer reports on it.
    assert await render_source(b"not a presentation") == b"not a presentation"


def test_deck_structure_is_what_pptx_fill_describe_prints_and_is_cut_at_a_line():
    from bowhead_core.pptx_result_view import deck_structure

    view = deck_structure(_deck(slides=2))
    assert view["structure"].startswith("2 slides, 720x540 pt.\nslide 1:")
    assert "chart" in view["structure"] and "text: 'Sample title'" in view["structure"]
    long = deck_structure(_deck(slides=80))["structure"]
    assert len(long) <= 6200 and long.splitlines()[-1].startswith("... ") and "describe(prs, slides=[...])" in long


async def test_a_template_the_call_reads_comes_back_with_its_structure_once_per_agent_run(monkeypatch):
    from bowhead_core.pptx_result_view import attach_input_views

    monkeypatch.setattr(pptx_result_view, "_INPUTS_DESCRIBED", {})
    template = _deck()
    store = _Store(
        {
            "art_template": (_artifact("art_template", "template.pptx", PPTX_MIME, template, turn="t_earlier"), template),
            "art_step": (_artifact("art_step", "step.pptx", PPTX_MIME, template), template),
        }
    )

    def ctx(agent):
        return SimpleNamespace(artifact_store=store, user_id="u1", turn_id="t_view", tool_step=SimpleNamespace(parent_agent_id=agent))

    result = {
        "ok": True,
        "result_json": {"input_artifacts": [{"key": "tpl", "artifact_id": "art_template"}, {"key": "step", "artifact_id": "art_step"}]},
    }
    first = await attach_input_views(result, ctx("sub_a"))
    # The file written in this Turn already came back with its own result_view.
    assert set(first["input_views"]) == {"tpl"} and first["input_views"]["tpl"]["name"] == "template.pptx"
    assert first["input_views"]["tpl"]["structure"].startswith("1 slides")
    assert "input_views" not in await attach_input_views(result, ctx("sub_a"))
    assert "input_views" in await attach_input_views(result, ctx("sub_b"))
