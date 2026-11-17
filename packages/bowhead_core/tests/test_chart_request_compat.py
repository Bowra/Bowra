"""add_chart accepts requests whose intent has one reading and reports each adjustment.

Shapes come from refused 116 calls. Each rule has a contradicting variant that is still refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart
from test_add_chart_validation import _StubCtx
from test_chart_explicit_presentation import args, context

pytestmark = pytest.mark.unit

STORES = "门店,营业额_亿,同比%,客流量_万\n一店,19.6,0.5,232\n二店,17.6,3.5,216\n三店,12.4,-1.2,198\n".encode()
UNITS = {"营业额_亿": "亿元", "同比%": "%", "客流量_万": "万人次"}
TYPES = {"同比%": "percent_points"}
AMOUNTS = {"营业额_亿": "亿元", "客流量_万": "万人次"}


def codes(result: dict) -> list[str]:
    return [entry["code"] for entry in result.get("presentation_fallbacks", [])]


def right_axis(spec: dict) -> list[str]:
    return [column["key"] for column in spec["columns"] if column.get("y_axis_id") == 1]


@pytest.mark.parametrize(("kind", "given"), [("hbar", None), ("hbar", "linear"), ("line", None), ("scatter", None)])
async def test_x_scale_is_set_to_the_one_its_kind_draws(kind: str, given: str | None) -> None:
    """116: 20 calls omitted x_scale and 3 gave hbar a linear one."""
    ctx, canvas = context(STORES)
    axes = {"x": "营业额_亿", "y": ["客流量_万"]} if kind == "scatter" else {"x": "门店", "y": ["营业额_亿"]}
    if given:
        axes["x_scale"] = given
    result = await _add_chart(args(force_kind=kind, axes=axes, group_by=[], column_units=AMOUNTS), ctx)
    assert "error" not in result, result
    expected = "linear" if kind == "scatter" else "category"
    assert canvas.appended_payload["spec"]["explicit_presentation"]["axes"]["x_scale"] == expected
    assert codes(result) == ["x_scale_set"]


async def test_table_columns_sent_as_json_text_are_read() -> None:
    """116 the regression case: columns arrived as '["类别", ...]' and were refused twice per table."""
    ctx, canvas = context(STORES)
    result = await _add_chart(args(force_kind="table", columns='["门店", "营业额_亿"]'), ctx)
    assert "error" not in result, result
    assert canvas.appended_payload["spec"]["explicit_presentation"]["columns"] == ["门店", "营业额_亿"]
    assert codes(result) == ["json_text_read"]


@pytest.mark.parametrize("kind", ["combo", "bar", "line", "hbar"])
async def test_two_units_on_one_plot_are_drawn_on_a_dual_axis(kind: str) -> None:
    """116: 19 combo/bar/line calls put an amount and a rate on one value axis; titles asked for bars
    and a rate line. The first y field's unit stays left (bars), the other goes right (line)."""
    ctx, canvas = context(STORES)
    request = args(
        force_kind=kind, axes={"x": "门店", "y": ["营业额_亿", "同比%"], "x_scale": "category"}, group_by=[], column_units=UNITS, column_types=TYPES
    )
    result = await _add_chart(request, ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert spec["kind"] == result["kind"] == "dual_axis"
    assert right_axis(spec) == ["同比%"]
    assert spec["explicit_presentation"]["axes"]["y_right"] == ["同比%"]
    (entry,) = result["presentation_fallbacks"]
    assert entry["code"] == "two_units_drawn_as_dual_axis"
    assert "(亿元, %)" in entry["detail"]
    assert ("A dual axis is vertical." in entry["detail"]) == (kind == "hbar")


async def test_three_units_and_one_unit_keep_their_rules() -> None:
    ctx, canvas = context(STORES)
    three = args(
        force_kind="combo",
        axes={"x": "门店", "y": ["营业额_亿", "同比%", "客流量_万"], "x_scale": "category"},
        group_by=[],
        column_units=UNITS,
        column_types=TYPES,
    )
    assert "cannot share a value axis" in (await _add_chart(three, ctx))["error"]
    assert canvas.appended_payload is None
    one = args(force_kind="combo", axes={"x": "门店", "y": ["营业额_亿", "营业额_亿2"], "x_scale": "category"}, group_by=[])
    ctx, canvas = context("门店,营业额_亿,营业额_亿2\n一店,1,2\n二店,3,4\n".encode())
    result = await _add_chart({**one, "column_units": {"营业额_亿": "亿元", "营业额_亿2": "亿元"}}, ctx)
    assert result["kind"] == "combo" and "presentation_fallbacks" not in result


async def test_a_combo_with_a_right_axis_is_drawn_as_dual_axis() -> None:
    """116 the regression case: combo with y_right was refused; dual_axis is the kind that has that axis."""
    ctx, canvas = context(STORES)
    request = args(
        force_kind="combo",
        axes={"x": "门店", "y": ["营业额_亿"], "y_right": ["同比%"], "x_scale": "category"},
        group_by=[],
        column_units=UNITS,
        column_types=TYPES,
    )
    result = await _add_chart(request, ctx)
    assert "error" not in result, result
    assert canvas.appended_payload["spec"]["kind"] == "dual_axis"
    assert right_axis(canvas.appended_payload["spec"]) == ["同比%"]
    assert codes(result) == ["combo_drawn_as_dual_axis"]


async def test_a_converted_chart_saves_and_reloads_as_drawn(tmp_path: Path) -> None:
    from test_chart_explicit_presentation import _FakeArtifact, _RichArtifactStore

    canvas = CanvasStore(tmp_path / "canvas.db")
    await canvas.initialize()
    artifact = _FakeArtifact("art_aaaaaaaaaaaa", "result.csv", "result.csv")
    ctx = _StubCtx(artifact_store=_RichArtifactStore([artifact], {artifact.path: STORES}), canvas_store=canvas)
    view = {"id": "trend", "title": "营业额与同比", "force_kind": "combo", "axes": {"x": "门店", "y": ["营业额_亿", "同比%"]}, "group_by": []}
    table = {"id": "detail", "title": "明细", "force_kind": "table", "columns": "all"}
    result = await _add_chart(args(views=[view, table], layout="stack", column_units=UNITS, column_types=TYPES), ctx)
    assert "error" not in result, result
    assert set(codes(result)) == {"x_scale_set", "two_units_drawn_as_dual_axis"}
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    saved = section.payload["spec"]["views"][0]["spec"]
    assert saved["kind"] == "dual_axis" and right_axis(saved) == ["同比%"]
    assert "presentation_fallbacks" not in section.payload["spec"]


async def test_root_selection_copied_from_a_view_is_ignored_and_a_contradiction_is_refused() -> None:
    """116 the regression case: root axes/force_kind/group_by repeated the first view's."""
    view = {"id": "top", "title": "营业额", "force_kind": "hbar", "axes": {"x": "门店", "y": ["营业额_亿"], "x_scale": "category"}, "group_by": []}
    ctx, canvas = context(STORES)
    result = await _add_chart(args(views=[view], force_kind="hbar", axes=view["axes"], group_by=[]), ctx)
    assert "error" not in result, result
    assert codes(result) == ["root_selection_ignored"] * 3
    ctx, canvas = context(STORES)
    result = await _add_chart(args(views=[view], force_kind="bar"), ctx)
    assert result["error"].startswith("Explicit composition does not accept ['force_kind']")
    assert canvas.appended_payload is None


async def test_conflicting_view_fields_stay_in_the_view_and_are_refused() -> None:
    views = [
        {
            "id": "a",
            "title": "A",
            "force_kind": "bar",
            "axes": {"x": "门店", "y": ["同比%"], "x_scale": "category"},
            "group_by": [],
            "column_types": {"同比%": "percent_points"},
        },
        {
            "id": "b",
            "title": "B",
            "force_kind": "bar",
            "axes": {"x": "门店", "y": ["同比%"], "x_scale": "category"},
            "group_by": [],
            "column_types": {"同比%": "percent_fraction"},
        },
    ]
    ctx, canvas = context(STORES)
    result = await _add_chart(args(views=views), ctx)
    assert result["error"].startswith("View b has column_types, which a view does not accept.")
    assert canvas.appended_payload is None


async def test_units_for_columns_that_are_absent_or_not_numeric_are_ignored() -> None:
    """116: units were declared for columns the data did not have, or for the category column."""
    ctx, canvas = context(STORES)
    units = {**AMOUNTS, "2025营业额_亿": "亿元", "门店": "家"}
    result = await _add_chart(
        args(force_kind="bar", axes={"x": "门店", "y": ["营业额_亿"], "x_scale": "category"}, group_by=[], column_units=units), ctx
    )
    assert "error" not in result, result
    assert [entry["path"] for entry in result["presentation_fallbacks"]] == ["/column_units/2025营业额_亿", "/column_units/门店"]
    assert "'2025营业额_亿' is not a column of this data" in result["presentation_fallbacks"][0]["detail"]
    assert "'门店' is not numeric" in result["presentation_fallbacks"][1]["detail"]
    ctx, canvas = context(STORES)
    refused = await _add_chart(
        args(force_kind="bar", axes={"x": "门店", "y": ["营业额_亿"], "x_scale": "category"}, group_by=[], column_units={"同比%": "%"}), ctx
    )
    assert "'同比%' uses unit % without a percentage shape" in refused["error"]


async def test_group_by_inside_axes_moves_out_unless_it_contradicts() -> None:
    """116 t_sched_f5f087fcb8: group_by was written inside axes as well as beside it."""
    ctx, canvas = context(STORES)
    axes = {"x": "门店", "y": ["营业额_亿"], "x_scale": "category", "group_by": []}
    result = await _add_chart(args(force_kind="bar", axes=axes, group_by=[]), ctx)
    assert "error" not in result, result
    assert codes(result) == ["group_by_moved"]
    regions = "月份,区域,营业额_亿\n2025-01,华东,1\n2025-01,华北,2\n2025-02,华东,3\n2025-02,华北,4\n".encode()
    ctx, canvas = context(regions)
    split = {"x": "月份", "y": ["营业额_亿"], "x_scale": "category", "group_by": ["区域"]}
    result = await _add_chart(args(force_kind="bar", axes=split, group_by=[]), ctx)
    assert "Declare axes with exact x, y and x_scale keys" in result["error"]
    assert canvas.appended_payload is None


async def test_selection_fields_of_the_other_shape_are_ignored_only_when_they_add_nothing() -> None:
    ctx, canvas = context(STORES)
    table = await _add_chart(args(force_kind="table", axes={"x": "门店", "y": ["营业额_亿"], "x_scale": "category"}, group_by=[]), ctx)
    assert "error" not in table, table
    assert codes(table) == ["table_plot_field_ignored", "table_plot_field_ignored"]
    assert canvas.appended_payload["spec"]["explicit_presentation"]["columns"] == ["门店", "营业额_亿", "同比%", "客流量_万"]
    ctx, canvas = context(STORES)
    plot = await _add_chart(
        args(force_kind="bar", axes={"x": "门店", "y": ["营业额_亿"], "x_scale": "category"}, group_by=[], columns=["门店", "营业额_亿"]), ctx
    )
    assert codes(plot) == ["plot_columns_ignored"]
    ctx, canvas = context(STORES)
    wider = await _add_chart(
        args(force_kind="bar", axes={"x": "门店", "y": ["营业额_亿"], "x_scale": "category"}, group_by=[], columns=["门店", "营业额_亿", "同比%"]),
        ctx,
    )
    assert "Plots use axes and group_by, not Table columns." in wider["error"]


async def test_a_refused_request_reports_no_adjustments() -> None:
    ctx, canvas = context(STORES)
    result = await _add_chart(args(force_kind="hbar", axes={"x": "区域", "y": ["营业额_亿"]}, group_by=[]), ctx)
    assert "Requested fields are absent: ['区域']" in result["error"]
    assert "presentation_fallbacks" not in result
