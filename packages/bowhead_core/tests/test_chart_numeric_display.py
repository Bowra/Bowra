"""Reader-facing precision survives the actual chart Tool and its wire contract."""

import pytest
from bowhead_core.canvas_tools import _add_chart, _parse_csv_with_types
from bowhead_core.chart.numeric_display import currency_identity, numeric_display, numeric_format_kinds
from test_canvas_chart_units import _ctx_with_source


def test_numeric_precision_kinds_require_explicit_percent_or_currency_facts():
    assert numeric_format_kinds({"type": "numeric", "header": "Revenue"}) == ["number"]
    assert numeric_format_kinds({"type": "numeric", "percent_scale": "fraction"}) == ["number", "percent"]
    assert numeric_format_kinds({"type": "numeric", "unit": "pp"}) == ["number"]
    assert numeric_format_kinds({"type": "numeric", "currency_code": "CAD"}) == ["number", "currency"]
    assert numeric_format_kinds({"type": "numeric", "unit": "$"}) == ["number"]
    assert numeric_format_kinds({"type": "numeric", "currency_code": "CAD", "percent_scale": "fraction"}) == ["number"]
    assert numeric_format_kinds({"type": "numeric", "currency_code": "CAD", "unit": "USD"}) == ["number"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        {"decimal_places": True, "decimal_mode": "max"},
        {"decimal_places": 13, "decimal_mode": "max"},
        {"decimal_places": 2, "decimal_mode": "fixed", "unit": "USD"},
        "0.00",
        None,
    ],
)
def test_unsupported_optional_precision_is_not_accepted(value):
    assert numeric_display(value) is None


@pytest.mark.asyncio
async def test_real_tool_retains_valid_precision_and_reports_only_invalid_entries():
    ctx, canvas = _ctx_with_source(None)
    result = await _add_chart(
        {
            "data_artifact_id": "art_333333333333",
            "hint": "raw",
            "force_kind": "table",
            "title": "Prepared values",
            "column_units": {"销售金额": "USD"},
            "column_display": {
                "销售金额": {"decimal_places": 3, "decimal_mode": "fixed"},
                "金额达成率": {"decimal_places": -1, "decimal_mode": "max"},
                "unknown": {"decimal_places": 2, "decimal_mode": "max"},
            },
        },
        ctx,
    )
    assert "error" not in result, result
    assert {item["path"] for item in result["presentation_fallbacks"]} == {"/column_display/金额达成率", "/column_display/unknown"}
    spec = canvas.appended_payload["spec"]
    amount = next(column for column in spec["columns"] if column["key"] == "销售金额")
    assert amount["currency_code"] == "USD"
    assert amount["display"] == {"decimal_places": 3, "decimal_mode": "fixed"}
    assert spec["rows"][0]["销售金额"] == 347065487.47


@pytest.mark.unit
@pytest.mark.parametrize("declaration,expected", [("percent_fraction", [0.0035, 0.2]), ("percent_points", [0.35, 0.2])])
def test_mixed_tokens_need_declared_common_percentage_shape(declaration, expected):
    rows, columns = _parse_csv_with_types(b"category,value\nA,0.35%\nB,0.2\n", type_overrides={"value": declaration})
    assert [row["value"] for row in rows] == expected
    assert columns[1].percent_scale == ("fraction" if declaration == "percent_fraction" else "points")


@pytest.mark.unit
def test_undeclared_mixed_tokens_and_unknown_currencies_remain_uninterpreted():
    rows, columns = _parse_csv_with_types(b"category,value\nA,0.35%\nB,0.2\n")
    assert rows[0]["value"] == "0.35%"
    assert columns[1].type == "categorical"
    assert currency_identity("XYZ") is None
    assert currency_identity("usd") == "USD"


@pytest.mark.asyncio
async def test_source_precision_only_enriches_a_verified_unchanged_result():
    from test_canvas_chart_units import _FakeSource

    source = _FakeSource({"销售金额": {"currency_code": "CAD", "value_format": "0.000", "unit_name": ""}})
    ctx, canvas = _ctx_with_source(source)
    result = await _add_chart({"data_artifact_id": "art_333333333333", "title": "Source precision", "force_kind": "table"}, ctx)
    assert "error" not in result
    column = next(c for c in canvas.appended_payload["spec"]["columns"] if c["key"] == "销售金额")
    assert column["currency_code"] == "CAD"
    assert column["display"] == {"decimal_places": 3, "decimal_mode": "fixed"}
    # An explicit display request wins, without converting the source amount.
    result = await _add_chart(
        {
            "data_artifact_id": "art_333333333333",
            "title": "Explicit precision",
            "force_kind": "table",
            "column_display": {"销售金额": {"decimal_places": 6, "decimal_mode": "max"}},
        },
        ctx,
    )
    assert "error" not in result
    column = next(c for c in canvas.appended_payload["spec"]["columns"] if c["key"] == "销售金额")
    assert column["display"] == {"decimal_places": 6, "decimal_mode": "max"}


def test_explicit_output_unit_does_not_borrow_a_conflicting_source_currency():
    from bowhead_core.chart.types import ChartColumn
    from bowhead_core.chart.units_enrich import enrich_columns_with_units

    column = ChartColumn("amount", "Amount", "numeric", unit="万元")
    enrich_columns_with_units([column], lambda _: {"currency_code": "CAD", "value_format": "0.000", "unit_name": ""})
    assert column.unit == "万元"
    assert column.currency_code is None and column.display is None
