"""值轴分组：哪些指标能共用一根 y 轴（chart-config-ux 批2）.

旧判据是「最大量级的放左轴、其余全放右轴」，有两个病（116 实测）：

1. **≥3 个指标就塌了**：营业额(1e9) + 客流量(1e6) + 利润率(0.15) 里，客流量和利润率
   被一起塞进右轴，利润率照样是贴着零的一条线——分了轴等于没分。
2. **同单位的两列被分轴比不分更糟**：营业额 10 亿与利润 1 亿各占一根轴，画出来
   **一样高**，读者会得出「持平」这个完全相反的结论。

新判据：两列共轴 ⟺ **量纲族相同**（不看量级）。组数 1 → 改 combo（同轴柱折）、2 → 左右各一族、
≥3 → 两根轴装不下，多出来的族按「并进去之后哪根轴跨度更小」并入——见 `_right_axis_columns`。

⚠ 量级刻意**不参与分组**：我第一版写成「同族 且 量级差 <10x」，结果同族但差得多的
两列照样被劈开——`各市会员客流 vs 总客流量`（子集与全集，同为人次）各占一根轴后画得
一样高。同族里某列小到看不见时，诚实的答案是「它就是小」。
"""

from __future__ import annotations

from bowhead_core.chart import ChartColumn, infer_chart_spec
from bowhead_core.chart.infer import _axis_family, _axis_groups, _right_axis_columns


def _cols(*defs: tuple[str, str]) -> list[ChartColumn]:
    return [ChartColumn(key=k, header=k, type=t) for k, t in defs]  # type: ignore[arg-type]


def _num(*names: str) -> list[ChartColumn]:
    return [ChartColumn(key=n, header=n, type="numeric") for n in names]


# ── Unit families ──


def test_axis_family_separates_money_from_ratio():
    money, ratio = _num("营业额", "利润率")
    assert _axis_family(money) != _axis_family(ratio)


def test_axis_family_groups_same_kind_of_money():
    rev, profit = _num("营业额", "利润")
    assert _axis_family(rev) == _axis_family(profit)


def test_amount_suffix_counts_as_money():
    """`退款额` 是钱，必须和 `利润` 同族。

    真机（`种类大类 Top10 退款额与退款率`，指标 = 退款额/利润/退款率）：货币词表里只有
    `金额/营业额/销售额`，`退款额` 枚举不到 → 判成 plain → 与 currency、percent 凑成
    **三族** → 「三种量纲不出双轴」把它降成了共轴柱，退款率被压没。
    """
    refund, profit = _num("退款额", "利润")
    assert _axis_family(refund) == _axis_family(profit)


def test_share_is_still_a_percentage_not_money():
    """★ 顺序即正确性：`份额` 含「额」，但它是百分比不是钱。

    百分比族排在货币族**之前**判，所以加通用后缀「额」不会把它抢走。
    把 `units._NAME_KEYWORDS` 里两族的先后调换，本例就红。
    """
    assert _axis_family(_num("市场份额")[0]) == "percent"
    assert _axis_family(_num("营业额")[0]) != "percent"


def test_axis_family_separates_currencies():
    """¥ 和 $ 画在一根轴上是在编一个不存在的汇率."""
    cny = ChartColumn(key="a", header="a", type="numeric", format="currency", unit="¥")
    usd = ChartColumn(key="b", header="b", type="numeric", format="currency", unit="$")
    assert _axis_family(cny) != _axis_family(usd)


def test_axis_family_uses_declared_percent_regardless_of_scale():
    """百分比要不要单独一根轴，与它是 fraction 还是 points 无关.

    `percent_scale` 只影响**怎么格式化**（0.18 还是 18），不影响**放哪根轴**。
    这两件事必须分开，否则 percent_scale 的产出方缺口会传染到轴分配上。
    """
    frac = ChartColumn(key="a", header="a", type="numeric", percent_scale="fraction")
    pts = ChartColumn(key="b", header="b", type="numeric", percent_scale="points")
    assert _axis_family(frac) == _axis_family(pts) == "percent"


# ── Grouping ──


def test_same_unit_columns_share_one_axis_even_when_magnitudes_differ():
    """★ 同族列必须同轴：10 亿和 1 亿分两根轴会画得一样高，读成「持平」."""
    rows = [{"营业额": 1_000_000_000 + i, "利润": 100_000_000 + i} for i in range(8)]
    groups = _axis_groups(rows, _num("营业额", "利润"))
    assert len(groups) == 1, [[c.key for c in g] for g in groups]


