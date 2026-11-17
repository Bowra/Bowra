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
    unchanged = inspect_pptx(template, template)
    assert _kinds(unchanged) == ["same_text_as_template"] and "Sample title" in _details(unchanged)
    assert unchanged["template"] == {"compared": True, "matched_shapes": 2, "shapes": 2}

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
    view = inspect_pptx(template, template)
    assert _kinds(view).count("chart") == 8 and _kinds(view).count("same_text_as_template") == 6
    assert view["facts_omitted"] == {"chart": 4, "same_text_as_template_slides": 6}
    assert all(len(fact["detail"]) <= 320 for fact in view["facts"])


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
