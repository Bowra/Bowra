"""单位规范形 + 各面投影."""

from __future__ import annotations

import pytest
from bowhead_core.chart.types import ChartColumn
from bowhead_core.chart.units_enrich import enrich_columns_with_units, relabel_columns_for_locale

from bowhead_core import units


# ─────────────────────────────────────────────────────────────
# Input: metadata normalization.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_metadata_beats_name_guess() -> None:
    """真货币代码压过「名字像钱就是 CNY」的猜测。

    没有这条，USD 部署的 revenue 会被标成人民币——而下游是把单位当**事实**用的。
    """
    spec = units.resolve("revenue", currency_code="USD")
    assert spec == units.UnitSpec(kind="currency", code="USD", source="metadata")
    # Negative control: guess only without metadata, and guess CNY.
    guessed = units.resolve("revenue")
    assert guessed is not None
    assert guessed.code == "CNY"
    assert guessed.source == "name_guess"


@pytest.mark.unit
def test_percentage_format_means_fraction() -> None:
    """`format=percentage` ⟺ 原始值是 fraction（真实例实测：value=0.17109 显示 17.11%）."""
    spec = units.resolve("利润率", value_format="percentage")
    assert spec is not None
    assert spec.kind == "percent"
    assert spec.percent_scale == "fraction"


@pytest.mark.unit
def test_user_typed_percent_label_is_points() -> None:
    """人填的 `%` 意味着值已经放大过 ⇒ points（否则源会标 format=percentage）."""
    spec = units.resolve("市场份额", unit_name="%")
    assert spec is not None
    assert spec.percent_scale == "points"


@pytest.mark.unit
def test_user_typed_label_used_as_is() -> None:
    spec = units.resolve("客流量", unit_name="人次")
    assert spec == units.UnitSpec(kind="plain", label="人次", source="metadata")


# ─────────────────────────────────────────────────────────────
# Name fallback: containment across families.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_rate_words_win_over_money_words() -> None:
    """★`利润` 是 `利润率` 的子串——货币族先判会把所有以金额词打底的比率吞掉。

    判定顺序在这里是正确性要求，不是偏好。摘掉顺序修复这条必红。
    """
    spec = units.infer_from_name("利润率")
    assert spec is not None
    assert spec.kind == "percent", "利润率 被判成了货币"
    # No reverse false positives: monetary terms without "率" remain currency.
    money = units.infer_from_name("营业额")
    assert money is not None
    assert money.kind == "currency"


@pytest.mark.unit
def test_pct_suffix_is_percent_not_money() -> None:
    """Recognize pct suffixes before currency keywords.

    Percentage columns with monetary names must not share a currency axis merely
    because their names also contain revenue or a localized currency term.
    """
    for name in ("QoQ_pct", "同比增速_pct", "Margin_pct", "member_ipt_premium_pct"):
        spec = units.infer_from_name(name)
        assert spec is not None and spec.kind == "percent", f"{name} 没被判成百分比"

    # ★ Those 9 money-bearing names: currency is the worst misclassification.
    for name in ("营业额同比_pct", "gmv_mom_pct", "Revenue_YoY_pct", "Profit_YoY_pct"):
        spec = units.infer_from_name(name)
        assert spec is not None and spec.kind == "percent", f"{name} 被货币族吞了"

    # ★ Negative control: money terms without pct remain currency; addition does not overturn the whole family.
    for name in ("营业额", "gmv", "Revenue_YoY"):
        spec = units.infer_from_name(name)
        assert spec is not None and spec.kind == "currency", f"{name} 不该被判成百分比"


@pytest.mark.unit
def test_name_guess_leaves_percent_scale_unknown() -> None:
    """名字只能说明「是个百分比」，说不出 fraction 还是 points——不许在这里替下游拍板."""
    spec = units.infer_from_name("完成率")
    assert spec is not None
    assert spec.percent_scale is None


@pytest.mark.unit
def test_unknown_name_returns_none() -> None:
    """宁可不标也不要标错：猜不出就是 None，不许兜成 plain."""
    assert units.infer_from_name("xyzzy") is None
    assert units.infer_from_name("") is None
    assert units.infer_from_name(None) is None


# ─────────────────────────────────────────────────────────────
# Output: two projections.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_points_percent_never_maps_to_percent_unit_type() -> None:
    """★★points 标成 `percent` 会被渲染面无条件 ×100 → 2.89 渲成 289%.

    `percent` 这个 unit_type 在所有渲染面都隐含 fraction，这条闸锁住这个约定。
    """
    points = units.UnitSpec(kind="percent", percent_scale="points", label="%")
    assert units.to_unit_type(points) == "%"

    fraction = units.UnitSpec(kind="percent", percent_scale="fraction")
    assert units.to_unit_type(fraction) == "percent"