def test_subset_and_total_of_same_unit_never_split():
    """★ 真机最刺眼的一例（`各市会员客流 vs 总客流量`）。

    两列同为人次，会员客流是总客流的**子集**。各占一根轴后画得一样高，
    读成「会员占了全部客流」，与事实相反。同族一律不分轴。

    我第一版把量级也写进分组条件（同族但差 ≥10x 也劈开），正好制造了这个谎——
    把 `_axis_groups` 改回按量级分，本例就红。
    """
    rows = [{"客流量 / 人次": 1_000_000 + i, "会员客流": 50_000 + i} for i in range(8)]
    groups = _axis_groups(rows, _num("客流量 / 人次", "会员客流"))
    assert len(groups) == 1, [[c.key for c in g] for g in groups]


def test_money_and_ratio_split_into_two_axes():
    rows = [{"营业额": 1_000_000_000 + i, "利润率": 0.15 + i / 1000} for i in range(8)]
    groups = _axis_groups(rows, _num("营业额", "利润率"))
    assert len(groups) == 2
    assert [c.key for c in groups[0]] == ["营业额"], "量级大的组排在前（左轴）"


def test_three_incompatible_dimensions_yield_three_groups():
    """★ 旧逻辑在这里塌掉：客流量与利润率会被一起塞进右轴."""
    rows = [{"营业额": 1_000_000_000 + i, "客流量": 1_000_000 + i, "利润率": 0.15 + i / 1000} for i in range(8)]
    groups = _axis_groups(rows, _num("营业额", "客流量", "利润率"))
    assert len(groups) == 3, [[c.key for c in g] for g in groups]


# ── End to end: group count determines chart kind ──


def test_two_groups_give_dual_axis_with_correct_assignment():
    """用户原话那个场景：营业额 + 变化率，必须两根轴."""
    rows = [{"省": f"P{i}", "营业额": 1_000_000_000 + i, "变化率": 0.15 + i / 1000} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("省", "categorical"), ("营业额", "numeric"), ("变化率", "numeric")),
        hint="compare",
        title="各省营业额与变化率",
    )
    assert spec.kind == "dual_axis"
    axis = {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}
    assert axis == {"营业额": 0, "变化率": 1}


def test_one_group_does_not_split_axes():
    """同族多列不该出双轴——落回同轴的柱/条."""
    rows = [{"门店": f"S{i}", "营业额": 100 + i, "利润": 90 + i} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("门店", "categorical"), ("营业额", "numeric"), ("利润", "numeric")),
        hint="compare",
        title="t",
    )
    assert spec.kind != "dual_axis"


def test_three_groups_refuse_dual_axis():
    """★ 两根轴装不下三种量纲，硬塞就是说谎——不出 dual_axis，交回形状树.

    把规则 10 的 `len(groups) == 2` 放宽成 `>= 2`，本例就红。
    """
    rows = [{"省": f"P{i}", "营业额": 1_000_000_000 + i, "客流量": 1_000_000 + i, "利润率": 0.15 + i / 1000} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(
            ("省", "categorical"),
            ("营业额", "numeric"),
            ("客流量", "numeric"),
            ("利润率", "numeric"),
        ),
        hint="compare",
        title="t",
    )
    assert spec.kind != "dual_axis", f"三种量纲不该硬塞进两根轴，得到 {spec.kind}"


def test_every_dual_axis_producer_gets_axis_ids():
    """★ 三个产出方都必须拿到轴分配，不能只有规则 10 那条分支有。

    此前只有规则 10 派 `y_axis_id`；意图层的 `hint=compare` 与模型的
    `force_kind="dual_axis"` 发出的 spec 全 0，前端会退回「第一列柱、其余线」——
    按**列序**分轴而不是按量纲。把分配收进 `_make_spec` 之后三条路共用一份。

    把 `_make_spec` 里那句 `if kind == "dual_axis"` 摘掉，本例就红。
    """
    rows = [{"省": f"P{i}", "营业额": 1_000_000_000 + i, "变化率": 0.15 + i / 1000} for i in range(8)]
    cols = _cols(("省", "categorical"), ("营业额", "numeric"), ("变化率", "numeric"))

    # ① Intent layer (hint=compare) ② Shape-tree rule 10 (hint=raw) ③ Explicit model request
    for kwargs in ({"hint": "compare"}, {"hint": "raw"}, {"hint": "raw", "force_kind": "dual_axis"}):
        spec = infer_chart_spec(rows, cols, title="t", **kwargs)  # type: ignore[arg-type]
        assert spec.kind == "dual_axis", kwargs
        axis = {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}
        assert axis == {"营业额": 0, "变化率": 1}, f"{kwargs} → {axis}"


