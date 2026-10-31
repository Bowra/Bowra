"""Date display metadata preserves the data supplied to the chart Tool."""

from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.date_display import date_pattern, date_pattern_issue
from bowhead_core.chart.display_receipt import chart_display_receipt
from test_add_chart_validation import _FakeArtifact, _RichArtifactStore, _StubCtx


def test_axis_receipt_does_not_invent_date_types_or_describe_analytical_scales():
    spec = {"kind": "line", "columns": [{"key": "when", "type": "categorical"}]}
    assert chart_display_receipt(spec)["dimensions"] == [{"key": "when", "type": "categorical"}]
    assert chart_display_receipt(spec)["horizontal_axis"]["column_type"] == "categorical"
    assert "date_pattern" not in chart_display_receipt(spec)["horizontal_axis"]
    multi = {**spec, "columns": [*spec["columns"], {"key": "date", "type": "date", "date_pattern": "yyyyMMdd"}]}
    assert "column" not in chart_display_receipt(multi)["horizontal_axis"]
    assert "horizontal_axis" not in chart_display_receipt({**spec, "presentation_plan": {"dataset": {"id": "d"}}})
    assert "horizontal_axis" not in chart_display_receipt(spec, has_root_plan=True)


def test_value_display_receipt_does_not_guess_the_rendered_axis_unit():
    spec = {"kind": "line", "columns": [{"key": "value", "type": "numeric", "unit": "亿元"}]}
    assert chart_display_receipt(spec)["value_display"] == {
        "prepared_rows": "not_modified_by_axis_formatting",
        "axis_magnitude": "renderer_selected_may_change",
        "actual_axis_unit": "not_available_in_tool_receipt",
    }
    assert chart_display_receipt({**spec, "kind": "table"})["value_display"] == {
        "prepared_rows": "not_modified_by_table_formatting",
        "table_units": "renderer_may_convert_for_display",
        "actual_table_units": "not_available_in_tool_receipt",
    }


async def test_table_tool_receipt_keeps_prepared_values_and_source_units(tmp_path: Path):
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    artifact = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="values.csv", path="values.csv")
    artifacts = _RichArtifactStore([artifact], {artifact.path: b"region,revenue\nWest,120.5\nNorth,0\n"})
    context = _StubCtx(artifact_store=artifacts, canvas_store=store)
    result = await _add_chart(
        {"data_artifact_id": artifact.id, "title": "Revenue", "force_kind": "table", "column_units": {"revenue": "万元"}}, context
    )
    assert "error" not in result, result
    assert result["display_receipt"]["value_display"]["table_units"] == "renderer_may_convert_for_display"
    section = (await store.list_sections(result["canvas_id"]))[0]
    assert [row["revenue"] for row in section.payload["spec"]["rows"]] == [120.5, 0]
    assert next(column for column in section.payload["spec"]["columns"] if column["key"] == "revenue")["unit"] == "万元"


@pytest.mark.parametrize("pattern", ["YYYYMMDD", "yyyy<script>", "--", "yyyyMMdd'foo'", "y" * 81])
def test_unsupported_patterns_are_rejected(pattern):
    assert date_pattern(pattern) is None


def test_common_lowercase_date_patterns_are_normalized_without_reinterpreting_minutes():
    assert date_pattern("yyyymmdd") == "yyyyMMdd"
    assert date_pattern("yyyy-mm-dd") == "yyyy-MM-dd"
    assert date_pattern("HH:mm") == "HH:mm"
    assert date_pattern("yyyy-mm-dd HH:mm") is None


@pytest.mark.parametrize(
    ("pattern", "values", "message"),
    [
        ("yyyyMMdd", ["2026-10", "2026-11"], "absent"),
        ("yyyy-MM", ["2026-10-28", "2026-10-29"], "indistinguishable"),
        ("yyyyMMdd", ["2026-02-30"], "invalid"),
        ("yyyyMMdd", ["20260230"], "invalid"),
        ("yyyy-MM", ["20260102", "20260128"], "indistinguishable"),
        ("yyyy-MM-dd HH:mm", ["2026-10-28T16:05+08:00", "2026-10-28T16:05+09:00"], "indistinguishable"),
    ],
)
def test_incompatible_source_grain_and_collisions_are_rejected(pattern, values, message):
    assert message in date_pattern_issue(pattern, [{"date": value} for value in values], "date")