@pytest.mark.unit
def test_chart_projection_retains_currency_identity_and_supplied_symbol() -> None:
    """Same-symbol currencies remain distinguishable across chart surfaces."""
    assert units.to_chart_column(units.UnitSpec(kind="currency", code="CNY")) == {
        "format": "currency",
        "unit": "¥",
        "currency_code": "CNY",
    }
    assert units.to_chart_column(units.UnitSpec(kind="currency", code="USD"))["unit"] == "$"
    # Same fact, another projection: marker surface needs code.
    assert units.to_unit_type(units.UnitSpec(kind="currency", code="CNY")) == "CNY"


@pytest.mark.unit
def test_chart_projection_never_stamps_percent_format() -> None:
    """★前端另有一条 formatCell 对 format=percent 无条件 ×100——points 列被标上就炸。

    形状只走 percent_scale，不许顺手写 format。
    """
    patch = units.to_chart_column(units.UnitSpec(kind="percent", percent_scale="points"))
    assert patch == {"percent_scale": "points"}
    assert "format" not in patch


# ─────────────────────────────────────────────────────────────
# Column enrichment.
# ─────────────────────────────────────────────────────────────
def _cols() -> list[ChartColumn]:
    return [
        ChartColumn(key="brand", header="品牌", type="categorical"),
        ChartColumn(key="ach", header="金额达成率", type="numeric"),
        ChartColumn(key="amt", header="销售金额", type="numeric"),
    ]


@pytest.mark.unit
def test_enrich_fills_percent_scale_and_currency() -> None:
    table = {
        "金额达成率": {"currency_code": "", "value_format": "percentage", "unit_name": ""},
        "销售金额": {"currency_code": "CNY", "value_format": "", "unit_name": ""},
    }
    cols = _cols()
    changed = enrich_columns_with_units(cols, lambda n: table.get(n))
    assert changed == 2
    assert cols[1].percent_scale == "fraction"
    assert cols[2].format == "currency"
    assert cols[2].unit == "¥"
    # Nonnumeric columns unchanged.
    assert cols[0].unit is None


@pytest.mark.unit
def test_derived_columns_never_inherit_the_source_unit() -> None:
    """★★116 生产实测（2026-08-25）逮到的：给派生列套源单位会差几个数量级。

    那一轮 agent 自己算了 `df['营业额 / 元'] / 1e6` 并把列命名为 `营业额(¥M)`，
    早先版本剥掉后缀匹配到指标「营业额」，盖上 `unit="元"` —— 值是百万，单位写元，
    差 10⁶。近 60 张图里剥后缀一共命中 3 个：2 个冗余、这 1 个有害。
    所以只认精确同名，摘掉这条限制这个用例必红。
    """
    table = {"营业额": {"currency_code": "", "value_format": "", "unit_name": "元"}}
    cols = [
        ChartColumn(key="a", header="营业额(¥M)", type="numeric"),  # Derived: already divided by 1e6.
        ChartColumn(key="b", header="营业额 / 元", type="numeric"),  # Original: name includes unit.
    ]
    assert enrich_columns_with_units(cols, lambda n: table.get(n)) == 0
    assert cols[0].unit is None, "派生列被套上了源单位"
    assert cols[1].unit is None

    # Positive control: exact names still enrich; mechanism remains enabled for deployments with full metadata.
    exact = [ChartColumn(key="c", header="营业额", type="numeric")]
    assert enrich_columns_with_units(exact, lambda n: table.get(n)) == 1
    assert exact[0].unit == "元"


@pytest.mark.unit
def test_enrich_does_not_overwrite_declared_values() -> None:
    """产出方声明过的不覆盖——它比按名字回查更了解这一列."""
    cols = _cols()
    cols[1].percent_scale = "points"
    enrich_columns_with_units(
        cols,
        lambda n: {"currency_code": "", "value_format": "percentage", "unit_name": ""},
    )
    assert cols[1].percent_scale == "points"


@pytest.mark.unit
def test_enrich_skips_name_guess() -> None:
    """★查不到元数据就不动——名称兜底留给前端，它还能看到整列的值，信息比这里多。

    正对照在下半段：同样这批列，有元数据时是会被改的。
    """
    cols = _cols()
    assert enrich_columns_with_units(cols, lambda n: None) == 0
    assert cols[1].percent_scale is None
    assert cols[2].unit is None

    assert (
        enrich_columns_with_units(
            cols,
            lambda n: {"currency_code": "CNY", "value_format": "", "unit_name": ""},
        )
        > 0
    )


