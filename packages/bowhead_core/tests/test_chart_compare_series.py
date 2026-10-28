"""对比图必须有两条序列（116 prod 1.0.79 dogfood 观察 B）。

实测形态（会话 `s_ca475963b4d1`）：peer 对**同一份 18 列宽表**（`art_e24d143619aa`，含
`营业额` / `利润` / `营业额_6月` / `利润_6月` / `营业额环比变化` …）连开 4 张图，只靠
`title` + `hint` 区分。后端 `_prune_cols_for_hint` 每次都裁成 `(省, 第一个数值列)`，于是：

| 图表标题                              | hint    | 实际画出的列   |
| ------------------------------------- | ------- | -------------- |
| 2026年7月各省营业额排名               | rank    | 省, 营业额     |
| 各省营业额与利润对比（7月 vs 6月）    | compare | 省, 营业额 ←同 |
| 各省营业额与利润环比变化（6月→7月）   | compare | 省, 营业额 ←同 |
| 2026年7月各省净利率排名               | rank    | 省, 净利率     |

三张图长得一模一样，**标题承诺了对比，数据里没有对比列**。

两层根因，两处都修了：
1. `compare` 掉进通用分支被裁成单列——对比图按定义至少要两条序列（`correlation` 早有特例，
   `compare` 漏了）。
2. 模型**根本没有办法表达「画哪几列」**：契约里只有 `sort_by` 能挑一列。新增 `series`。
"""

from __future__ import annotations

import pytest
from bowhead_core.canvas_tools import _explicit_series, _prune_cols_for_hint, _sibling_numeric
from bowhead_core.chart.infer import infer_chart_spec
from bowhead_core.chart.types import ChartColumn

# Key columns from the wide table on 116 (actual order)
_WIDE_KEYS = [
    ("省", "geo"),
    ("营业额", "numeric"),
    ("利润", "numeric"),
    ("客流量", "numeric"),
    ("有效门店数", "numeric"),
    ("营业额_6月", "numeric"),
    ("利润_6月", "numeric"),
    ("净利率", "numeric"),
]


@pytest.fixture
def cols() -> list[ChartColumn]:
    return [ChartColumn(key=k, header=k, type=t) for k, t in _WIDE_KEYS]


@pytest.fixture
def rows() -> list[dict]:
    return [
        {"省": "上海", "营业额": 3.09e8, "利润": 5.6e7, "客流量": 498372, "有效门店数": 11, "营业额_6月": 3.96e8, "利润_6月": 7.3e7, "净利率": 18.2},
        {"省": "北京", "营业额": 3.06e8, "利润": 5.2e7, "客流量": 523920, "有效门店数": 13, "营业额_6月": 3.80e8, "利润_6月": 6.4e7, "净利率": 16.9},
    ]


def _keys(cols_out: list[ChartColumn]) -> list[str]:
    return [c.key for c in cols_out]


# ── compare must retain two series ────────────────────────────────────────────────────


def test_compare_keeps_two_series(rows, cols) -> None:
    """★ 核心：修复前这里只剩 `['省','营业额']`，与旁边的排名图完全相同。"""
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="compare", sort_by=None)
    assert _keys(out) == ["省", "营业额", "营业额_6月"]


def test_compare_pairs_sibling_not_neighbour(rows, cols) -> None:
    """★ 第二条序列取**同族列**而不是列表里的下一个。

    按顺序取会把「营业额 vs 6月营业额」配成「营业额 vs 利润」——量纲不同，
    画在一张图上比只画一条还误导。
    """
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="compare", sort_by="利润")
    assert _keys(out) == ["省", "利润", "利润_6月"]


def test_compare_rows_carry_both_series(rows, cols) -> None:
    new_rows, _out, _cut = _prune_cols_for_hint(rows, cols, hint="compare", sort_by=None)
    assert set(new_rows[0]) == {"省", "营业额", "营业额_6月"}


def test_sibling_falls_back_when_no_kin() -> None:
    """没有同族列时退回第一个其它数值列——不能返回自己（那就又变单序列了）。"""
    a = ChartColumn(key="营业额", header="营业额", type="numeric")
    b = ChartColumn(key="客流量", header="客流量", type="numeric")
    assert _sibling_numeric(a, [a, b]) is b
    assert _sibling_numeric(a, [a]) is a  # No alternative to choose with only one column.


def test_rank_still_single_series(rows, cols) -> None:
    """★ 反向闸：排名图不能被顺手改成双序列。"""
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="rank", sort_by="净利率")
    assert _keys(out) == ["省", "净利率"]


def test_correlation_unchanged(rows, cols) -> None:
    """correlation 走 scatter，x/y 都是数值、不带维度轴——原行为不能被改动。"""
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="correlation", sort_by="客流量")
    assert len(out) == 2
    assert all(c.type == "numeric" for c in out)


# ── Explicit series ──────────────────────────────────────────────────────────────


