"""Unit tests for infer_chart_spec.

覆盖 MVP 6 类图表 × 边界（空 / 超长 / 列爆 / hint 误用）.
"""

from __future__ import annotations

import math

from bowhead_core.chart import ChartColumn, infer_chart_spec, spec_to_dict
from bowhead_core.chart.infer import _MAX_LINE_POINTS, _axis_groups
from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1


def _cols(*defs: tuple[str, str]) -> list[ChartColumn]:
    """便捷：[("医院", "categorical"), ("金额", "numeric")] → list[ChartColumn]."""
    return [ChartColumn(key=k, header=k, type=t) for k, t in defs]  # type: ignore[arg-type]


def test_kpi_single_row_numeric_only():
    rows = [{"金额": 9553.77, "数量": 28664}]
    cols = _cols(("金额", "numeric"), ("数量", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t")
    assert spec.kind == "kpi"
    assert spec.row_count == 1
    assert spec.rows == rows  # inline


def test_empty_rows_fallback_table():
    spec = infer_chart_spec([], _cols(("医院", "categorical"), ("金额", "numeric")), hint="rank", title="t")
    assert spec.kind == "table"
    assert spec.row_count == 0
    assert "n=0" in (spec._infer_reason or "")


def test_bar_single_cat_single_num_short_names():
    # ⚠ Use neutral hint raw: this tests the label-length shape rule (short names → vertical bars);
    # previously hint="rank" was passed casually, but rank now means hbar and masks the test target.
    rows = [{"月": f"{i}月", "金额": i * 100} for i in range(1, 13)]
    cols = _cols(("月", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t")
    assert spec.kind == "bar"


def test_hbar_long_labels():
    rows = [
        {"医院": "徐州市铜山区人民医院（徐州市第六人民医院）", "金额": 3366360},
        {"医院": "驻马店市中心医院", "金额": 833258},
        {"医院": "河北医科大学第二医院", "金额": 669939},
        {"医院": "临沂市人民医院", "金额": 599945},
        {"医院": "聊城市人民医院", "金额": 566615},
        {"医院": "大同市第三人民医院", "金额": 433294},
        {"医院": "哈尔滨医科大学附属第二医院", "金额": 429960},
        {"医院": "定州市人民医院", "金额": 399964},
        {"医院": "龙口市人民医院", "金额": 399964},
        {"医院": "东阿县人民医院", "金额": 366634},
    ]
    cols = _cols(("医院", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t")
    assert spec.kind == "hbar", f"expected hbar for long labels, got {spec.kind}"


def test_line_with_date_col():
    rows = [{"月": f"2025-{i:02d}", "金额": 100 * i} for i in range(1, 7)]
    cols = _cols(("月", "date"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "line"


def test_pie_share_hint_low_rows():
    rows = [{"品类": c, "占比": v} for c, v in [("A", 30), ("B", 25), ("C", 20), ("D", 15), ("E", 10)]]
    cols = _cols(("品类", "categorical"), ("占比", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="share", title="t")
    assert spec.kind == "pie"


def test_pie_share_too_many_rows_falls_back():
    """share hint 但 n > 8 → 退 bar，不硬塞 pie."""
    rows = [{"品类": f"C{i}", "占比": i} for i in range(1, 12)]
    cols = _cols(("品类", "categorical"), ("占比", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="share", title="t")
    assert spec.kind in ("bar", "hbar")


def test_many_rows_now_truncate_instead_of_degrading_to_table():
    """行数多不再整张退表，改成取 top-N 后照常出图（chart-config-ux 批3 ⑤）。

    旧行为 `n > 30 → table`：116 live 窗口 41 张图栽在它上面（p50=39），
    用户点名要图、拿到一张 39 行的表。

    ⚠ 这里落的是 **hbar**（类目在纵轴、容器会长高内滚），所以行数上限放宽到
    `inline_rows_limit`，不是 30。竖柱那条见 `test_vertical_bar_still_capped`。
    """
    rows = [{"医院": f"h{i}", "金额": i} for i in range(100)]
    cols = _cols(("医院", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t")
    assert spec.kind == "hbar"
    assert len(spec.rows or []) == 50, "横条能长高内滚，放宽到内联上限"
    assert spec.top_n == 50, "仍然只画了 50/100，标记必须跟着真实行数走"
    assert spec.row_count == 100, "row_count 是总量口径，截断改的是展示"
    # Keep the largest values, not the first N.
    assert min(r["金额"] for r in spec.rows or []) == 50


def test_vertical_bar_still_capped_at_max_bar_rows():
    """竖柱/折线族的类目在**横轴**，行多只能挤 —— 仍然截到 `_MAX_BAR_ROWS`。

    放开只对「能长高内滚」的图种成立，不是对所有图。
    """
    from bowhead_core.chart.infer import _MAX_BAR_ROWS

    # Short category names + raw hint ⇒ rule 11 gives vertical bars.
    rows = [{"月": f"{i}", "金额": 100 - i} for i in range(100)]
    cols = _cols(("月", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t")
    assert spec.kind == "bar"
    assert len(spec.rows or []) == _MAX_BAR_ROWS
    assert spec.top_n == _MAX_BAR_ROWS


def test_four_same_family_series_are_drawable_not_tabled():
    """★ 4 个**同族**指标本来就画得出——旧判据只数列数，把这类整张退成表。

    116 真机：`2025年月度核心指标趋势` 12 行 × 4 个全是金额的指标，
    本该是一张四线图（chart-config-ux 批4 ⑨）。
    """
    rows = [{"month": f"M{i}", "a": 1 + i, "b": 2 + i, "c": 3 + i, "d": 4 + i} for i in range(5)]
    cols = _cols(("month", "categorical"), ("a", "numeric"), ("b", "numeric"), ("c", "numeric"), ("d", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind != "table", spec._infer_reason


def test_three_dimensional_families_still_table():
    """量纲族 > 2 仍退表——与规则 10 的双轴判据同一条口径。"""
    rows = [{"门店": f"S{i}", "营业额": 1e9 + i, "客流量": 1e6 + i, "利润率": 0.1 + i / 100} for i in range(5)]
    cols = _cols(("门店", "categorical"), ("营业额", "numeric"), ("客流量", "numeric"), ("利润率", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind == "table"
    assert "量纲族" in (spec._infer_reason or "")


def test_same_family_series_beyond_palette_still_draws():
    """★★ 同一量纲族（一个指标的 N 个切片）超过 6 条**不再退表**。

    116 dogfood 1.2.28：`Top10 品类月度趋势` 撞上「series=10 > 调色板 6 槽 → table」，
    而调色板那个约束在同一个构建里已被前端 `seriesColor()` 拿掉了。更糟的是模型
    **读了这条 reason 并照做**，转头用 matplotlib 画 PNG 交差。
    """
    from bowhead_core.chart.infer import _MAX_SERIES

    n = _MAX_SERIES + 1
    rows = [{"品类": f"C{i}", **{f"m{j}": (i + 1) * (j + 1) for j in range(n)}} for i in range(5)]
    cols = _cols(("品类", "categorical"), *[(f"m{j}", "numeric") for j in range(n)])
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind != "table", spec._infer_reason


def test_mixed_family_series_beyond_palette_still_falls_back_to_table():
    """★ 反向钉死：**异族**多序列照旧退表——那是「N 个不同指标并排」，
    挤一根轴上小的会被压成贴零直线。语料里 >10 列的全是这种。
    """
    from bowhead_core.chart.infer import _MAX_SERIES

    n = _MAX_SERIES + 1
    # Half use 元, half % ⇒ two unit families.
    rows = [
        {"省": f"P{i}", **{f"额{j}": (i + 1) * 1e6 for j in range(n // 2)}, **{f"率{j}": 0.1 * (i + 1) for j in range(n - n // 2)}} for i in range(5)
    ]
    cols = _cols(
        ("省", "geo"),
        *[(f"额{j}", "numeric") for j in range(n // 2)],
        *[(f"率{j}", "numeric") for j in range(n - n // 2)],
    )
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind == "table"


def test_series_cap_reason_never_cites_the_palette():
    """★★ reason 是**给模型看的**，不许再提调色板。

    1.2.28 实测：模型把这句话原样转述给用户（「因10条线超出原生图表6色调色板容量」）
    并据此绕开 add_chart。闸可以拦，但要说真正的理由。
    """
    from bowhead_core.chart.infer import _MAX_SERIES_SAME_FAMILY

    n = _MAX_SERIES_SAME_FAMILY + 1
    rows = [{"品类": f"C{i}", **{f"m{j}": (i + 1) * (j + 1) for j in range(n)}} for i in range(5)]
    cols = _cols(("品类", "categorical"), *[(f"m{j}", "numeric") for j in range(n)])
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind == "table"
    assert "调色板" not in (spec._infer_reason or ""), spec._infer_reason
    assert "读不动" in (spec._infer_reason or ""), spec._infer_reason


def test_dual_axis_mag_diff():
    """同 cat 下两 numeric 列量级差 >= 10 → dual_axis."""
    rows = [{"月": f"{i}月", "销售额": i * 1000000, "订单数": i * 5} for i in range(1, 7)]
    # 月 is categorical, not date; use dual-axis classification.
    cols = _cols(("月", "categorical"), ("销售额", "numeric"), ("订单数", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind == "dual_axis"


def test_stacked_bar_parent_child():
    """省 → 市 是严格父子（市只属一个省）→ stacked_bar."""
    rows = [
        {"省": "河北", "市": "石家庄", "GMV": 100},
        {"省": "河北", "市": "保定", "GMV": 80},
        {"省": "山东", "市": "济南", "GMV": 120},
        {"省": "山东", "市": "青岛", "GMV": 110},
    ]
    cols = _cols(("省", "categorical"), ("市", "categorical"), ("GMV", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="composition", title="t")
    assert spec.kind == "stacked_bar"


def test_scatter_correlation_hint():
    rows = [{"x": i, "y": i * 2 + 3, "品牌": "A"} for i in range(15)]
    cols = _cols(("品牌", "categorical"), ("x", "numeric"), ("y", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="correlation", title="t")
    assert spec.kind == "scatter"


def test_force_kind_override():
    rows = [{"月": f"{i}月", "金额": i * 100} for i in range(1, 4)]
    cols = _cols(("月", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t", force_kind="pie")
    assert spec.kind == "pie"


def test_force_kind_invalid_falls_back_to_table():
    rows = [{"月": "1月", "金额": 100}]
    cols = _cols(("月", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t", force_kind="treemap")
    assert spec.kind == "table"


def test_invalid_hint_normalized_to_raw():
    rows = [{"x": "a", "y": 1}]
    cols = _cols(("x", "categorical"), ("y", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="GARBAGE", title="t")
    # Do not raise; continue kind inference as raw (one row + one category + one numeric column here).
    assert spec.kind in ("bar", "hbar", "table")


def test_chart_carries_every_row_it_draws():
    """Every plotted row must be included in the chart spec.

    Synthetic multi-series trends exceed the table preview limit. Chart rendering
    and exports must retain them; table pagination has its own separate contract.
    """
    months = [f"2025年{m}月" for m in range(9, 13)] + [f"2026年{m}月" for m in range(1, 9)]
    brands = [f"Brand {i}" for i in range(6)]
    rows = [{"每月": m, "中文品牌": b, "销售金额": 1e7 + i} for i, (m, b) in enumerate((m, b) for m in months for b in brands)]
    cols = _cols(("每月", "date"), ("中文品牌", "categorical"), ("销售金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="trend", title="t", data_artifact_id="art_abc", inline_rows_limit=50)
    assert spec.kind == "line"
    assert len(spec.rows or []) == 72, f"72 行只带了 {len(spec.rows or [])} 行"
    assert {r["每月"] for r in spec.rows or []} == set(months), "最后几个月不能丢"
    assert spec.top_n is None, "全画出来了就不该说「仅显示前 N」"
    assert spec.row_count == 72
    assert spec.data_artifact_id == "art_abc"


def _drawn_or_disclosed(spec) -> bool:
    """非表格的图：要么行数完整，要么打了 `top_n` 且它等于真实带上的行数。"""
    have = len(spec.rows or [])
    return have == spec.row_count or spec.top_n == have


def test_no_chart_kind_is_silently_partial():
    """★ 不变量：表格以外的图，`rows` 少于 `row_count` 时必须打 `top_n`，且 `top_n` 说的是真话。

    每个形状都是 2026-09-19 用真 `infer_chart_spec` 跑出来的静默截断（旧码 `top_n=None` 或
    `top_n` 与实际带上的行数不符）。改 `_CHART_ROWS_MAX` 或退回按 `inline_rows_limit`
    切图的行，这里就红。
    """
    months = [f"2025年{m}月" for m in range(1, 13)]
    long_cols = _cols(("每月", "date"), ("品牌", "categorical"), ("销售金额", "numeric"))

    def long_rows(brands: int, *, brand_major: bool) -> list[dict]:
        pairs = [(m, b) for b in range(brands) for m in months] if brand_major else [(m, b) for m in months for b in range(brands)]
        return [{"每月": m, "品牌": f"品牌{b}", "销售金额": 1000.0 + i} for i, (m, b) in enumerate(pairs)]

    cases = {
        # Brand-major ordering: old code drew all 12 months but retained only 5/10 brands, with just five legend entries.
        "品牌优先 10x12": infer_chart_spec(long_rows(10, brand_major=True), long_cols, hint="trend", title="t"),
        "模型强制柱状": infer_chart_spec(long_rows(6, brand_major=False), long_cols, hint="trend", title="t", force_kind="bar"),
        "模型强制堆叠柱": infer_chart_spec(long_rows(6, brand_major=False), long_cols, hint="composition", title="t", force_kind="stacked_bar"),
        "不给意图": infer_chart_spec(long_rows(6, brand_major=False), long_cols, hint="raw", title="t"),
        # Unit-family gate promotes a line to dual axis (≤60 rows).
        "双轴 55 行": infer_chart_spec(
            [{"日期": f"2025年{(i % 12) + 1}月", "营业额": 1e8 + i, "退款率": 0.01 + i / 1e4} for i in range(55)],
            _cols(("日期", "date"), ("营业额", "numeric"), ("退款率", "numeric")),
            hint="trend",
            title="t",
        ),
        # Sankey display cap is 60: 55 rows silently drew 50; 70 rows claimed top 60 but drew 50.
        "桑基 55 行": infer_chart_spec(
            [{"来源": f"S{i % 8}", "去向": f"T{i}", "金额": 100.0 + i} for i in range(55)],
            _cols(("来源", "categorical"), ("去向", "categorical"), ("金额", "numeric")),
            hint="flow",
            title="t",
        ),
        "桑基 70 行": infer_chart_spec(
            [{"来源": f"S{i % 8}", "去向": f"T{i}", "金额": 100.0 + i} for i in range(70)],
            _cols(("来源", "categorical"), ("去向", "categorical"), ("金额", "numeric")),
            hint="flow",
            title="t",
        ),
    }
    bad = {
        name: f"kind={s.kind} rows={len(s.rows or [])} row_count={s.row_count} top_n={s.top_n}"
        for name, s in cases.items()
        if s.kind != "table" and not _drawn_or_disclosed(s)
    }
    assert not bad, bad
    assert all(s.kind != "table" for s in cases.values()), "用例要测的是图，形状退成表格就没测到"
    assert len(cases["品牌优先 10x12"].rows or []) == 120
    assert cases["桑基 70 行"].top_n == len(cases["桑基 70 行"].rows or []) == 60


def test_a_capped_chart_always_discloses_it():
    """★ 不变量：**两道闸哪道先到都一样**——图带不上全部行时必须打 `top_n`，截了不说才是 bug。

    行数上限（`_CHART_ROWS_MAX`，用户拍板放到几万）实际很少先到：窄表约 1.2 万行就撞到
    1MB 体积闸。所以这条按体积闸触发，断言的是「截断必披露」这条共同契约。
    """
    from bowhead_core.chart import infer as infer_mod

    rows = [{"门店": f"某某某某某某门店{i:05d}", "面积": float(i), "金额": float(i * 3)} for i in range(infer_mod._CHART_ROWS_MAX)]
    cols = _cols(("门店", "categorical"), ("面积", "numeric"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="correlation", title="t")
    assert spec.kind == "scatter"
    kept = len(spec.rows or [])
    assert 0 < kept < infer_mod._CHART_ROWS_MAX
    assert spec.top_n == kept, "截了必须说"
    assert spec.row_count == infer_mod._CHART_ROWS_MAX, "总数照实说"


def test_chart_rows_stay_within_the_payload_budget():
    """★ 行数上限管不住宽表/长文本：一张图的数据超 1MB 就会顶破导出的单图 4MB 与整份 10MB。

    116 实测（review）：13 列 × 1 万行的折线 5.34MB、带门店全名的 1 万点散点 1.92MB。
    超出时照样截，但必须打 `top_n`——截了不说才是这一串修复要消灭的那件事。
    """
    import json as _json

    from bowhead_core.chart import infer as infer_mod

    # ⚠ Derive row count from the current budget, not a constant: raising 1MB to 2MB made the unchanged fixture stop exceeding it
    # (green tests with no test target). Compute bytes per row first, then choose rows at exactly 1.5 times the budget.
    def _row(i: int) -> dict:
        return {
            "门店名称": f"华东大区上海市浦东新区第{i}号直营旗舰店",
            "门店编码": f"SH-PD-{i:05d}",
            "客流": 1000 + i * 0.37,
            "销售额": 123456.78 + i * 1.9,
        }

    per_row = len(_json.dumps([_row(0)], ensure_ascii=False, separators=(",", ":")).encode())
    n_rows = int(infer_mod._CHART_BYTES_MAX * 1.5 / per_row)
    rows = [_row(i) for i in range(n_rows)]
    assert infer_mod._rows_bytes(rows) > infer_mod._CHART_BYTES_MAX, "夹具本身必须真的超预算（正对照）"
    cols = _cols(("门店名称", "categorical"), ("门店编码", "categorical"), ("客流", "numeric"), ("销售额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="correlation", title="t")
    assert spec.kind == "scatter"
    size = len(_json.dumps(spec.rows, ensure_ascii=False, separators=(",", ":"), default=str).encode())
    assert size <= infer_mod._CHART_BYTES_MAX, f"图的数据 {size}B 超过预算"
    assert len(spec.rows or []) < n_rows
    # ★ Also retain as much as possible: one row satisfies the three assertions above but destroys the chart (mutation caught in rereview).
    assert size > infer_mod._CHART_BYTES_MAX * 0.8, f"预算没用满（只留了 {len(spec.rows or [])} 行 / {size}B）"
    assert spec.top_n == len(spec.rows or [])
    assert spec.row_count == n_rows

    # Positive control: ordinary trend charts fitting the x axis (1000 points, ~40KB) must remain untouched by the byte gate.
    # Do not use 10000 time-series rows: the >1000 rendered-x-points gate first falls back to a table.
    narrow = [{"日期": f"D{i:05d}", "金额": float(i)} for i in range(1_000)]
    kept = infer_chart_spec(narrow, _cols(("日期", "date"), ("金额", "numeric")), hint="trend", title="t")
    assert kept.kind == "line"
    assert len(kept.rows or []) == 1_000, "横轴画得下的走势图不该被体积闸动到"
    assert kept.top_n is None


def test_a_single_row_over_budget_falls_back_to_table():
    """单独一行就超预算（某个单元格塞了整篇文本）⇒ 退表。

    复审逮到的旧行为：收缩循环停在 1 行却不校验是否达标，于是一张 1 万点的图被削成
    **1 个点**还报「仅显示前 1 项」；行数恰好为 1 时更是既超预算又一声不吭。
    """
    # Use a time axis: category-axis value-based top-N can remove this row before reaching the byte gate.
    fat = "词" * 700_000  # One row alone exceeds 1MB.
    rows = [{"日期": f"2025-{i:05d}", "备注": fat if i == 0 else "短", "金额": float(i)} for i in range(200)]
    spec = infer_chart_spec(rows, _cols(("日期", "date"), ("备注", "categorical"), ("金额", "numeric")), hint="trend", title="t")
    assert spec.kind == "table", f"应退表，实际 {spec.kind}"
    assert "单行" in (spec._infer_reason or "")
    assert spec.row_count == len(rows), "总数照实说"
    assert spec.top_n is None, "表格由页脚说明，不再打截断标记"


def test_byte_budget_uses_one_pass_and_stays_fast(monkeypatch):
    """体积闸在事件循环上同步跑：全量行不许反复序列化（复审实测最坏 177ms）。"""

    # Use scatter: category axes cap at 30 rows, and oversized time axes fall back to tables; neither reaches the byte gate itself
    # (this test measures that gate's binary shrinking and overhead).
    from bowhead_core.chart import infer as infer_mod

    # Derive row count from the current budget (same rationale as test_chart_rows_stay_within_the_payload_budget).
    sample = {"名称": "门店0的很长名称" * 6, "面积": 0.0, "金额": 0.0}
    n_rows = int(infer_mod._CHART_BYTES_MAX * 1.5 / infer_mod._rows_bytes([sample]))
    rows = [{"名称": f"门店{i}的很长名称" * 6, "面积": float(i), "金额": float(i * 3)} for i in range(n_rows)]
    assert infer_mod._rows_bytes(rows) > infer_mod._CHART_BYTES_MAX, "夹具本身必须真的超预算（正对照）"
    cols = _cols(("名称", "categorical"), ("面积", "numeric"), ("金额", "numeric"))

    # ⚠ Measure total serialized rows, not milliseconds. Two attempted versions both failed:
    #   · Absolute milliseconds distort as fixtures grow (1MB→2MB budget doubled this fixture's bytes),
    #     and concurrent-test load causes failures (passes alone, fails in the full suite).
    #   · Counting equivalent full serializations is worse: one pass takes only tens of milliseconds with 6x–25x jitter;
    #     using it as the denominator introduces noise into the criterion.
    # Row counts are deterministic: O(log n) binary-shrink calls serialize only candidate slices; total should be a constant multiple.
    serialized_rows = []
    real_rows_bytes = infer_mod._rows_bytes

    def counting_rows_bytes(rs):
        serialized_rows.append(len(rs))
        return real_rows_bytes(rs)

    monkeypatch.setattr(infer_mod, "_rows_bytes", counting_rows_bytes)
    spec = infer_chart_spec(rows, cols, hint="correlation", title="t")
    kept = len(spec.rows or [])
    assert 0 < kept < n_rows and spec.top_n == kept
    assert kept > 1_000, f"只留了 {kept} 行——收缩不该退化成「留一行」"
    # Binary shrinking takes O(log n) calls (17341 rows measured 16 calls, ~9.3 times the rows: expected binary-search cost).
    # Prevent O(n) degeneration: add rows individually and reserialize the entire accumulated set after each.
    import math as _math

    budget = int(_math.log2(max(n_rows, 2))) + 6
    assert len(serialized_rows) <= budget, f"序列化调用了 {len(serialized_rows)} 次（全量 {n_rows} 行，二分该在 {budget} 次以内）——收缩退化成逐行扫了"
    assert max(serialized_rows) <= n_rows, "没有哪一次该序列化超过全量"


def test_heatmap_with_too_many_y_categories_falls_back_to_table():
    """热力图格高 = 绘图区高 / 纵轴类目数 − 2：类目一多每格只剩 1px，整张图空白。

    自动路碰不到（没有时间轴的图先被压到 30 行），这道闸只拦 `force_kind` 带日期列那条。
    """
    rows = [{"日期": f"2025-{m:02d}", "门店": f"S{k}", "金额": float(m + k)} for m in range(1, 13) for k in range(100)]
    cols = _cols(("日期", "date"), ("门店", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="heatmap")
    assert spec.kind == "table"
    assert "纵轴" in (spec._infer_reason or "")

    small = [{"日期": f"2025-{m:02d}", "门店": f"S{k}", "金额": float(m + k)} for m in range(1, 13) for k in range(20)]
    ok = infer_chart_spec(small, cols, hint="raw", title="t", force_kind="heatmap")
    assert ok.kind == "heatmap", "20 个门店照画"


def test_line_length_is_measured_in_time_points_not_rows():
    """★ 折线的上限按**时间点数**算：长表的行数 = 时间点 × 系列，按行数判会把正经走势图退成表格。

    116 实测：带时间轴却落成表格的 46 张里有 4 张栽在旧的「>200 行」上——
    《各省月度刷卡金额走势》708 行（十几个月 × 50 个省）、《每日营业额趋势》365 行（一年每天）。
    """
    # ① 12 brands × 24 months = 288 rows, but only 24 time points.
    #    ⚠ Deliberately keep brands within _MAX_TIME_SERIES: the series gate is a separate criterion
    #    (test_too_many_time_series_are_capped_not_stacked); mixing it here makes this test
    #    fail for either broken rule without identifying which failed.
    months = [f"2025年{m}月" for m in range(1, 25)]
    long_rows = [{"每月": m, "品牌": f"品牌{b}", "销售额": 1000.0 + b} for m in months for b in range(12)]
    long_cols = _cols(("每月", "date"), ("品牌", "categorical"), ("销售额", "numeric"))
    spec = infer_chart_spec(long_rows, long_cols, hint="trend", title="各品牌月度销售额")
    assert spec.kind == "line", f"288 行但只有 24 个时间点，不该退表（实际 {spec.kind}）"
    assert len(spec.rows or []) == 288
    assert spec.series_shown is None, "12 条系列没超上限，不该打截断标记"
    # ★ Guard both criteria independently: the intent layer (hint=trend) and shape-tree rule 4 back each other up;
    # asserting only kind misses a broken rule on either side (mutation observed in rereview).
    assert "hint=trend" in (spec._infer_reason or ""), "这一张该由意图层判出来"

    shape_tree = infer_chart_spec(long_rows, long_cols, hint="raw", title="各品牌月度销售额")
    assert shape_tree.kind == "line"
    assert "time detected" in (shape_tree._infer_reason or ""), "不给意图时该由形状树规则 4 判出来"

    # ② Every day of one year × three metrics (365-row wide table)
    days = [f"2025-{d // 31 + 1:02d}-{d % 31 + 1:02d}" for d in range(365)]
    daily = [{"日期": d, "营业额": 1000.0 + i, "客流": 50.0 + i} for i, d in enumerate(days)]
    spec2 = infer_chart_spec(daily, _cols(("日期", "date"), ("营业额", "numeric"), ("客流", "numeric")), hint="trend", title="每日营业额")
    assert spec2.kind in ("line", "dual_axis"), f"一年日度不该退表（实际 {spec2.kind}）"
    assert len(spec2.rows or []) == 365

    # ③ Too many time points ⇒ downsample rather than fall back to a table (since batch 2); other products do not replace oversized lines with tables.
    many = [{"日期": f"D{i:05d}", "值": float(i)} for i in range(_MAX_LINE_POINTS + 1)]
    spec3 = infer_chart_spec(many, _cols(("日期", "date"), ("值", "numeric")), hint="trend", title="超长日度")
    assert spec3.kind == "line", f"超上限该降采样而不是退表（实际 {spec3.kind}）"
    assert spec3.points_shown == _MAX_LINE_POINTS and spec3.points_total == _MAX_LINE_POINTS + 1
    assert spec3.rows[0]["日期"] == "D00000" and spec3.rows[-1]["日期"] == f"D{_MAX_LINE_POINTS:05d}", "首尾必保"

    # ④ Negative control: bar families never downsample or raise their cap; dropping some bars makes widths/spacing misleading.
    #    ⚠ Use 1001–3000 here: testing only 3001 still passes the regression where bars share the line cap of 3000
    #    (caught in rereview: 2500 bars on 820px yield 0.33px each; previously fell back to a table).
    from bowhead_core.chart.infer import _MAX_BAR_SLOTS

    assert _MAX_BAR_SLOTS < _MAX_LINE_POINTS, "两档必须真的不同，否则下面是空测"
    for kind in ("bar", "stacked_bar", "waterfall"):
        for n in (_MAX_BAR_SLOTS + 200, 2500):
            bars = [{"日期": f"D{i:05d}", "值": float(i)} for i in range(n)]
            got = infer_chart_spec(bars, _cols(("日期", "date"), ("值", "numeric")), hint="raw", title="t", force_kind=kind)
            assert got.kind == "table", f"force {kind} / {n} 根柱该退表（实际 {got.kind}）"
            assert got.points_shown is None, f"force {kind}：柱状族不降采样"
    # Positive control: render bars exactly within their cap normally.
    ok_bars = [{"日期": f"D{i:05d}", "值": float(i)} for i in range(_MAX_BAR_SLOTS)]
    assert infer_chart_spec(ok_bars, _cols(("日期", "date"), ("值", "numeric")), hint="raw", title="t", force_kind="bar").kind == "bar"


def test_a_column_with_one_single_date_is_not_a_time_axis():
    """★ 整列同一天不是走势：那是一条筛选痕迹，画成折线等于把 500 行摊在一个时刻上。

    `_constant_dim_keys` 看不见这一形——它要求至少两个维度列才判得出常量
    （这里只有 date 一个维度）。所以判时间轴时直接问「这列有几个不同取值」。
    """
    rows = [{"日期": "2025-01-01", "额": float(i)} for i in range(500)]
    cols = _cols(("日期", "date"), ("额", "numeric"))
    for hint in ("trend", "raw"):
        spec = infer_chart_spec(rows, cols, hint=hint, title="t")
        assert spec.kind != "line", f"hint={hint}：只有一个时刻，不该画成折线（实际 {spec.kind}）"
        assert spec.kind != "area_stacked"

    # Positive control: two time points already form a trend.
    two = [{"日期": f"2025-01-0{1 + i % 2}", "额": float(i)} for i in range(500)]
    assert infer_chart_spec(two, cols, hint="trend", title="t").kind == "line"


def test_a_time_series_that_does_not_fit_is_downsampled_never_cut_short():
    """Downsample a large time series without shortening its time span.

    Keep its first and last timestamps instead of selecting ranking-style top-N rows.
    """
    # ⚠ Choose a shape the series gate cannot rescue; otherwise this tests a different rule:
    # keep stores within _MAX_TIME_SERIES (series capping cannot help), exceeding the byte budget with very long store names.
    long_name = "S{}·" + "华东大区旗舰店" * 20
    rows = [{"日期": f"D{i:05d}", "门店": long_name.format(b), "金额": 1000.0 + i + b} for i in range(900) for b in range(5)]
    cols = _cols(("日期", "date"), ("门店", "categorical"), ("金额", "numeric"))
    from bowhead_core.chart import infer as infer_mod

    assert infer_mod._rows_bytes(rows) > infer_mod._CHART_BYTES_MAX, "夹具本身必须真的超预算（正对照）"

    spec = infer_chart_spec(rows, cols, hint="trend", title="各店日度金额")
    assert spec.kind == "line", f"装不下该降采样而不是退表，实际 {spec.kind}"
    assert spec.row_count == 4_500, "总数照实说"
    assert spec.series_shown is None, "5 条系列没超上限，不该冒出系列截断标记"
    assert spec.points_total == 900 and 0 < (spec.points_shown or 0) < 900
    # ★ Preserve the entire time interval: neither first nor last time point may disappear.
    days = [r["日期"] for r in spec.rows or []]
    assert days[0] == "D00000" and days[-1] == "D00899", f"时间段被截了：{days[0]}…{days[-1]}"
    assert infer_mod._rows_bytes(spec.rows or []) <= infer_mod._CHART_BYTES_MAX, "降完必须真的装得下"

    # Positive control: render all fitting data (365 days × five stores = 1825 short-name rows, ~90KB).
    fits = [{"日期": f"D{i:05d}", "门店": f"S{b}", "金额": 1000.0 + i + b} for i in range(365) for b in range(5)]
    ok = infer_chart_spec(fits, cols, hint="trend", title="各店日度金额")
    assert ok.kind == "line"
    assert len(ok.rows or []) == 1_825 and ok.top_n is None, "装得下就一行不少"


def test_dual_axis_assigned_on_every_drawn_row():
    """双轴的轴分配要按**画出来的全部行**算，不能按内联的头 50 行。

    review 复现：150 行日度数据，客流量前 50 行很小、第 51 行起暴涨（新渠道上线）。
    按头 50 行派轴 ⇒ 客流量被派到右轴、跟转化率挤一根轴，转化率压成贴零线。
    """
    cols = _cols(("日期", "date"), ("营业额", "numeric"), ("客流量", "numeric"), ("转化率", "numeric"))
    rows = [{"日期": f"D{i:03d}", "营业额": 1e6, "客流量": (50.0 if i < 50 else 5e6), "转化率": 0.05} for i in range(150)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t", force_kind="dual_axis")
    assert spec.kind == "dual_axis"
    assert len(spec.rows or []) == 150
    axes = {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}
    assert axes["转化率"] == 1
    assert axes["客流量"] == 0, f"客流量按全量应和营业额同在左轴，实际 {axes}"


def test_scatter_draws_every_point():
    """散点没有「柱子读得动几根」这个问题，也就没有理由只画头 50 个点。"""
    rows = [{"门店": f"S{i}", "面积": float(i), "营业额": float(i * 3)} for i in range(100)]
    cols = _cols(("门店", "categorical"), ("面积", "numeric"), ("营业额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="correlation", title="t")
    assert spec.kind == "scatter"
    assert len(spec.rows or []) == 100
    assert spec.top_n is None


def test_too_many_time_series_are_capped_not_stacked():
    """★ 时间轴上系列太多 ⇒ 按合计取前 `_MAX_TIME_SERIES` 条并明示，不画 59 层堆叠面积。

    这道闸此前**根本不存在**：旧判据「时间序列 >200 行退表」一直在兼职当它
    （708 行 = 12 月 × 59 省，按行数就被拦住了）。`4bd93dfc9` 换成按时间点数判之后兼职消失，
    116 上 4 张真实的图从「表格」变成 36–59 层堆叠面积图——820×360 的容器里图例排 6 行、
    吃掉约 200px，剩下的 59 条带子彼此无法区分，比退表更差。这里用的就是那张真图的形状。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("商户所在省", "geo"), ("刷卡金额", "numeric"))
    # Larger province IDs mean larger amounts; the 15 largest totals are 44..58.
    rows = [{"每月": m, "商户所在省": f"省{p:02d}", "刷卡金额": 1000.0 + p} for m in months for p in range(59)]

    spec = infer_chart_spec(rows, cols, hint="trend", title="2025年各省月度刷卡金额走势")
    assert spec.kind == "line", f"截断后不能再堆叠（高度不再是真合计），实际 {spec.kind}"
    assert spec.series_shown == 15 and spec.series_total == 59
    assert spec.row_count == 708, "总数照实说"
    assert spec.top_n is None, "系列截断不能打 `top_n`：下面那条「时间序列被截了就退表」正是按它判的，打了这张图会立刻掉回表格"

    # ① Retain the 15 largest by total, not the first 15 or last 15.
    kept = {r["商户所在省"] for r in spec.rows or []}
    assert kept == {f"省{p:02d}" for p in range(44, 59)}, f"该留合计最大的 15 个，实际 {sorted(kept)}"

    # ② Preserve every month on the time axis; cap series, not time.
    assert {r["每月"] for r in spec.rows or []} == set(months)
    assert len(spec.rows or []) == 15 * 12

    # ③ Actually render 15 series (use export-freezing normalization rather than counting independently).
    _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    assert shape.get("seriesCount") == 15
    assert shape.get("categoryCount") == 12

    # ④ ★ Change the same shape to a categorical dimension; only this path first selects area_stacked,
    #    then must revert to line after truncation: stacked height means total, which is false without 44 provinces.
    #    ⚠ The fixture above uses geo and never reaches area_stacked (rule 4's
    #    if cat_cols excludes geo). Testing only geo lets a still-stacked-after-truncation mutation survive (observed).
    cat_cols = _cols(("每月", "date"), ("品类", "categorical"), ("销售额", "numeric"))
    cat_rows = [{"每月": m, "品类": f"品类{p:02d}", "销售额": 1000.0 + p} for m in months for p in range(36)]
    cat_spec = infer_chart_spec(cat_rows, cat_cols, hint="trend", title="各品类月度销售额")
    assert "area_stacked" in (cat_spec._infer_reason or ""), "这一路该先判成堆叠面积"
    assert cat_spec.kind == "line", f"截断后不能再堆叠，实际 {cat_spec.kind}"
    assert "不再是真合计" in (cat_spec._infer_reason or ""), "换图种的理由要写下来"
    assert cat_spec.series_shown == 15 and cat_spec.series_total == 36


def test_capped_series_is_one_predicate_for_all_consumers():
    """★ 「系列截了吗」只能有一条判据 —— 三处各写各的会让同一份 spec 三种说法。

    复审实测：飞书那份不要求 `shown > 0`，`series_shown=0` 印出「按合计显示前 **0** 条」、
    `-1` 印出「前 **-1** 条」，而同一输入网页一句不出。
    """
    from bowhead_core.chart.types import capped_series

    assert capped_series({"series_shown": 15, "series_total": 59}) == (15, 59)
    for bad in (
        {},
        {"series_shown": 15},
        {"series_total": 59},
        {"series_shown": 15, "series_total": 15},  # Not truncated
        {"series_shown": 15, "series_total": 3},  # Total smaller than shown
        {"series_shown": 0, "series_total": 5},
        {"series_shown": -1, "series_total": 5},
        {"series_shown": True, "series_total": 5},  # bool is a subclass of int.
        {"series_shown": "15", "series_total": 59},
        {"series_shown": 15.0, "series_total": 59},
    ):
        assert capped_series(bad) is None, f"{bad} 不该被当成「截过」"

    # dict and ChartSpec inputs must agree (Feishu receives the serialized dict).
    spec = infer_chart_spec(
        [{"每月": f"2025-{m:02d}", "品牌": f"B{b:02d}", "额": 1.0 + b} for m in range(1, 13) for b in range(20)],
        _cols(("每月", "date"), ("品牌", "categorical"), ("额", "numeric")),
        hint="trend",
        title="t",
    )
    assert capped_series(spec) == (15, 20)
    assert capped_series(spec_to_dict(spec)) == (15, 20)


def test_lttb_keeps_the_spikes_that_naive_sampling_loses():
    """★★ 降采样必须保住尖峰：朴素每 N 取 1 会把峰值砍掉一半，图上看不出出过事。

    实测（3500 点埋 3 个业务尖峰，降到 1000 点）：
        朴素每 N 取 1  尖峰 0/3，画出的最大值 −50.3%
        LTTB           尖峰 3/3，画出的最大值 ±0.0%
    这条用例把「峰值逐字相等」钉在**画出来的值**上，不是钉在「调用了降采样」上——
    把 LTTB 换成朴素抽样时它必须红。
    """
    import math as _math

    from bowhead_core.chart.infer import _lttb_indices

    n = 3500
    values = [1000 + i * 0.3 + 180 * _math.sin(i / 60) for i in range(n)]
    spikes = (1111, 2200, 3010)
    for idx, mult in zip(spikes, (3.2, 2.4, 0.15)):
        values[idx] *= mult

    picked = _lttb_indices(values, 1000)
    assert len(picked) == 1000
    assert picked[0] == 0 and picked[-1] == n - 1, "首尾必保"
    assert picked == sorted(picked), "挑出来的下标必须保持时间顺序"
    assert set(spikes) <= set(picked), f"尖峰被丢了：{sorted(set(spikes) - set(picked))}"
    assert max(values[i] for i in picked) == max(values), "画出来的最大值必须就是真实最大值"
    assert min(values[i] for i in picked) == min(values), "最小值同理（单日塌陷也是事实）"

    # Negative control: naive one-in-N sampling preserves none of the spikes in this same data.
    naive = list(range(0, n, n // 1000))[:1000]
    assert not set(spikes) & set(naive), "这份夹具本身要能区分两种算法（正对照）"


def test_lttb_is_reproducible_on_flat_data():
    """★ 打平时固定取下标小的那个 —— 同一份数据任何时候都挑出同一批点。

    （两种取法都确定性，这条钉的是**跨版本可复现**，不是防迭代序抖动。）
    """
    from bowhead_core.chart.infer import _lttb_indices

    flat = [42.0] * 500
    first = _lttb_indices(flat, 50)
    assert first == _lttb_indices(list(flat), 50)
    assert first[0] == 0 and first[-1] == 499
    # Equal values choose each bucket's first index ⇒ strictly increasing, unique indices.
    assert len(set(first)) == len(first) == 50


def test_the_byte_budget_stays_at_one_megabyte_and_downsampling_carries_the_load():
    """★★ 体积预算**不放宽**：靠降采样装下长序列，不靠抬上限。

    本批一度把它提到 2MB。复审量出代价：整份快照 10MB 那道闸是无条件抛
    `snapshot_too_large`、**没有逐张降级**（只有冻结样式 64KB 那条有），
    ⇒ 一份报告能放的顶格大图从约 10 张掉到约 5 张，超了是整份导出失败。
    而实测 1MB 下同一张 3500 点 × 6 系列照样画得出来（采到 2363 个时刻），视觉上分不出来。
    """
    from bowhead_core.chart import infer as infer_mod

    assert infer_mod._CHART_BYTES_MAX == 1_000_000, "别在没有逐张降级之前再抬它"

    cols = _cols(("日期", "date"), ("品牌", "categorical"), ("销售额", "numeric"))
    rows = [
        {"日期": f"2020-{1 + i // 300:02d}-{1 + i % 28:02d}#{i:04d}", "品牌": f"品牌{b}", "销售额": 1000.0 + i + b}
        for i in range(3500)
        for b in range(6)
    ]
    assert infer_mod._rows_bytes(rows) > infer_mod._CHART_BYTES_MAX, "夹具要真的超预算（正对照）"

    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "line", f"该靠降采样画出来（实际 {spec.kind}）"
    assert 0 < (spec.points_shown or 0) < 3500 and spec.points_total == 3500
    assert infer_mod._rows_bytes(spec.rows or []) <= infer_mod._CHART_BYTES_MAX


def test_downsampling_sorts_time_the_way_the_frontend_draws_it():
    """★★ 挑点必须沿着**画出来的顺序**：不补零的日期按字面排会把 1-10 排到 1-2 前面。

    北极九章的日期桶可能不补零（`2025-1-5`）。按字面序挑点等于沿着一条锯齿状的假曲线看形状，
    挑出来的点是错的，而且首尾也会挑错（字面序的「最后一个」不是时间上的最后一天）。
    """
    import datetime as dt

    d0 = dt.date(2025, 1, 1)
    # No zero-padding: 2025-1-1 / 2025-1-10 / 2025-2-3 …
    days = [(d0 + dt.timedelta(days=i)) for i in range(3200)]
    labels = [f"{d.year}-{d.month}-{d.day}" for d in days]
    assert sorted(labels) != labels, "夹具必须真的「字面序 ≠ 时间序」（正对照）"

    assert sorted(labels)[-1] != labels[-1], "字面序的「最后一个」必须不是时间上的最后一天（正对照）"

    cols = _cols(("日期", "date"), ("额", "numeric"))
    values = [float(i) + 300 * math.sin(i / 37) for i in range(len(labels))]
    rows = [{"日期": lb, "额": values[i]} for i, lb in enumerate(labels)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.points_shown == _MAX_LINE_POINTS

    # Compare exactly with the chronological-selection reference; lexical ordering makes different buckets and selects different points.
    # (First/last assertions alone are weak: lexical order happened to select the chronologically last day too, leaving the test green.)
    from bowhead_core.chart.infer import _lttb_indices

    expected = {labels[i] for i in _lttb_indices(values, _MAX_LINE_POINTS)}
    assert {r["日期"] for r in spec.rows or []} == expected, "挑出来的点与「按时间序挑」的参考结果不一致"
    kept = [r["日期"] for r in spec.rows or []]
    assert kept[0] == labels[0] and kept[-1] == labels[-1], f"首尾挑错了：{kept[0]}…{kept[-1]}"


def test_downsampling_totals_ignore_sign_so_a_crash_is_not_cancelled_out():
    """★ 合计用 |值|：一条系列单日暴跌时，别被另一条的正值抵消掉、把那一刻挑没了。"""
    n = 3500
    cols = _cols(("日期", "date"), ("品牌", "categorical"), ("利润", "numeric"))
    rows = []
    for i in range(n):
        for b in ("A", "B"):
            v = 100.0
            # ⚠ Both series must cancel exactly to baseline (-49900 + 50100 = 200 = usual 100+100);
            #   otherwise signed summation yields 0, still an obvious anomaly, and cannot distinguish the algorithms
            #   (the first version summed ±50000 to 0 and the mutation survived).
            if b == "A" and i == 1777:
                v = -49_900.0
            elif b == "B" and i == 1777:
                v = 50_100.0
            rows.append({"日期": f"D{i:05d}", "品牌": b, "利润": v})
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert (spec.points_shown or 0) < n, "夹具必须真的触发降采样（正对照）"
    assert "D01777" in {r["日期"] for r in spec.rows or []}, "抵消掉的那一刻被丢了"


def test_downsampling_twice_keeps_the_original_point_total():
    """★ 先按点数降一次、再为挤进体积预算降一次时，`points_total` 必须还是**最初**那个数。

    第二次的 `slots` 已经是降过的了，覆盖上去会把「原本有几个时刻」说小。
    """
    from bowhead_core.chart import infer as infer_mod

    n = 4000
    long_name = "华东大区旗舰店" * 12
    cols = _cols(("日期", "date"), ("门店", "categorical"), ("金额", "numeric"))
    rows = [{"日期": f"D{i:05d}", "门店": f"{long_name}{b}", "金额": 1000.0 + i + b} for i in range(n) for b in range(3)]
    assert infer_mod._rows_bytes(rows) > infer_mod._CHART_BYTES_MAX

    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "line"
    assert spec.points_total == n, f"总数被第二次降采样改小成了 {spec.points_total}"
    assert (spec.points_shown or 0) < _MAX_LINE_POINTS, "为了挤进体积预算应当再降一档"
    assert infer_mod._rows_bytes(spec.rows or []) <= infer_mod._CHART_BYTES_MAX


def test_downsampling_skips_shapes_where_slots_are_not_time_points():
    """★★ 横轴上画出来的槽位 ≠ 时刻数时不降采样 —— 丢一多半数据还是画不出来。

    长表摊不开时（填充率不够 / 2 个数值列）**一行一个槽位**：3200 天 × 20 个品牌
    摊不开就是 64000 个槽位，把时刻降到 3000 之后仍然是 3000 × 20 = 60000 个槽位。
    那种形状本来就已经坏了（横轴上每个日期重复 20 遍），交给「画得出吗」那道闸退表即可。

    ⚠ 诚实标注：这条用例**证明不了那道守卫的必要性**——有没有它这张图都落成表格
    （复审突变实测两边一样绿，退表时 `_make_spec` 会把 `points_shown` 天然清掉）。
    它钉的是「这形状的最终结果是完整的表格、一个点都没白丢」，守卫省的是无用功。

    与系列闸同一条原则：**数的那个东西必须就是画出来的那个东西**。
    """
    n = 3200
    cols = _cols(("日期", "date"), ("品牌", "categorical"), ("销售额", "numeric"), ("客流", "numeric"))
    rows = [{"日期": f"D{i:05d}", "品牌": f"B{b:02d}", "销售额": 1000.0 + i, "客流": 50.0 + b} for i in range(n) for b in range(20)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "table", f"这种形状该退表（实际 {spec.kind}）"
    assert spec.points_shown is None, "既然救不了，就一个点都别丢"
    assert spec.row_count == n * 20, "总数照实说"


def test_the_byte_pass_refuses_to_sample_below_a_floor():
    """★ 为挤进体积预算而二次降采样有**下界**：再稀就不画了，按原路退表。

    没有下界时实测能降到 102/3125（复审：16 系列 × 400 字符店名那档），
    而三个消费方那时还在说「首尾时刻保留、整段时间没缺」——图上只剩百来个折点，
    小系列的尖峰早被合计序列盖掉了。表格是完整的，宁可给表。
    """
    from bowhead_core.chart import infer as infer_mod

    # ⚠ Stay within _CHART_ROWS_MAX: exceeding it first takes head slicing → truncated time series → table fallback,
    #    never reaching the byte gate; the lower-bound assertion becomes empty (as in the first 3200×16=51200-row version).
    long_name = "华东大区上海市浦东新区直营旗舰店" * 12
    cols = _cols(("日期", "date"), ("门店", "categorical"), ("金额", "numeric"))
    rows = [{"日期": f"D{i:05d}", "门店": f"{long_name}{b}", "金额": 1000.0 + i + b} for i in range(3200) for b in range(15)]
    assert len(rows) < infer_mod._CHART_ROWS_MAX, "夹具要走得到体积那一档（正对照）"
    assert infer_mod._rows_bytes(rows) > infer_mod._CHART_BYTES_MAX * 10, "夹具要远超预算（正对照）"

    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "table", (
        f"按比例只能降到约 {int(3200 * infer_mod._CHART_BYTES_MAX * 0.95 / infer_mod._rows_bytes(rows))} 个点，"
        f"低于下界 {infer_mod._MIN_DOWNSAMPLE_POINTS} ⇒ 应当老实退表（实际 {spec.kind}，points={spec.points_shown}）"
    )
    assert spec.points_shown is None, "退表就别留降采样标记"

    # Positive control: render the same shape with gentler downsampling; the lower bound must not block normal charts.
    mild = [{"日期": f"D{i:05d}", "门店": f"门店{b}", "金额": 1000.0 + i + b} for i in range(3200) for b in range(8)]
    assert infer_mod._rows_bytes(mild) > infer_mod._CHART_BYTES_MAX
    mild_spec = infer_chart_spec(mild, cols, hint="trend", title="t")
    assert mild_spec.kind == "line" and (mild_spec.points_shown or 0) >= infer_mod._MIN_DOWNSAMPLE_POINTS


def test_downsampling_skips_when_two_date_columns_could_be_the_axis():
    """★ 两个非常量 date 列时哪个是横轴由正规化定，在这里猜会猜错 ⇒ 不碰。"""
    n = 3300
    cols = _cols(("下单日", "date"), ("发货日", "date"), ("额", "numeric"))
    rows = [{"下单日": f"D{i:05d}", "发货日": f"S{i:05d}", "额": float(i)} for i in range(n)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.points_shown is None, "两个 date 列时不猜"


def test_downsampling_does_not_multiply_normalize_calls(monkeypatch):
    """★ 正规化在 gateway 事件循环上同步跑：一张图最多跑 3 次（系列闸 / 降采样 / 可画闸）。

    spec 在这三步之间会变，所以 3 次是下限；但**不能再涨**——没有这条闸的话，
    加一道新判据就顺手多一遍几万行的正规化，谁都不会注意到（实测 45015 行那档
    每遍约 70–150ms）。
    """
    from bowhead_core import chart_style_presentation as presentation

    calls = {"n": 0}
    real = presentation.normalize_chart_for_rendering_v1

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(presentation, "normalize_chart_for_rendering_v1", counting)
    cols = _cols(("日期", "date"), ("品牌", "categorical"), ("额", "numeric"))
    rows = [{"日期": f"D{i:06d}", "品牌": f"B{b:02d}", "额": float(i + b)} for i in range(3300) for b in range(15)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "line" and spec.points_shown
    assert calls["n"] <= 3, f"正规化跑了 {calls['n']} 遍"


def test_downsampling_covers_dual_axis_and_combo_too():
    """★ 四个可降采样图种都要有端到端覆盖 —— `dual_axis` / `combo` 此前零覆盖（复审突变逮到）。"""
    n = 3300
    for kind, cols, row in (
        (
            "dual_axis",
            _cols(("日期", "date"), ("营业额", "numeric"), ("转化率", "numeric")),
            lambda i: {"日期": f"D{i:05d}", "营业额": 1e6 + i, "转化率": 0.05 + i * 1e-6},
        ),
        (
            "combo",
            _cols(("日期", "date"), ("目标", "numeric"), ("实际", "numeric")),
            lambda i: {"日期": f"D{i:05d}", "目标": 1000.0 + i, "实际": 990.0 + i},
        ),
        ("area_stacked", _cols(("日期", "date"), ("品类", "categorical"), ("额", "numeric")), None),
    ):
        rows = [row(i) for i in range(n)] if row else [{"日期": f"D{i:05d}", "品类": f"C{c}", "额": 10.0 + i + c} for i in range(n) for c in range(3)]
        spec = infer_chart_spec(rows, cols, hint="trend", title="t", force_kind=kind)
        assert spec.points_shown == _MAX_LINE_POINTS, f"force {kind}: 该降采样（实际 {spec.points_shown}，kind={spec.kind}）"
        assert spec.points_total == n


def test_a_constant_date_column_does_not_hijack_the_downsample_axis():
    """★ 常量 date 列（filter 痕迹，如整列都是 `2025年8月`）不是时间轴，别拿它当挑点依据。

    116 语料里这形状 9/2121。拿它去挑点会把「只有 1 个时刻」当成结论、整档跳过，
    真正的时间轴反而不被降采样（复审突变逮到，零覆盖）。
    """
    n = 3300
    cols = _cols(("统计月", "date"), ("日期", "date"), ("值", "numeric"))
    rows = [{"统计月": "2025-08", "日期": f"D{i:05d}", "值": float(i)} for i in range(n)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.points_shown == _MAX_LINE_POINTS, f"该按真时间轴降采样（实际 {spec.points_shown}，kind={spec.kind}）"
    assert spec.points_total == n


def test_no_subcaption_when_nothing_was_actually_sampled_away():
    """★ `points_shown == points_total` 不是降采样 —— 共用判据必须挡住它。"""
    from bowhead_core.chart.types import downsampled_points

    assert downsampled_points({"points_shown": 3000, "points_total": 3200}) == (3000, 3200)
    for bad in ({"points_shown": 3000, "points_total": 3000}, {"points_shown": 0, "points_total": 5}, {"points_shown": 3000}, {}):
        assert downsampled_points(bad) is None, f"{bad} 不该被当成「降过采样」"


def test_downsampling_keeps_every_series_on_the_same_x():
    """★ 各系列必须共用同一组时刻——各跑各的 LTTB 会让几条线在 x 上错位。"""
    n = 4000
    cols = _cols(("日期", "date"), ("品牌", "categorical"), ("销售额", "numeric"))
    rows = [{"日期": f"D{i:05d}", "品牌": b, "销售额": float(i + hash(b) % 7)} for i in range(n) for b in ("A", "B", "C")]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert spec.kind == "line"
    per_series = {}
    for r in spec.rows or []:
        per_series.setdefault(r["品牌"], []).append(r["日期"])
    assert len(per_series) == 3
    assert len({tuple(v) for v in per_series.values()}) == 1, "三条线的时刻集合必须逐字相同"


def test_downsampling_finds_a_spike_that_lives_in_only_one_series():
    """★ 合计序列要能替单条系列说话：某条线单日暴涨时，那个时刻必须被留下。

    这是「各系列共用一组 x」的代价——挑点只能看一条合计序列。用 |值| 求和是为了
    让任何一条系列的异常都把那一刻的合计抬高。
    """
    n = 3500
    cols = _cols(("日期", "date"), ("品牌", "categorical"), ("销售额", "numeric"))
    rows = []
    for i in range(n):
        for b in ("A", "B", "C", "D", "E", "F"):
            v = 100.0 + i * 0.01
            if b == "C" and i == 1777:
                v = 50_000.0  # Only C spikes on this day.
            rows.append({"日期": f"D{i:05d}", "品牌": b, "销售额": v})
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")
    kept_days = {r["日期"] for r in spec.rows or []}
    assert (spec.points_shown or 0) < n, "这份夹具必须真的触发降采样（正对照）"
    assert "D01777" in kept_days, "单条系列的尖峰被丢了"


def test_date_axis_order_matches_the_frontend():
    """★★ 后端挑点的顺序必须与前端画出来的顺序一致。

    前端 `lib/chart/date-order.ts` 用 `Intl.Collator("en", {numeric: true})` 排时间轴，
    北极九章的月份桶**不补零**（`2025-1` / `2025-11`）——按字面排会把 11 月排到 2 月前面，
    那样 LTTB 是沿着一条错的曲线在挑点。这里用同一组标签逐字对拍两份实现。
    ⚠ 改任何一侧都要同步改另一侧，并把标签加进两边的夹具。
    """
    from bowhead_core.chart.infer import _date_axis_sort_key

    cases = [
        (["2025-11", "2025-1", "2025-2", "2025-10"], ["2025-1", "2025-2", "2025-10", "2025-11"]),
        (["2025年11月", "2025年1月", "2024年12月"], ["2024年12月", "2025年1月", "2025年11月"]),
        (["2025-01-15", "2025-01-02", "2024-12-31"], ["2024-12-31", "2025-01-02", "2025-01-15"]),
        (["Q4", "Q1", "Q10", "Q2"], ["Q1", "Q2", "Q4", "Q10"]),
        (["2025Q4", "2025Q1", "2026Q1"], ["2025Q1", "2025Q4", "2026Q1"]),
        # ★ Mix in non-date labels: these inputs crashed the first version's entire chart (pivot totals / SQL NULL).
        (["合计", "2025-02", "2025-01"], ["2025-01", "2025-02", "合计"]),
        (["2025-02", "Q1", "2025-01"], ["2025-01", "2025-02", "Q1"]),
        (["小计", "合计"], ["合计", "小计"]),
        (["a", "1"], ["1", "a"]),
        (["FY25", "fy24", "FY26"], ["fy24", "FY25", "FY26"]),  # Ignore case when sorting (casefold / sensitivity:"base").
        # Empty/missing values (str(None) = "None") sort last, matching FE compareDateAxisValues.
        (["2025-2", "", "2025-1", "None"], ["2025-1", "2025-2", "", "None"]),
    ]
    for raw, expected in cases:
        assert sorted(raw, key=_date_axis_sort_key) == expected, f"{raw} 排成了 {sorted(raw, key=_date_axis_sort_key)}"

    # Negative control: these cases must distinguish numeric and lexical ordering, or they test nothing.
    assert sorted(["2025-11", "2025-2"]) == ["2025-11", "2025-2"], "字面序确实会把 11 月排前面"

    # ★★ Mixed types must not raise: the first version returned tuples such as (2025, '-', 1), mixing int and str;
    #    comparing with ('合计',) raised TypeError and crashed the whole chart (add_chart has no fallback).
    for mixed in (["2025-01", "合计"], ["2025-01", None], ["2025-01", ""], ["2025-01", "Q1"], [None, ""]):
        sorted([("" if v is None else v) for v in mixed], key=_date_axis_sort_key)  # Passing means no exception.


def test_a_non_date_label_in_the_date_column_does_not_crash_the_chart():
    """★★★ 日期列里混一个「合计」/ NULL / Q1 不许让整张图崩掉。

    透视表尾行与 SQL NULL 日期都很常见，而列类型仍会被推断成 `date`
    （门槛是前 20 个样本 ≥80% 像日期，一个异类挡不住）。
    崩的后果不是排错序：`add_chart` 没有 try/except ⇒ 工具调用直接失败；
    自动出图那条被 `except Exception` 吞掉 ⇒ **用户看到「本来有图现在没了」**。
    """
    from bowhead_core.canvas_tools import _infer_col_type

    base = [f"2025-{m:02d}" for m in range(1, 13)]
    for tail in ("合计", None, "", "Q1", "未知"):
        labels = [*base, tail]
        assert _infer_col_type("月份", labels) == "date", f"{tail!r}：列类型仍会被推断成 date（前提成立）"
        rows = [{"月份": v, "销售额": 100.0 + i} for i, v in enumerate(labels)]
        cols = _cols(("月份", "date"), ("销售额", "numeric"))
        spec = infer_chart_spec(rows, cols, hint="trend", title="月度销售额走势")
        assert spec.kind == "line", f"{tail!r}：应当照常出图（实际 {spec.kind}）"
        assert len(spec.rows or []) == 13, f"{tail!r}：一行都不该丢"

    # The downsampling path must not crash either (sorting runs unconditionally before checking whether there are enough points).
    many = [{"日期": f"2020-01-{i:05d}", "值": float(i)} for i in range(_MAX_LINE_POINTS + 50)]
    many.append({"日期": "合计", "值": 99.0})
    big = infer_chart_spec(many, _cols(("日期", "date"), ("值", "numeric")), hint="trend", title="t")
    assert big.kind == "line" and big.points_shown == _MAX_LINE_POINTS


def test_series_cap_runs_before_the_drawable_gate_not_after():
    """★★ 顺序：系列闸必须排在「画得出吗」那道闸**之前**。

    反过来的话 1070 个品牌会先撞 `_MAX_DRAWN_SERIES=100` 退成表格，再也轮不到取前 15。
    这是整笔改动的核心主张，而它此前零覆盖（复审把 `_cap_time_series` 挪到
    `_undrawable_reason` 之后，100 条用例一条不红）。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("品牌", "categorical"), ("营业额", "numeric"))
    rows = [{"每月": m, "品牌": f"B{b:04d}", "营业额": 1000.0 + b} for m in months for b in range(1070)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="2025年月度营业额走势")
    assert spec.kind == "line", f"1070 个品牌该先截到 15 再画，实际 {spec.kind}"
    assert spec.series_shown == 15 and spec.series_total == 1070
    assert len(spec.rows or []) == 15 * 12


def test_series_cap_only_fires_when_that_dimension_is_the_series_axis():
    """★★ 我数的那个维度必须**就是**它摊出来的系列轴，否则一行都不许动。

    柱状族按**列序**取 x 轴：`(门店, 日期, 金额)` 摊开是每天一条系列（30 条），
    而维度里只认 categorical/geo ⇒ 永远选中门店（20 个）。只判「>15 就截」拦不住它：
    30 确实 > 15。复审实测的后果是三件事同时发生——静默丢掉 5 家门店 150 行、
    图上**仍然画着 30 条系列**（可读性一点没修）、三个消费方都被告知「共 20 条系列」
    （20 是类目数不是系列数）。
    """
    rows = [{"门店": f"S{k:02d}", "日期": f"D{d:03d}", "金额": float(d * 7 + k)} for k in range(20) for d in range(30)]
    cols = _cols(("门店", "categorical"), ("日期", "date"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="bar")
    assert len(spec.rows or []) == 600, f"数错了对象就一行都别动，实际丢到 {len(spec.rows or [])}"
    assert spec.series_shown is None and spec.series_total is None
    _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    assert shape.get("seriesCount") == 30, "画出来的仍是 30 条——这一档本就不归这道闸管"


def test_series_cap_ranks_by_every_numeric_column_not_just_the_first():
    """★★ 转置那条路（1 个维度 + 多个时间表头列）是**一行一条系列**，合计要整行横着加。

    只看第一个数值列时复审实测：第一个月的排名与三个月合计的排名相反 ⇒ 留下的是正确答案的
    **补集**（30 个省、零重合），而图注逐字写着「按合计显示前 15 条」。
    """
    cols = _cols(("省", "categorical"), ("2025-01", "numeric"), ("2025-02", "numeric"), ("2025-03", "numeric"))
    rows = [{"省": f"省{p:02d}", "2025-01": float(p), "2025-02": float(1000 - 30 * p), "2025-03": float(1000 - 30 * p)} for p in range(30)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t", force_kind="line")
    kept = {r["省"] for r in spec.rows or []}
    truth = {r["省"] for r in sorted(rows, key=lambda r: -(r["2025-01"] + r["2025-02"] + r["2025-03"]))[:15]}
    assert spec.series_shown == 15 and spec.series_total == 30
    assert kept == truth, f"留下的不是真·合计前 15：{sorted(kept)[:3]}… vs {sorted(truth)[:3]}…"


def test_series_cap_skips_kinds_whose_dimension_is_not_a_series():
    """★ 非时间轴图种一行都不许动 —— 散点的那一列是点的身份，不是系列。"""
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("省", "categorical"), ("金额", "numeric"))
    rows = [{"每月": m, "省": f"省{p:02d}", "金额": 1000.0 + p} for m in months for p in range(59)]
    for kind in ("scatter", "heatmap", "pie"):
        spec = infer_chart_spec([dict(r) for r in rows], list(cols), hint="raw", title="t", force_kind=kind)
        assert spec.series_shown is None, f"force {kind}: 不该走系列闸（实际 {spec.series_shown}）"
        if spec.kind == kind:
            assert len(spec.rows or []) == 708, f"force {kind}: 一行都不该丢"


def test_series_cap_picks_the_dimension_that_matches_the_drawn_series_count():
    """★★ 多个维度时不靠「第几个」猜，靠「取值数恰好等于画出来的系列数」认。

    ⚠ 第一版写的是「两个以上维度一律不碰」，复审的突变逼出反例：同一份数据 force 堆叠柱时
    正规化把**省**摊成 59 条系列，而那条守卫把闸关掉了 ⇒ 59 条照画，正是要修的那个形状。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    # ⚠ Column order is part of this test: normalization picks the pivot dimension by column order.
    #   (每月, 省, 大区, …) ⇒ pivot province, 59 series; (每月, 大区, 省, …) ⇒ pivot region, four series.
    #   My first version asserted the latter, testing the wrong case (discovered only when run).
    cols = _cols(("每月", "date"), ("省", "categorical"), ("大区", "categorical"), ("金额", "numeric"))
    # Region rotates with the month so province and region are **crossed**, not a hierarchy. A fixed region per province
    # is a parent/child pair, for which `_resolve_stack_dims` now declares x = province, series = region (4 series),
    # which is what `buildBar` always drew; this test is about the series gate on crossed dimensions.
    rows = [{"每月": m, "省": f"省{p:02d}", "大区": f"R{(p + i) % 4}", "金额": 1000.0 + p} for i, m in enumerate(months) for p in range(59)]

    # ① No pivot (one rendered series) ⇒ capping dimensions cannot improve readability; preserve every row.
    flat = infer_chart_spec([dict(r) for r in rows], list(cols), hint="trend", title="t")
    _, flat_shape = normalize_chart_for_rendering_v1(spec_to_dict(flat))
    assert flat_shape.get("seriesCount") == 1
    assert flat.series_shown is None and len(flat.rows or []) == 708

    # ② When province really pivots to 59 series, identify province as the series axis (four regions do not match) and cap at 15.
    stacked = infer_chart_spec([dict(r) for r in rows], list(cols), hint="raw", title="t", force_kind="stacked_bar")
    assert stacked.series_shown == 15 and stacked.series_total == 59, "该认出「省」才是系列轴"
    assert len({r["省"] for r in stacked.rows or []}) == 15, "截的是省"
    _, shape = normalize_chart_for_rendering_v1(spec_to_dict(stacked))
    assert shape.get("seriesCount") == 15, "截完真的只剩 15 条"

    # ③ Negative control: (每月, 大区, 省, …) order pivots region (four series), within the cap; preserve every row.
    other = _cols(("每月", "date"), ("大区", "categorical"), ("省", "categorical"), ("金额", "numeric"))
    by_region = infer_chart_spec([dict(r) for r in rows], other, hint="raw", title="t", force_kind="stacked_bar")
    assert by_region.series_shown is None and len(by_region.rows or []) == 708


def test_series_cap_boundary_is_exactly_the_cap():
    """★ 边界两侧各钉一档：15 一行不动、16 才截。"""
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("品牌", "categorical"), ("销售额", "numeric"))

    def spec_for(n: int):
        rows = [{"每月": m, "品牌": f"B{b:02d}", "销售额": 1000.0 + b} for m in months for b in range(n)]
        return infer_chart_spec(rows, cols, hint="trend", title="t")

    at_cap = spec_for(15)
    assert at_cap.series_shown is None and at_cap.series_total is None, "正好 15 条不算截断，标记一个都不许打"
    assert len(at_cap.rows or []) == 15 * 12

    over = spec_for(16)
    assert over.series_shown == 15 and over.series_total == 16
    assert len(over.rows or []) == 15 * 12


def test_series_cap_does_not_normalize_when_no_dimension_is_big_enough(monkeypatch):
    """★ 维度基数都 ≤15 时不许跑正规化 —— 它在几万行上要几十毫秒，且是在事件循环上同步算的。

    这道早退是**性能**闸（行为上等价），只能靠「有没有调用」来钉；靠断言输出钉不住它。
    """
    from bowhead_core import chart_style_presentation as presentation

    calls = {"n": 0}
    real = presentation.normalize_chart_for_rendering_v1

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(presentation, "normalize_chart_for_rendering_v1", counting)

    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("品牌", "categorical"), ("销售额", "numeric"))
    rows = [{"每月": m, "品牌": f"B{b:02d}", "销售额": 1000.0 + b} for m in months for b in range(12)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="t")

    assert spec.series_shown is None
    assert calls["n"] == 1, f"只该由「画得出吗」那道闸跑一次，实际 {calls['n']} 次"


def test_series_cap_ties_break_on_name_not_dict_order():
    """合计全等时按**系列名**定序——没有第二键的话留下哪 15 条要看 dict 迭代序，重启就换一批。"""
    months = [f"2025-{m:02d}" for m in range(1, 7)]
    cols = _cols(("每月", "date"), ("品牌", "categorical"), ("销售额", "numeric"))
    names = [f"B{i:02d}" for i in range(40)]
    rows = [{"每月": m, "品牌": b, "销售额": 100.0} for m in months for b in names]
    first = infer_chart_spec(rows, cols, hint="trend", title="t")
    assert {r["品牌"] for r in first.rows or []} == set(names[:15]), "全等时按名字升序取前 15"

    # Change insertion order (and dict iteration order); results must remain exactly identical.
    shuffled = [{"每月": m, "品牌": b, "销售额": 100.0} for m in months for b in reversed(names)]
    again = infer_chart_spec(shuffled, cols, hint="trend", title="t")
    assert {r["品牌"] for r in again.rows or []} == {r["品牌"] for r in first.rows or []}


def test_series_cap_ranks_by_magnitude_so_losses_are_not_dropped():
    """★ 按 **|值|** 排序：一条全是负数的「利润」序列同样是这张图上最重要的线之一。

    按带符号值排的话，亏得最狠的那几条会被排到最末尾、第一个被丢掉——
    而「谁在亏」正是看这张图的人要找的东西。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("业务线", "categorical"), ("利润", "numeric"))
    # L00..L19: small profits (+1..+20); L90..L94: large losses (starting at -1000).
    rows = [{"每月": m, "业务线": f"L{i:02d}", "利润": 1.0 + i} for m in months for i in range(20)]
    rows += [{"每月": m, "业务线": f"L9{i}", "利润": -1000.0 - i * 100} for m in months for i in range(5)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="各业务线月度利润")
    kept = {r["业务线"] for r in spec.rows or []}
    assert spec.series_shown == 15 and spec.series_total == 25
    assert {f"L9{i}" for i in range(5)} <= kept, f"亏损最大的五条必须留下，实际 {sorted(kept)}"


def test_series_cap_does_not_fabricate_an_other_bucket_for_rates():
    """不可加的列（比率）同样只取前 N，**不折成「其余」**——59 个省的利润率相加是垃圾。"""
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cols = _cols(("每月", "date"), ("省份", "categorical"), ("利润率", "numeric"))
    rows = [{"每月": m, "省份": f"省{p:02d}", "利润率": 0.10 + p / 1000} for m in months for p in range(30)]
    spec = infer_chart_spec(rows, cols, hint="trend", title="各省月度利润率")
    assert spec.series_shown == 15 and spec.series_total == 30
    kept = {r["省份"] for r in spec.rows or []}
    assert len(kept) == 15
    assert not any("其余" in str(v) or "其他" in str(v) for v in kept), "不能编造一个合并桶"
    # Every rendered value remains an original observation.
    assert all(r["利润率"] == 0.10 + int(r["省份"][1:]) / 1000 for r in spec.rows or [])


def test_series_cap_leaves_the_drawable_gate_alive():
    """系列闸够不着的形状仍由 `_MAX_DRAWN_SERIES` 接住——它问的是「画得出吗」，是另一个问题。

    `(门店, 日期, 金额)` 的柱状族按**列序**取 x 轴 ⇒ 摊开是每天一条系列（300 条），
    而门店只有 5 个：截门店一行都截不掉，这时必须让「画得出吗」那道闸退表。
    """
    store_first = [{"门店": f"S{k}", "日期": f"D{d:03d}", "金额": float(d * 7 + k)} for k in range(5) for d in range(300)]
    cols = _cols(("门店", "categorical"), ("日期", "date"), ("金额", "numeric"))
    spec = infer_chart_spec(store_first, cols, hint="raw", title="t", force_kind="bar")
    assert spec.kind == "table", f"300 条系列该退表，实际 {spec.kind}"
    assert spec.series_shown is None, "截不动就别打标记——否则图注凭空多出「共 5 条系列 / 显示前 5 条」"

    # ★ Table fallback creates a new spec, dropping markers; the assertion above misses unnecessarily filled fields.
    #   Use a drawable chart of the same shape (five stores × 20 days ⇒ 20 pivoted series, >15 but <100):
    #   capping stores cannot remove any rows. If it still writes series_shown=5/series_total=5,
    #   the caption falsely adds 「共 5 条系列 / 按合计显示前 5 条」, a user-visible lie.
    drawable = [{"门店": f"S{k}", "日期": f"D{d:03d}", "金额": float(d * 7 + k)} for k in range(5) for d in range(20)]
    ok = infer_chart_spec(drawable, cols, hint="raw", title="t", force_kind="bar")
    assert ok.kind == "bar", f"20 条系列画得出，不该退表（实际 {ok.kind}）"
    assert ok.series_shown is None and ok.series_total is None, (
        f"5 个门店截不到 15 以下，一行都没丢，不该打标记（实际 {ok.series_shown}/{ok.series_total}）"
    )


def test_series_guard_counts_what_is_actually_drawn():
    """系列数按真画法数，不按「非日期那一列」猜（review 第二轮）。

    ① 柱状族按**列序**取 x 轴：`(门店, 日期, 金额)` 的柱状图摊开是每天一条系列（300 条），
       不是 5 条门店——第一版的闸数成 5 条放行了，导出照样整份失败。
    ② 不摊开的图种（热力图）系列数自然小，不该被这道闸退成表——「日期 × SKU」本来就是热力图的形状。
    """
    store_first = [{"门店": f"S{k}", "日期": f"D{d:03d}", "金额": float(d * 7 + k)} for k in range(5) for d in range(300)]
    cols = _cols(("门店", "categorical"), ("日期", "date"), ("金额", "numeric"))
    for kind in ("bar", "stacked_bar"):
        spec = infer_chart_spec([dict(r) for r in store_first], list(cols), hint="raw", title="t", force_kind=kind)
        assert spec.kind == "table", f"force {kind}: 300 条系列应退表，实际 {spec.kind}"

    # Keep y-axis categories within the heatmap's own cap (see test_heatmap_with_too_many_y_categories_falls_back_to_table);
    # this tests that charts without pivoted series must not suffer from the series gate.
    grid = [{"月": f"2025-{m:02d}", "SKU": f"SKU{k}", "销量": float(m + k)} for m in range(1, 13) for k in range(30)]
    heat = infer_chart_spec(grid, _cols(("月", "date"), ("SKU", "categorical"), ("销量", "numeric")), hint="raw", title="t", force_kind="heatmap")
    assert heat.kind == "heatmap"


def test_huge_sparse_stacked_bar_is_rejected_before_normalizing(monkeypatch):
    """稀疏堆叠柱的正规化要物化 类目×分组 整个矩阵——5000×5000 在事件循环上同步算要几十秒、
    四百多 MB（review 第三轮）。格数超过「能画的上限之积」就必然画不了，不许先去算。"""
    import bowhead_core.chart_style_presentation as presentation

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("格数早就超了，不该再跑正规化")

    monkeypatch.setattr(presentation, "normalize_chart_for_rendering_v1", must_not_run)
    rows = [{"日期": f"D{i:05d}", "SKU": f"SKU{(i * 7919) % 5000}", "金额": float(i)} for i in range(10_000)]
    spec = infer_chart_spec(
        rows, _cols(("日期", "date"), ("SKU", "categorical"), ("金额", "numeric")), hint="raw", title="t", force_kind="stacked_bar"
    )
    assert spec.kind == "table"
    assert "格" in (spec._infer_reason or "")


def test_hbar_with_date_column_is_capped_like_the_restore_path():
    """带日期列的横条走不到撤回段（`top_n` 为空）——同样按值取前 50 根并打 `top_n`，
    否则 5000 根横条会超出浏览器画布高度上限。"""
    rows = [{"日期": f"D{i:05d}", "金额": float(i)} for i in range(5000)]
    spec = infer_chart_spec(rows, _cols(("日期", "date"), ("金额", "numeric")), hint="rank", title="t", force_kind="hbar")
    assert spec.kind == "hbar"
    assert len(spec.rows or []) == 50
    assert spec.top_n == 50
    assert min(r["金额"] for r in spec.rows or []) == 4950, "按值取最大的 50 根"


def test_table_result_is_not_truncated():
    """★ 落到表格时撤回截断——30 是「柱子读得动几根」，表格没这个问题。

    116 验收逮到的真回归（session s_c88220d81f9e）：模型 `force_kind="table"`、标题
    写着 `All 38 Category Classes`，却被截成 30 行——它点名要全表，给了 3/4。
    旧码在这里是内联全部 38 行；是把截断放到图种判定**之前**才带出来的。
    """
    rows = [{"店": f"S{i}", "额": 100 - i} for i in range(38)]
    cols = _cols(("店", "categorical"), ("额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="全部 38 项", force_kind="table")
    assert spec.kind == "table"
    assert len(spec.rows or []) == 38, "表格不该被截"
    assert spec.top_n is None, "没截就不许打截断标记（否则前端会说假话）"
    assert spec.row_count == 38


def test_table_from_shape_tree_also_untruncated():
    """形状树自己判成表（三种量纲）时同样不截——不只 force_kind 那条路。"""
    rows = [{"店": f"S{i}", "营业额": 1e9 + i, "客流量": 1e6 + i, "利润率": 0.1 + i / 1000} for i in range(38)]
    cols = _cols(("店", "categorical"), ("营业额", "numeric"), ("客流量", "numeric"), ("利润率", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind == "table"
    assert len(spec.rows or []) == 38
    assert spec.top_n is None


def test_table_beyond_inline_limit_still_caps_at_inline_rows():
    """正对照：撤回截断不等于无限内联——超过 `inline_rows_limit` 仍只带头 N 行。

    `top_n` 保持 None 是**有意的**：表格页脚自己会写「显示 1-50 / 共 120」，
    再打标记就是同一件事说两遍（图下又冒一句「仅显示前 50 项」）。
    横条没有页脚，所以它要打——见 `test_hbar_over_inline_limit_keeps_the_marker`。
    """
    rows = [{"店": f"S{i}", "额": i} for i in range(120)]
    cols = _cols(("店", "categorical"), ("额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="table", inline_rows_limit=50)
    assert spec.kind == "table"
    assert len(spec.rows or []) == 50
    assert spec.row_count == 120
    assert spec.top_n is None


def test_hbar_over_inline_limit_keeps_the_marker():
    """★ 横条撤回截断后**仍可能没画全**——那就必须打标记，不能一律清空。

    图上没有页脚，清空 `top_n` 等于把「只画了 50/120」瞒掉。
    """
    rows = [{"店": f"某某某某某某店{i}", "额": 200 - i} for i in range(120)]
    cols = _cols(("店", "categorical"), ("额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t", inline_rows_limit=50)
    assert spec.kind == "hbar"
    assert len(spec.rows or []) == 50
    assert spec.top_n == 50
    assert spec.row_count == 120


def test_hbar_restore_takes_top_by_value_not_head_slice():
    """★★ 撤回截断时横条必须**按值**取前 N，不能直接切片。

    喂**升序**的行（agent 完全可能这么给），直接切片画出来的是**最小的 50 个**，
    标题还写着「排名」——正是本仓反复警告的「假 Top10」。
    把 `_take_top_n(...)` 换回 `full_rows[:inline_rows_limit]`，本例就红。
    """
    rows = [{"店": f"某某某某某某店{i}", "额": i} for i in range(120)]  # Ascending order; largest last.
    cols = _cols(("店", "categorical"), ("额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t", inline_rows_limit=50)
    assert spec.kind == "hbar"
    got = [r["额"] for r in spec.rows or []]
    assert len(got) == 50
    assert min(got) == 70, f"该画最大的 50 个（70..119），实际最小值 {min(got)}"


def test_table_restore_keeps_original_order():
    """正对照：表格保原序取头 N —— 页脚写的就是「显示 1-50」，且行序可能有意义。"""
    rows = [{"店": f"S{i}", "额": i} for i in range(120)]
    cols = _cols(("店", "categorical"), ("额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="table", inline_rows_limit=50)
    assert [r["额"] for r in spec.rows or []] == list(range(50))


def test_hbar_within_inline_limit_has_no_marker():
    """正对照：40 行的横条现在**全画出来**，也就不该再说「仅显示前 N」。

    这正是 116 dogfood 那张图（40 家门店截到 30）要解决的。
    """
    rows = [{"店": f"某某某某某某店{i}", "额": 100 - i} for i in range(40)]
    cols = _cols(("店", "categorical"), ("额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t")
    assert spec.kind == "hbar"
    assert len(spec.rows or []) == 40
    assert spec.top_n is None


def test_98_row_chart_keeps_true_total_after_truncation():
    """Replays exact prod scenario (sec_62e51a9f919b, 行为KPI×销售达成 98 reps).

    原缺陷是页脚写「显示 0-0 / 98 行」——`rows` 被丢成 None。批3 ⑤ 之后这份数据走
    截断而不是退表，但**同一条口径必须继续成立**：手里的行数可以少，`row_count`
    永远是真实总数，前端才拼得出「显示前 N / 共 98」。
    """
    rows = [{"代表": f"rep_{i}", "kpi": i / 100, "sales": i * 2 / 100} for i in range(98)]
    cols = _cols(("代表", "categorical"), ("kpi", "numeric"), ("sales", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="行为KPI vs 销售达成 关联分析", data_artifact_id="art_real", inline_rows_limit=50)
    assert spec.row_count == 98
    assert spec.rows is not None and len(spec.rows) == 30
    assert spec.top_n == 30


def test_spec_to_dict_drops_empty():
    spec = infer_chart_spec(
        [{"x": "a", "y": 1}],
        _cols(("x", "categorical"), ("y", "numeric")),
        hint="rank",
        title="t",
    )
    d = spec_to_dict(spec)
    # Top-level keys always exist.
    assert d["kind"]
    assert d["title"]
    # drill defaults to None; omit from dict.
    assert "drill" not in d
    # fallback_iframe_url defaults to None; omit from dict.
    assert "fallback_iframe_url" not in d


def test_parent_child_false_when_child_shared():
    """不是真正的父子：一个 child 跨多个 parent → not parent-child → stacked_bar 兜底."""
    rows = [
        {"cat1": "A", "cat2": "X", "v": 1},
        {"cat1": "B", "cat2": "X", "v": 2},  # X belongs to both A and B.
        {"cat1": "A", "cat2": "Y", "v": 3},
    ]
    cols = _cols(("cat1", "categorical"), ("cat2", "categorical"), ("v", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="composition", title="t")
    # Non-strict parent-child relationships fall back to stacked_bar (M137 follow-up: reason becomes "multi-dim grouping").
    assert spec.kind == "stacked_bar"


def test_heatmap_high_cardinality_two_cats():
    """两 cat 一 num、基数 ≥20 → heatmap。

    ⚠ 本例原来喂 75 行、断言 `table`，测的其实是「n 过大先退表」那条——批3 ⑤ 之后
    行数由截断保证（永远 ≤30），那条已不存在。改成喂一份**本来就画得下**的高基数数据，
    测它名字里说的那件事：heatmap 选型本身。
    """
    # Avoid all three rules to reach heatmap:
    #   ① Rule 0a prunes cardinality=1 category columns as filter remnants; both dimensions must vary.
    #   ② Parent-child relationships (every child belongs to one parent) choose stacked_bar first; neither direction may qualify.
    #   ③ The high-cardinality dimension must have ≥20 values.
    #
    # ⚠ Fixture changed on 2026-09-03. Formerly 20 months × alternating A/B hospitals; it avoided parent-child detection
    # by assigning months across multiple hospitals, valid only when the old implementation chose parents by column order.
    # Cardinality ordering makes hospitals (two) the parents, with each month belonging to one hospital, yielding parent-child instead.
    # That fixture guarded the old implementation's shape, not high cardinality producing heatmaps.
    # Use genuine cross-grouping (h0 spans two months, others one each): neither direction is one-to-many,
    # truly reaching the heatmap branch.
    rows = [{"月": "1月" if i % 2 else "2月", "医院": f"h{i}", "金额": (i + 1) * 100} for i in range(20)]
    rows.append({"月": "2月" if rows[0]["月"] == "1月" else "1月", "医院": "h0", "金额": 999})
    cols = _cols(("月", "categorical"), ("医院", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t")
    assert spec.kind == "heatmap", spec._infer_reason


def test_high_cardinality_two_cats_over_cap_is_truncated_not_tabled():
    """同一份数据超上限时：截断后照常出图，不再整张退表。"""
    rows = [{"月": f"{m}月", "医院": f"h{i}", "金额": i * 1000} for m in range(1, 4) for i in range(25)]
    cols = _cols(("月", "categorical"), ("医院", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="composition", title="t")
    assert spec.kind != "table"
    assert spec.top_n == 30
    assert spec.row_count == 75


# ─── Prune wide-table columns to preserve hint effects (#61) ──────────────────────────
def test_prune_cols_for_rank_hint_wide_table():
    """宽表 + hint=rank → 裁到 (cat + sort_by num) 2 列."""
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows = [{"品牌": "A", "营业额": 100, "客流量": 10, "利润": 20, "退款": 5}, {"品牌": "B", "营业额": 80, "客流量": 8, "利润": 15, "退款": 3}]
    cols = _cols(("品牌", "categorical"), ("营业额", "numeric"), ("客流量", "numeric"), ("利润", "numeric"), ("退款", "numeric"))
    cols.append(ChartColumn(key="其他1", header="其他1", type="numeric"))  # type: ignore[arg-type]
    cols.append(ChartColumn(key="其他2", header="其他2", type="numeric"))  # type: ignore[arg-type]
    for r in rows:
        r["其他1"] = 1
        r["其他2"] = 2
    # Seven columns before pruning → two afterward (品牌 + 营业额).
    new_rows, new_cols, _cut = _prune_cols_for_hint(rows, cols, hint="rank", sort_by="营业额")
    assert [c.key for c in new_cols] == ["品牌", "营业额"]
    assert all(set(r.keys()) == {"品牌", "营业额"} for r in new_rows)


def test_prune_cols_noop_when_narrow():
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows = [{"A": "x", "B": 1}]
    cols = _cols(("A", "categorical"), ("B", "numeric"))
    _new_rows, new_cols, _cut = _prune_cols_for_hint(rows, cols, hint="rank", sort_by=None)
    # Preserve two-column wide tables.
    assert len(new_cols) == 2


def test_prune_cols_fallback_first_numeric_when_no_sort_by():
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows = [{"城市": "上海", "营收": 100, "利润": 20}]
    cols = _cols(("城市", "categorical"), ("营收", "numeric"), ("利润", "numeric"))
    cols.extend([ChartColumn(key=f"x{i}", header=f"x{i}", type="numeric") for i in range(5)])  # type: ignore[arg-type]
    for r in rows:
        for i in range(5):
            r[f"x{i}"] = i
    # Prune to (城市, 营收), the first numeric column.
    _new_rows, new_cols, _cut = _prune_cols_for_hint(rows, cols, hint="share", sort_by=None)
    assert [c.key for c in new_cols] == ["城市", "营收"]


# #76 G4 extended tests
def test_prune_cols_excludes_id_columns():
    """'部门编码' / '申请人工号' 虽 type=numeric，不应当度量."""
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows = [
        {"部门名称": "物流部", "部门编码": 211493, "申请人工号": 90010260, "营业额": 10000},
    ]
    cols = _cols(
        ("部门名称", "categorical"),
        ("部门编码", "numeric"),
        ("申请人工号", "numeric"),
        ("营业额", "numeric"),
    )
    _new_rows, new_cols, _cut = _prune_cols_for_hint(rows, cols, hint="rank", sort_by=None)
    assert [c.key for c in new_cols] == ["部门名称", "营业额"]


def test_prune_cols_correlation_keeps_two_numeric():
    """hint=correlation → scatter 需要 2 个数值轴."""
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows = [{"城市": "上海", "营收": 100, "利润": 20, "客流": 500}]
    cols = _cols(
        ("城市", "categorical"),
        ("营收", "numeric"),
        ("利润", "numeric"),
        ("客流", "numeric"),
    )
    _new_rows, new_cols, _cut = _prune_cols_for_hint(
        rows,
        cols,
        hint="correlation",
        sort_by="营收",
    )
    assert len(new_cols) == 2
    assert [c.type for c in new_cols].count("numeric") == 2


def test_prune_cols_prefers_name_column_for_cat():
    """cat 轴优选列名含'名称/分类'的."""
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows = [{"ID1": "a", "部门名称": "物流", "金额": 100}]
    cols = _cols(
        ("ID1", "categorical"),
        ("部门名称", "categorical"),
        ("金额", "numeric"),
    )
    # Pruning requires >6 columns; add several.
    cols_wide = cols + _cols(("x", "numeric"), ("y", "numeric"), ("z", "numeric"), ("w", "numeric"))
    _new_rows, new_cols, _cut = _prune_cols_for_hint(rows, cols_wide, hint="rank", sort_by=None)
    # Category axis selects 部门名称 (contains 名称) rather than ID1.
    assert new_cols[0].key == "部门名称"


# ─────────────────────────────────────────────────────────────
# WP2: bridge → waterfall / flow → sankey
# ─────────────────────────────────────────────────────────────


def _bridge_rows():
    return [
        {"item": "Volume", "delta": 4.1},
        {"item": "Price", "delta": 2.8},
        {"item": "Mix", "delta": -1.2},
    ]


def _flow_rows():
    # A real flow shares nodes (Direct feeds two regions, EMEA is fed by two channels). Two unconnected links
    # are not a flow and become a table (`_not_a_flow_reason`, readability audit item 1).
    return [
        {"ch": "Direct", "rg": "AMER", "rev": 30.4},
        {"ch": "Direct", "rg": "EMEA", "rev": 12.0},
        {"ch": "Partner", "rg": "EMEA", "rev": 18.6},
    ]


def test_bridge_hint_one_cat_one_num_infers_waterfall():
    spec = infer_chart_spec(_bridge_rows(), _cols(("item", "categorical"), ("delta", "numeric")), hint="bridge", title="bridge")
    assert spec.kind == "waterfall"
    assert "bridge" in spec._infer_reason


def test_bridge_hint_shape_mismatch_falls_through():
    # Two numeric columns do not fit bridge shape; do not force waterfall.
    rows = [{"item": "A", "a": 1, "b": 2}, {"item": "B", "a": 3, "b": 4}]
    spec = infer_chart_spec(rows, _cols(("item", "categorical"), ("a", "numeric"), ("b", "numeric")), hint="bridge", title="x")
    assert spec.kind != "waterfall"


def test_bridge_hint_too_many_segments_falls_through():
    rows = [{"item": f"i{i}", "delta": i} for i in range(25)]
    spec = infer_chart_spec(rows, _cols(("item", "categorical"), ("delta", "numeric")), hint="bridge", title="x")
    assert spec.kind != "waterfall"


def test_flow_hint_two_cat_one_num_infers_sankey():
    spec = infer_chart_spec(_flow_rows(), _cols(("ch", "categorical"), ("rg", "categorical"), ("rev", "numeric")), hint="flow", title="flow")
    assert spec.kind == "sankey"
    assert "flow" in spec._infer_reason


def test_flow_hint_single_cat_falls_through():
    rows = [{"ch": "Direct", "rev": 30.4}, {"ch": "Partner", "rev": 18.6}]
    spec = infer_chart_spec(rows, _cols(("ch", "categorical"), ("rev", "numeric")), hint="flow", title="x")
    assert spec.kind != "sankey"


# ─────────────────────────────────────────────────────────────
# Column typing: values determine time axes first (chart-config-ux batch 1 ③).
#
# Trigger (observed in 116 live window): dimension 每天 has standard date values 2026-07-21,
# but _DATE_HINT lacks 天. Old criterion required a time word in the name AND date-like values;
# failing the name check skipped values. Ten of 14 misses arose here; none became lines.
# ─────────────────────────────────────────────────────────────


def test_col_type_iso_dates_without_time_word_in_name():
    """列名不带时间词，但整列取值是 ISO 日期 → date（116 真例：「每天」）."""
    from bowhead_core.canvas_tools import _infer_col_type

    samples = ["2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"]
    assert _infer_col_type("每天", samples) == "date"


def test_col_type_value_first_covers_common_date_shapes():
    from bowhead_core.canvas_tools import _infer_col_type

    for samples in (
        ["2026/7/21", "2026/7/22", "2026/7/23"],
        ["2026-07", "2026-08", "2026-09"],
        ["2026年7月", "2026年8月", "2026年9月"],
        ["2026Q1", "2026Q2", "2026Q3"],
    ):
        assert _infer_col_type("统计口径", samples) == "date", samples


# ── Positive controls: these must not be date. Removing _STRICT_DATE_RE's whole-string anchors
#    or _STRICT_DATE_MIN_RATIO's ratio threshold must fail at least one of these three tests. ──


def test_col_type_category_containing_time_words_is_not_date():
    """含时间字的普通类目不许被值优先那档吃掉（宽判据 `_looks_like_date` 会误判它们）."""
    from bowhead_core.canvas_tools import _infer_col_type

    assert _infer_col_type("商品类目", ["周边商品", "季度礼盒", "年货节"]) == "categorical"
    assert _infer_col_type("活动档期", ["第3季度促销档", "第4季度促销档", "第1季度促销档"]) == "categorical"


def test_col_type_mostly_categorical_with_one_date_stays_categorical():
    """一列类目里混进一个像日期的值，不该把整列翻成时间轴（占比门槛守的就是这个）."""
    from bowhead_core.canvas_tools import _infer_col_type

    # ⚠ Do not name the column 大区: 区 is in _GEO_HINT and selects geo first, bypassing the ratio-threshold test.
    samples = ["直营", "加盟", "联营", "代销", "2026-07-21"]
    assert _infer_col_type("渠道", samples) == "categorical"


def test_col_type_single_date_sample_does_not_flip_column():
    """只有 1 个样本时不靠值定性——巧合不足以定生死，仍走列名那档."""
    from bowhead_core.canvas_tools import _infer_col_type

    assert _infer_col_type("备注", ["2026-07-21"]) == "categorical"


def test_col_type_name_gated_date_still_works_with_float_polluted_labels():
    """不回归：列名带时间词那档保持**宽**判据。

    116 上有 70 张图的「每月」列取值被浮点污染成 `2026.0年1.0月`——严格式样匹配不上，
    只有旧的宽判据认得。两档必须并存，不能合并成一套。
    """
    from bowhead_core.canvas_tools import _infer_col_type

    samples = ["2026.0年1.0月", "2026.0年2.0月", "2026.0年3.0月"]
    assert _infer_col_type("每月", samples) == "date"


def test_col_type_geo_still_wins_when_values_are_not_dates():
    """不回归：值不像日期时，地理词那档照常生效."""
    from bowhead_core.canvas_tools import _infer_col_type

    assert _infer_col_type("省", ["广东", "江苏", "浙江"]) == "geo"


def test_daily_series_now_infers_line_end_to_end():
    """端到端：「每天」+ ISO 日期 + 1 个数值列 → 折线，而不是横条/柱。

    116 真例 `近30天退款利润占利润比走势` 此前出成 hbar。
    """
    from bowhead_core.canvas_tools import _parse_csv_with_types

    lines = ["每天,营业额"]
    lines += [f"2026-07-{d:02d},{100 + d}" for d in range(1, 30)]
    rows, cols = _parse_csv_with_types("\n".join(lines).encode("utf-8"))
    assert [c.type for c in cols] == ["date", "numeric"]
    spec = infer_chart_spec(rows, cols, hint="trend", title="近30天营业额走势")
    assert spec.kind == "line"


# ─────────────────────────────────────────────────────────────
# Pruning and table-fallback gates share a source (chart-config-ux batch 1 ②).
#
# Previously separate criteria: pruning checked total columns >6; table fallback checked numeric columns >=4.
# Wide tables with four to six columns fell between them: no pruning, but immediate table fallback.
# 39 charts hit this in the 116 live window; 26 had ≤30 rows, perfectly drawable by row count.
# ─────────────────────────────────────────────────────────────


def test_pruning_gate_leaves_drawable_multi_series_alone():
    """★ 4 个数值列现在**画得出**，就不该被裁掉。

    批1 这条原来断言「5 列宽表必须裁列」——那是当时 `numeric>=4 → table` 的配套：
    画不出才需要裁。批4 ⑨ 把上限提到调色板槽位之后，4 列是可画的，再裁就是
    把好图裁瘦（真机 `2025年月度核心指标趋势` 正是这个形状）。
    两道闸仍然同源，只是那个数从 3 变成了 `_MAX_SERIES`。
    """
    from bowhead_core.canvas_tools import _needs_col_pruning

    cols = _cols(
        ("产品细分市场", "categorical"),
        ("share_cy", "numeric"),
        ("share_ly", "numeric"),
        ("share_cy_pct", "numeric"),
        ("delta_pp", "numeric"),
    )
    assert _needs_col_pruning(cols) is False


def test_pruning_gate_still_noop_for_drawable_shapes():
    """正对照：画得出的窄表不许被裁（否则会把好图裁瘦）."""
    from bowhead_core.canvas_tools import _needs_col_pruning

    assert _needs_col_pruning(_cols(("省", "geo"), ("营业额", "numeric"))) is False
    assert (
        _needs_col_pruning(
            _cols(("省", "geo"), ("今年", "numeric"), ("去年", "numeric"), ("同比", "numeric")),
        )
        is False
    )


def test_pruning_gate_shares_the_threshold_with_the_table_fallback():
    """两道闸同源，但裁列这道按**同族上限**放行——比退表闸松一档。

    ★ 松的理由不是随意：`_needs_col_pruning` 只拿到 cols、算不出量纲族，
    而**裁早了就没法在下游按族分档**（`各品类月度趋势` 10 列同族，裁到 6 之后
    退表闸再宽也救不回被丢掉的 4 个品类）。信息不全的上游宁可放过去。
    """
    from bowhead_core.canvas_tools import _needs_col_pruning
    from bowhead_core.chart.infer import _MAX_SERIES, _MAX_SERIES_SAME_FAMILY

    n = _MAX_SERIES_SAME_FAMILY
    at_limit = _cols(("省", "geo"), *[(f"n{i}", "numeric") for i in range(n)])
    over_limit = _cols(("省", "geo"), *[(f"n{i}", "numeric") for i in range(n + 1)])
    assert _needs_col_pruning(at_limit) is False
    assert _needs_col_pruning(over_limit) is True
    # ★ Do not prune same-family wide tables within their cap upstream (different families let table fallback use actual family counts).
    same_family_wide = _cols(("省", "geo"), *[(f"n{i}", "numeric") for i in range(_MAX_SERIES + 1)])
    assert _needs_col_pruning(same_family_wide) is False


def test_wide_table_with_intent_no_longer_degrades_to_table():
    """端到端：真机那份 5 列宽表（同一指标的 5 种表述）现在直接画得出。

    ⚠ 批1 时它要靠**裁列**才画得出（当时 `numeric>=4 → table`）；批4 ⑨ 之后
    4 个数值列本身就是可画的，连裁都不用裁。断言从「裁完能画」改成「本来就能画」
    ——测的仍是同一件事：这份形状不该掉表格。
    """
    rows = [
        {"细分市场": f"S{i}", "share_cy": 0.1 + i / 100, "share_ly": 0.09 + i / 100, "share_cy_pct": 10 + i, "delta_pp": i / 10} for i in range(11)
    ]
    cols = _cols(
        ("细分市场", "categorical"),
        ("share_cy", "numeric"),
        ("share_ly", "numeric"),
        ("share_cy_pct", "numeric"),
        ("delta_pp", "numeric"),
    )
    spec = infer_chart_spec(rows, cols, hint="compare", title="t")
    assert spec.kind != "table", spec._infer_reason
    assert sum(1 for c in spec.columns if c.type == "numeric") == 4, "四个序列都该留在图上"


# ── Order parent-child hierarchies by cardinality, not column order (2026-09-03 same-family audit) ────────────────


def test_parent_child_order_by_cardinality_not_column_order() -> None:
    """★★ 取数侧给的列序常常是反的：`门店(40) / 效能象限(4)`，真层级是象限 → 门店。

    旧实现按列序传 `(cat_cols[0], cat_cols[1])` ⇒ 「每个象限只属于一个门店」不成立
    ⇒ 判否 ⇒ 掉到高基数分支出 **40×4 的热力图**，而标题（`门店坪效 vs 利润率四象限`）
    要的是按象限堆叠。116 语料实测 44 张走这条规则的图里 **7 张**是这个形状。
    """
    # ⚠ 28 rows within the display cap: test parent-child ordering without also testing truncation.
    # The old 40-row fixture capped to 30; the rule forbidding stacking after truncation now removes stacking,
    # failing this test without an ordering error. Separate the two cases (see next test).
    # Corpus evidence: only one of 31 stacked charts truncated rows; genuine parent-child stacked charts stay within the cap.
    rows = [{"门店": f"S{i}", "效能象限": f"Q{i % 4}", "坪效": 100 + i} for i in range(28)]
    cols = _cols(("门店", "categorical"), ("效能象限", "categorical"), ("坪效", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="门店坪效 vs 利润率四象限")
    assert spec.kind == "stacked_bar", spec._infer_reason
    assert "parent-child" in (spec._infer_reason or "")


def test_a_row_truncated_stack_stops_being_a_stack() -> None:
    """★★ 行被截之后柱高就不是合计了 —— 不许再堆叠。

    116 实测 `t_664e64068487`：同一份报告里饼图说上海 37.71 亿，堆叠柱的上海只有 26.66 亿；
    451 行被砍到 30 行，**柱高整体少 46%，各省还缺得不一样多**（河南少 85%，
    9.3% 的份额塌成 2.5%）。图注说「仅显示前 30 项」对行数诚实，可用户读的是柱高。
    同一条原则 `_cap_time_series` 早就写了（`area_stacked → line`），只差行截断这一侧。
    """
    rows = [{"门店": f"S{i}", "效能象限": f"Q{i % 4}", "坪效": 100 + i} for i in range(40)]
    cols = _cols(("门店", "categorical"), ("效能象限", "categorical"), ("坪效", "numeric"))

    spec = infer_chart_spec(rows, cols, hint="raw", title="门店坪效 vs 利润率四象限")

    assert spec.top_n is not None and spec.top_n < spec.row_count, "前提：这份数据确实被行截了"
    assert spec.kind == "bar", f"行截过还在堆叠：{spec._infer_reason}"
    assert "不再是真合计" in (spec._infer_reason or "")


def test_constant_column_is_never_a_parent() -> None:
    """A constant filter dimension is not a parent hierarchy, even when every child maps to it."""
    from bowhead_core.chart.infer import _order_parent_child

    rows = [{"产品名称": "示例产品乙", "医疗机构": f"H{i}", "金额": 100 + i} for i in range(20)]
    cols = _cols(("产品名称", "categorical"), ("医疗机构", "categorical"), ("金额", "numeric"))
    assert _order_parent_child(rows, cols[0], cols[1]) is None
    # End to end: no stacked bar chart with all 20 bars labeled 示例产品乙.
    spec = infer_chart_spec(rows, cols, hint="raw", title="示例产品乙 Top 20 医院")
    assert "parent-child" not in (spec._infer_reason or ""), spec._infer_reason


def test_real_hierarchy_still_detected_either_way() -> None:
    """正对照：真层级（省 → 市）无论列序怎么给都要认出来。"""
    from bowhead_core.chart.infer import _order_parent_child

    rows = [
        {"省": "河北", "市": "石家庄", "GMV": 100},
        {"省": "河北", "市": "保定", "GMV": 80},
        {"省": "山东", "市": "济南", "GMV": 120},
        {"省": "山东", "市": "青岛", "GMV": 110},
    ]
    prov = ChartColumn(key="省", header="省", type="categorical")
    city = ChartColumn(key="市", header="市", type="categorical")
    for a, b in ((prov, city), (city, prov)):
        got = _order_parent_child(rows, a, b)
        assert got is not None, (a.key, b.key)
        assert got[0].key == "省", f"parent 必须是基数小的那个，得到 {got[0].key}"


# ── Move constant dimension columns to the end (2026-09-03 dogfood on 116) ──────────────────────────
#
# Every FE builder chooses x via spec.columns.find(date|categorical|geo),
# the first dimension column by order. Query results often put filter dimensions (constant columns) before grouping dimensions,
# making every x-axis label identical. Observed 9/2121 in the 116 corpus.


def _x_col(spec) -> str | None:
    """复刻 `build-config.ts`：列序第一个 date|categorical|geo 就是 x 轴。"""
    return next((c.key for c in spec.columns if c.type in ("date", "categorical", "geo")), None)


def test_constant_dim_sinks_so_x_axis_can_tell_rows_apart() -> None:
    """Move constant dimensions behind useful labels in the emitted chart columns."""
    rows = [{"产品名称": "示例产品甲", "医疗机构": f"H{i}", "金额": 100 + i} for i in range(20)]
    cols = _cols(("产品名称", "categorical"), ("医疗机构", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="示例产品乙 Top 20 医院")
    x = _x_col(spec)
    assert x == "医疗机构", [c.key for c in spec.columns]
    assert len({str(r[x]) for r in rows}) == 20  # One label per row


def test_constant_date_dim_is_not_a_time_axis() -> None:
    """★ 常量的 **date** 列同病：`2025年各省月度刷卡金额走势` 只有一个月、50 个省，
    却按「time detected」出折线，横轴 50 个点全叫「每月」。

    规则 0a 原来只认 `categorical`，date/geo 漏在外面。
    """
    rows = [{"每月": "2025-06", "商户所在省": f"P{i}", "刷卡金额": 1000 + i} for i in range(50)]
    cols = _cols(("每月", "date"), ("商户所在省", "categorical"), ("刷卡金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="2025年各省月度刷卡金额走势")
    assert _x_col(spec) == "商户所在省", [c.key for c in spec.columns]
    assert spec.kind != "line", spec._infer_reason  # A single month cannot show a trend.


def test_force_kind_also_gets_the_reordered_columns() -> None:
    """The early force_kind branch must preserve the same column ordering."""
    rows = [{"产品线": "ONC", "大区": f"R{i}", "销量": 100 + i} for i in range(6)]
    cols = _cols(("产品线", "categorical"), ("大区", "categorical"), ("销量", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="table")
    assert spec.kind == "table"
    assert _x_col(spec) == "大区", [c.key for c in spec.columns]


def test_discriminating_dims_keep_their_order() -> None:
    """负对照：两个维度都能区分行时**一列都不许动**（省 → 市 的层级靠列序表达）。"""
    rows = [{"省份": f"P{i // 5}", "城市": f"C{i}", "销量": 10 + i} for i in range(20)]
    cols = _cols(("省份", "categorical"), ("城市", "categorical"), ("销量", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t")
    assert [c.key for c in spec.columns] == ["省份", "城市", "销量"]


def test_all_dims_constant_is_left_to_the_guard() -> None:
    """负对照：唯一的维度列就是常量时无处可换 —— 不改形状，交给
    `indistinguishable_rows_reason` 报警（它兜的正是「真的一个能区分的列都没有」）。
    """
    from bowhead_core.chart.infer import _constant_dim_keys

    rows = [{"每月": "2026年8月", "转化率": 0.1 + i} for i in range(20)]
    cols = _cols(("每月", "date"), ("转化率", "numeric"))
    assert _constant_dim_keys(rows, cols) == set()
    spec = infer_chart_spec(rows, cols, hint="rank", title="2026年8月各门店到访转化率排名")
    assert [c.key for c in spec.columns] == ["每月", "转化率"]


def test_numeric_column_order_is_never_touched() -> None:
    """数值列的次序 = 调色板次序，动了就把线的颜色换掉了。"""
    rows = [{"产品线": "ONC", "大区": f"R{i}", "今年": 100 + i, "去年": 90 + i} for i in range(6)]
    cols = _cols(("产品线", "categorical"), ("大区", "categorical"), ("今年", "numeric"), ("去年", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t")
    assert [c.key for c in spec.columns if c.type == "numeric"] == ["今年", "去年"]


def test_constant_dims_judged_on_full_rows_not_the_truncated_ones() -> None:
    """★ 截断后「碰巧同值」的列比全量多——判据必须按**全量** rows 算。

    `hint=rank` 会先按值取 top-N（`_display_row_cap`）。这里 `批次` 在全量里有两个值，
    但高值那些行全是 `B1` ⇒ 若在截断后判，`批次` 会被误当成 filter 痕迹沉掉。
    """
    rows = [{"批次": "B1" if i < 40 else "B2", "门店": f"S{i}", "销量": 1000 - i} for i in range(60)]
    cols = _cols(("批次", "categorical"), ("门店", "categorical"), ("销量", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="t")
    assert [c.key for c in spec.columns] == ["批次", "门店", "销量"]


# ─────────────────────────────────────────────────────────────
# Formatted numbers misclassified as dimensions (chart-config-ux §21)
#
# float("24,303") / float("15.37%") raises immediately, leaving string columns classified as categorical.
# Consequence: not aesthetics, but permanently excluded value axes. Observed in 16 of 1948 charts on 116;
# seven became structurally undrawable with only one numeric column left.
# ─────────────────────────────────────────────────────────────

_AREA_CSV = '门店,门店面积,营业额\n东湖街,"23,817",112846305\n杏林,"17,264",38190427\n新纪城,"13,952",49630718\n'


def test_thousands_separator_column_is_numeric():
    """★ 千分位逗号：116 真例 `门店面积` = 23,817 —— 是面积不是类目名."""
    from bowhead_core.canvas_tools import _parse_csv_with_types

    rows, cols = _parse_csv_with_types(_AREA_CSV.encode("utf-8"))
    assert [c.type for c in cols] == ["categorical", "numeric", "numeric"]
    assert [r["门店面积"] for r in rows] == [23817, 17264, 13952]


def test_thousands_separator_unblocks_scatter():
    """★ 真正的代价在这里：少一个数值列，散点图直接画不出来。"""
    from bowhead_core.canvas_tools import _parse_csv_with_types

    _, cols = _parse_csv_with_types(_AREA_CSV.encode("utf-8"))
    assert sum(1 for c in cols if c.type == "numeric") == 2


def test_percent_string_column_is_numeric_points():
    """★ 百分号：`2026年1-6月退款率月度趋势` 要画的那条率本身不是数值列。

    还原成 15.37 + `unit='%'`（points 档），**不除以 100**——前端的
    fraction/points 两档正是靠 unit + 值域分的。
    """
    from bowhead_core.canvas_tools import _parse_csv_with_types

    csv = "月份,退款率\n1月,15.37%\n2月,18.33%\n3月,16.13%\n"
    rows, cols = _parse_csv_with_types(csv.encode("utf-8"))
    by_key = {c.key: c for c in cols}
    assert by_key["退款率"].type == "numeric"
    assert by_key["退款率"].unit == "%"
    assert [r["退款率"] for r in rows] == [15.37, 18.33, 16.13]


def test_bare_digit_strings_stay_a_category_axis():
    """★ 反向钉死：裸数字串不在判据里。

    `month` = 1/2/3、`排名` = 1/2/3 当类目轴才是对的。这条判据只认**带格式标记**的
    （逗号 / 百分号），没有标记就不动——否则会把 x 轴弄没。
    """
    from bowhead_core.canvas_tools import _coerce_formatted_numeric_columns

    rows = [{"档位": "1"}, {"档位": "2"}, {"档位": "3"}]
    assert _coerce_formatted_numeric_columns(rows, ["档位"]) == {}
    assert [r["档位"] for r in rows] == ["1", "2", "3"]


def test_mixed_column_is_left_alone():
    """★ 整列生效、不逐格生效：混着就说明它不是一列数。"""
    from bowhead_core.canvas_tools import _coerce_formatted_numeric_columns

    rows = [{"c": "24,303"}, {"c": "未开业"}, {"c": "16,918"}]
    assert _coerce_formatted_numeric_columns(rows, ["c"]) == {}
    assert [r["c"] for r in rows] == ["24,303", "未开业", "16,918"]


def test_decimal_comma_is_not_treated_as_thousands():
    """★ `1,5` 这种（某些地区的小数点）不匹配分组式样，原样留着。"""
    from bowhead_core.canvas_tools import _coerce_formatted_numeric_columns

    rows = [{"c": "1,5"}, {"c": "2,7"}, {"c": "3,9"}]
    assert _coerce_formatted_numeric_columns(rows, ["c"]) == {}


# ─────────────────────────────────────────────────────────────
# Scatter must not use the cap for readable bar counts (chart-config-ux §22).
#
# 116 dogfood s_71e3a520634e (1.2.23): 39 stores' 门店面积 vs 营业额
# persisted as top_n=30 / row_count=39 / inline=30, with subtitle 「39 stores · r=0.54」;
# body-mentioned 五棵松 (1,135 m²) and 花园北路 (1,655 m²) were absent from the chart (x starts at 6K).
# ─────────────────────────────────────────────────────────────


def _area_revenue_rows(n: int) -> list[dict[str, float | str]]:
    # Ascending area: value-based top-N cuts off all small-area rows, exactly this defect's shape.
    return [{"门店": f"店{i:02d}", "门店面积": 1000.0 + i * 600, "营业额": 5e6 + i * 3e6} for i in range(n)]


def _area_revenue_cols() -> list[ChartColumn]:
    return [
        ChartColumn(key="门店", header="门店", type="categorical"),
        ChartColumn(key="门店面积", header="门店面积", type="numeric"),
        ChartColumn(key="营业额", header="营业额", type="numeric", unit="元"),
    ]


def test_scatter_keeps_every_point():
    """★ 39 个点全画出来，不打「仅显示前 30」."""
    rows = _area_revenue_rows(39)
    spec = infer_chart_spec(rows, _area_revenue_cols(), hint="correlation", title="门店面积 vs 营业额", force_kind="scatter")
    assert spec.kind == "scatter"
    assert len(spec.rows or []) == 39
    assert spec.row_count == 39
    assert spec.top_n is None


def test_scatter_keeps_the_low_end_of_the_axis():
    """★★ 真正的代价：按值取 top-N 削的是**坐标轴的一整段**。

    最小面积那家店必须还在——正文引用的就是它。
    """
    rows = _area_revenue_rows(39)
    spec = infer_chart_spec(rows, _area_revenue_cols(), hint="correlation", title="门店面积 vs 营业额", force_kind="scatter")
    areas = [r["门店面积"] for r in spec.rows or []]
    assert min(areas) == 1000.0
    assert max(areas) == 1000.0 + 38 * 600


def test_vertical_bar_is_still_truncated():
    """★ 反向钉死：竖柱的类目在横轴，行多只能挤，照旧要截。

    ⚠ 只留**一个**数值列：两个量纲族 + 百倍跨度会被 `_upgrade_single_axis` 升成
    双轴（那是 ⑮ 在干正事），拿它当竖柱的对照会验错东西。
    """
    rows = [{"门店": f"店{i:02d}", "营业额": 5e6 + i * 3e6} for i in range(39)]
    cols = [
        ChartColumn(key="门店", header="门店", type="categorical"),
        ChartColumn(key="营业额", header="营业额", type="numeric", unit="元"),
    ]
    spec = infer_chart_spec(rows, cols, hint="rank", title="门店营业额排名", force_kind="bar")
    assert spec.kind == "bar"
    assert len(spec.rows or []) == 30
    assert spec.top_n == 30
    assert spec.row_count == 39


# ─────────────────────────────────────────────────────────────
# Integer-form time ticks are not measures (chart-config-ux §23).
#
# _infer_col_type first tests all-numeric values → numeric, before reading the column name.
# Matched 21 of 1948 charts in the 116 corpus, zero false positives; three charts plotted month indices themselves.
# ─────────────────────────────────────────────────────────────


def test_month_index_no_longer_eats_the_real_metric():
    """★★ 116 真例：`逐月营业额趋势` 落库只剩 month_num，图上一条 1→6 的斜线。

    根因链：`month_num` 判成 numeric ⇒ 裁列按 `true_nums[0]`（列序第一个）挑主指标
    ⇒ 真的营业额被顶掉。改判之后主指标才轮得到。
    """
    from bowhead_core.canvas_tools import _parse_csv_with_types

    csv = "month,month_num,营业额\n2026年1月,1,1989735625\n2026年2月,2,1774358584\n2026年3月,3,1416245742\n"
    _, cols = _parse_csv_with_types(csv.encode("utf-8"))
    by_key = {c.key: c for c in cols}
    assert by_key["month_num"].type != "numeric"
    assert [c.key for c in cols if c.type == "numeric"] == ["营业额"]


def test_year_column_is_a_grouping_not_a_series():
    """★ 116 真例：`Q2 Net Margin: 2026 vs 2025` 的左轴画的是 Year=[2026,2026,…]。

    表里已经有别的时间列（Month）⇒ 年份改判成 categorical 当分组维度，
    **不能给 date**：那会让它按「date 优先」抢走 Month 的 x 轴。
    """
    from bowhead_core.canvas_tools import _parse_csv_with_types

    csv = (
        "Month,Year,Sales\n"
        "April,2026,1386522142\nMay,2026,1295042731\nJune,2026,1254877473\n"
        "April,2025,1387755975\nMay,2025,1285978478\nJune,2025,1257034858\n"
    )
    _, cols = _parse_csv_with_types(csv.encode("utf-8"))
    by_key = {c.key: c for c in cols}
    assert by_key["Year"].type == "categorical"
    assert [c.key for c in cols if c.type == "numeric"] == ["Sales"]


def test_lone_time_index_becomes_the_time_axis():
    """★ 表里没有别的时间列时给 date —— `月销量趋势` 的 `月份` 就该是时间轴。"""
    from bowhead_core.canvas_tools import _parse_csv_with_types

    lines = ["月份,实际销量(箱),拟合值(箱)"] + [f"{m},{100 + m * 7},{101 + m * 7}" for m in range(1, 13)]
    _, cols = _parse_csv_with_types("\n".join(lines).encode("utf-8"))
    assert {c.key: c.type for c in cols}["月份"] == "date"


def test_compound_name_measures_are_not_touched():
    """★★ 反向钉死误杀：复合名是真度量，哪怕取值恰好落在时间域。

    116 语料里 `本月门店数` 的取值正好在 1900-2100 —— 只靠值域判会把它杀掉，
    所以列名必须整串锚定。
    """
    from bowhead_core.canvas_tools import _parse_csv_with_types

    csv = "省,本月门店数,月坪效\n上海,2025,4852\n北京,1998,5120\n广东,2040,4410\n"
    _, cols = _parse_csv_with_types(csv.encode("utf-8"))
    types = {c.key: c.type for c in cols}
    assert types["本月门店数"] == "numeric"
    assert types["月坪效"] == "numeric"


def test_out_of_range_values_are_not_a_time_index():
    """★ 名字对但取值超出该单位合法域 ⇒ 不是时间刻度（`月份=88` 只能是别的东西）。"""
    from bowhead_core.canvas_tools import _parse_csv_with_types

    csv = "省,月份\n上海,88\n北京,91\n广东,77\n"
    _, cols = _parse_csv_with_types(csv.encode("utf-8"))
    assert {c.key: c.type for c in cols}["月份"] == "numeric"


def test_explicit_column_types_still_win():
    """★ 模型显式声明的 `column_types` 优先——这道改判是兜底，不是仲裁。"""
    from bowhead_core.canvas_tools import _parse_csv_with_types

    csv = "省,年份\n上海,2025\n北京,2026\n广东,2025\n"
    _, cols = _parse_csv_with_types(csv.encode("utf-8"), type_overrides={"年份": "numeric"})
    assert {c.key: c.type for c in cols}["年份"] == "numeric"


# ── force_kind's unit-family gate runs one step too early (observed on 116 1.3.47) ───────────────────────


def _revenue_vs_yoy_rows() -> list[dict]:
    """116 `2026年月度营业额及同比趋势` 的真实形状：金额 9e8 量级 + 同比变化率 0.2 量级。"""
    return [{"月份": f"2026年{m}月", "营业额 / 元": 5.1e8 + m * 9e7, "营业额同比变化率_pct": (-0.545 + m * 0.13)} for m in range(1, 10)]


def _revenue_vs_yoy_cols() -> list[ChartColumn]:
    return [
        ChartColumn(key="月份", header="月份", type="date"),
        ChartColumn(key="营业额 / 元", header="营业额 / 元", type="numeric"),
        ChartColumn(key="营业额同比变化率_pct", header="营业额同比变化率_pct", type="numeric"),
    ]


def test_force_kind_cannot_flatten_a_rate_that_only_the_upgrade_would_have_split() -> None:
    """★★ 量纲闸此前问早了一步：它在内层 `_infer_from_rows` 里看 `auto.kind`，
    而升双轴（`_upgrade_single_axis`）跑在外层 `infer_chart_spec`。

    于是「靠升轴才变成 dual_axis」的那一族——时间轴：规则 4 先出 `line`，跨度够大才升——
    闸永远判不出来。116 实测（1.3.47 部署版）：营业额 9.15e8 + 同比变化率 0.267、
    跨度 **34 亿倍**，不带 `force_kind` 时外层给 dual_axis 且轴派得对；
    带 `force_kind="combo"` 就回到单轴，那条百分比在 9 亿量级的轴上是一条贴零直线。
    """
    rows, cols = _revenue_vs_yoy_rows(), _revenue_vs_yoy_cols()

    auto = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x")
    forced = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="combo")

    assert auto.kind == "dual_axis", "前提：不强制时自动路本来就该出双轴"
    assert forced.kind == "dual_axis", f"force_kind=combo 把两个量纲组按回了一根轴：{forced._infer_reason}"
    axes = {c.key: c.y_axis_id for c in forced.columns if c.type == "numeric"}
    assert axes["营业额 / 元"] == 0 and axes["营业额同比变化率_pct"] == 1, axes


def test_the_guard_still_leaves_same_family_forced_kinds_alone() -> None:
    """★ 负对照：两列同族时 `force_kind` 照旧说了算——闸只管「跨量纲族」这一种。
    同族劈成两根轴会让 10 亿和 1 亿画得一样高，那是另一个更糟的谎。"""
    rows = [{"月份": f"2026年{m}月", "营业额": 1e9 + m * 1e8, "利润": 2e8 + m * 1e7} for m in range(1, 10)]
    cols = [
        ChartColumn(key="月份", header="月份", type="date"),
        ChartColumn(key="营业额", header="营业额", type="numeric", unit="元"),
        ChartColumn(key="利润", header="利润", type="numeric", unit="元"),
    ]

    forced = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="combo")

    assert forced.kind == "combo"
    assert {c.y_axis_id for c in forced.columns if c.type == "numeric"} == {0}


def test_a_long_label_hbar_still_wins_because_the_frontend_has_no_horizontal_dual() -> None:
    """★ 第二个负对照：类目名很长时 hbar 保留 —— `buildDualAxis` 没有横向模式，
    升了就把「名字太长才走横条」这个理由丢了。这条例外是**有意的**，
    真解法是前端加横向双轴（见 `_infer_from_rows` 里那段注释），不在这次范围。
    """
    rows = [{"品牌": f"非常长的品牌名称示例-{i:02d}", "营业额": 1e7 + i * 1e5, "利润率": 0.1 + i * 0.01} for i in range(12)]
    cols = [
        ChartColumn(key="品牌", header="品牌", type="categorical"),
        ChartColumn(key="营业额", header="营业额", type="numeric"),
        ChartColumn(key="利润率", header="利润率", type="numeric"),
    ]

    forced = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="hbar")

    assert forced.kind == "hbar"


def test_two_families_that_are_both_visible_are_left_on_one_axis() -> None:
    """★★ 跨量纲族**不等于**要劈轴。116 实测 09-11 之后 16 张「跨族同轴」里
    **11 张根本没问题**：`营业额_亿元[9.8..19.9]` + `环比变化率[-22..38]`，
    百分比那条的幅度比金额还大，两条都看得清清楚楚 —— 这时劈一根轴纯属添乱
    （`_FLAT_SPAN` 就是为挡住这一档而存在的）。

    第一版只测了「跨度 34 亿倍」那一档，把 `_FLAT_SPAN` 从 100 改成 1 的突变**存活**了。
    """
    rows = [{"月份": f"2026年{m}月", "营业额_亿元": 9.8 + m * 1.1, "环比变化率": -22.0 + m * 6.0} for m in range(1, 10)]
    cols = [
        ChartColumn(key="月份", header="月份", type="date"),
        ChartColumn(key="营业额_亿元", header="营业额_亿元", type="numeric"),
        ChartColumn(key="环比变化率", header="环比变化率", type="numeric"),
    ]

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x")

    assert spec.kind == "line", f"两条都看得见，不该升双轴：{spec._infer_reason}"
    assert {c.y_axis_id for c in spec.columns if c.type == "numeric"} == {0}


def test_same_family_is_never_split_even_when_the_span_is_huge() -> None:
    """★ 同族**一律不分**，跨度再大也不分 —— 分了之后相对高低就失真。
    真机逮到过最刺眼的一例是「会员客流 vs 总客流」（子集与全集画得一样高）。

    第一版的同族对照跨度只有 5 倍，被 `_FLAT_SPAN` 先挡住了，
    于是「拿掉量纲族早退」的突变**存活**——那条早退根本没被测到。
    """
    rows = [{"月份": f"2026年{m}月", "营业额": 1e9 + m * 1e8, "赠品成本": 1e6 + m * 1e5} for m in range(1, 10)]
    cols = [
        ChartColumn(key="月份", header="月份", type="date"),
        ChartColumn(key="营业额", header="营业额", type="numeric", unit="元"),
        ChartColumn(key="赠品成本", header="赠品成本", type="numeric", unit="元"),
    ]

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x")

    assert spec.kind == "line", f"同族被劈了轴：{spec._infer_reason}"
    assert {c.y_axis_id for c in spec.columns if c.type == "numeric"} == {0}


def test_force_kind_cannot_squeeze_three_unit_families_onto_shared_axes() -> None:
    """★★ 「两根轴装不下三种量纲」是规则 10 早就写下的结论，而 `force_kind` 一直绕得过去。

    116 实测（1.3.51，09-01 起 831 张 force_kind 图）：**10 张**是 force_kind 画出了
    形状树判「画不出」的图，其中 6 张是这一档。典型
    `各省营业额与利润同比增速` = 营业额 / 营业额同比 / 利润 / 利润同比 —— 四列三族挤一根轴，
    两条百分比贴着零。不带 force_kind 时形状树给的是 table（「3 个量纲族 > 2 → table」）。
    """
    # ★ Deliberately use long category names, making honors_own_reason true (only long names select horizontal bars),
    # so the outer condition relies solely on ≥3 families. Short names also satisfy the other half,
    # letting a mutation removing the ≥3-family override survive (as the first version did).
    rows = [
        {"省份": f"非常长的省级行政区名称示例-{i:02d}", "营业额": 1e9 + i * 1e8, "营业额同比": 0.1 + i * 0.01, "销售量": 5000 + i * 100}
        for i in range(12)
    ]
    cols = [
        ChartColumn(key="省份", header="省份", type="categorical"),
        ChartColumn(key="营业额", header="营业额", type="numeric", unit="元"),
        ChartColumn(key="营业额同比", header="营业额同比", type="numeric"),
        ChartColumn(key="销售量", header="销售量", type="numeric", unit="件"),
    ]
    nums = [c for c in cols if c.type == "numeric"]
    assert len(_axis_groups(rows, nums)) == 3, "前提：这份数据确实是 3 个量纲族"

    for fk in ("hbar", "bar", "combo"):
        spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind=fk)
        assert spec.kind == "table", f"force_kind={fk} 把 3 个量纲族按在了共用值轴上：{spec._infer_reason}"


def test_long_label_hbar_still_wins_when_there_are_only_two_families() -> None:
    """★ 负对照：例外只对 **2 组** 保留。`honors_own_reason`（类目名太长才走横条）
    在 2 组时仍然说了算——前端 `buildDualAxis` 没有横向模式，升了反而更糟。
    这一条在加「≥3 族一律夺权」之后必须**不变**。"""
    rows = [{"品牌": f"非常长的品牌名称示例-{i:02d}", "营业额": 1e7 + i * 1e5, "利润率": 0.1 + i * 0.01} for i in range(12)]
    cols = [
        ChartColumn(key="品牌", header="品牌", type="categorical"),
        ChartColumn(key="营业额", header="营业额", type="numeric"),
        ChartColumn(key="利润率", header="利润率", type="numeric"),
    ]
    assert len(_axis_groups(rows, [c for c in cols if c.type == "numeric"])) == 2

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="hbar")

    assert spec.kind == "hbar"


def test_a_single_family_forced_kind_is_untouched() -> None:
    """★ 第二个负对照：同族多列（营业额 / 利润 / 赠品成本 全是钱）不受影响，
    force_kind 照旧说了算 —— 闸只管「量纲族装不下」这一种。"""
    rows = [{"省份": f"省{i:02d}", "营业额": 1e9 + i * 1e8, "利润": 2e8 + i * 1e7, "成本": 8e8} for i in range(12)]
    cols = [
        ChartColumn(key="省份", header="省份", type="categorical"),
        ChartColumn(key="营业额", header="营业额", type="numeric", unit="元"),
        ChartColumn(key="利润", header="利润", type="numeric", unit="元"),
        ChartColumn(key="成本", header="成本", type="numeric", unit="元"),
    ]

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="bar")

    assert spec.kind == "bar"


# ── No dimension columns ⇒ category axes cannot render names (all ten 116 cases came from force_kind) ──────


def test_force_kind_cannot_draw_a_bar_chart_with_no_labels_at_all() -> None:
    """★★ 116 实录《2026年1-6月品类大类营业额排名 Top12》：`force_kind="hbar"`，
    而数据是 **1 行 1 列**（只有一个总额 91.2 亿）⇒ 画出来是一根**没有标签**的横条，
    标题却承诺了 Top12 排名。同形状今天出现 3 次（还有门店 Top15、品牌 Top30）。

    形状树对它有诚实答案：1 行 1 数值 → `kpi`（数字连着它自己的名字）。
    """
    rows = [{"营业额 / 元": 9104527316.482753}]
    cols = [ChartColumn(key="营业额 / 元", header="营业额 / 元", type="numeric")]

    spec = infer_chart_spec(
        rows=rows,
        columns=cols,
        hint="",
        title="2026年1-6月品类大类营业额排名 Top12",
        subtitle=None,
        data_artifact_id="art_x",
        force_kind="hbar",
    )

    assert spec.kind == "kpi", f"画出了没有标签的横条：{spec._infer_reason}"


def test_a_wide_table_with_no_x_column_does_not_become_a_line_chart() -> None:
    """116 实录《2026年各品类大类月度营业额趋势（折线图）》：9 个品类各一列、
    **月份那一列不在数据里** ⇒ 折线图的 x 轴没有任何刻度名。形状树给 table。"""
    cats = ["女装", "鞋包皮件", "金银珠宝", "护肤美妆用品", "钟表/眼镜", "运动休闲", "日用针纺", "男装", "其他"]
    rows = [{c: 1.0 + i * 0.1 + j for j, c in enumerate(cats)} for i in range(10)]
    cols = [ChartColumn(key=c, header=c, type="numeric") for c in cats]

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="line")

    assert spec.kind == "table", f"没有 x 轴列却画成了折线：{spec._infer_reason}"


def test_scatter_is_not_caught_by_the_no_dimension_rule() -> None:
    """★ 负对照：`scatter` 的 x/y **本来就都是数值**，没有维度列是它的正常形态
    （`hint=correlation + 2 numeric → scatter` 是既有正路）。闸不能顺手把它也拦了。"""
    rows = [{"营业额": 1e8 + i * 1e7, "客流量": 140000 + i * 1000} for i in range(20)]
    cols = [
        ChartColumn(key="营业额", header="营业额", type="numeric"),
        ChartColumn(key="客流量", header="客流量", type="numeric"),
    ]

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="scatter")

    assert spec.kind == "scatter"


def test_force_kind_still_wins_when_a_dimension_column_exists() -> None:
    """★ 第二个负对照：有维度列时 `force_kind` 照旧说了算 —— 闸只管
    「一个标签都渲不出来」这一种，不是给 force_kind 普遍降权。"""
    rows = [{"品类": f"品类{i:02d}", "营业额": 1e8 + i * 1e7} for i in range(12)]
    cols = [
        ChartColumn(key="品类", header="品类", type="categorical"),
        ChartColumn(key="营业额", header="营业额", type="numeric"),
    ]

    spec = infer_chart_spec(rows=rows, columns=cols, hint="", title="t", subtitle=None, data_artifact_id="art_x", force_kind="hbar")

    assert spec.kind == "hbar"


def test_a_row_truncated_area_stack_becomes_a_line_not_a_bar() -> None:
    """★ 面积堆叠退的是 `line`，不是 `bar` —— 它本来就是时间轴上的形态，
    退成柱会把「一段时间」拆成一根根离散的柱。`_cap_time_series` 里那条先例
    （`area_stacked → line`）就是这么定的，行截断这一侧必须跟它一致。

    只测了 `stacked_bar` 的话，「area_stacked 也变 bar」和「集合里漏掉 area_stacked」
    两个突变都会存活（实测如此）。
    """
    # ⚠ Use a category axis rather than time: oversized time axes fall back to tables
    # (an unfitting time series cannot meaningfully truncate time → table), never reaching row truncation;
    # top_n stays None, failing this test's prerequisite rather than its target (as in the first version).
    rows = [{"店": f"S{i}", "象限": f"Q{i % 4}", "额": 100 + i} for i in range(40)]
    cols = _cols(("店", "categorical"), ("象限", "categorical"), ("额", "numeric"))

    spec = infer_chart_spec(rows, cols, hint="raw", title="各象限构成", force_kind="area_stacked")

    assert spec.top_n is not None and spec.top_n < spec.row_count, "前提：确实被行截了"
    assert spec.kind == "line", f"面积堆叠该退成 line：{spec._infer_reason}"
    assert "不再是真合计" in (spec._infer_reason or "")


# ── Stacked bars carry the axis decision in the spec (readability audit I, 2026-10-01) ────────────────────────────


def _roles(spec) -> dict[str, str | None]:
    return {c.key: c.role for c in spec.columns if c.type in ("categorical", "geo")}


def test_stacked_hierarchy_declares_child_as_x_and_parent_as_series() -> None:
    """Hierarchy against column order: the child (12 categories) is x, the parent (2 types) the series.

    Before, the conclusion of `_order_parent_child` was dropped and the frontend took column order, so the same data
    drew either way depending on how the query happened to order its columns.
    """
    rows = [{"品类": f"C{i:02d}", "类型": "新增" if i % 3 else "回流", "营业额": 1000 - i} for i in range(12)]
    for cols in (
        _cols(("品类", "categorical"), ("类型", "categorical"), ("营业额", "numeric")),
        _cols(("类型", "categorical"), ("品类", "categorical"), ("营业额", "numeric")),
    ):
        spec = infer_chart_spec([dict(r) for r in rows], list(cols), hint="raw", title="t", force_kind="stacked_bar")
        assert spec.kind == "stacked_bar", spec._infer_reason
        assert _roles(spec) == {"品类": "x", "类型": "color"}, "column order must not decide the split"
        assert "x=品类 (child), series=类型 (parent)" in (spec._infer_reason or "")


def test_stacked_hierarchy_roles_are_serialized_for_every_renderer() -> None:
    """The roles reach the wire: web normalizer, export freezing and Feishu (`_first_role_key`) all read `role`."""
    rows = [{"Province": f"P{i % 5}", "City": f"City{i:02d}", "UV_YoY": -0.01 * i} for i in range(20)]
    cols = _cols(("Province", "categorical"), ("City", "categorical"), ("UV_YoY", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="City UV YoY")
    wire = {c["key"]: c.get("role") for c in spec_to_dict(spec)["columns"]}
    assert wire == {"Province": "color", "City": "x", "UV_YoY": None}
    _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    # 20 city bars, 5 province series — not 5 bars summing 20 YoY rates.
    assert (shape["categoryCount"], shape["seriesCount"]) == (20, 5)


def test_one_to_one_dimensions_are_not_stacked() -> None:
    """`门店` / `门店面积` (area typed as a category): each store has one area; stacking drew 15 one-bar series."""
    rows = [{"门店": f"S{i:02d}", "门店面积": f"{1000 + 37 * i}㎡", "营业额": 5000 - 10 * i} for i in range(15)]
    cols = _cols(("门店", "categorical"), ("门店面积", "categorical"), ("营业额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="各门店面积排名 Top 15", force_kind="stacked_bar")
    assert spec.kind == "bar", spec._infer_reason
    assert "one-to-one" in (spec._infer_reason or "")
    assert all(role is None for role in _roles(spec).values()), "a plain bar declares no series dimension"


def test_crossed_dimensions_keep_column_order() -> None:
    """Negative control: a genuinely crossed store × category grid is not a hierarchy; nothing changes."""
    rows = [{"门店": f"S{s}", "种类大类": f"K{k}", "营业额": 100 * s + k} for s in range(5) for k in range(6)]
    cols = _cols(("门店", "categorical"), ("种类大类", "categorical"), ("营业额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="stacked_bar")
    assert spec.kind == "stacked_bar"
    assert all(role is None for role in _roles(spec).values())
    assert "child" not in (spec._infer_reason or "")


def test_stack_dims_are_judged_on_the_plotted_rows_not_the_inline_slice() -> None:
    """59 provinces × 12 months with the region rotating monthly: the first 50 inline rows (one month) look like a
    province -> region hierarchy, the plotted rows do not. Judging the slice would declare a hierarchy that is false."""
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    rows = [{"每月": m, "省": f"省{p:02d}", "大区": f"R{(p + i) % 4}", "金额": 1.0 + p} for i, m in enumerate(months) for p in range(59)]
    cols = _cols(("每月", "date"), ("省", "categorical"), ("大区", "categorical"), ("金额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="stacked_bar")
    assert all(role is None for role in _roles(spec).values()), spec._infer_reason


# ── A mostly empty heatmap is a list, not a cross table (readability audit item 2, 2026-10-01) ─────────────────────


def test_one_cell_per_row_heatmap_becomes_bars_coloured_by_group() -> None:
    """`关店建议：综合评分排名`: 23 stores × 3 levels, one filled cell per store (fill 33% of a 23×3 grid is exactly one
    per row). Forced heatmap or not, it is drawn as one bar per store coloured by level."""
    levels = ["建议立即关闭"] * 3 + ["考虑关闭"] * 10 + ["观察"] * 10
    rows = [{"门店": f"S{i:02d}", "综合评分": 35.0 - i, "建议层级": level} for i, level in enumerate(levels)]
    cols = _cols(("门店", "categorical"), ("综合评分", "numeric"), ("建议层级", "categorical"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="关店建议：综合评分排名", force_kind="heatmap")
    assert spec.kind == "stacked_bar", spec._infer_reason
    assert "a list, not a cross table" in (spec._infer_reason or "")
    assert {c.key: c.role for c in spec.columns if c.type == "categorical"} == {"门店": "x", "建议层级": "color"}


def test_sparse_crossed_heatmap_is_not_stacked() -> None:
    """Counter-example (review): 10 stores × 8 channels of refund rate at 29% fill is crossed, not a hierarchy.
    Stacking it would sum each store's rates into one bar; it stays a heatmap."""
    rows = [
        {"门店": f"S{s}", "渠道": f"C{c}", "退款率": 0.01 * (s + c)} for s in range(10) for c in range(8) if (s * 3 + c) % 7 < 2 or (s == 0 and c < 3)
    ]
    cells = {(r["门店"], r["渠道"]) for r in rows}
    assert 0.2 < len(cells) / 80 < 1 / 3, "premise: sparse"
    assert not all(sum(1 for r in rows if r["门店"] == f"S{s}") == 1 for s in range(10)), "premise: not one cell per row"
    cols = _cols(("门店", "categorical"), ("渠道", "categorical"), ("退款率", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="heatmap")
    assert spec.kind == "heatmap", spec._infer_reason


def test_brand_by_category_heatmap_stays_a_heatmap() -> None:
    """The case that first motivated the stacked conversion: 29 brands × 7 categories, 30 filled cells (15%). One
    brand sells in two categories, so brand does not determine category; it is a sparse cross table, not a
    one-cell-per-row hierarchy, and it stays a heatmap (stacking would sum a brand's categories into one bar)."""
    rows = [{"品牌": f"B{i:02d}", "种类大类": f"K{i % 7}", "营业额": 1000.0 - i} for i in range(29)]
    rows.append({"品牌": "B00", "种类大类": "K3", "营业额": 50.0})
    assert len({r["种类大类"] for r in rows if r["品牌"] == "B00"}) == 2, "premise: not functional"
    cols = _cols(("品牌", "categorical"), ("种类大类", "categorical"), ("营业额", "numeric"))
    for force_kind in (None, "heatmap"):
        spec = infer_chart_spec(rows, cols, hint="raw", title="品牌×种类大类 营业额分布", force_kind=force_kind)
        assert spec.kind == "heatmap", (force_kind, spec._infer_reason)


def test_one_cell_per_row_heatmap_with_a_date_dimension_stays_a_heatmap() -> None:
    """Counter-example (review): each store opened in exactly one month (month determines nothing, store determines
    the month). The stacked-bar path only reads categorical/geo dimensions, so converting would drop the month and
    collapse the chart; it stays a heatmap."""
    months = [f"2026-{m:02d}" for m in range(1, 7)]
    rows = [{"开业月份": months[i % 6], "门店": f"S{i:02d}", "首月营业额": 100.0 + i} for i in range(18)]
    cols = _cols(("开业月份", "date"), ("门店", "categorical"), ("首月营业额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="heatmap")
    assert spec.kind == "heatmap", spec._infer_reason


def test_dense_cross_table_stays_a_heatmap() -> None:
    """Negative control: a filled 20 × 5 grid is exactly what a heatmap is for."""
    rows = [{"门店": f"S{s:02d}", "月份": f"M{m}", "营业额": float(s * 10 + m)} for s in range(20) for m in range(5)]
    cols = _cols(("门店", "categorical"), ("月份", "categorical"), ("营业额", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="raw", title="t", force_kind="heatmap")
    assert spec.kind == "heatmap", spec._infer_reason


# ── A Sankey must draw a flow (readability audit item 1, 2026-10-01) ─────────────────────────────────────────────


def test_unconnected_links_are_not_a_flow() -> None:
    """A synthetic funnel: three stage -> label rows mixing visits and yuan; no node shared by two links."""
    rows = [
        {"stage": "客流量 (Traffic)", "value": 1000000.0, "label": "100%"},
        {"stage": "会员客流 (Member Traffic)", "value": 20000.0, "label": "2%"},
        {"stage": "会员营业额 Member Revenue (¥)", "value": 300000000.25, "label": "50% of total"},
    ]
    cols = _cols(("stage", "categorical"), ("value", "numeric"), ("label", "categorical"))
    spec = infer_chart_spec(rows, cols, hint="flow", title="Member Funnel")
    assert spec.kind == "table", spec._infer_reason
    assert "share no node" in (spec._infer_reason or "")


def test_a_user_path_with_unmodelled_entries_stays_a_sankey() -> None:
    """Review: visitors joining mid-path make a node emit more than it receives. That is an honest flow; a
    conservation rule would have turned it into a table."""
    rows = [
        {"from": "首页", "to": "商品页", "v": 1000.0},
        {"from": "商品页", "to": "加购", "v": 2600.0},
        {"from": "商品页", "to": "离开", "v": 900.0},
    ]
    cols = _cols(("from", "categorical"), ("to", "categorical"), ("v", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="flow", title="t")
    assert spec.kind == "sankey", spec._infer_reason


def test_conserving_flow_stays_a_sankey() -> None:
    """Negative control: a funnel that loses volume at every stage is exactly what a Sankey is for."""
    rows = [
        {"from": "访问", "to": "加购", "v": 1000.0},
        {"from": "访问", "to": "流失", "v": 9000.0},
        {"from": "加购", "to": "下单", "v": 600.0},
        {"from": "加购", "to": "放弃", "v": 400.0},
    ]
    cols = _cols(("from", "categorical"), ("to", "categorical"), ("v", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="flow", title="t")
    assert spec.kind == "sankey", spec._infer_reason