@pytest.mark.unit
def test_enrich_survives_broken_lookup() -> None:
    """富化项不许搞死出图."""

    def boom(_name: str) -> dict[str, str]:
        raise RuntimeError("source down")

    cols = _cols()
    assert enrich_columns_with_units(cols, boom) == 0
    assert cols[1].percent_scale is None


# ─────────────────────────────────────────────────────────────
# Replace column names with this turn's localized display labels (preserve qualifiers exactly).
# ─────────────────────────────────────────────────────────────
_DISPLAY_TABLE = {
    "营业额": {
        "currency_code": "",
        "value_format": "",
        "unit_name": "",
        "display_names": {"zh-CN": "营业额", "en-US": "GMV"},
        "primary_name": "营业额",
    },
    "利润率": {
        "currency_code": "",
        "value_format": "percentage",
        "unit_name": "",
        "display_names": {"zh-CN": "利润率", "en-US": "Profit Margin"},
        "primary_name": "利润率",
    },
}


def _lookup(name: str):
    return _DISPLAY_TABLE.get(name)


@pytest.mark.unit
def test_formula_measure_metadata_reaches_the_column() -> None:
    """★★端到端：源说了「这是百分比」，就必须一路走到列上——不该让下游去猜列名。

    116 真机（2026-08-27）：`利润率` 的概念层 item 自带 `format=percentage`，但取数只读
    模型层、公式指标在那儿没有 id ⇒ 整条事实丢失 ⇒ 表格渲 0.1802、KPI 与散文却写 18.0%。
    取数补上之后，这条链必须真的通到 `percent_scale`。
    """

    def lookup(name: str):
        # Shape matches DatarcClient.measure_metadata.
        if name.strip() == "利润率":
            return {"currency_code": "", "value_format": "percentage", "unit_name": "", "primary_name": "利润率"}
        return None

    cols = [ChartColumn(key="利润率", header="利润率", type="numeric")]
    assert enrich_columns_with_units(cols, lookup) == 1
    assert cols[0].percent_scale == "fraction"  # 0.1802 → 18.0%


@pytest.mark.unit
def test_verified_source_fraction_is_not_bounded_by_observed_magnitude() -> None:
    def lookup(name: str):
        return {"value_format": "percentage"}

    columns = [ChartColumn(key="growth", header="Growth", type="numeric")]
    enrich_columns_with_units(columns, lookup, [{"growth": 2.1735}, {"growth": 17.84}])
    assert columns[0].percent_scale == "fraction"


@pytest.mark.unit
def test_unit_enrichment_does_not_mix_currency_with_declared_percent() -> None:
    columns = [ChartColumn(key="margin", header="Margin", type="numeric", format="percent", unit="%", percent_scale="points")]
    changed = enrich_columns_with_units(columns, lambda name: {"currency_code": "CNY"})
    assert changed == 0
    assert (columns[0].format, columns[0].unit, columns[0].percent_scale) == ("percent", "%", "points")


@pytest.mark.unit
def test_relabel_keeps_the_qualifier() -> None:
    """★★只换指标名那一截，限定词原样留着。

    `营业额(¥M)` 里的 `(¥M)` 是产出方**真的除过 1e6** 才写的量纲，
    `营业额 / 元` 里的 `元` 是单位——整条换成 `GMV` 就把它们抹掉了，
    那正是 2026-08-25 在 116 逮到的「给派生列套源单位」同一类错。
    """
    cols = [
        ChartColumn(key="a", header="营业额(¥M)", type="numeric"),
        ChartColumn(key="b", header="营业额 / 元", type="numeric"),
        ChartColumn(key="c", header="利润率", type="numeric"),
    ]
    assert relabel_columns_for_locale(cols, _lookup, "en-US") == 3
    assert cols[0].header == "GMV(¥M)"
    assert cols[1].header == "GMV / 元"
    assert cols[2].header == "Profit Margin"


@pytest.mark.unit
def test_relabel_keeps_the_original_name_for_downstream_word_matching() -> None:
    """★换名必须把原名留在列上——下游有一档按列名词面判百分比，词表是按中文原名调的。

    116 真机（2026-08-27，en-US 轮）：`利润率` 换成 `profit_margin` 后，源恰好没配
    value_format，词面那一档又认不出英文，整列退回裸数 —— 表格显示 0.1802，
    而同一轮的 KPI 和散文都写 18.0%。判定要基于事实、展示才用译名。
    """
    cols = [
        ChartColumn(key="c", header="利润率", type="numeric"),
        ChartColumn(key="d", header="没这个指标", type="numeric"),
    ]
    relabel_columns_for_locale(cols, _lookup, "en-US")
    assert cols[0].header == "Profit Margin"
    assert cols[0].source_header == "利润率"  # Carry original name along.
    assert cols[1].source_header is None  # No trace for unchanged names (smaller payload).