def test_explicit_series_wins(rows, cols) -> None:
    got = _explicit_series({"series": ["营业额", "营业额_6月"]}, rows, cols)
    assert got is not None
    assert _keys(got[1]) == ["省", "营业额", "营业额_6月"]
    assert set(got[0][0]) == {"省", "营业额", "营业额_6月"}


def test_explicit_series_can_exceed_two(rows, cols) -> None:
    """启发式只给两条；显式点名不该被这个数字限制。"""
    got = _explicit_series({"series": ["营业额", "利润", "净利率"]}, rows, cols)
    assert got is not None
    assert _keys(got[1]) == ["省", "营业额", "利润", "净利率"]


def test_unknown_keys_ignored_not_fatal(rows, cols) -> None:
    """一个错 key 不该让整张图不出——能画的照画。"""
    got = _explicit_series({"series": ["营业额", "不存在的列"]}, rows, cols)
    assert got is not None
    assert _keys(got[1]) == ["省", "营业额"]


def test_all_keys_unknown_falls_back_to_heuristic(rows, cols) -> None:
    assert _explicit_series({"series": ["xx", "yy"]}, rows, cols) is None
    assert _explicit_series({}, rows, cols) is None
    assert _explicit_series({"series": []}, rows, cols) is None


# ── Renderable charts: geo + two series must not fall back to tables ────────────────────────────────────


def test_geo_with_two_series_is_grouped_bar() -> None:
    """★ geo 不在 `cat_cols` 里：规则 3 原本要求恰好 1 个数值列、规则 11 又只认 cat，
    于是「geo + 2 序列」两头不靠，一路掉到规则 12 兜底表格——对比图会变成一张表。"""
    cols = [
        ChartColumn(key="省", header="省", type="geo"),
        ChartColumn(key="营业额", header="营业额", type="numeric"),
        ChartColumn(key="营业额_6月", header="营业额_6月", type="numeric"),
    ]
    rows = [{"省": p, "营业额": v, "营业额_6月": v * 1.2} for p, v in [("上海", 3.09e8), ("北京", 3.06e8), ("江苏", 1.39e8)]]
    spec = infer_chart_spec(rows, cols, hint="compare", title="各省营业额对比（7月 vs 6月）")
    assert spec.kind == "bar"
    assert [c.key for c in spec.columns] == ["省", "营业额", "营业额_6月"]


def test_geo_single_series_still_bar() -> None:
    cols = [ChartColumn(key="省", header="省", type="geo"), ChartColumn(key="营业额", header="营业额", type="numeric")]
    spec = infer_chart_spec([{"省": "上海", "营业额": 1.0}], cols, hint="rank", title="排名")
    assert spec.kind == "bar"


def test_geo_branch_cannot_bypass_the_width_gate() -> None:
    """规则 2 必须仍先行拦截，放宽规则 3（geo）不能把它绕过去。

    ⚠ 判据本身在批4 ⑨ 换过：从「numeric ≥ 4」换成「量纲族 > 2 或 序列 > 调色板槽位」
    ——4 个同族指标现在是**画得出**的（真机 `2025年月度核心指标趋势`）。
    本例守的仍是原来那件事：geo 分支不得先于宽度闸拿到判决权。所以喂一份**真的**
    超宽的数据，断言它照样退表。

    ⚠ 阈值第二次搬家（1.2.28 dogfood）：同族上限已与异族分档，7 个同族指标现在
    也是画得出的。要测「闸先行」，fixture 就得真的超过**同族**上限才行——
    否则测的是「7 列画不出」这个已经不成立的前提，而不是「geo 不能绕过闸」。
    """
    from bowhead_core.chart.infer import _MAX_SERIES_SAME_FAMILY

    n = _MAX_SERIES_SAME_FAMILY + 1
    cols = [ChartColumn(key="省", header="省", type="geo")] + [ChartColumn(key=f"m{i}", header=f"m{i}", type="numeric") for i in range(n)]
    rows = [
        {"省": "上海", **{f"m{i}": float(i) for i in range(n)}},
        {"省": "北京", **{f"m{i}": float(i + n) for i in range(n)}},
    ]
    assert infer_chart_spec(rows, cols, hint="compare", title="t").kind == "table"


# ── Wide-table pivot: retain the N largest categories, not first by column order (2026-09-03 dogfood on 116) ──────


def _pivot_wide(n_cats: int = 38) -> tuple[list[dict], list[ChartColumn]]:
    """9 个月 × N 个品类的宽表 —— 沙箱 `pivot_table` 的典型产物。

    量级刻意做成 `一次性用品`（列序第一）最小、`女装` 最大，差 3 万倍。
    """
    cats = [f"品类{i:02d}" for i in range(n_cats)]
    cats[0], cats[-1] = "一次性用品", "女装"
    mag = {c: 1.0e6 * (i + 1) for i, c in enumerate(cats)}
    mag["一次性用品"] = 1.5e4
    mag["女装"] = 4.56e8
    rows = [{"月份": f"{m}月", **{c: mag[c] for c in cats}} for m in range(1, 10)]
    cols = [ChartColumn(key="月份", header="月份", type="date")] + [ChartColumn(key=c, header=c, type="numeric") for c in cats]
    return rows, cols


