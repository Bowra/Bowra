"""The producer can declare fractional percentages or percentage points.

Value ranges and column names cannot distinguish every valid convention. Verify
that explicit declarations survive the pipeline and contradictions are rejected.
"""

from __future__ import annotations

import pytest
from bowhead_core.canvas_tools import _PERCENT_TYPE_OVERRIDES, _parse_csv_with_types


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def _col(cols, key):
    return next(c for c in cols if c.key == key)


@pytest.mark.unit
def test_declared_fraction_lands_on_the_column() -> None:
    """验收 1：声明 `percent_fraction` → 列上带 `percent_scale=fraction`。"""
    rows, cols = _parse_csv_with_types(
        _csv("品牌,占比\n金辉福,0.0416\n六福,0.0211\n"),
        type_overrides={"占比": "percent_fraction"},
    )
    c = _col(cols, "占比")
    assert c.type == "numeric"
    assert c.percent_scale == "fraction"
    # Also set `format`: frontend `resolvePercentColumn` recognizes `format === "percent"`;
    # otherwise the column never enters percent formatting.
    assert c.format == "percent"
    assert rows[0]["占比"] == pytest.approx(0.0416)


@pytest.mark.unit
def test_declared_points_lands_on_the_column() -> None:
    rows, cols = _parse_csv_with_types(
        _csv("品牌,市场份额\nA,2.45\n"),
        type_overrides={"市场份额": "percent_points"},
    )
    c = _col(cols, "市场份额")
    assert (c.type, c.format, c.percent_scale) == ("numeric", "percent", "points")
    assert rows[0]["市场份额"] == pytest.approx(2.45)


@pytest.mark.unit
def test_declared_fraction_is_not_reclassified_by_value_range() -> None:
    _, cols = _parse_csv_with_types(
        b"category,growth\nA,2.1735\nB,17.84\n",
        type_overrides={"growth": "percent_fraction"},
    )
    column = _col(cols, "growth")
    assert column.percent_scale == "fraction"
    assert column.format == "percent"


@pytest.mark.unit
def test_fraction_declaration_survives_ratios_over_100_percent() -> None:
    """A valid fractional percentage may exceed 100 percent without losing its shape."""
    _, cols = _parse_csv_with_types(
        _csv("代表,销售目标达成率\n燕虹,1.2798\n庄辉翔,1.1601\n"),
        type_overrides={"销售目标达成率": "percent_fraction"},
    )
    assert _col(cols, "销售目标达成率").percent_scale == "fraction"


@pytest.mark.unit
def test_points_declaration_has_no_value_gate() -> None:
    """points 说「13.5 就是 13.5%」，任何值域都自洽，不设闸。"""
    _, cols = _parse_csv_with_types(
        _csv("品牌,占比\nA,1784.0\n"),
        type_overrides={"占比": "percent_points"},
    )
    assert _col(cols, "占比").percent_scale == "points"


@pytest.mark.unit
def test_undeclared_columns_are_byte_for_byte_unchanged() -> None:
    """★ 验收 6：不声明时行为**逐字不变**——这条改动只加一条新路，不动老路。"""
    csv = _csv("品牌,占比,营业额\nA,0.0416,1200\nB,0.0211,900\n")
    rows_a, cols_a = _parse_csv_with_types(csv)
    rows_b, cols_b = _parse_csv_with_types(csv, type_overrides={})
    assert rows_a == rows_b
    for c in cols_a + cols_b:
        assert c.percent_scale is None
        assert c.format is None


@pytest.mark.unit
def test_the_four_original_type_overrides_still_work() -> None:
    """★ 阴性对照：老的四种类型 override 一个都不能坏。"""
    for declared in ("numeric", "categorical", "date", "geo"):
        _, cols = _parse_csv_with_types(
            _csv("月份,值\n1,10\n2,20\n"),
            type_overrides={"月份": declared},
        )
        c = _col(cols, "月份")
        assert c.type == declared
        assert c.percent_scale is None, "普通类型 override 不该顺手标百分比"