def test_equivalent_month_values_and_explicit_offsets_are_not_fabricated():
    assert date_pattern_issue("yyyy-MM", [{"date": "2026年1月"}, {"date": "2026-01"}], "date") is None
    assert date_pattern_issue("yyyy-MM-dd HH:mm XXX", [{"date": "2026-10-28T16:05+0800"}], "date") is None
    assert date_pattern_issue("yyyy-MM-dd", [{"date": "20261028"}], "date") is None


async def test_tool_persists_date_format_and_keeps_source_values(tmp_path: Path):
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    artifact = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="values.csv", path="values.csv")
    artifacts = _RichArtifactStore([artifact], {artifact.path: b"date,value\n2026-10-28,12\n2026-10-29,21\n"})
    context = _StubCtx(artifact_store=artifacts, canvas_store=store)
    result = await _add_chart(
        {
            "data_artifact_id": artifact.id,
            "title": "Values",
            "force_kind": "line",
            "column_types": {"date": "date"},
            "column_date_formats": {"date": "yyyyMMdd", "value": "yyyy", "missing": "yyyy"},
        },
        context,
    )
    assert "error" not in result, result
    assert result["display_receipt"]["horizontal_axis"] == {
        "scale": "category",
        "spacing": "equal",
        "elapsed_time_proportional": False,
        "column": "date",
        "column_type": "date",
        "date_pattern": "yyyyMMdd",
    }
    assert result["display_receipt"]["dimensions"] == [{"key": "date", "type": "date", "date_pattern": "yyyyMMdd"}]
    assert result["display_receipt"]["value_display"]["actual_axis_unit"] == "not_available_in_tool_receipt"
    assert {item["path"] for item in result["presentation_fallbacks"]} == {"/column_date_formats/value", "/column_date_formats/missing"}
    section = (await store.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    assert next(column for column in spec["columns"] if column["key"] == "date")["date_pattern"] == "yyyyMMdd"
    assert [row["date"] for row in spec["rows"]] == ["2026-10-28", "2026-10-29"]


async def test_tool_uses_valid_presets_and_reports_incompatible_patterns(tmp_path: Path):
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    artifact = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="values.csv", path="values.csv")
    artifacts = _RichArtifactStore([artifact], {artifact.path: b"date,value\n2026-10-28,12\n2026-10-29,21\n"})
    context = _StubCtx(artifact_store=artifacts, canvas_store=store)
    base = {"data_artifact_id": artifact.id, "title": "Values", "force_kind": "line", "column_types": {"date": "date"}}
    valid = await _add_chart({**base, "column_date_formats": {"date": "compact_date"}}, context)
    first = next(section for section in await store.list_sections(valid["canvas_id"]) if section.id == valid["section_id"])
    assert next(column for column in first.payload["spec"]["columns"] if column["key"] == "date")["date_pattern"] == "yyyyMMdd"
    invalid = await _add_chart({**base, "title": "Different values", "column_date_formats": {"date": "year_month"}}, context)
    assert invalid["presentation_fallbacks"][0]["path"] == "/column_date_formats/date"
    assert invalid["display_receipt"]["dimensions"] == [{"key": "date", "type": "date"}]
    assert invalid["display_receipt"]["horizontal_axis"]["date_pattern"] == "auto"
    assert "indistinguishable" in invalid["presentation_fallbacks"][0]["message"]
    second = next(section for section in await store.list_sections(invalid["canvas_id"]) if section.id == invalid["section_id"])
    assert "date_pattern" not in next(column for column in second.payload["spec"]["columns"] if column["key"] == "date")


async def test_compact_source_dates_keep_format_and_reject_collisions(tmp_path: Path):
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    artifact = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="values.csv", path="values.csv")
    artifacts = _RichArtifactStore([artifact], {artifact.path: b"date,value\n20260102,12\n20260128,21\n"})
    context = _StubCtx(artifact_store=artifacts, canvas_store=store)
    base = {"data_artifact_id": artifact.id, "title": "Values", "force_kind": "line", "column_types": {"date": "date"}}
    result = await _add_chart({**base, "column_date_formats": {"date": "yyyy-MM-dd"}}, context)
    assert not result.get("presentation_fallbacks")
    section = next(section for section in await store.list_sections(result["canvas_id"]) if section.id == result["section_id"])
    assert [row["date"] for row in section.payload["spec"]["rows"]] == ["20260102", "20260128"]
    assert section.payload["spec"]["columns"][0]["date_pattern"] == "yyyy-MM-dd"
    collision = await _add_chart({**base, "title": "Monthly", "column_date_formats": {"date": "yyyy-MM"}}, context)
    assert "indistinguishable" in collision["presentation_fallbacks"][0]["message"]