def test_force_dual_axis_on_single_family_becomes_combo():
    """★ 模型点名要双轴、但数据同单位时 → 改出 `combo`（同轴柱折），不硬派两根轴。

    真机：`2026年各月营业额与利润趋势`，两列都是百万元。无论怎么派轴都会把同族列
    劈开，而那正是「两根柱画得一样高、读成持平」的谎。模型要的是**柱折混合**这个
    形态，`combo` 给得了，还不用两根轴撒谎。

    ⚠ 本例此前钉的是「按量级劈开兜底」——那个兜底把刚修掉的谎又造了一遍，
    连同它的测试一起废弃。
    """
    rows = [{"门店": f"S{i}", "利润": 100 + i, "营业额": 900 + i} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("门店", "categorical"), ("利润", "numeric"), ("营业额", "numeric")),
        hint="raw",
        title="t",
        force_kind="dual_axis",
    )
    assert spec.kind == "combo", spec._infer_reason
    assert {c.y_axis_id for c in spec.columns if c.type == "numeric"} == {0}


def test_axis_ids_are_only_zero_or_one():
    """契约守卫：`y_axis_id` 前端类型是 `0 | 1`，后端不许发出别的值."""
    rows = [{"省": f"P{i}", "营业额": 1_000_000_000 + i, "变化率": 0.15 + i / 1000} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("省", "categorical"), ("营业额", "numeric"), ("变化率", "numeric")),
        hint="compare",
        title="t",
    )
    assert {c.y_axis_id for c in spec.columns if c.type == "numeric"} <= {0, 1}


# ── Rows with no values (regression caught by 116 acceptance matrix R38/R39) ──


def test_all_null_measure_falls_back_to_table() -> None:
    """有行但没有一个可用数值 → 表格，不画一排零高柱子。

    ⚠ 这条是批3 ⑤ 的配套：在那之前 `n>30 → table` 恰好兜住了这种形状，改成截断
    之后会被截到 30 行照常出图。**线上没发生过**（2024 张真图里主数值列全空 = 0），
    是我的改动带来的一处退步，补回来。
    """
    rows = [{"店": f"S{i}", "额": None} for i in range(40)]
    spec = infer_chart_spec(rows, _cols(("店", "categorical"), ("额", "numeric")), hint="rank", title="t")
    assert spec.kind == "table", spec._infer_reason
    assert "无可用值" in (spec._infer_reason or "")


def test_nan_only_measure_also_falls_back() -> None:
    rows = [{"店": f"S{i}", "额": float("nan")} for i in range(40)]
    spec = infer_chart_spec(rows, _cols(("店", "categorical"), ("额", "numeric")), hint="rank", title="t")
    assert spec.kind == "table", spec._infer_reason


def test_one_usable_value_is_enough_to_draw() -> None:
    """正对照：只要有一个真值就照常出图——别把「大部分为空」也一起吞掉。"""
    rows = [{"店": f"S{i}", "额": (100 if i == 3 else None)} for i in range(40)]
    spec = infer_chart_spec(rows, _cols(("店", "categorical"), ("额", "numeric")), hint="rank", title="t")
    assert spec.kind != "table", spec._infer_reason


# ── ≥3 unit families: two axes cannot fit them; merging after chart-config-ux ⑥, dogfood on 116 ──
#
# _axis_groups' docstring already described the counterexample: largest magnitude left, everything else right with ≥3 families
# puts visitor counts (1e6) and margins (0.15) on the right together, still flattening margins to zero. Yet _resolve_dual_axis
# sent all groups[1:] to the right: correct comments, implementation did not follow them.
# Observed on 116: 12 of 90 dual_axis charts had ≥3 families; all 12 mixed families on the right axis.


def _right_keys(rows, cols):
    groups = _axis_groups(rows, cols)
    return {c.key for c in _right_axis_columns(rows, groups)}