@pytest.mark.unit
def test_explicit_numeric_is_a_declaration_too() -> None:
    """Explicit numeric must persist as an ordinary-number declaration.

    Otherwise a percentage-like name could trigger a second scaling pass.
    """
    _, cols = _parse_csv_with_types(
        _csv("事业部,销售目标达成率,排名,达成率百分点\nDivision A,1.25,1,125.0\nDivision B,1.04,2,104.0\nDivision C,0.95,3,95.0\n"),
        type_overrides={"销售目标达成率": "percent_fraction", "达成率百分点": "numeric"},
    )
    pts = _col(cols, "达成率百分点")
    assert pts.percent_shape_unknown is True
    assert pts.format is None and pts.percent_scale is None
    # Positive declarations in the same call remain unaffected.
    rate = _col(cols, "销售目标达成率")
    assert (rate.format, rate.percent_scale, rate.percent_shape_unknown) == ("percent", "fraction", None)
    # Do not incidentally mark undeclared columns (rank is inferred as numeric).
    assert _col(cols, "排名").percent_shape_unknown is None


@pytest.mark.unit
def test_numeric_declaration_on_a_non_numeric_column_is_not_flagged() -> None:
    """阴性对照：只有声明**生效**成数值列才打标——其余三种 override 一律不碰。"""
    _, cols = _parse_csv_with_types(
        _csv("月份,达成率\n1,0.9\n2,1.1\n"),
        type_overrides={"月份": "date", "达成率": "categorical"},
    )
    for c in cols:
        assert c.percent_shape_unknown is None


@pytest.mark.unit
def test_undeclared_columns_carry_no_flag() -> None:
    """Undeclared explicit-chart columns retain the existing formatting fallback."""
    _, cols = _parse_csv_with_types(_csv("品牌,达成率\nA,1.02\nB,0.97\n"))
    assert all(c.percent_shape_unknown is None for c in cols)


@pytest.mark.unit
def test_percent_override_values_are_exactly_the_two_documented_ones() -> None:
    """正对照：值域写死在一处，工具描述里承诺的就是这两个。

    多一个少一个都会让上面几条变成空转（模型按描述传，代码不认 = 静默忽略）。
    """
    assert _PERCENT_TYPE_OVERRIDES == {
        "percent_fraction": "fraction",
        "percent_points": "points",
    }


@pytest.mark.unit
def test_tool_description_advertises_the_new_values() -> None:
    """★★ 能力 ≠ SOP：值域加了但工具描述没说，模型不会传。

    批1 的教训——原生散点标注做出来了，agent 照样 savefig，直到改了它读得到的文本。
    这里只钉工具描述这一处；另外两处（compute 提示词 / python_sandbox SKILL）
    在批2 连同一条跨面守卫一起加。
    """
    from bowhead_core.canvas_tools import _add_chart_schema  # noqa: PLC0415

    blob = str(_add_chart_schema())
    for token in ("percent_fraction", "percent_points"):
        assert token in blob, f"工具描述里没有 {token}，模型无从知道可以声明"
    # Explicit `numeric` now has consequences (no % sign, no name-based guessing); describe them where the model can read them.
    assert "不加" in blob and "numeric" in blob
    # ★ Undeclared sandbox data gets no % sign: without stating this, the model will not know omission loses the sign.
    assert "没声明形状的数值列一律按普通数显示" in blob


# ── ι automatic charts: explicitly prohibit guessing when there is no producer ──


@pytest.mark.unit
def test_auto_chart_marks_columns_whose_shape_nobody_declared() -> None:
    """Automatic charts mark undeclared numeric shapes rather than guessing scaling."""
    from bowhead_core.canvas_tools import _mark_unknown_percent_shape  # noqa: PLC0415

    _, cols = _parse_csv_with_types(
        _csv("门店,营业额 / 元,营业额环比变化率,QoQ_pct\n示例店甲,12000,-0.12,-12.0\n示例店乙,24000,-0.18,-18.0\n"),
    )
    assert _mark_unknown_percent_shape(cols) == 3
    assert _col(cols, "营业额环比变化率").percent_shape_unknown is True
    assert _col(cols, "QoQ_pct").percent_shape_unknown is True
    assert _col(cols, "门店").percent_shape_unknown is None