def test_wide_pivot_keeps_top_series_not_the_first_column() -> None:
    """★★ 116 现场 `t_da7397e4d95c`：问「各品类每月营业额趋势」，沙箱 pivot 出
    9×38 的宽表，裁列留下**列序第一个类目** `一次性用品`（月均 1.5 万），
    而最大的 `女装` 是 4.56 亿——差 3 万倍。图上那条线画的是最小的类目，
    副标题却还写着「按种类大类分组」。

    宽表 pivot 里没有「主指标」可留：每一列都是一个类目。
    """
    from bowhead_core.canvas_tools import _prune_cols_for_hint
    from bowhead_core.chart.infer import _MAX_SERIES_SAME_FAMILY

    rows, cols = _pivot_wide()
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="trend", sort_by=None)
    kept = [c.key for c in out if c.type == "numeric"]
    # Every pivoted wide-table column is a category of the same metric; use the same-family limit.
    assert len(kept) == _MAX_SERIES_SAME_FAMILY, kept
    assert "女装" in kept, kept
    assert "一次性用品" not in kept, kept


def test_wide_pivot_keeps_original_column_order() -> None:
    """留哪几列按量级挑，但**排列仍按原列序**——序列顺序就是调色板顺序。"""
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    rows, cols = _pivot_wide()
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="trend", sort_by=None)
    kept = [c.key for c in out if c.type == "numeric"]
    src_order = [c.key for c in cols if c.type == "numeric"]
    assert len(kept) > 1, kept  # With only one retained column, the assertion below is always true and protects nothing.
    assert kept == [k for k in src_order if k in set(kept)], kept


def test_narrow_table_selection_is_untouched() -> None:
    """★ 负对照：数值列 ≤ 序列上限时**不许**改判据。

    语料实测 81 张里「没留最大那列」的 16 张**全是对的**——`退款率最高 Top10`
    就该留 `退款率`，而不是量级大它 6 个数量级的 `营业额`。按量级挑只在
    「所有候选列都是同一指标的类目展开」时成立，不能推广。
    """
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    cols = [ChartColumn(key="品牌", header="品牌", type="categorical")] + [
        ChartColumn(key=k, header=k, type="numeric") for k in ("营业额", "销售量", "退款率", "退款额")
    ]
    rows = [{"品牌": f"B{i}", "营业额": 1.2e8, "销售量": 5.4e4, "退款率": 0.17, "退款额": 3.0e6} for i in range(20)]
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="rank", sort_by="退款率")
    assert [c.key for c in out] == ["品牌", "退款率"]


def test_many_numerics_across_families_is_not_a_pivot() -> None:
    """★ 负对照：数值列很多但**跨量纲族**（营业额/客流量/利润率…）不是 pivot
    ——那是一份多指标宽表，主指标的概念仍然成立，按量级挑会挑错。
    """
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    specs = [("营业额", "元"), ("利润", "元"), ("客流量", "人次"), ("会员数", "人"), ("利润率", "%"), ("退款率", "%"), ("转化率", "%")]
    cols = [ChartColumn(key="月份", header="月份", type="date")] + [ChartColumn(key=k, header=k, type="numeric", unit=u) for k, u in specs]
    vals = {"营业额": 2e9, "利润": 3e8, "客流量": 2.3e6, "会员数": 1.1e5, "利润率": 0.17, "退款率": 0.03, "转化率": 0.4}
    rows = [{"月份": f"{m}月", **vals} for m in range(1, 10)]
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="trend", sort_by=None)
    assert [c.key for c in out] == ["月份", "营业额"], [c.key for c in out]


def test_wide_pivot_needs_a_single_index_column() -> None:
    """★ 负对照：两个维度列时不是 pivot 形状（真 pivot 只有一根索引列）。"""
    from bowhead_core.canvas_tools import _prune_cols_for_hint

    cats = [f"品类{i:02d}" for i in range(10)]
    cols = [
        ChartColumn(key="月份", header="月份", type="date"),
        ChartColumn(key="大区", header="大区", type="categorical"),
    ] + [ChartColumn(key=c, header=c, type="numeric") for c in cats]
    rows = [{"月份": f"{m}月", "大区": f"R{m % 3}", **{c: 1.0e6 * (i + 1) for i, c in enumerate(cats)}} for m in range(1, 10)]
    _r, out, _cut = _prune_cols_for_hint(rows, cols, hint="trend", sort_by=None)
    assert sum(1 for c in out if c.type == "numeric") == 1, [c.key for c in out]