def test_two_families_unchanged() -> None:
    """两族时没得选：大的左、小的右。这条是回归护栏，不是新行为。"""
    cols = _num("营业额", "利润率")
    rows = [{"营业额": 1e8, "利润率": 0.18}]
    assert _right_keys(rows, cols) == {"利润率"}


def test_third_family_joins_the_axis_that_keeps_the_span_small() -> None:
    """★ 116 真机 `近30天品牌退款率排名（TOP 20）`：金额 + 数量 + 率。

    旧行为把「数量」和「率」一起塞右轴 ⇒ 那根轴跨度 76 万倍，退款率是贴零的直线。
    数量与金额同在左轴时跨度只有 2 倍 —— 判据就是「并进去之后哪根轴更糟」。
    """
    cols = _num("sales_amount", "refund_amount", "refund_rate")
    rows = [{"sales_amount": 2.0e6, "refund_amount": 1.0e6, "refund_rate": 0.5}]
    assert _right_keys(rows, cols) == {"refund_rate"}


def test_third_family_can_also_land_on_the_right() -> None:
    """反向：量级靠近右轴锚的那一族要并到**右**边，不能一律往左推。

    116 真机 `2026 Provincial Revenue Ranking`：营业额(1e8) / 占比(0.15) / 排名(~15)。
    排名与占比同轴跨度约 100 倍，与营业额同轴则是 1e7 —— 显然该跟占比走。
    """
    cols = _num("Revenue", "Share", "Rank")
    rows = [{"Revenue": 1.0e8, "Share": 0.15, "Rank": 15}]
    assert _right_keys(rows, cols) == {"Share", "Rank"}


def test_never_leaves_an_axis_empty() -> None:
    """并的过程不许把右轴并空——那样 dual_axis 就名不副实了。"""
    cols = _num("A", "B", "C")
    rows = [{"A": 1e6, "B": 9e5, "C": 8e5}]  # Magnitudes are close together.
    groups = _axis_groups(rows, cols)
    if len(groups) >= 2:  # Unrecognized family names may yield only one group; a separate branch covers that.
        assert _right_axis_columns(rows, groups), "右轴被并空了"


def test_real_116_case_profit_rate_gets_its_own_axis() -> None:
    """★ 116 真机 `头部15品牌营业额占比测算`：营业额(亿) / 加权利润率 / 客流量(万)。

    旧行为：营业额和利润率一起去右轴（货币与百分比同轴，利润率被压平）。
    """
    cols = _num("营业额_亿", "加权利润率", "客流量_万")
    rows = [{"营业额_亿": 12.3, "加权利润率": 0.18, "客流量_万": 30.1}]
    right = _right_keys(rows, cols)
    assert right == {"加权利润率"}, right


def test_end_to_end_spec_puts_rate_alone_on_the_right() -> None:
    """穿过 `infer_chart_spec` 验一次——直接调内部函数容易验了个不 live 的分支。

    这里的量级取自 116 真机 `近30天品牌退款率排名`：数量与金额只差 2 倍，
    与率差 6 个数量级 ⇒ 数量归左轴、率独占右轴，答案唯一。
    """
    cols = [
        ChartColumn(key="品牌", header="品牌", type="categorical"),
        ChartColumn(key="退款额", header="退款额", type="numeric"),
        ChartColumn(key="退款量", header="退款量", type="numeric"),
        ChartColumn(key="退款率", header="退款率", type="numeric"),
    ]
    rows = [{"品牌": f"B{i}", "退款额": 2.0e6 - i, "退款量": 1.0e6 - i, "退款率": 0.5} for i in range(12)]
    spec = infer_chart_spec(rows, cols, hint="compare", title="退款率排名", force_kind="dual_axis")
    assert spec.kind == "dual_axis", spec._infer_reason
    by_key = {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}
    assert by_key["退款率"] == 1, by_key
    assert by_key["退款额"] == 0 and by_key["退款量"] == 0, by_key