@pytest.mark.unit
def test_auto_chart_never_overwrites_a_declared_shape() -> None:
    """★ 负对照：**声明过的列不许被打标**——否则自动出图会把已知形状按成「不知道」。

    两个产出方各一条：模型 `column_types` 声明；源元数据富化落下的 `format=percent`。
    """
    from bowhead_core.canvas_tools import _mark_unknown_percent_shape  # noqa: PLC0415
    from bowhead_core.chart import ChartColumn  # noqa: PLC0415

    _, cols = _parse_csv_with_types(
        _csv("品牌,占比\n金辉福,0.0416\n"),
        type_overrides={"占比": "percent_fraction"},
    )
    assert _mark_unknown_percent_shape(cols) == 0
    assert _col(cols, "占比").percent_shape_unknown is None
    assert _col(cols, "占比").percent_scale == "fraction"

    enriched = [ChartColumn(key="利润率", header="利润率", type="numeric", format="percent")]
    assert _mark_unknown_percent_shape(enriched) == 0
    assert enriched[0].percent_shape_unknown is None


@pytest.mark.unit
def test_unknown_shape_flag_reaches_the_wire_only_when_true() -> None:
    """★ `_drop_empty` 只丢 None —— 落 False 会让**每一列**都带上这个字段。

    同族教训：`show_data_labels` 当年默认写 `False`，于是 116 生产 2781/2781 张图
    都带着一个「显式 false」进前端，把它下面整条策略梯子按成了死码。
    """
    from bowhead_core.chart import ChartColumn, ChartSpec, spec_to_dict  # noqa: PLC0415

    spec = ChartSpec(
        kind="bar",
        title="t",
        columns=[
            ChartColumn(key="a", header="a", type="numeric", percent_shape_unknown=True),
            ChartColumn(key="b", header="b", type="numeric"),
        ],
    )
    out = spec_to_dict(spec)
    assert out["columns"][0]["percent_shape_unknown"] is True
    assert "percent_shape_unknown" not in out["columns"][1]


@pytest.mark.unit
def test_single_percentage_cell_supplies_shape_even_against_fraction_declaration() -> None:
    rows, columns = _parse_csv_with_types(b"rate\n0.35%\n", {"rate": "percent_fraction"})
    assert rows == [{"rate": 0.35}]
    assert (columns[0].format, columns[0].unit, columns[0].percent_scale) == ("percent", "%", "points")


@pytest.mark.unit
def test_text_declarations_precede_numeric_and_formatted_parsing() -> None:
    rows, columns = _parse_csv_with_types(
        b'identifier,code,rate\n00123,"1,234",0.35%\n',
        {"identifier": "categorical", "code": "categorical", "rate": "categorical"},
    )
    assert rows == [{"identifier": "00123", "code": "1,234", "rate": "0.35%"}]
    assert all(column.type == "categorical" and column.unit is None for column in columns)


@pytest.mark.unit
def test_unsafe_integer_tokens_never_pass_through_float() -> None:
    rows, columns = _parse_csv_with_types(b'value\n9007199254740993\n"9,007,199,254,740,995"\n9007199254740997.0\n')
    assert rows == [{"value": "9007199254740993"}, {"value": "9007199254740995"}, {"value": "9007199254740997"}]
    assert columns[0].type == "numeric"


@pytest.mark.unit
def test_safe_and_unsafe_grouped_integer_cells_remain_one_numeric_column() -> None:
    rows, columns = _parse_csv_with_types(b'value\n500\n9007199254740993\n"9,007,199,254,740,995"\n')
    assert rows == [{"value": 500}, {"value": "9007199254740993"}, {"value": "9007199254740995"}]
    assert columns[0].type == "numeric"
