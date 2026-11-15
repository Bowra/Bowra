"""A written presentation comes back with facts read from the saved file."""

import io
from types import SimpleNamespace

import pytest
from bowhead_core.pptx_result_view import PPTX_MIME, attach_result_views, inspect_pptx
from bowhead_core.untrusted_parse import run_worker_json
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

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