def test_three_families_over_many_decades_is_best_effort_not_a_fix() -> None:
    """★ 诚实记账：三个族横跨 6 个数量级时，**两根线性轴怎么派都有一条被压平**。

    116 真机 `2026年各品牌退款率排名（Top 20）`：退款额 2.9e6 / 退款量 1.1e3 / 退款率 0.49。
    两个选项的跨度分别是 2636x（量并左）和 2245x（量并右），本算法取小的那个——
    但 2245x 仍然意味着退款率贴着零。**这不是本次改动能解决的**：真正的出路是
    「best span 仍超阈值时就别出 dual_axis」，那会改变画什么，属于另一个决定。

    这条用例存在的意义是**别让人以为这类图已经好了**：它只断言"取了较小的那个"，
    并把残余写在这里。116 语料里这样的图有 5 张。
    """
    cols = _num("退款额", "退款量", "退款率")
    rows = [{"退款额": 2.9e6, "退款量": 1.1e3, "退款率": 0.49}]
    groups = _axis_groups(rows, cols)
    assert len(groups) == 3
    right = {c.key for c in _right_axis_columns(rows, groups)}
    # Rates must be on the right (the right anchor); quantities choose the smaller span, which is right here.
    assert "退款率" in right
    assert right == {"退款率", "退款量"}, right


# ── Two unit families on a single-axis chart ⇒ promote to dual axis (⑮, 2026-09-03) ────────────────────────
#
# n_axes = len(_axis_groups(...)) was already computed, but only _kind_from_intent's
# hint=compare branch read it. Shape-tree rules 4 (time→line) and 11 (category→bar) ignored it,
# while hint was empty in 92.3% of cases: another computed value nobody read.
#
# End-to-end measurements on the 116 corpus (actual rendered datasets through builders, not spec-layer guesses):
#   Of 416 charts rendering ≥2 series, invisible near-zero series occurred in
#   121 charts (29%) → 63 (15%) after the change, with zero regressions.


def _num_axes(spec):
    return {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}


def test_single_axis_with_two_families_upgrades_to_dual() -> None:
    """★★ 116 真机 `2026 Revenue & Profit by Category`：营业额+利润 / 利润率，跨度 39 亿倍。"""
    rows = [{"品类": f"C{i}", "营业额": 1e8 + i, "利润": 2e7 + i, "利润率": 0.18} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("品类", "categorical"), ("营业额", "numeric"), ("利润", "numeric"), ("利润率", "numeric")),
        title="各品类营业额与利润率",
    )
    assert spec.kind == "dual_axis", spec._infer_reason
    assert _num_axes(spec) == {"营业额": 0, "利润": 0, "利润率": 1}, _num_axes(spec)


def test_time_series_with_two_families_also_upgrades() -> None:
    """时间序列同理——`华东月度营业额&客流趋势`：营业额+利润 / 客流量，752 倍。"""
    rows = [{"每月": f"2026-{i + 1:02d}", "营业额": 8.6e7, "利润": 1.5e7, "客流量": 1.1e5} for i in range(9)]
    spec = infer_chart_spec(
        rows,
        _cols(("每月", "date"), ("营业额", "numeric"), ("利润", "numeric"), ("客流量", "numeric")),
        title="月度营业额与客流趋势",
    )
    assert spec.kind == "dual_axis", spec._infer_reason
    assert _num_axes(spec)["客流量"] == 1


def test_same_family_does_not_upgrade() -> None:
    """负对照：同族（都是钱）不该升——一根轴本来就画得对。"""
    rows = [{"品类": f"C{i}", "营业额": 1e8, "利润": 1e6} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("品类", "categorical"), ("营业额", "numeric"), ("利润", "numeric")),
        title="营业额与利润",
    )
    assert spec.kind != "dual_axis", spec._infer_reason


def test_two_families_but_similar_magnitude_does_not_upgrade() -> None:
    """★ 负对照：两个族但量级相近 ⇒ 一根轴画得好好的，再劈一根纯属添乱。"""
    rows = [{"店": f"S{i}", "客单价": 650.0, "件均数": 3.2} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("店", "categorical"), ("客单价", "numeric"), ("件均数", "numeric")),
        title="客单价与件均",
    )
    assert spec.kind != "dual_axis", spec._infer_reason


def test_hbar_is_never_upgraded() -> None:
    """★★ hbar 不许升：`buildDualAxis` 没有横向模式，升了就把「类目名太长走横条」
    这个理由丢了——本 sprint 刚修完 hbar 按类目数长高，不能反手打回去。

    ⚠ 用 `force_kind` 构造：判定树自己走不到「hbar + 2 族」——`hint=rank` 那条早就
    写着 `shares_one_axis = n_axes == 1`，两根轴时交回规则 10 出 dual_axis
    （「可读性优先于『排名就该横着』」）。所以这道闸挡的是**存量 spec 与显式指定**，
    是防御性的；我第一版拿 `hint="rank"` 造 fixture，造出来的其实是 dual_axis。
    """
    rows = [{"品牌": f"很长的品牌名称-{i}", "营业额": 1e8, "退款率": 0.03} for i in range(12)]
    spec = infer_chart_spec(
        rows,
        _cols(("品牌", "categorical"), ("营业额", "numeric"), ("退款率", "numeric")),
        title="品牌营业额排名",
        force_kind="hbar",
    )
    assert spec.kind == "hbar", spec._infer_reason