@pytest.mark.unit
def test_relabel_covers_dimensions_too() -> None:
    """★★116 实测缺口：只接指标那一半，英文轮里维度列名仍是中文。

    实录（2026-08-26，output_language=en-US）：
      {"key": "营业额 / 元", "header": "GMV / 元"}   ← 指标换了
      {"key": "省",        "header": "省"}          ← 维度**纹丝不动**（应为 province）
    图表列两种都有，lookup 必须两边都查。
    """
    table = {
        "营业额": {
            "currency_code": "",
            "value_format": "",
            "unit_name": "",
            "display_names": {"zh-CN": "营业额", "en-US": "GMV"},
            "primary_name": "营业额",
        },
        "省": {
            "currency_code": "",
            "value_format": "",
            "unit_name": "",
            "display_names": {"zh-CN": "省", "en-US": "province"},
            "primary_name": "省",
        },
    }
    cols = [
        ChartColumn(key="省", header="省", type="geo"),
        ChartColumn(key="营业额 / 元", header="营业额 / 元", type="numeric"),
    ]
    assert relabel_columns_for_locale(cols, lambda n: table.get(n), "en-US") == 2
    assert cols[0].header == "province"
    assert cols[1].header == "GMV / 元"
    assert cols[0].key == "省" and cols[1].key == "营业额 / 元"


@pytest.mark.unit
def test_relabel_never_touches_key() -> None:
    """★★不变量：只改 `header`，**绝不改 `key`**。

    2026-08-26 review 查证：前端多处判据同时看两者并以 key 兜底——
    `view-transforms.isAdditive` 用 `header` 和 `key` 双判「这列能不能求和」，
    行数据也全按 `r[c.key]` 索引。key 一旦跟着换成英文，中文词面判据全失效、
    行取值直接取不到。换名之所以安全，全靠 key 保留原名。
    """
    cols = [ChartColumn(key="利润率", header="利润率", type="numeric")]
    relabel_columns_for_locale(cols, _lookup, "en-US")
    assert cols[0].header == "Profit Margin"
    assert cols[0].key == "利润率", "key 被改了——行取值和词面判据都会崩"


@pytest.mark.unit
def test_relabel_leaves_derived_columns_alone() -> None:
    """★派生列不许被改名：`营业额环比%` 不是「营业额」，只是名字里含它。"""
    cols = [ChartColumn(key="a", header="营业额环比%", type="numeric")]
    assert relabel_columns_for_locale(cols, _lookup, "en-US") == 0
    assert cols[0].header == "营业额环比%"


@pytest.mark.unit
def test_relabel_is_a_noop_in_the_source_language() -> None:
    """★负对照：中文轮里标签与规范名相同 ⇒ 一列都不动。"""
    cols = [
        ChartColumn(key="a", header="营业额 / 元", type="numeric"),
        ChartColumn(key="b", header="利润率", type="numeric"),
    ]
    assert relabel_columns_for_locale(cols, _lookup, "zh-CN") == 0
    assert cols[0].header == "营业额 / 元"
    assert cols[1].header == "利润率"


@pytest.mark.unit
def test_relabel_skips_unknown_and_survives_failure() -> None:
    """查不到就不动；lookup 炸了也不许搞死出图。"""
    cols = [ChartColumn(key="a", header="某个不认识的列", type="numeric")]
    assert relabel_columns_for_locale(cols, _lookup, "en-US") == 0

    def boom(_n: str):
        raise RuntimeError("down")

    cols2 = [ChartColumn(key="a", header="营业额 / 元", type="numeric")]
    assert relabel_columns_for_locale(cols2, boom, "en-US") == 0
    assert cols2[0].header == "营业额 / 元"


@pytest.mark.unit
def test_relabel_runs_after_unit_enrichment() -> None:
    """★顺序即正确性：补单位按**原列名**精确匹配，换完名就匹配不上了。

    这条钉住两步的先后——反过来跑的话单位会全丢。
    """
    cols = [ChartColumn(key="a", header="利润率", type="numeric")]
    assert enrich_columns_with_units(cols, _lookup) == 1
    assert cols[0].percent_scale == "fraction"
    relabel_columns_for_locale(cols, _lookup, "en-US")
    assert cols[0].header == "Profit Margin"
    assert cols[0].percent_scale == "fraction", "换名把已补的单位弄丢了"