def test_rank_with_two_families_goes_dual_not_hbar() -> None:
    """上一条的对照：不显式指定时，两个族的排名图本来就该出 dual_axis
    （`hint=rank` 分支自己让的路，不是我这次改的）。"""
    rows = [{"品牌": f"很长的品牌名称-{i}", "营业额": 1e8, "退款率": 0.03} for i in range(12)]
    spec = infer_chart_spec(
        rows,
        _cols(("品牌", "categorical"), ("营业额", "numeric"), ("退款率", "numeric")),
        title="品牌营业额排名",
        hint="rank",
    )
    assert spec.kind == "dual_axis", spec._infer_reason


def test_a_long_time_series_still_upgrades_to_dual_axis() -> None:
    """★ 时间轴上按「横轴画得下多少点」放宽，不再卡 60 行。

    不升的代价是**那条比率彻底看不见**：365 天的营业额（1e9）+ 利润率（0.01）挤一根轴，
    利润率就是一条贴着零的直线。以前这形状因为超过 200 行被退成表格，放开行数之后
    才暴露出来（复审逮到的回归）。柱子密一点，总好过有一条线根本看不见。
    """
    rows = [{"日期": f"D{i:03d}", "营业额": 1e9 + i, "利润率": 0.01 + i / 1e5} for i in range(365)]
    spec = infer_chart_spec(
        rows,
        _cols(("日期", "date"), ("营业额", "numeric"), ("利润率", "numeric")),
        hint="trend",
        title="日度",
    )
    assert spec.kind == "dual_axis", spec._infer_reason
    axes = {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}
    assert axes["利润率"] == 1, f"比率该拿右轴，实际 {axes}"


def test_geo_dimension_also_upgrades() -> None:
    """★★ geo 那条路才是本次改动真正接住的一半。

    规则 3「geo col detected → 退化为 bar」是**无条件**的，不看量纲族；而规则 10 的
    dual_axis 只认 `cat_cols`，接不到 geo。于是「各省营业额 + 利润率」一路落成单轴柱，
    利润率贴零。116 语料里 `bar → dual_axis` 的 21 张基本都是这个形状。

    （`1 cat + N num` 那半规则 10 本来就覆盖，摘掉本次改动也不会红——所以这条不能省。）
    """
    rows = [{"省": f"P{i}", "营业额": 1e8, "利润率": 0.18} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("省", "geo"), ("营业额", "numeric"), ("利润率", "numeric")),
        title="各省营业额与利润率",
    )
    assert spec.kind == "dual_axis", spec._infer_reason
    assert _num_axes(spec) == {"营业额": 0, "利润率": 1}, _num_axes(spec)


# ── force_kind unit-family gate (116 dogfood 2026-09-09, t_e95476991dcb) ──


def test_force_hbar_with_short_labels_yields_dual_axis() -> None:
    """★★ 模型把图种**说出口**，不该因此绕过它不说时会被挡住的守卫。

    116 真机形状逐字：`series=["Q3销售额_亿元","QoQ环比变化率_pct"]` +
    `force_kind="hbar"`，两列同轴 ⇒ 轴被负的百分比撑到 −40…+20，五根销售额条全贴
    在 0 上，而标题正是「Top 5 门店销售额**排名**」。

    ⚠ 这里的类目名是「东湖街 / 西岸二期」（≤5 字、5 行）——**横条没有自己的理由**，
    所以量纲闸夺权。长类目名那半见 `test_hbar_is_never_upgraded`（契约保持）。
    """
    rows = [
        {"门店": "东湖街", "Q3销售额_亿元": 2.84, "QoQ环比变化率_pct": -24.3},
        {"门店": "南湖花园城", "Q3销售额_亿元": 1.71, "QoQ环比变化率_pct": -31.8},
        {"门店": "西岸二期", "Q3销售额_亿元": 1.49, "QoQ环比变化率_pct": -35.2},
        {"门店": "湖景公寓", "Q3销售额_亿元": 1.27, "QoQ环比变化率_pct": -17.4},
        {"门店": "翠堤湾", "Q3销售额_亿元": 1.08, "QoQ环比变化率_pct": -27.6},
    ]
    cols = _cols(("门店", "categorical"), ("Q3销售额_亿元", "numeric"), ("QoQ环比变化率_pct", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="2026年Q3 Top 5 门店销售额排名", force_kind="hbar")
    assert spec.kind == "dual_axis", spec._infer_reason
    # Asserting kind alone is insufficient; axes must really separate (all-zero assignments make FE use its old first-column-bar/rest-lines
    # fallback, splitting axes by column order instead of units).
    assert _num_axes(spec) == {"QoQ环比变化率_pct": 0, "Q3销售额_亿元": 1}, _num_axes(spec)


def test_force_hbar_stays_hbar_when_single_family() -> None:
    """★ 负对照：同一族多序列时 `force_kind` 照旧生效——闸只挡「两个量纲组共轴」，
    不是见 force 就夺权。摘掉 `_axis_groups` 那半判据，这条会红。"""
    rows = [{"门店": f"S{i}", "Q2销售额_亿元": 2.0, "Q3销售额_亿元": 3.0} for i in range(5)]
    cols = _cols(("门店", "categorical"), ("Q2销售额_亿元", "numeric"), ("Q3销售额_亿元", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="门店销售额环比", force_kind="hbar")
    assert spec.kind == "hbar", spec._infer_reason


def test_force_table_is_not_touched_by_axis_guard() -> None:
    """★ 负对照：`table` 不是多序列共轴的形态，量纲闸不该碰它。

    这条盯的是既有回归（session `s_c88220d81f9e`：模型 `force_kind="table"` 点名要
    全表却被截行）——闸的集合写宽一格就会把它卷进来。
    """
    rows = [{"门店": f"S{i}", "销售额_亿元": 3.0, "环比_pct": -20.0} for i in range(5)]
    cols = _cols(("门店", "categorical"), ("销售额_亿元", "numeric"), ("环比_pct", "numeric"))
    spec = infer_chart_spec(rows, cols, title="全表", force_kind="table")
    assert spec.kind == "table", spec._infer_reason


def test_force_single_series_never_reaches_the_guard() -> None:
    """★ 负对照：只有一个数值列时谈不上「两个族共轴」，force 原样通过。"""
    rows = [{"门店": f"S{i}", "销售额_亿元": 3.0} for i in range(5)]
    cols = _cols(("门店", "categorical"), ("销售额_亿元", "numeric"))
    spec = infer_chart_spec(rows, cols, hint="rank", title="门店销售额", force_kind="hbar")
    assert spec.kind == "hbar", spec._infer_reason


def test_pct_column_shares_a_family_with_its_chinese_twin() -> None:
    """Localized rate names and pct suffixes share one unit family.

    The synthetic columns differ only in scaling; shape declarations remain a
    separate rendering responsibility.
    """
    rows = [
        {"门店": "示例店甲", "营业额 / 元": 12000.0, "营业额环比变化率": -0.12, "QoQ_pct": -12.0},
        {"门店": "示例店乙", "营业额 / 元": 24000.0, "营业额环比变化率": -0.18, "QoQ_pct": -18.0},
        {"门店": "示例店丙", "营业额 / 元": 18000.0, "营业额环比变化率": -0.24, "QoQ_pct": -24.0},
    ]
    num = _num("营业额 / 元", "营业额环比变化率", "QoQ_pct")
    assert _axis_family(num[1]) == _axis_family(num[2]), f"同一个量的两列不同族：{_axis_family(num[1])} vs {_axis_family(num[2])}"
    assert len(_axis_groups(rows, num)) == 2, [[c.key for c in g] for g in _axis_groups(rows, num)]

    spec = infer_chart_spec(
        rows,
        _cols(("门店", "categorical"), ("营业额 / 元", "numeric"), ("营业额环比变化率", "numeric"), ("QoQ_pct", "numeric")),
        title="全部门店 QoQ 排序",
    )
    axes = {c.key: c.y_axis_id for c in spec.columns if c.type == "numeric"}
    assert axes["营业额环比变化率"] == axes["QoQ_pct"], f"同族两列被派到了不同轴：{axes}"
    assert axes["营业额 / 元"] != axes["QoQ_pct"], f"金额与比率共轴：{axes}"
